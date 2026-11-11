import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from evaluate_agent_filters import score,evaluate


def message(args):
    return {'tool_calls':[{'function':{'name':'query_recipes','arguments':json.dumps(args)}}]}


class PlanningEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.case={'id':'test','question':'完整问题','expected_filters':{'category':'soup','max_difficulty':2}}

    def test_exact_arguments_pass_but_lost_question_fails(self):
        args={'question':'完整问题','category':'soup','max_difficulty':2}
        self.assertTrue(score(self.case,message(args))['passed'])
        result=score(self.case,message({**args,'question':'改写'}))
        self.assertTrue(result['filters_exact']);self.assertFalse(result['passed'])

    def test_wrong_extra_missing_and_invalid_filters_fail(self):
        for filters in ({'category':'meat_dish','max_difficulty':2},{'category':'soup'},
                        {'category':'soup','max_difficulty':True},{'category':None},
                        {'category':'soup','max_difficulty':2,'extra':1}):
            self.assertFalse(score(self.case,message({'question':'完整问题',**filters}))['passed'])
        self.assertFalse(score(self.case,{'tool_calls':[]})['passed'])
        self.assertFalse(score(self.case,{'tool_calls':[None]})['passed'])

    def test_unrequested_numeric_filter_fails(self):
        case={**self.case,'expected_filters':{}}
        self.assertFalse(score(case,message({'question':'完整问题','max_difficulty':2}))['passed'])

    def test_observed_cancelled_prefix_is_restored_by_runtime_guard(self):
        from recipe_agent import normalize_tool_request
        original='不是难度不超过2星，难度不要限制；蒸水蛋需要哪些原料？'
        name,args,changes=normalize_tool_request('query_recipes',{'question':'蒸水蛋需要哪些原料？'},original)
        self.assertEqual(name,'query_recipes')
        self.assertEqual(args,{'question':original})
        self.assertTrue(changes)

    def test_ingredient_array_order_and_aliases_are_semantically_equivalent(self):
        case={**self.case,'expected_filters':{'ingredients_all':['番茄','鸡蛋']}}
        self.assertTrue(score(case,message({'question':'完整问题','ingredients_all':['鸡蛋','西红柿']}))['passed'])

    def test_provider_failure_keeps_full_denominator_and_stops_calls(self):
        fixture={'id':'test','scope':'test','cases':[self.case,{**self.case,'id':'second'}]}
        with TemporaryDirectory() as directory,patch.dict(os.environ,{'RECIPE_USAGE_LEDGER':'previous'}):
            with patch('evaluate_agent_filters.call_model',side_effect=ValueError('private')) as model:
                report=evaluate(Path(directory)/'run',fixture=fixture,interval=0)
            self.assertEqual(model.call_count,1)
            self.assertEqual(report['metrics']['passed'],{'passed':0,'total':2})
            self.assertEqual(report['rows'][1]['status'],'not_run_after_provider_failure')
            self.assertEqual(os.environ['RECIPE_USAGE_LEDGER'],'previous')
            self.assertNotIn('private',json.dumps(report))
            with self.assertRaises(FileExistsError):evaluate(Path(directory)/'run',fixture=fixture,interval=0)

    def test_raw_wrong_answer_is_kept_and_code_is_frozen(self):
        fixture={'id':'test','scope':'test','cases':[self.case]}
        wrong=message({'question':'完整问题','category':'meat_dish'})
        with TemporaryDirectory() as directory:
            report=evaluate(Path(directory)/'run',fixture=fixture,model_call=lambda _:(wrong,{}),interval=0)
        self.assertEqual(report['rows'][0]['message'],wrong)
        self.assertFalse(report['rows'][0]['passed']);self.assertTrue(report['code_unchanged'])


if __name__=='__main__':unittest.main()
