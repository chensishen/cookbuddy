import unittest
from unittest.mock import Mock,patch
from dialogue_harness import DialogueHarness
from recipe_tools import load_chunks,resolve_title
from src.nlu.router import understand,validate


class IngredientIdentityTests(unittest.TestCase):
    def test_quantity_suffixes_share_identity_without_losing_source_notes(self):
        from ingredient_recommendation import recommend
        rows=[r for r in load_chunks() if r['title']=='西红柿土豆炖牛肉']
        row=recommend(['番茄','土豆'],rows,1)[0]
        self.assertNotIn('土豆两三个',row['missing'])
        self.assertNotIn('姜四片',row['required'])
        self.assertNotIn('葱一根',row['required'])
        self.assertNotIn('香叶两片',row['required'])
        self.assertNotIn('小料',row['required'])
        self.assertEqual(sum('洋葱' in n for n in row['required']),1)
        self.assertTrue(any('土豆两三个' in n for n in row['unresolved']))

    def test_real_food_names_and_options_are_preserved(self):
        from ingredient_recommendation import normalize_ingredient
        for name in ('三文鱼','五花肉','八角','九层塔','四季豆','十三香'):
            self.assertEqual(normalize_ingredient(name),name)
        self.assertEqual(normalize_ingredient('土豆两三个（看喜好）'),'土豆')
        self.assertEqual(normalize_ingredient('姜四片'),'姜')
        self.assertEqual(normalize_ingredient('比拳头大一点的洋葱一个'),'洋葱')


class ClarificationContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_choosing_dish_resumes_original_calculation_request(self):
        chat=DialogueHarness()
        await chat.turn('安排两个人的晚餐，60分钟，两菜一汤。')
        prepared,_,_=await chat.turn('第九道的计算原文。')
        self.assertEqual(prepared['task']['action'],'clarify')
        pending=chat.session.slots['nlu_pending']
        self.assertEqual(pending['slot'],'dish')
        choice=pending['options'][0]
        bad=lambda *_:validate({'intent':'recipe_lookup','confidence':.99,'slots':{'dish':choice,'request_kind':'source'}})
        prepared,result,_=await chat.turn(choice,model=bad)
        self.assertEqual(prepared['nlu']['slots']['section'],'计算')
        self.assertEqual(prepared['named_recipe']['dish_name'],choice)
        self.assertNotIn('附加内容',result['outputs'][0]['result']['short_answer'])


class DirectChapterTests(unittest.TestCase):
    def test_extra_constraints_and_unknown_chapters_do_not_take_shortcut(self):
        from src.nlu.router import literal_decision
        title,source=resolve_title('瘦肉土豆片',load_chunks())
        context={'slots':{'selected_dish':{'dish_name':title,'source_path':source}}}
        for question in ('计算，不要花生','计算，预算5元','不存在的章节'):
            self.assertIsNone(literal_decision(question,context))

    def test_pending_selection_keeps_constraints_and_ignores_new_topics(self):
        from src.nlu.router import pending_selection
        slots=validate({'intent':'recipe_section','confidence':1.,'slots':{'section':'计算','request_kind':'source',
            'constraints':{'excluded_ingredients':['花生']}}})['slots']
        context={'slots':{'nlu_pending':{'slot':'dish','options':['瘦肉土豆片'],
            'slots':slots}}}
        selected=pending_selection('瘦肉土豆片',context)
        self.assertEqual(selected['slots']['constraints']['excluded_ingredients'],['花生'])
        self.assertEqual(slots['dish'],'')
        self.assertIsNone(pending_selection('你是谁？',context))
        self.assertIsNone(pending_selection('瘦肉土豆片，不要花生',context))

    def test_known_chapter_does_not_call_remote_model(self):
        title,source=resolve_title('瘦肉土豆片',load_chunks())
        context={'slots':{'selected_dish':{'dish_name':title,'source_path':source}}}
        model=Mock(side_effect=AssertionError('Known chapter should be local'))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('计算',context,model_call=model)
        model.assert_not_called()
        self.assertEqual(result['slots']['section'],'计算')
        self.assertEqual(result['slots']['request_kind'],'source')


class MenuAndMemoryTests(unittest.TestCase):
    def test_partial_meal_plan_explains_requested_and_actual_counts(self):
        from cookbuddy.meal_planning import meal_request,plan_meal
        request=meal_request('两个人，30分钟，想做三菜一汤。')
        request.update(selection_seed='browser-regression',previous_sources=[],ingredients=[],mode='preference',constraints={'unresolved':[]})
        result=plan_meal(request,load_chunks())
        self.assertFalse(result['meal_plan']['complete'])
        self.assertIn('未能在30分钟内完成请求的3道菜＋1道汤',result['short_answer'])
        self.assertIn('当前先安排2道菜＋1道汤',result['short_answer'])

    def test_no_memory_command_is_recognized(self):
        from cookbuddy.ltm.management import parse_command
        self.assertEqual(parse_command('以后也不要记忆这些饮食偏好。'),('disable',''))
