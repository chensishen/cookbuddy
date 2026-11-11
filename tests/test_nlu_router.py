import copy
import json
import unittest
from unittest.mock import patch
from src.nlu.router import understand,validate
from src.nlu.config import settings

class Cache:
    values={}
    def get(self,k):return copy.deepcopy(self.values.get(k))
    def put(self,k,v,**kw):self.values[k]=copy.deepcopy(v)

class RouterTests(unittest.TestCase):
    def setUp(self):
        Cache.values={};self.cache=patch('service_runtime.RuntimeStore',Cache);self.cache.start()
    def tearDown(self):self.cache.stop()

    def test_unspecified_composition_accepts_null_without_replacing_template(self):
        meal={'meal_type':'breakfast','explicit_composition':False,'dishes':None,'soups':None}
        result=validate({'intent':'meal_plan','confidence':.99,'slots':{'meal':meal}})
        self.assertFalse(result['slots']['meal']['explicit_composition'])
        with self.assertRaises(ValueError):
            validate({'intent':'meal_plan','confidence':.99,'slots':{'meal':{**meal,'explicit_composition':True}}})

    def test_model_first_and_context_bounded(self):
        seen=[]
        def model(raw,normalized,context):
            seen.append(context)
            return {'intent':'identity_capability','confidence':.96}
        r=understand('劳驾，介绍一下你的身份',{'messages':[{'role':'user','content':str(i)} for i in range(12)]},model_call=model)
        self.assertEqual(r['intent'],'identity_capability');self.assertEqual(r['path'],'model')
        self.assertEqual(r['answer_mode'],'general_chat');self.assertEqual(len(seen[0]['messages']),4)

    def test_cache_context_and_tenant_isolation(self):
        seen=[]
        def model(*a):seen.append(a);return {'intent':'identity_capability','confidence':.96}
        for ctx in [{'tenant':'a'},{'tenant':'a'},{'tenant':'b'},{'tenant':'a','summary':'changed'}]:
            understand('介绍自己',ctx,model_call=model)
        self.assertEqual(len(seen),3)
        self.assertTrue(all('介绍' not in k for k in Cache.values))

    def test_failure_and_low_confidence_fall_back(self):
        def unavailable(*a):raise TimeoutError('RAW_SECRET_USER_TEXT')
        r=understand('你是谁',model_call=unavailable)
        self.assertEqual(r['answer_mode'],'general_chat');self.assertEqual(r['fallback'],'rule')
        self.assertEqual(r['errors'],['timeout']);self.assertNotIn('RAW_SECRET',json.dumps(r['errors']))
        r=understand('今天天气怎么样',model_call=lambda *a:{'intent':'unclear','confidence':.2})
        self.assertEqual(r['answer_mode'],'out_of_scope')

    def test_schema_rejects_invalid_refs_hallucinated_permission_and_nan(self):
        for data in [dict(intent='recipe_section',confidence=float('nan')),
                     dict(intent='constraint_refine',confidence=.9,slots={'exclude_refs':[{'type':'ordinal','value':True}]}),
                     dict(intent='recipe_lookup',confidence=.9,slots={'user_id':'somebody'}),
                     dict(intent='recipe_lookup',confidence=.9,slots={'constraints':{'ignore_allergy':True}})]:
            with self.assertRaises(ValueError):validate(data)

    def test_fast_path_toggle_does_not_change_mode(self):
        config=copy.deepcopy(settings());config['fast_path']['enabled']=False
        a=understand('你好',use_model=False)
        with patch('src.nlu.router.settings',return_value=config):b=understand('你好',model_call=lambda *a:{'intent':'chitchat','confidence':1.})
        self.assertEqual(a['intent'],b['intent']);self.assertEqual(a['answer_mode'],b['answer_mode'])

    def test_model_has_no_authority_to_discard_hard_constraint(self):
        r=understand('不要花生',model_call=lambda *a:{'intent':'ingredient_recommend','confidence':.9,'slots':{'raw_noise':['不要花生']}})
        self.assertNotIn('不要花生',r['ignored_tokens'])

    def test_free_model_assumptions_are_not_presented_as_recipe_facts(self):
        r=understand('蒸水蛋怎么做',model_call=lambda *a:{'intent':'recipe_lookup','confidence':.9,
            'slots':{'dish':'蒸水蛋'},'assumptions':['随意添加999克盐']})
        self.assertNotIn('999',json.dumps(r['assumptions'],ensure_ascii=False))

    def test_cache_failure_does_not_block_model(self):
        with patch.object(Cache,'get',side_effect=RuntimeError('private-user-text')):
            r=understand('介绍自己',model_call=lambda *a:{'intent':'identity_capability','confidence':.99})
        self.assertEqual(r['answer_mode'],'general_chat');self.assertIn('cache_unavailable',r['errors'])

    def test_invalid_cached_schema_does_not_block_model(self):
        with patch.object(Cache,'get',return_value={'intent':'invalid','confidence':.9}):
            r=understand('介绍自己',model_call=lambda *a:{'intent':'identity_capability','confidence':.99})
        self.assertEqual(r['path'],'model');self.assertIn('cache_invalid',r['errors'])

    def test_optional_recommendation_slots_do_not_force_clarification(self):
        r=understand('推荐3道菜',model_call=lambda *a:{'intent':'ingredient_recommend','confidence':.9,
            'slots':{'limit':3},'needs_clarification':True,'clarify_question':'想用什么食材？','options':['鸡肉','猪肉']})
        self.assertEqual(r['answer_mode'],'grounded_recipe');self.assertFalse(r['needs_clarification'])

    def test_literal_source_fact_does_not_require_optional_section(self):
        r=understand('基础牛奶面包要烤多久，温度是多少？',model_call=lambda *a:{'intent':'recipe_lookup','confidence':.9,
            'slots':{'dish':'基础牛奶面包'},'needs_clarification':True,'clarify_question':'查看哪个章节？','options':['操作','原料']})
        self.assertEqual(r['answer_mode'],'grounded_recipe');self.assertFalse(r['needs_clarification'])
