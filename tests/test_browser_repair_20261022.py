"""User-visible failures reproduced without network/model calls."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_tools import load_chunks,find_recipe_images
from cookbuddy.meal_planning import meal_request,plan_meal
from cookbuddy.ltm.extractor import rule_extract
from cookbuddy.ltm.runtime import MemoryRuntime
from cookbuddy.session import SessionState
from cookbuddy.task_context import advance_task
from src.agent.degrade import fallback

class BrowserRepairTests(unittest.TestCase):
    def test_streamed_fact_prefers_focused_answer_to_the_expanded_audit(self):
        from src.agent.engine import render
        text=render([{'tool':'search_recipes','result':{'short_answer':'酵母2g，静置5分钟。',
            'answer':'具体答案和很长的原文摘录','sources':[]}}])
        self.assertEqual(text,'酵母2g，静置5分钟。')

    def test_persist_does_not_clear_the_source_selected_during_this_turn(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from copy import deepcopy
        session=SessionState('u','s')
        selected={'idx':0,'dish_name':'基础牛奶面包','source_path':'bread.md'}
        slots=deepcopy(session.slots);slots['selected_dish']=selected
        store=MagicMock();store.session_context.return_value=session
        prepared={'enabled':True,'level':6,'slots':slots,'summary':'','messages':[],
            'task':{'action':'none','state':None,'boundary':True}}
        args={'question':'查看基础牛奶面包的完整原文和图片。','prepared':prepared,'result':{'outputs':[]},'text':'原文'}
        with patch('cookbuddy.store.Store',return_value=store):
            worker.execute(dict(operation='persist',arguments=args,user_id='u',session_id='s',request_id='j',hard=[],config=load_config().model_dump()))
        saved=store.save_session_context.call_args.args[-1]
        self.assertEqual(saved.slots['selected_dish'],selected)

    def test_specific_followups_keep_recipe_but_not_the_previous_full_request(self):
        import langchain_service_worker as worker
        source=next(r['source'] for r in load_chunks() if r['title']=='基础牛奶面包')
        for q in ('只看酵头制作的图片。','酵头需要多少酵母、水温多少、发酵多久？请区分静置时间和发酵时间，并给出依据。',
                  '面包的烘烤温度和时间是多少？原文有没有根据烤箱表现调整的方法？'):
            with self.subTest(question=q):
                store,memory=self.runtime(q)
                memory.session.slots['selected_dish']=dict(idx=0,dish_name='基础牛奶面包',source_path=source)
                with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
                    prepared=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
                self.assertEqual(prepared['named_recipe']['source_path'],source)
                self.assertEqual(prepared['slots']['selected_dish']['source_path'],source)
                self.assertIn(q,prepared['task']['question'])
                self.assertNotIn('完整原文和图片',prepared['task']['question'])

    def test_manual_second_recipe_followup_keeps_choices(self):
        previous=advance_task('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。',None)['state']
        previous['choices']=[{'title':'西红柿炒鸡蛋','source':'a.md'},{'title':'炒滑蛋','source':'b.md'}]
        result=advance_task('第二道怎么做？请给我用料和完整步骤。',previous)
        self.assertEqual(result['action'],'followup')
        self.assertEqual(result['selected_source'],'b.md')
        self.assertEqual(result['state']['ingredients'],previous['ingredients'])

    def test_manual_ordinal_rejection_is_not_an_ingredient_preference(self):
        self.assertEqual(rule_extract('换一道，不要刚才第二道。'),[])

    def test_manual_replacement_consumes_ordinal_not_extra_constraint(self):
        import langchain_service_worker as worker
        previous=advance_task('我有番茄和鸡蛋，推荐两道菜。',None)['state']
        previous['choices']=[{'title':'西红柿炒鸡蛋','source':'a.md'},{'title':'炒滑蛋','source':'b.md'}]
        q='换一道，不要刚才第二道。'
        store,memory=self.runtime(q,previous)
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
            result=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
        self.assertIsNone(result['management'])
        self.assertEqual(result['task']['action'],'recommend')
        self.assertEqual(result['task']['state']['extra_constraints'],[])
        self.assertEqual(result['task']['state']['limit'],1)

    def test_missing_ingredients_uses_single_replacement_source_and_inventory(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        previous=advance_task('我有番茄和鸡蛋，推荐两道菜。',None)['state']
        q='这道还需要准备哪些食材？'
        store,memory=self.runtime(q,previous)
        source=next(r['source'] for r in load_chunks() if r['title']=='蛋煎糍粑')
        memory.session.slots['ingredients_on_hand']=['番茄','鸡蛋']
        memory.session.slots['last_recommendations']=[dict(idx=1,dish_name='蛋煎糍粑',source_path=source)]
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
            prepared=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
        self.assertEqual(prepared['task']['action'],'ingredients')
        self.assertEqual(prepared['task']['selected_source'],source)
        result=worker.execute(dict(operation='recipe_ingredients',arguments={'source_path':source,'ingredients':['番茄','鸡蛋']},
            user_id='u',session_id='s',request_id='j',hard=[],config=load_config().model_dump()))
        self.assertIn('鸡蛋',result['matched'])
        self.assertIn('糍粑',result['missing'])
        self.assertNotIn('鸡蛋',result['missing'])
        self.assertEqual({r['source'] for r in result['sources']},{source})

    def test_inventory_replacement_excludes_old_dishes_and_renders_reasons(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from src.agent.engine import render
        payload=dict(operation='recommend',arguments={'ingredients':['番茄','鸡蛋'],'top_k':2},
            user_id='u',session_id='s',request_id='j',hard=[],config=load_config().model_dump())
        first=worker.execute(payload)
        titles=[r['title'] for r in first['recommendations']]
        payload['arguments'].update(top_k=1,exclude_dishes=titles)
        second=worker.execute(payload)
        self.assertEqual(len(second['recommendations']),1)
        self.assertNotIn(second['recommendations'][0]['title'],titles)
        self.assertTrue(all(r['reason'] for r in first['recommendations']))
        text=render([{'tool':'recommend_by_ingredients','result':second}])
        self.assertIn('推荐理由',text)
        self.assertIn(second['recommendations'][0]['title'],text)
        for item in second['recommendations'][0]['unresolved']:self.assertIn(item,text)

    def test_homepage_meal_template(self):
        q='帮我安排2人的晚餐，有30分钟，想做2菜1汤。'
        self.assertEqual(plan_meal(meal_request(q),load_chunks())['status'],'verified')

    def runtime(self,q,previous=None):
        store=MagicMock();store.session_context.return_value=SessionState('u','s')
        store.task_context.return_value=previous
        store.connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value=[]
        prefs=MagicMock();prefs.enabled.return_value=True;prefs.confirm.return_value=None
        prefs.pending_message.return_value='';prefs.load_profile.return_value={'hard_constraints':[],'soft_prefs':[]}
        with patch('cookbuddy.ltm.runtime.ProfileStore',return_value=prefs):
            return store,MemoryRuntime(store,'u','s','j',q)

    def test_recipe_restriction_is_not_memory_only_or_global(self):
        q='基础牛奶面包不要放鸡蛋，给我完整原文和做法。'
        candidates=rule_extract(q)
        self.assertEqual([(c.value,c.scope) for c in candidates],[('鸡蛋','session')])
        _,memory=self.runtime(q)
        self.assertIsNone(memory.management)
        self.assertTrue(any(c['value']=='鸡蛋' for c in memory.hard))
        import langchain_service_worker as worker
        store,memory=self.runtime(q)
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
            prepared=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
        self.assertEqual(prepared['recipe_conflict']['status'],'insufficient')
        self.assertIn('鸡蛋',prepared['recipe_conflict']['short_answer'])
        self.assertIn('未删改',prepared['recipe_conflict']['short_answer'])


    def test_replacement_retains_inventory_and_requests_one(self):
        import langchain_service_worker as worker
        q='换一道，不要炒滑蛋。'
        previous=advance_task('我有番茄和鸡蛋，推荐两道菜。',None)['state']
        store,memory=self.runtime(q,previous)
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
            result=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
        self.assertEqual(result['task']['action'],'recommend')
        self.assertEqual(result['task']['state']['limit'],1)
        self.assertEqual(result['task']['state']['ingredients'],previous['ingredients'])
        self.assertIsNone(result['named_recipe'])
        from src.agent.tools import wants_alternative
        self.assertTrue(wants_alternative(q))
        self.assertFalse(wants_alternative('炒滑蛋换一道步骤怎么做？'))

    def test_chapter_image_matching_keeps_unknown_chapters_empty(self):
        result=find_recipe_images('基础牛奶面包','酵头制作')
        self.assertEqual(result['status'],'verified_source')
        self.assertEqual(len(result['images']),3)
        self.assertTrue(all('酵头' in r['section'] for r in result['images']))
        self.assertEqual(find_recipe_images('基础牛奶面包','不存在的章节')['status'],'insufficient')

    def test_personalized_reason_is_bound_to_literal_intro(self):
        from cookbuddy.ltm.retriever import personalize
        profile={'soft_prefs':[{'dimension':'flavor','value':'清淡','polarity':'like'}]}
        rows=[{'title':'菜','source':'a','description':'这道菜口味清淡，操作简单。','reason':'通用理由','score':0}]
        selected=personalize(rows,profile,k=1)
        self.assertIn('清淡',selected[0]['reason'])
        self.assertEqual(rows[0]['reason'],'通用理由')
        profile['soft_prefs'][0]['value']='酸甜'
        self.assertNotIn('酸甜',personalize(rows,profile,k=1)[0]['reason'])

    def test_M6_recommendation_uses_authenticated_profile(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from cookbuddy.recommendation_schema import new_state
        from cookbuddy.ltm.models import CONFIG_OVERRIDE
        state=new_state();state['limit']=2
        store=MagicMock();store.session_context.return_value=SessionState('current-user','s')
        prefs=MagicMock();prefs.enabled.return_value=True
        prefs.load_profile.return_value={'hard_constraints':[],'soft_prefs':[
            {'dimension':'flavor','value':'清淡','polarity':'like'},
            {'dimension':'max_difficulty','value':'1','polarity':'like'}]}
        cfg=load_config().model_dump();cfg['memory']['mode']='M6'
        old=CONFIG_OVERRIDE.get()
        try:
            with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.profile_store.ProfileStore',return_value=prefs):
                result=worker.execute(dict(operation='task_recommend',arguments={'state':state},
                    user_id='current-user',session_id='s',request_id='j',hard=[],config=cfg))
            self.assertEqual(len(result['recommendations']),2)
            self.assertTrue(any('偏好' in r['reason'] for r in result['recommendations']))
            prefs.load_profile.assert_called_once_with('current-user')
        finally:CONFIG_OVERRIDE.set(old)

    def test_exploration_choice_explains_why_it_is_also_offered(self):
        from cookbuddy.ltm.retriever import personalize
        profile={'soft_prefs':[{'dimension':'flavor','value':'清淡','polarity':'like'}]}
        rows=[dict(title=str(i),description='口味清淡' if i<2 else '口味浓郁',score=0 if i<2 else .01,reason='通用理由') for i in range(3)]
        result=personalize(rows,profile,k=2)
        self.assertIn('备选',result[-1]['reason'])
        self.assertNotIn('偏好相符',result[-1]['reason'])

    def test_fallback_groups_chunks_by_source(self):
        rows=[dict(source_path='a.md',dish_name='菜',section='操作',line_range=f'L{i}',snippet=f'原文{i}') for i in range(3)]
        run=SimpleNamespace(degraded=False,evidence={})
        outputs=fallback(run,[dict(name='search_recipes',full={'artifact':'a'},compact={'status':'ok','items':rows})])
        self.assertEqual(len(outputs),1)
        self.assertIn('未能核实',outputs[0]['result']['short_answer'])
        self.assertEqual(len(outputs[0]['result']['sources']),3)

    def test_fallback_keeps_one_full_original_per_source(self):
        rows=[dict(source_path='a.md',dish_name='菜',section='操作',line_range='L1',snippet='摘录')]
        entries=[dict(name='search_recipes',full={'artifact':'a'},compact={'status':'ok','items':rows}),
                 dict(name='get_recipe_detail',full={'status':'verified_source','short_answer':'完整原文',
                    'sources':[{'source':'a.md'}]},compact={'status':'ok','items':rows})]
        result=fallback(SimpleNamespace(degraded=False,evidence={}),entries)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['result']['status'],'verified_source')
        self.assertIn('未能核实',result[0]['result']['notice'])

if __name__=='__main__':unittest.main()
