"""Regressions for the five P1 browser findings (2026-11-11)."""
import unittest


class DishMatchTests(unittest.TestCase):
    def test_unknown_dish_is_not_forced_onto_a_different_recipe(self):
        from src.nlu.matching import fuzzy_match
        from src.nlu.config import settings
        titles={'田螺酿','红烧鱼','烤鱼','蒸水蛋'}
        for query in ('佛跳墙','烤乳猪'):
            result=fuzzy_match(query,titles,fillers=False,min_score=settings()['fuzzy']['t_dish'])
            self.assertIsNone(result['value'],result)
        self.assertEqual(fuzzy_match('宫爆鸡丁',{'宫保鸡丁'},fillers=False,
                                     min_score=settings()['fuzzy']['t_dish'])['value'],'宫保鸡丁')


class ChangeDishTests(unittest.TestCase):
    def test_change_of_dish_is_a_recommendation_not_a_memory_write(self):
        from cookbuddy.recommendation_turn import interpret
        turn=interpret('换成别的菜')
        self.assertTrue(turn.recommend);self.assertTrue(turn.resume);self.assertEqual(turn.unresolved,[])
        turn=interpret('换成清淡一点的菜')
        self.assertTrue(turn.recommend);self.assertEqual(turn.unresolved,['清淡一点'])

    def test_change_detection_skips_ingredient_and_count_edits(self):
        from src.agent.turn_intents import change_dish_request
        for question in ('换成别的菜','换成清淡一点的菜','再来一道别的菜','换菜'):
            self.assertTrue(change_dish_request(question),question)
        for question in ('换成牛肉','改成三道菜','把土豆换成牛肉','记住我不吃香菜'):
            self.assertFalse(change_dish_request(question),question)


class FactRoutingTests(unittest.TestCase):
    def test_original_as_qualifier_keeps_a_fact_question(self):
        from src.nlu.router import understand
        plan=understand('宫保鸡丁里的花生需要多少？可以用腰果替换吗？原文没说明的请明确指出。',{},use_model=False)
        self.assertEqual(plan['intent'],'recipe_lookup')
        self.assertEqual(plan['slots']['request_kind'],'facts')

    def test_explicit_full_text_request_stays_source(self):
        from src.nlu.router import understand
        plan=understand('看一下蒸水蛋的操作原文。',{},use_model=False)
        self.assertEqual(plan['slots']['request_kind'],'source')


class ExplicitSectionTests(unittest.TestCase):
    def test_named_chapter_is_kept(self):
        from src.nlu.router import understand
        plan=understand('看一下蒸水蛋的操作原文。',{},use_model=False)
        self.assertEqual(plan['slots']['request_kind'],'source')
        self.assertEqual(plan['slots']['section'],'操作')

    def test_whole_text_request_keeps_no_section(self):
        from src.nlu.router import understand
        plan=understand('请给我宫保鸡丁的完整原文和图片',{},use_model=False)
        self.assertEqual(plan['slots']['request_kind'],'bundle')
        self.assertEqual(plan['slots']['section'],'')


class IngredientCheckTests(unittest.TestCase):
    def test_check_ingredients_skips_the_chapter_question(self):
        from src.nlu.router import understand
        plan=understand('核对凉拌黄瓜用料。',{},use_model=False)
        self.assertEqual(plan['slots']['request_kind'],'ingredients')
        self.assertEqual(plan['slots']['dish'],'凉拌黄瓜')

    def test_simple_template_request_is_fully_understood(self):
        from cookbuddy.recommendation_turn import interpret
        turn=interpret('今天想用鸡蛋做一道简单的菜')
        self.assertTrue(turn.recommend)
        self.assertEqual(turn.max_difficulty,2)
        self.assertEqual(turn.unresolved,[])
        self.assertIn(('ingredients','set',('鸡蛋',)),
                      [(e.slot,e.operation,e.values) for e in turn.edits])


class StyleFilterTests(unittest.TestCase):
    def test_style_word_is_a_supported_condition(self):
        from cookbuddy.recommendation_turn import interpret
        turn=interpret('推荐一道凉拌菜，我不吃香菜')
        self.assertEqual(turn.unresolved,[])
        self.assertIn(('extra_constraints','add',('凉拌',)),
                      [(e.slot,e.operation,e.values) for e in turn.edits])

    def test_cold_dish_request_returns_cold_dishes(self):
        import asyncio
        from dialogue_harness import DialogueHarness

        async def run():
            harness=DialogueHarness()
            _,result,_=await harness.turn('推荐一道凉拌菜')
            return result['outputs'][0]['result'].get('recommendations',[])

        rows=asyncio.run(run())
        self.assertTrue(rows)
        for row in rows:
            self.assertIn('凉拌',row['title']+row.get('description',''),row)


class EquipmentMemoryTests(unittest.TestCase):
    def test_owned_equipment_is_extracted_and_not_inventory(self):
        from cookbuddy.ltm.extractor import rule_extract
        rows=rule_extract('请记住：我不吃香菜，家里有空气炸锅。')
        equipment=[c for c in rows if c.dimension=='equipment']
        self.assertEqual([(c.value,c.polarity,c.strength) for c in equipment],
                         [('有空气炸锅','like','soft')])
        self.assertFalse([c for c in rows if c.dimension=='inventory'])

    def test_owned_equipment_is_not_a_hard_filter(self):
        from cookbuddy.ltm.guard import forbidden,source_allowed
        hard=[{'dimension':'equipment','value':'有空气炸锅','polarity':'like','strength':'soft'}]
        self.assertEqual(forbidden(hard),([],[],[]))
        self.assertTrue(source_allowed('data/HowToCook/dishes/staple/空气炸锅照烧鸡饭/空气炸锅照烧鸡饭.md',hard))

    def test_owned_equipment_is_preferred_in_recommendations(self):
        from preference_recommendation import recommend_preferences
        from cookbuddy.recommendation_schema import new_state
        from recipe_tools import load_chunks
        state=new_state();state['ingredients']=['鸡蛋'];state['limit']=3
        result=recommend_preferences(state,load_chunks(),owned_equipment=['空气炸锅'])
        self.assertEqual(result['status'],'verified')
        self.assertIn('空气炸锅',result['recommendations'][0]['title']+result['recommendations'][0].get('reason',''))
        self.assertTrue(result['recommendations'][0].get('equipment_match'))

    def test_equipment_confirmation_label(self):
        from cookbuddy.ltm.management import describe
        from types import SimpleNamespace
        self.assertEqual(describe(SimpleNamespace(dimension='equipment',value='有空气炸锅',polarity='like',strength='soft')),
                         '设备：有空气炸锅')


class CompoundRequestTests(unittest.TestCase):
    def test_memory_and_recipe_in_one_turn(self):
        import asyncio
        from dialogue_harness import DialogueHarness

        async def run(question):
            harness=DialogueHarness()
            prepared,result,_=await harness.turn(question,
                management={'message':'已记住：不吃香菜。','needs_confirmation':False})
            return prepared,result

        prepared,result=asyncio.run(run('请记住：我不吃香菜。今天想用鸡蛋做一道简单的菜。'))
        self.assertEqual(prepared['memory_confirmation'],'已记住：不吃香菜。')
        self.assertIsNone(prepared['management'])
        self.assertEqual(prepared['task']['action'],'recommend')
        self.assertTrue(result['outputs'][0]['result'].get('recommendations'))

    def test_memory_only_turn_is_unchanged(self):
        import asyncio
        from dialogue_harness import DialogueHarness

        async def run():
            harness=DialogueHarness()
            return await harness.turn('请记住：我不吃香菜。',
                management={'message':'已记住：不吃香菜。','needs_confirmation':False})

        prepared,result,_=asyncio.run(run())
        self.assertIsNone(prepared['memory_confirmation'])
        self.assertEqual(prepared['management']['message'],'已记住：不吃香菜。')
        self.assertEqual(result['outputs'][0]['tool'],'memory_management')


class WeeklyMenuTests(unittest.TestCase):
    def test_multi_day_request_returns_a_boundary_not_one_meal(self):
        from cookbuddy.meal_planning import multi_day_request,plan_meal
        question='帮我设计一周的菜谱，每天三菜一汤，家里有老人和小孩。'
        self.assertTrue(multi_day_request(question))
        result=plan_meal({'question':question,'people':None,'minutes':None,'dishes':3,'soups':1,
                          'constraints':{}},[],)
        self.assertEqual(result['status'],'insufficient')
        self.assertIn('一次只能安排一餐',result['short_answer'])


class ChangeDishExclusionTests(unittest.TestCase):
    def test_change_of_dish_excludes_the_current_batch(self):
        import asyncio
        from dialogue_harness import DialogueHarness

        async def run():
            harness=DialogueHarness()
            _,first,_=await harness.turn('我有鸡蛋，推荐两道菜。')
            first_titles=[row['title'] for row in first['outputs'][0]['result']['recommendations']]
            prepared,_,_=await harness.turn('换成别的菜。')
            return first_titles,prepared['task'].get('exclude_dishes',[])

        first_titles,excluded=asyncio.run(run())
        self.assertTrue(first_titles)
        self.assertTrue(set(first_titles)<=set(excluded),excluded)


if __name__=='__main__':
    unittest.main()
