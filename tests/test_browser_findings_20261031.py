import unittest
import json
from unittest.mock import patch
from dialogue_harness import DialogueHarness


class BrowserFindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_temporary_today_exclusion(self):
        plan,result,_=await DialogueHarness().turn('今天临时不要香菜，按我的口味推荐两道菜。')
        self.assertIn('香菜',plan['task']['state']['excluded_ingredients'])
        self.assertEqual(plan['task']['state']['extra_constraints'],[])
        self.assertEqual(len(result['outputs'][0]['result']['recommendations']),2)

    async def test_replace_keep_then_condition(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和土豆，推荐两道菜。')
        previous=chat.session.slots['last_recommendations'].copy()
        plan,result,_=await chat.turn('第二道换一道，第一道保留，还是不要油炸。')
        self.assertEqual(plan['task'].get('replacement_index'),2)
        self.assertEqual(plan['task']['state']['extra_constraints'],[])
        self.assertIn('油炸',plan['task']['state']['excluded_methods'])
        current=chat.session.slots['last_recommendations']
        self.assertEqual(current[0]['source_path'],previous[0]['source_path'])
        self.assertNotEqual(current[1]['source_path'],previous[1]['source_path'])

    async def test_inventory_typo_and_colloquial_request_work_without_model(self):
        for question in ('我有西红市和马铃薯，推荐两道菜，说明还缺什么。',
                         '冰箱剩西红市和马铃薯，给我整俩菜，缺啥也说下。'):
            with self.subTest(question=question):
                plan,result,_=await DialogueHarness().turn(question)
                self.assertEqual(plan['task']['action'],'recommend')
                self.assertEqual(plan['task']['state']['extra_constraints'],[])
                rows=result['outputs'][0]['result']['recommendations']
                self.assertEqual(len(rows),2)
                self.assertTrue({'番茄','土豆'}<={s for r in rows for s in r['matched']})

    async def test_replace_with_condition_and_preserve_other_dish(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和马铃薯，推荐两道菜，说明还缺什么。')
        previous=chat.session.slots['last_recommendations'].copy()
        plan,result,_=await chat.turn('第二道换一道不要油炸的，第一道保留。')
        self.assertEqual(plan['task'].get('replacement_index'),2)
        self.assertEqual(plan['task']['state']['extra_constraints'],[])
        self.assertIn('油炸',plan['task']['state']['excluded_methods'])
        current=chat.session.slots['last_recommendations']
        self.assertEqual(current[0]['source_path'],previous[0]['source_path'])
        self.assertNotEqual(current[1]['source_path'],previous[1]['source_path'])

    async def test_natural_meal_update_has_no_grammatical_residue(self):
        chat=DialogueHarness()
        await chat.turn('两个人，30分钟，安排三菜一汤。')
        plan,result,_=await chat.turn('按晚餐安排，仍然两个人、30分钟、三菜一汤。')
        request=plan['task']['meal_request']
        self.assertEqual(request['meal_type'],'dinner')
        self.assertEqual(request['constraints'].get('unresolved',[]),[])
        self.assertEqual((request['people'],request['minutes'],request['dishes'],request['soups']),(2,30,3,1))
        self.assertTrue(result['outputs'][0]['result']['recommendations'])

    async def test_unknown_replace_condition_still_requires_clarification(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和马铃薯，推荐两道菜。')
        plan,result,_=await chat.turn('第二道换一道预算不超过5元的，第一道保留。')
        self.assertTrue(plan['task']['state']['extra_constraints'])
        self.assertFalse(result['outputs'][0]['result'].get('recommendations'))


class BrowserBoundaryTests(unittest.TestCase):
    def test_keep_clause_preserves_unknown_trailing_condition(self):
        from src.agent.turn_intents import alternative_request
        parsed=alternative_request('第二道换一道，第一道保留，预算不超过5元。')
        self.assertEqual(parsed['preserved_index'],1)
        self.assertEqual(parsed['constraint'],'预算不超过5元')
        conflict=alternative_request('第二道换一道，第二道保留，还是不要油炸。')
        self.assertIn('clarification',conflict)

    def test_hy3_understanding_reserves_room_for_reasoning_and_valid_json(self):
        from src.nlu.router import _model
        seen=[]
        def response(request,**kwargs):
            seen.append(json.loads(request.data))
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'intent':'ingredient_recommend','confidence':.98,'slots':{'ingredients':['番茄','土豆'],'limit':2,'mode':'inventory'}})}}]}
        with patch('llm_config.require_llm',return_value={'model':'workbuddy-ai/hy3','base_url':'http://127.0.0.1:3425/v1','api_key':'test-key'}),patch('service_runtime.request_json',side_effect=response):
            result=_model('我有番茄和土豆，推荐两道','我有番茄和土豆，推荐两道',{})
        self.assertEqual(result['slots']['limit'],2)
        # The observed valid response used 2052 tokens, including 1835 reasoning tokens.
        self.assertGreaterEqual(seen[0]['max_tokens'],2052)

    def test_reasoning_only_truncation_is_never_treated_as_understanding(self):
        from src.nlu.router import _model
        response={'choices':[{'finish_reason':'length','message':{'content':'','reasoning_content':'internal'}}]}
        with patch('service_runtime.request_json',return_value=response),self.assertRaisesRegex(ValueError,'nlu_incomplete'):
            _model('推荐两道菜','推荐两道菜',{})

    def test_correction_does_not_turn_negated_or_compound_stock_into_plain_food(self):
        from cookbuddy.recommendation_turn import interpret
        for text in ('我有不是番茄和土豆，推荐两道菜。','我有番茄酱和土豆，推荐两道菜。'):
            turn=interpret(text)
            ingredients={v for edit in turn.edits if edit.slot=='ingredients' for v in edit.values}
            self.assertNotIn('番茄',ingredients)

    def test_replace_keeps_unknown_requirement_for_clarification(self):
        from src.agent.turn_intents import alternative_request
        from cookbuddy.recommendation_turn import interpret
        alt=alternative_request('第二道换一道预算不超过5元的，第一道保留。')
        self.assertTrue(interpret(alt['constraint']).unresolved)

    def test_meal_grammar_does_not_remove_meaningful_constraints(self):
        from cookbuddy.meal_planning import meal_conditions
        _,residue=meal_conditions('按晚餐安排，仍然两个人、30分钟、三菜一汤，预算50元。')
        self.assertIn('预算50元',residue)

    def test_explicit_composition_overrides_inferred_light_meal_template(self):
        from cookbuddy.meal_planning import meal_request
        from cookbuddy.menu_planner import resolve_template
        for kind in ('unspecified','breakfast','snack_late'):
            request={**meal_request('两个人，30分钟，安排三菜一汤。'),'meal_type':kind,'local_hour':1}
            _,template,_=resolve_template(request)
            self.assertEqual([s['role'] for s in template['slots']],['dish','dish','dish','soup'])
            self.assertEqual(template['budget'],30)

    def test_understanding_failure_keeps_request_diagnostics(self):
        from src.capabilities import degradation
        outputs=[{'tool':'general_chat','result':{'status':'verified_source','short_answer':'理解服务暂时不可用，请稍后重试。'}}]
        metadata=degradation({'intent':'unclear','errors':['timeout'],'path':'general_chat'},outputs,'test')
        self.assertTrue(metadata['unsupported_in_fallback'])
        self.assertIn('test',metadata['service_notice'])

    def test_degraded_image_answer_states_evidence_limit(self):
        from src.capabilities import degradation
        plan={'intent':'recipe_images','original':'这张图片里的鸡蛋熟了吗？','slots':{'request_kind':'images'}}
        metadata=degradation(plan,[],'test',tool_degraded=True,limited_reason='dependency_unavailable')
        self.assertIn('不能单独证明内部熟度',metadata['service_notice'])
        self.assertIn('中心温度',metadata['service_notice'])
