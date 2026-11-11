import unittest
from dialogue_harness import DialogueHarness
from src.nlu.router import validate


class FullBrowserRepairsTests(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_replacement_and_following_stock_removal(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和土豆，推荐两道菜，不要油炸和花生。')
        original=list(chat.state['choices'])
        prepared,result,_=await chat.turn('把第二道换一道，其他不变。')
        self.assertEqual(prepared['task'].get('replacement_index'),2)
        self.assertEqual(prepared['task']['state']['extra_constraints'],[])
        self.assertEqual(len(result['outputs'][0]['result']['recommendations']),1)
        self.assertEqual(chat.state['choices'][0],original[0])
        self.assertNotEqual(chat.state['choices'][1],original[1])
        prepared,result,_=await chat.turn('现在没有番茄了，按剩下的食材再推荐两道，不要油炸和花生。')
        self.assertEqual(prepared['task']['state']['ingredients'],['土豆'])
        self.assertEqual(prepared['task']['state']['extra_constraints'],[])
        self.assertEqual(len(result['outputs'][0]['result']['recommendations']),2)

    async def test_legacy_action_residue_is_removed_without_removing_real_budget(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和土豆，推荐两道菜。')
        chat.state['extra_constraints']=['把第二道换一道','预算不超过5元']
        prepared,_,_=await chat.turn('我有土豆，推荐两道菜。')
        self.assertEqual(prepared['task']['state']['extra_constraints'],['预算不超过5元'])

    async def test_short_missing_ingredients_question_keeps_selected_recipe(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和土豆，推荐两道菜，不要油炸和花生。')
        await chat.turn('第二道怎么做？')
        selected=chat.session.slots['selected_dish']['source_path']
        bad=lambda *_:validate({'intent':'recipe_lookup','confidence':.99,'slots':{'dish':'这道还缺哪些食材'}})
        prepared,result,_=await chat.turn('这道还缺哪些食材？',model=bad)
        self.assertEqual(prepared['task']['action'],'ingredients')
        self.assertEqual(prepared['named_recipe']['source_path'],selected)
        self.assertIn('土豆',result['outputs'][0]['result']['short_answer'])

    async def test_ordinal_and_chapter_are_resolved_independently(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和土豆，推荐两道菜，不要油炸和花生。')
        source=chat.session.slots['last_recommendations'][0]['source_path']
        bad=lambda *_:validate({'intent':'recipe_lookup','confidence':.99,
            'slots':{'selected_ref':1,'request_kind':'source'}})
        prepared,result,_=await chat.turn('第一道的计算原文。',model=bad)
        self.assertEqual(prepared['nlu']['slots']['section'],'计算')
        self.assertEqual(prepared['named_recipe']['source_path'],source)
        text=result['outputs'][0]['result']['short_answer']
        self.assertNotIn('附加内容',text)

    async def test_cancel_price_condition_preserves_menu_and_removes_only_permissions(self):
        chat=DialogueHarness()
        await chat.turn('改成晚餐，仍然两个人，60分钟，两菜一汤，预算不超过5元。')
        bad=lambda *_:validate({'intent':'meal_plan','confidence':.99,
            'slots':{'meal':{'people':2,'minutes':60,'dishes':2,'soups':1,'meal_type':'dinner'},
                     'constraints':{'unresolved':['价格不用核实可以用常备调料.继续按']}}})
        prepared,result,_=await chat.turn('价格不用核实，可以用常备调料。继续按两个人、60分钟、两菜一汤安排晚餐。',model=bad)
        request=prepared['task']['meal_request']
        self.assertEqual(request['constraints'].get('unresolved',[]),[])
        self.assertEqual((request['people'],request['minutes'],request['dishes'],request['soups']),(2,60,2,1))
        self.assertTrue(result['outputs'][0]['result']['meal_plan']['complete'])

    async def test_menu_budget_survives_model_omission_until_explicit_waiver(self):
        chat=DialogueHarness()
        await chat.turn('两个人，30分钟，两菜一汤，安排晚餐，预算不超过5元。')
        model=lambda *_:validate({'intent':'meal_plan','confidence':.99,
            'slots':{'meal':{'people':2,'minutes':60,'dishes':2,'soups':1,'meal_type':'dinner'}}})
        prepared,result,_=await chat.turn('把时间改成60分钟。',model=model)
        self.assertTrue(prepared['task']['meal_request']['constraints']['unresolved'])
        self.assertEqual(result['outputs'][0]['result']['status'],'insufficient')

    async def test_out_of_range_chapter_does_not_fall_back_to_selected_dish(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和土豆，推荐两道菜。')
        await chat.turn('第二道怎么做？')
        model=lambda *_:validate({'intent':'recipe_lookup','confidence':.99,
            'slots':{'selected_ref':9,'request_kind':'source'}})
        prepared,_,_=await chat.turn('第九道的计算原文。',model=model)
        self.assertEqual(prepared['task']['action'],'clarify')

    def test_permission_cleanup_keeps_dietary_and_unknown_constraints(self):
        from cookbuddy.meal_planning import unresolved_conditions
        values=['采购预算不超过5元','不要花生','难度上限1','每天钠不超过5克']
        result=unresolved_conditions(values,'价格不用核实，可以用常备调料。继续按两个人60分钟安排晚餐。')
        self.assertEqual(result,values[1:])

    def test_action_and_followup_parsers_do_not_swallow_extra_requirements(self):
        from src.agent.turn_intents import alternative_request,ingredient_followup,chapter_request
        self.assertIsNone(alternative_request('不要把第二道换掉。'))
        self.assertIsNone(alternative_request('把第二道换成鱼香肉丝。'))
        self.assertEqual(alternative_request('把第二道换一道，不要花生，其他不变。')['constraint'],'不要花生')
        self.assertFalse(ingredient_followup('这道还缺哪些食材？而且预算最多5元。'))
        self.assertIsNone(chapter_request('第一道的计算和操作原文。'))
