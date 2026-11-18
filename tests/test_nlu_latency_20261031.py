import copy
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock,patch
from src.nlu.router import understand,validate
from src.nlu.config import settings


class LiteralLatencyTests(unittest.TestCase):
    def test_complete_inventory_does_not_wait_for_remote_understanding(self):
        model=Mock(return_value=validate({'intent':'ingredient_recommend','confidence':.99,
            'slots':{'ingredients':['番茄','土豆'],'limit':2,'mode':'inventory'}}))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('冰箱剩西红市和马铃薯，给我整俩菜，缺啥也说下。',model_call=model)
        model.assert_not_called()
        self.assertEqual(result['path'],'fast')
        self.assertEqual(set(result['slots']['ingredients']),{'番茄','土豆'})
        self.assertEqual(result['slots']['limit'],2)
        self.assertIn('missing_ingredients',result['slots']['response_fields'])

    def test_complete_meal_does_not_wait_for_remote_understanding(self):
        model=Mock(return_value=validate({'intent':'meal_plan','confidence':.99,
            'slots':{'meal':{'meal_type':'dinner','people':2,'minutes':30,'dishes':3,'soups':1,'explicit_composition':True}}}))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('按晚餐安排，仍然两个人、30分钟、三菜一汤。',model_call=model)
        model.assert_not_called()
        self.assertEqual(result['slots']['meal']['dishes'],3)
        self.assertEqual(result['slots']['constraints'].get('unresolved',[]),[])

    def test_unknown_requirement_still_reaches_model_and_stays_visible(self):
        model=Mock(return_value=validate({'intent':'ingredient_recommend','confidence':.99,
            'slots':{'ingredients':['番茄','土豆'],'constraints':{'unresolved':['预算不超过5元']}}}))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('我有番茄和土豆，推荐两道菜，预算不超过5元。',model_call=model)
        model.assert_called_once()
        self.assertIn('预算不超过5元',result['slots']['constraints']['unresolved'])

    def test_fast_path_toggle_preserves_executable_inventory_slots(self):
        oracle={'intent':'ingredient_recommend','confidence':.99,
            'slots':{'ingredients':['番茄','土豆'],'limit':2,'mode':'inventory'}}
        question='我有番茄和土豆，推荐两道菜'
        fast=understand(question,use_model=False)
        cfg=copy.deepcopy(settings());cfg['fast_path']['enabled']=False
        with patch('src.nlu.router.settings',return_value=cfg),patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            remote=understand(question,model_call=lambda *a:oracle)
        self.assertEqual((fast['intent'],fast['slots']),(remote['intent'],remote['slots']))

    def test_simple_recommendation_uses_literal_difficulty_without_model(self):
        model=Mock(side_effect=AssertionError('simple difficulty should be local'))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('推荐两道简单的鸡肉菜',model_call=model)
        model.assert_not_called()
        self.assertEqual(result['path'],'fast')
        self.assertEqual(result['intent'],'ingredient_recommend')
        self.assertEqual(result['slots']['constraints']['max_difficulty'],2)
        self.assertNotIn('unresolved',result['slots']['constraints'])

    def test_ordinal_name_question_uses_selected_recommendation_without_model(self):
        model=Mock(side_effect=AssertionError('ordinal recommendation reference should be local'))
        context={'slots':{'last_recommendations':[
            {'dish_name':'鸡蛋花','source_path':'a.md'},
            {'dish_name':'糖拌西红柿','source_path':'b.md'}]}}
        for question,kind in (('现在第二道叫什么？','facts'),('第二道菜是什么？','facts'),
                              ('现在第二道叫什么？给我完整做法。','source')):
            with self.subTest(question=question),patch('service_runtime.RuntimeStore') as cache:
                cache.return_value.get.return_value=None
                result=understand(question,context,model_call=model)
            self.assertEqual(result['path'],'fast')
            self.assertEqual(result['intent'],'follow_up')
            self.assertEqual(result['slots']['selected_ref'],2)
            self.assertEqual(result['slots']['request_kind'],kind)
        model.assert_not_called()

    def test_ordinal_name_question_survives_rate_limit_without_identity_fallback(self):
        context={'slots':{'last_recommendations':[
            {'dish_name':'鸡蛋花','source_path':'a.md'},
            {'dish_name':'糖拌西红柿','source_path':'b.md'}]}}
        cfg=copy.deepcopy(settings());cfg['fast_path']['enabled']=False
        def limited(*args):raise HTTPError('https://example.invalid',429,'busy',{},None)
        with patch('src.nlu.router.settings',return_value=cfg),patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('现在第二道叫什么？',context,model_call=limited)
        self.assertEqual(result['path'],'rule')
        self.assertEqual(result['intent'],'follow_up')
        self.assertEqual(result['slots']['selected_ref'],2)

    def test_ordinal_selection_and_full_method_use_local_route(self):
        model=Mock(side_effect=AssertionError('ordinal selection and full method should be local'))
        context={'slots':{'last_recommendations':[
            {'dish_name':'西红柿炒鸡蛋','source_path':'a.md'},
            {'dish_name':'糖拌西红柿','source_path':'b.md'}]}}
        cases=(('只看第九道。','follow_up',9,'facts'),
               ('第二道的完整做法。','recipe_lookup',2,'source'))
        for question,intent,ref,kind in cases:
            with self.subTest(question=question),patch('service_runtime.RuntimeStore') as cache:
                cache.return_value.get.return_value=None
                result=understand(question,context,model_call=model)
            self.assertEqual(result['path'],'fast')
            self.assertEqual(result['intent'],intent)
            self.assertEqual(result['slots']['selected_ref'],ref)
            self.assertEqual(result['slots']['request_kind'],kind)
        model.assert_not_called()

    def test_named_complete_source_and_bundle_use_local_route(self):
        model=Mock(side_effect=AssertionError('named source bundles should be local'))
        cases=(('查看宫保鸡丁完整原文和图片。','宫保鸡丁','bundle'),
               ('查看基础牛奶面包原文。','基础牛奶面包','source'),
               ('现在换成西红柿炒鸡蛋，给我完整原文。','西红柿炒鸡蛋','source'))
        for question,dish,kind in cases:
            with self.subTest(question=question),patch('service_runtime.RuntimeStore') as cache:
                cache.return_value.get.return_value=None
                result=understand(question,{},model_call=model)
            self.assertEqual(result['path'],'fast')
            self.assertEqual(result['intent'],'recipe_lookup')
            self.assertEqual(result['slots']['dish'],dish)
            self.assertEqual(result['slots']['request_kind'],kind)
        model.assert_not_called()

    def test_image_evidence_limit_question_uses_local_image_capability(self):
        model=Mock(side_effect=AssertionError('image evidence limits should not need a model'))
        context={'slots':{'selected_dish':{
            'dish_name':'基础牛奶面包',
            'source_path':'data/HowToCook/dishes/staple/基础牛奶面包/基础牛奶面包.md'}}}
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('只根据面包图片判断内部是否已经完全熟透。',context,model_call=model)
        self.assertEqual(result['path'],'fast')
        self.assertEqual(result['slots']['request_kind'],'images')
        self.assertEqual(result['slots']['dish'],'基础牛奶面包')
        model.assert_not_called()

    def test_thanks_with_retention_words_is_local_chitchat(self):
        model=Mock(side_effect=AssertionError('polite closure should not need a model'))
        context={'slots':{'last_recommendations':[
            {'dish_name':'西红柿炒鸡蛋','source_path':'a.md'},
            {'dish_name':'糖拌西红柿','source_path':'b.md'}]}}
        result=understand('谢谢，先保留这两道。',context,model_call=model)
        self.assertEqual(result['path'],'fast')
        self.assertEqual(result['intent'],'chitchat')
        self.assertEqual(result['answer_mode'],'general_chat')
        model.assert_not_called()

    def test_ordinal_ingredient_followup_uses_local_route(self):
        model=Mock(side_effect=AssertionError('ordinal ingredient follow-up should be local'))
        context={'slots':{'last_recommendations':[
            {'dish_name':'鸡蛋花','source_path':'a.md'},
            {'dish_name':'糖拌西红柿','source_path':'b.md'}]}}
        for question in ('第二道还需要准备哪些食材？','第二项还缺什么？'):
            with self.subTest(question=question),patch('service_runtime.RuntimeStore') as cache:
                cache.return_value.get.return_value=None
                result=understand(question,context,model_call=model)
            self.assertEqual(result['path'],'fast')
            self.assertEqual(result['intent'],'follow_up')
            self.assertEqual(result['slots']['selected_ref'],2)
            self.assertEqual(result['slots']['request_kind'],'ingredients')
        model.assert_not_called()

    def test_ingredient_followup_with_source_clarifier_uses_local_route(self):
        model=Mock(side_effect=AssertionError('current-dish ingredient follow-up should be local'))
        context={'slots':{'selected_dish':{
            'dish_name':'西红柿炒鸡蛋',
            'source_path':'data/HowToCook/dishes/vegetable_dish/西红柿炒鸡蛋.md'}}}
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('这道还缺什么？别沿用面包的材料。',context,model_call=model)
        self.assertEqual(result['path'],'fast')
        self.assertEqual(result['intent'],'follow_up')
        self.assertEqual(result['slots']['request_kind'],'ingredients')
        self.assertEqual(result['slots']['dish'],'西红柿炒鸡蛋')
        model.assert_not_called()

    def test_selected_recipe_fact_questions_use_local_route(self):
        context={'slots':{'selected_dish':{
            'dish_name':'基础牛奶面包',
            'source_path':'data/HowToCook/dishes/staple/基础牛奶面包/基础牛奶面包.md'}}}
        questions=(
            '酵头需要多少酵母、水温多少、发酵多久？请区分静置时间和发酵时间，并给出依据。',
            '面包的烘烤温度和时间是多少？原文有没有根据烤箱表现调整的方法？')
        for question in questions:
            with self.subTest(question=question),patch('service_runtime.RuntimeStore') as cache,\
                    patch('src.nlu.router._model',side_effect=AssertionError('selected recipe facts should be local')):
                cache.return_value.get.return_value=None
                result=understand(question,context)
            self.assertEqual(result['path'],'fast')
            self.assertEqual(result['intent'],'recipe_lookup')
            self.assertEqual(result['slots']['request_kind'],'facts')
            self.assertEqual(result['slots']['dish'],'基础牛奶面包')

    def test_quick_recommendation_uses_literal_duration_filter_without_model(self):
        model=Mock(side_effect=AssertionError('quick request should be local'))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('推荐一道快手菜',model_call=model)
        model.assert_not_called()
        self.assertEqual(result['path'],'fast')
        self.assertEqual(result['intent'],'ingredient_recommend')
        self.assertEqual(result['slots']['limit'],1)
        self.assertNotIn('unresolved',result['slots']['constraints'])

    def test_ambiguous_meals_are_not_fast_tracked(self):
        model=Mock(return_value=validate({'intent':'unclear','confidence':.1}))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('安排早餐和晚餐',model_call=model)
        model.assert_called_once()
        self.assertNotEqual(result['path'],'fast')
