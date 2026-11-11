import unittest
from unittest.mock import Mock,patch
from dialogue_harness import DialogueHarness
from recipe_tools import load_chunks


class DialogueRepairsTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_stock_removal_preserves_other_stock_and_exclusions(self):
        chat=DialogueHarness()
        await chat.turn('我有鸡肉和土豆，这次推荐三道菜，不要油炸，不要花生，说明还缺什么。')
        plan,result,_=await chat.turn('现在没有鸡肉了，按剩下的食材再推荐两道，不要油炸和花生。')
        state=plan['task']['state']
        self.assertEqual(state['ingredients'],['土豆'])
        self.assertEqual(state['extra_constraints'],[])
        self.assertIn('花生',state['excluded_ingredients'])
        self.assertNotIn('鸡肉',state['excluded_ingredients'])
        self.assertIn('油炸',state['excluded_methods'])
        self.assertEqual(len(result['outputs'][0]['result']['recommendations']),2)

    async def test_recovered_stock_clause_does_not_clear_a_real_budget(self):
        chat=DialogueHarness()
        await chat.turn('我有鸡肉和土豆，推荐两道菜。')
        chat.state['extra_constraints']=['现在没有鸡肉了','预算不超过5元']
        plan,_,_=await chat.turn('我有土豆，推荐两道菜，不要油炸，不要花生。')
        self.assertEqual(plan['task']['state']['extra_constraints'],['预算不超过5元'])

    async def test_validated_breakfast_outranks_incidental_session_recording(self):
        management={'decision':'SESSION_ONLY','needs_confirmation':False,'message':'已记录临时要求'}
        plan,result,_=await DialogueHarness().turn('安排两个人的早餐，20分钟内做好。',management=management)
        self.assertIsNone(plan['management'])
        self.assertEqual(result['outputs'][0]['tool'],'plan_meal')
        self.assertEqual(result['outputs'][0]['result']['meal_plan']['people'],2)

    async def test_again_change_menu_is_a_menu_continuation(self):
        chat=DialogueHarness()
        await chat.turn('两个人，60分钟，安排午餐。')
        previous={r['source_path'] for r in chat.session.slots['last_recommendations']}
        plan,result,_=await chat.turn('再换一套，不要重复刚才的菜。')
        self.assertEqual(plan['task']['action'],'meal_plan')
        self.assertTrue(result['outputs'][0]['result']['recommendations'])
        self.assertFalse(previous & {r['source_path'] for r in chat.session.slots['last_recommendations']})

    async def test_old_unrecognized_change_command_does_not_remain_a_menu_constraint(self):
        chat=DialogueHarness()
        await chat.turn('两个人，60分钟，安排午餐。')
        chat.session.slots['meal_request']['constraints']['unresolved']=['再换一套']
        _,result,_=await chat.turn('再换一套，不要重复刚才的菜。')
        self.assertTrue(result['outputs'][0]['result']['recommendations'])


class SourceRepairsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.rows=load_chunks()

    def test_quantity_descriptor_does_not_make_owned_chicken_missing(self):
        from ingredient_recommendation import recommend
        rows=[r for r in self.rows if r['title']=='新疆大盘鸡']
        row=recommend(['鸡肉','土豆'],rows,1)[0]
        self.assertEqual(set(row['matched']),{'鸡肉','土豆'})
        self.assertFalse(any('鸡肉' in v for v in row['missing']))
        self.assertIn('盐',row['required'])
        self.assertTrue(any('两个火枪腿' in v for v in row['unresolved']))
        self.assertNotIn('各',row['required'])
        self.assertNotIn('菜椒甜椒各',row['required'])
        self.assertIn('菜椒甜椒各一个，各 50g',row['unresolved'])

    def test_main_foods_do_not_include_bread_machine_or_microwave(self):
        from cookbuddy.recommendation_schema import new_state
        from preference_recommendation import recommend_preferences
        state=new_state();state['limit']=2
        rows=[r for r in self.rows if r['title'] in ('吐司果酱','太阳蛋')]
        data=recommend_preferences(state,rows,categories={'breakfast','staple'})
        self.assertEqual(len(data['recommendations']),2)
        for row in data['recommendations']:
            self.assertFalse(any('面包机' in s or '微波炉' in s for s in row['main_ingredients']))

    def test_verified_alternative_stock_is_not_displayed_as_empty_inventory(self):
        from cookbuddy.recommendation_schema import new_state
        from preference_recommendation import recommend_preferences
        state=new_state();state.update(mode='inventory',ingredients=['土豆'],limit=1)
        data=recommend_preferences(state,[r for r in self.rows if r['title']=='粉蒸肉'])
        self.assertIn('土豆',data['recommendations'][0]['matched'])
        self.assertTrue(data['recommendations'][0]['unresolved'])

    def test_material_heading_is_not_a_main_ingredient(self):
        from cookbuddy.recommendation_schema import new_state
        from preference_recommendation import recommend_preferences
        state=new_state();state['limit']=1
        data=recommend_preferences(state,[r for r in self.rows if r['title']=='奇异果菠菜特调'],categories={'drink'})
        self.assertTrue(data['recommendations'])
        self.assertNotIn('原料:',data['recommendations'][0]['main_ingredients'])

    def test_starter_duration_relation_uses_complete_ordered_evidence(self):
        from recipe_fact_answer import source_fact
        data=source_fact('基础牛奶面包的发酵时间包含最开始静置的5分钟吗？请只依据原文回答。',self.rows)
        self.assertIsNotNone(data)
        self.assertIn('不包含',data['short_answer'])
        self.assertIn('45',data['short_answer'])
        self.assertIn('60',data['short_answer'])
        self.assertEqual(len(data['evidence']),2)

    def test_duration_relation_followup_skips_remote_understanding(self):
        from src.nlu.router import understand,validate
        source=next(r['source'] for r in self.rows if r['title']=='基础牛奶面包')
        context={'slots':{'selected_dish':{'dish_name':'基础牛奶面包','source_path':source},'selected_section':'酵头的制作'}}
        model=Mock(return_value=validate({'intent':'recipe_lookup','confidence':.99,'slots':{'dish':'基础牛奶面包'}}))
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            data=understand('这里的发酵时间包含最开始静置的5分钟吗？请只依据原文回答。',context,model_call=model)
        model.assert_not_called()
        self.assertEqual(data['slots']['dish'],'基础牛奶面包')

    def test_duration_relation_does_not_discard_extra_constraints_or_missing_evidence(self):
        from recipe_fact_answer import source_fact
        question='基础牛奶面包的发酵时间包含最开始静置的5分钟吗？'
        self.assertIsNone(source_fact(question+'而且需要零糖版本。',self.rows))
        rows=[r for r in self.rows if '酵头' not in r['section']]
        self.assertIsNone(source_fact(question,rows))

    def test_mixed_exclusions_are_atomic_when_a_target_is_unknown(self):
        from cookbuddy.recommendation_turn import interpret
        turn=interpret('不要油炸和未知香料。')
        self.assertTrue(turn.unresolved)
        self.assertFalse(turn.edits)

    def test_egg_side_is_not_described_as_a_vegetable(self):
        from cookbuddy.meal_planning import meal_request,plan_meal
        data=plan_meal(meal_request('安排午餐'),[r for r in self.rows if r['title']=='炒滑蛋'])
        self.assertTrue(data['recommendations'])
        self.assertNotIn('蔬菜',data['recommendations'][0]['reason'])

    def test_poached_egg_is_not_a_breakfast_staple(self):
        import re
        import yaml
        from pathlib import Path
        pattern=yaml.safe_load((Path(__file__).resolve().parents[1]/'config/meal_templates.yaml').read_text())['breakfast']['staple_pattern']
        for title in ('微波炉荷包蛋','荷包蛋'):
            self.assertIsNone(re.search(pattern,title))
        for title in ('面包','包子','蛋包饭','鸡蛋饼','吐司果酱'):
            self.assertIsNotNone(re.search(pattern,title))
