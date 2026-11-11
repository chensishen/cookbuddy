import unittest
from unittest.mock import Mock
from dialogue_harness import DialogueHarness
from src.nlu.router import validate


class MenuOfferAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_accept_complete_offer_preserves_the_offered_recipes(self):
        chat=DialogueHarness()
        _,result,_=await chat.turn('两个人，30分钟，安排午餐，三菜一汤。')
        data=result['outputs'][0]['result']
        offered=data['meal_plan']['alternative']['recipes']
        self.assertTrue(offered)
        model=Mock(side_effect=AssertionError('Explicit offer acceptance should use saved context'))
        _,result,_=await chat.turn('那就35分钟，按完整方案安排。',model=model)
        data=result['outputs'][0]['result']
        self.assertTrue(data['meal_plan']['complete'])
        self.assertEqual([r['source'] for r in data['recommendations']],[r['source'] for r in offered])
        model.assert_not_called()
        self.assertNotIn('pending_meal_offer',chat.session.slots)

    async def test_new_exclusion_is_not_consumed_as_plain_acceptance(self):
        chat=DialogueHarness()
        await chat.turn('两个人，30分钟，安排午餐，三菜一汤。')
        model=lambda *_:validate({'intent':'meal_plan','confidence':.99,
            'slots':{'meal':{'people':2,'minutes':35,'dishes':3,'soups':1,'meal_type':'lunch'},
                     'constraints':{'excluded_ingredients':['豆腐']}}})
        prepared,result,_=await chat.turn('那就35分钟，按完整方案安排，但是不要豆腐。',model=model)
        self.assertNotIn('accepted_sources',prepared['task']['meal_request'])
        self.assertIn('豆腐',prepared['task']['meal_request']['constraints']['excluded_ingredients'])
        self.assertFalse(any('豆腐' in r['title'] for r in result['outputs'][0]['result']['recommendations']))

    async def test_new_menu_does_not_keep_an_old_offer(self):
        chat=DialogueHarness()
        await chat.turn('两个人，30分钟，安排午餐，三菜一汤。')
        self.assertIn('pending_meal_offer',chat.session.slots)
        await chat.turn('两个人，60分钟，安排晚餐，两菜一汤。')
        self.assertNotIn('pending_meal_offer',chat.session.slots)

    def test_acceptance_does_not_ignore_budget_or_unknown_text(self):
        from cookbuddy.meal_planning import accepted_offer_minutes
        offer={'minutes':35,'sources':[]}
        self.assertEqual(accepted_offer_minutes('按刚才的完整方案安排。',offer),35)
        for question in ('按完整方案安排，预算5元。','按完整方案安排，不要花生。',
                         '按完整方案安排，改成早餐。','按完整方案安排，然后去市场。'):
            self.assertIsNone(accepted_offer_minutes(question,offer))
