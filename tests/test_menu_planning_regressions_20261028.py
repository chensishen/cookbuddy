"""Actual corpus regressions for repeated menus, time selection and a missing soup."""
import unittest
from check_embedding import ROOT
from cookbuddy.meal_planning import meal_request,plan_meal
from recipe_tools import load_chunks


class MenuPlanningRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.chunks=load_chunks()

    def menu(self,question,kind='lunch',**extra):
        return plan_meal({**meal_request(question),'meal_type':kind,**extra},self.chunks)

    def test_three_dishes_and_soup_within_thirty_minutes(self):
        result=self.menu('两个人，30分钟，想做三菜一汤。')
        # The source-backed minimum found under the existing primary-ingredient
        # and difficulty rules is 35 minutes; never invent parallel savings.
        self.assertFalse(result['meal_plan']['complete'],result['short_answer'])
        self.assertEqual(result['meal_plan']['dishes'],2)
        self.assertEqual(result['meal_plan']['soups'],1)
        self.assertLessEqual(result['meal_plan']['estimated_minutes'],30)
        alternative=result['meal_plan']['alternative']
        self.assertGreater(alternative['estimated_minutes'],30)
        self.assertEqual(len(alternative['recipes']),4)
        self.assertEqual(sum(r['role']=='soup' for r in alternative['recipes']),1)
        for ref in result['evidence']:
            original=(ROOT/ref['source']).read_text().splitlines()
            self.assertEqual('\n'.join(original[ref['start_line']-1:ref['end_line']]),ref['quote'])
        complete=self.menu('两个人，35分钟，想做三菜一汤。')
        self.assertTrue(complete['meal_plan']['complete'],complete['short_answer'])

    def test_time_budget_expands_selection_instead_of_always_shortest(self):
        fast=self.menu('两个人，30分钟，想做两菜一汤。')
        relaxed=self.menu('两个人，60分钟，想做两菜一汤。')
        self.assertNotEqual([r['source'] for r in fast['recommendations']],
                            [r['source'] for r in relaxed['recommendations']])
        self.assertGreater(relaxed['meal_plan']['estimated_minutes'],fast['meal_plan']['estimated_minutes'])
        self.assertLessEqual(relaxed['meal_plan']['estimated_minutes'],60)

    def test_fresh_lunch_and_dinner_are_not_identical(self):
        lunch=self.menu('两个人，60分钟，安排午餐。','lunch')
        dinner=self.menu('两个人，60分钟，安排晚餐。','dinner')
        self.assertNotEqual({r['source'] for r in lunch['recommendations']},
                            {r['source'] for r in dinner['recommendations']})

    def test_exact_meal_prompts_keep_constraints_while_varying_menus(self):
        for kind,label,suffix in (
            ('breakfast','早餐','尽量简单'),
            ('lunch','午餐','想做两菜一汤'),
            ('dinner','晚餐','想做两菜一汤'),
        ):
            menus=set()
            for seed in range(1,7):
                result=self.menu(f'帮我安排两个人的{label}，30分钟，{suffix}。',kind,selection_seed=seed)
                plan=result['meal_plan']
                self.assertTrue(plan['complete'])
                self.assertLessEqual(plan['estimated_minutes'],30)
                self.assertEqual((plan['dishes'],plan['soups']),(1,0) if kind=='breakfast' else (2,1))
                menus.add(frozenset(r['source'] for r in result['recommendations']))
            self.assertGreater(len(menus),1,kind)

    def test_saved_mild_spice_and_cilantro_exclusion(self):
        from cookbuddy.ltm.guard import policy,source_allowed
        from cookbuddy.ltm.retriever import preference_source_text,taste_preference_match
        mild={'dimension':'spice_level','value':'1','polarity':'like'}
        hard=[{'dimension':'avoid_ingredient','value':'香菜','polarity':'avoid','strength':'hard'}]
        row={'description':'这是道家常菜。','evidence':[{'section':'简介','quote':'这道菜带着微微辣意。'}]}
        self.assertEqual(taste_preference_match(preference_source_text(row),mild),('微辣','微微辣意'))
        with policy(hard):
            result=plan_meal(meal_request('今晚两个人，推荐两道菜。'),self.chunks,seed=2,taste_preferences=[mild])
            limited=plan_meal(meal_request('今晚两个人，30分钟，难度最多1，推荐两道菜。'),
                              self.chunks,seed=2,taste_preferences=[mild])
        self.assertTrue(any(r['taste_matches'] for r in result['recommendations']))
        for menu in (result,limited):
            self.assertTrue(all(source_allowed(r['source'],hard) for r in menu['recommendations']))
            self.assertIn('避开香菜',menu['short_answer'])
        self.assertLessEqual(limited['meal_plan']['estimated_minutes'],30)
        self.assertTrue(all(r['difficulty']==1 for r in limited['recommendations']))
        self.assertIn('未核实到符合你偏好的微辣菜谱',limited['short_answer'])

    def test_strong_spice_prefers_verified_spicy_alternatives(self):
        from cookbuddy.ltm.guard import policy
        hard=[{'dimension':'avoid_ingredient','value':'香菜','polarity':'avoid','strength':'hard'}]
        with policy(hard):
            result=plan_meal(meal_request('今晚两个人，推荐两道菜。'),self.chunks,seed=2,
                             taste_preferences=[{'dimension':'spice_level','value':'3','polarity':'like'}])
        self.assertTrue(any(r.get('spicy_alternative') for r in result['recommendations']))
        self.assertIn('具体辣度未标明',result['short_answer'])
        self.assertIn('较辣',result['short_answer'])
        self.assertNotIn('中辣',result['short_answer'])

    def test_current_mild_request_overrides_saved_strong_spice(self):
        request=meal_request('今晚想吃微辣，两个人，推荐两道菜。')
        request['constraints']={'taste':'微辣'}
        result=plan_meal(request,self.chunks,seed=2,taste_preferences=[
            {'dimension':'spice_level','value':'3','polarity':'like'}])
        self.assertEqual(len(result['recommendations']),2)
        self.assertTrue(all(r['spice_match']=='explicit' for r in result['recommendations']))
        self.assertIn('按本次的微辣要求',result['short_answer'])
        self.assertNotIn('长期饮食偏好',result['short_answer'])

    def test_tofu_variants_are_the_same_primary_ingredient(self):
        from cookbuddy.menu_planner import primary_ingredients
        plain=primary_ingredients({'title':'凉拌豆腐','main_ingredients':['豆腐']})
        soft=primary_ingredients({'title':'皮蛋豆腐','main_ingredients':['内酯豆腐','皮蛋']})
        self.assertIn('豆腐',plain & soft)

    def test_breakfast_staple_is_not_an_egg_dish(self):
        result=self.menu('两个人60分钟安排早餐','breakfast')
        staple=next(r for r in result['recommendations'] if r['role']=='main_staple')
        self.assertRegex(staple['title'],r'粥|饭|面|粉|饼|包|吐司|麦|薯|玉米')

    def test_complete_menu_precedes_recent_history_avoidance(self):
        from recipe_tools import resolve_title
        from dataclasses import asdict
        from cookbuddy.ltm.extractor import rule_extract
        from cookbuddy.ltm.guard import policy
        recent=['凉拌豆腐','小米辣炒肉','金针菇汤','蒜苔炒肉末','清蒸南瓜','西红柿鸡蛋汤',
                '炒土豆和胡萝卜蒸菜','皮蛋豆腐','紫菜蛋花汤','吐司果酱','微波炉荷包蛋','耙耙柑茶','蒜蓉炒芹菜','炒青菜',
                '洋葱炒鸡蛋','勾芡香菇汤','水油焖蔬菜','乾隆白菜','凉拌金针菇','炒滑蛋','奶油蘑菇汤','荷兰豆炒腊肠','鸡蛋花','羊肉汤']
        sources=[resolve_title(title,self.chunks)[1] for title in recent]
        for seed in range(8):
            with policy([asdict(c) for c in rule_extract('换一套，不要刚才的。')]):
                result=self.menu('两个人，35分钟，午餐三菜一汤。',previous_sources=sources,selection_seed=seed)
            self.assertTrue(result['meal_plan']['complete'],(seed,result['short_answer']))
            self.assertEqual((result['meal_plan']['dishes'],result['meal_plan']['soups']),(3,1))


class MenuDialogueRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_time_change_preserves_menu(self):
        from dialogue_harness import DialogueHarness
        harness=DialogueHarness()
        await harness.turn('两个人，30分钟，午餐想做两菜一汤。')
        prepared,result,_=await harness.turn('时间改成60分钟，还是两菜一汤。')
        data=result['outputs'][-1]['result']
        self.assertIn('recommendations',data,data.get('short_answer'))
        self.assertTrue(data['meal_plan']['complete'])
        self.assertEqual((data['meal_plan']['people'],data['meal_plan']['minutes']),(2,60))

    async def test_menu_replacement_overrides_model_recipe_refinement(self):
        from dialogue_harness import DialogueHarness
        from src.nlu.router import validate
        harness=DialogueHarness()
        await harness.turn('两个人，60分钟，午餐两菜一汤。')
        model=lambda *args:validate({'intent':'constraint_refine','confidence':.95,
            'slots':{},'needs_clarification':True,'clarify_question':'换什么？'})
        prepared,result,_=await harness.turn('换一套，不要刚才的。',model=model)
        self.assertEqual(prepared['task']['action'],'meal_plan')
        self.assertTrue(result['outputs'][-1]['result']['meal_plan']['complete'])
        prepared,result,_=await harness.turn('换一套，不要刚才的。')
        data=result['outputs'][-1]['result']
        self.assertIn('meal_plan',data,data.get('short_answer'))
        self.assertTrue(data['meal_plan']['complete'])
        self.assertEqual((data['meal_plan']['people'],data['meal_plan']['minutes']),(2,60))

    async def test_recent_menus_survive_session_reload(self):
        from dialogue_harness import DialogueHarness
        from cookbuddy.session import SessionState
        harness=DialogueHarness();menus=[]
        for question in ('两个人，60分钟，安排午餐。','安排晚餐。','安排午餐。'):
            prepared,result,_=await harness.turn(question)
            data=result['outputs'][-1]['result']
            self.assertTrue(data['meal_plan']['complete'],data['short_answer'])
            request=prepared['task']['meal_request']
            self.assertEqual(request['minutes'],60)
            current={r['source'] for r in data['recommendations']}
            self.assertFalse(current & set().union(*menus),data['short_answer'])
            menus.append(current)
            harness.session=SessionState(**harness.session.record())
        self.assertEqual(set(harness.session.slots['recent_meal_sources']),set().union(*menus))
        self.assertLessEqual(len(harness.session.slots['recent_meal_sources']),24)

    async def test_more_after_menu_reuses_meal_conditions(self):
        from dialogue_harness import DialogueHarness
        chat=DialogueHarness()
        await chat.turn('两个人，30分钟，午餐想做两菜一汤。')
        prepared,result,_=await chat.turn('再推荐几个菜')
        self.assertEqual(prepared['task']['action'],'meal_plan')
        plan=result['outputs'][0]['result']['meal_plan']
        self.assertEqual((plan['meal_type'],plan['dishes'],plan['soups']),('lunch',2,1))
        self.assertLessEqual(plan['estimated_minutes'],30)
