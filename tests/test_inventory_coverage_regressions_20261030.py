import unittest
from ingredient_recommendation import recommend,normalize_ingredient
from recipe_tools import load_chunks


class InventoryCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.chunks=load_chunks()

    def test_multiple_ingredients_are_covered_across_recommendations(self):
        for ingredients in (['番茄','马铃薯'],['西红柿','土豆'],['马铃薯','鸡蛋']):
            with self.subTest(ingredients=ingredients):
                rows=recommend(ingredients,self.chunks,2)
                self.assertEqual(len(rows),2)
                covered=set().union(*(set(r['matched']) for r in rows))
                self.assertTrue({normalize_ingredient(s) for s in ingredients}<=covered,rows)

    def test_tomato_and_egg_are_used_in_both_top_recipes(self):
        for tomato in ('西红柿','番茄'):
            with self.subTest(tomato=tomato):
                rows=recommend([tomato,'鸡蛋'],self.chunks,2)
                self.assertEqual([r['title'] for r in rows],['西红柿炒鸡蛋','西红柿鸡蛋汤'])
                self.assertTrue(all(set(r['matched'])=={'番茄','鸡蛋'} for r in rows))
                self.assertIn('味素',rows[1]['optional'])
                self.assertNotIn('味素',rows[1]['missing'])

    def test_constrained_inventory_can_use_different_ingredients_in_different_dishes(self):
        from cookbuddy.task_context import advance_task
        from preference_recommendation import recommend_preferences
        state=advance_task('我有番茄和马铃薯，推荐两道菜，不要鸡蛋，说明还缺什么。',None)['state']
        data=recommend_preferences(state,self.chunks)
        self.assertEqual(len(data.get('recommendations',[])),2,data['short_answer'])
        self.assertTrue({'番茄','土豆'}<=set().union(*(set(r['matched']) for r in data['recommendations'])))

    def test_equipment_is_not_reported_as_a_missing_ingredient(self):
        row=next(r for r in recommend(['番茄'],self.chunks,10) if r['title']=='糖拌西红柿')
        self.assertNotIn('冰箱',row['missing'])
        self.assertIn('冰箱',row['tools'])

    def test_calculation_ingredients_are_included_in_coverage(self):
        chunks=[r for r in self.chunks if r['title']=='罗宋汤']
        rows=recommend(['番茄','马铃薯'],chunks,1)
        self.assertEqual(set(rows[0]['matched']),{'番茄','土豆'})
        self.assertTrue(any(e['section']=='计算' for e in rows[0]['evidence']))

    def test_calculated_quantities_do_not_make_optional_food_required(self):
        from ingredient_recommendation import recipe_ingredient_fields
        from check_embedding import ROOT
        rows=[r for r in self.chunks if r['title']=='酱炖蟹']
        required,optional,_,_,_=recipe_ingredient_fields(rows,(ROOT/rows[0]['source']).read_text().splitlines())
        self.assertIn('鸡蛋',optional)
        self.assertNotIn('鸡蛋',required)

    def test_formula_notes_are_not_reported_as_ingredients(self):
        from ingredient_recommendation import recipe_ingredient_fields
        from check_embedding import ROOT
        rows=[r for r in self.chunks if r['title']=='西红柿炒鸡蛋'
              and ('必备原料' in r['section'] or r['section'].split(' > ')[0]=='计算')]
        required,_,_,uncertain,_=recipe_ingredient_fields(rows,(ROOT/rows[0]['source']).read_text().splitlines())
        self.assertNotIn('向上取整',required)
        self.assertNotIn('向上取整',uncertain)

    def test_conditional_calculation_notes_and_possessives_are_not_missing_ingredients(self):
        from ingredient_recommendation import recipe_ingredient_fields
        from check_embedding import ROOT
        for title,expected in (
            ('太阳蛋',{'油','鸡蛋','盐'}),
            ('酸辣蕨根粉',{'油泼辣子','盐','糖','蕨根粉','酱油','香醋'}),
            ('吐司果酱',{'新鲜吐司','果酱'}),
            ('西红柿鸡蛋汤',{'番茄','鸡蛋','大蒜','姜','盐','葱','香油'}),
        ):
            with self.subTest(title=title):
                rows=[r for r in self.chunks if r['title']==title]
                required,_,_,_,_=recipe_ingredient_fields(rows,(ROOT/rows[0]['source']).read_text().splitlines())
                self.assertEqual(required,expected)

    def test_named_stock_check_does_not_discard_other_conditions(self):
        from src.agent.turn_intents import named_ingredient_stock
        self.assertIsNone(named_ingredient_stock('罗宋汤还需要哪些食材？我有番茄和马铃薯，不要鸡蛋。','罗宋汤'))

    def test_personalization_cannot_displace_the_only_potato_dish(self):
        from cookbuddy.ltm.retriever import personalize
        rows=[{'title':'番茄甲','matched':['番茄'],'rerank_score':10},
              {'title':'番茄乙','matched':['番茄'],'rerank_score':9},
              {'title':'土豆','matched':['土豆'],'rerank_score':1}]
        chosen=personalize(rows,{'soft_prefs':[]},k=2,inventory=['番茄','马铃薯'])
        self.assertEqual({s for r in chosen for s in r['matched']},{'番茄','土豆'})

    def test_stored_soft_flavor_does_not_empty_an_inventory_result(self):
        from cookbuddy.ltm.retriever import personalize_result
        from types import SimpleNamespace
        rows=recommend(['番茄','马铃薯'],self.chunks,2)
        data={'recommendations':rows,'short_answer':'推荐两道菜','sources':[],'evidence':[]}
        state={'mode':'inventory','ingredients':['番茄','马铃薯'],'limit':2}
        profile={'soft_prefs':[{'dimension':'flavor','value':'清淡','polarity':'like'}]}
        session=SimpleNamespace(slots={'last_recommendations':[],'temp_constraints':{}})
        result=personalize_result(data,state,profile,session)
        self.assertEqual(len(result['recommendations']),2)
        self.assertEqual({s for row in result['recommendations'] for s in row['matched']},{'番茄','土豆'})

    def test_stored_spice_preference_does_not_empty_chicken_results(self):
        from cookbuddy.task_context import advance_task
        from preference_recommendation import recommend_preferences
        from search_filters import filter_chunks
        state=advance_task('推荐两道简单的鸡肉菜',None)['state']
        state['tastes']=['微辣']
        state['memory_tastes']=['微辣']
        rows,_,_=filter_chunks(self.chunks,max_difficulty=2)
        result=recommend_preferences(state,rows)
        self.assertEqual(len(result['recommendations']),2)
        self.assertNotIn('口味微辣',result['short_answer'])

    def test_stored_flavor_preference_does_not_drop_all_nonmatching_recipes(self):
        from cookbuddy.ltm.retriever import personalize_result
        from types import SimpleNamespace
        rows=[
            {'title':'菜甲','source':'a.md','description':'咸鲜下饭的家常菜。','reason':'通用理由','score':2},
            {'title':'菜乙','source':'b.md','description':'酱香浓郁的下饭菜。','reason':'通用理由','score':1},
        ]
        data={'recommendations':rows,'short_answer':'推荐两道菜','sources':[],'evidence':[]}
        state={'mode':'preference','ingredients':[],'tastes':[],'limit':2}
        profile={'soft_prefs':[{'dimension':'flavor','value':'清淡','polarity':'like'}]}
        session=SimpleNamespace(slots={'last_recommendations':[],'temp_constraints':{}})
        result=personalize_result(data,state,profile,session)
        self.assertEqual([r['title'] for r in result['recommendations']],['菜甲','菜乙'])

    def test_uncovered_stock_is_reported_without_fabricating_matches(self):
        from ingredient_recommendation import unused_inventory_note
        chunks=[r for r in self.chunks if r['title']=='糖拌西红柿']
        rows=recommend(['番茄','马铃薯'],chunks,2)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['matched'],['番茄'])
        self.assertIn('土豆',unused_inventory_note(['番茄','马铃薯'],rows))

    def test_explicit_per_dish_requirement_is_not_silently_relaxed(self):
        from cookbuddy.task_context import advance_task
        state=advance_task('我有番茄和马铃薯，推荐两道菜，每道都要同时有这两种食材。',None)['state']
        self.assertTrue(state['extra_constraints'])

    def test_requested_count_and_source_evidence_are_preserved(self):
        from check_embedding import ROOT
        for count in (1,2,3):
            rows=recommend(['番茄','马铃薯'],self.chunks,count)
            self.assertEqual(len(rows),count)
            for row in rows:
                for ref in row['evidence']:
                    original=(ROOT/ref['source']).read_text().splitlines()
                    self.assertEqual(ref['quote'],'\n'.join(original[ref['start_line']-1:ref['end_line']]))

    def test_optional_stock_does_not_qualify_a_recipe_or_claim_zero_coverage(self):
        rows=recommend(['番茄'],self.chunks,10)
        self.assertNotIn('西葫芦炒鸡蛋',{row['title'] for row in rows})
        for row in rows:
            self.assertTrue(row['matched'])
            self.assertTrue(set(row['matched'])<=set(row['required']))
            self.assertTrue(row['matched'])
            self.assertNotIn('coverage',row)


class InventoryDialogueTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_ingredient_check_preserves_declared_stock(self):
        from dialogue_harness import DialogueHarness
        plan,result,_=await DialogueHarness().turn('罗宋汤还需要哪些食材？我有番茄和马铃薯。')
        self.assertEqual(plan['task']['action'],'ingredients')
        data=result['outputs'][0]['result']
        self.assertEqual(set(data['matched']),{'番茄','土豆'})
        self.assertNotIn('土豆',data['missing'])

    async def test_reported_alias_question_preserves_both_ingredients(self):
        from dialogue_harness import DialogueHarness
        _,result,_=await DialogueHarness().turn('我有番茄和马铃薯，推荐两道菜，说明还缺什么。')
        rows=result['outputs'][-1]['result']['recommendations']
        self.assertTrue({'番茄','土豆'}<=set().union(*(set(r['matched']) for r in rows)))
