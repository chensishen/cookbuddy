"""故障不能被报告为已经查询后的资料不足；故障注入不冒充在线调用。"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ask_test_recipes import StageFailure
from recipe_agent import execute
from service_runtime import BudgetExceeded,CircuitOpen,request_budget


QUESTION='银河尘埃布丁怎么做？'


def query(_):
    return {'role':'assistant','tool_calls':[{'id':'query','type':'function','function':{
        'name':'query_recipes','arguments':json.dumps({'question':QUESTION},ensure_ascii=False)}}]},{}


class FailureRefusalBoundaryTests(unittest.TestCase):
    def test_http429_is_not_out_of_library_refusal(self):
        error=HTTPError('https://example.invalid',429,'rate limited',{},None)
        with patch('recipe_agent.request_json',side_effect=error):result=execute(QUESTION)
        self.assertEqual(result['status'],'agent_unavailable')
        self.assertEqual(result['outputs'],[])
        self.assertIn('429',result['error'])

    def test_circuit_and_budget_failures_do_not_claim_knowledge_absence(self):
        for error in (CircuitOpen('服务熔断'),BudgetExceeded('预算耗尽')):
            with self.subTest(kind=type(error).__name__):
                def fail(_):raise error
                result=execute(QUESTION,model_call=fail)
                self.assertEqual(result['status'],'agent_unavailable')
                self.assertTrue(result['error'])
                self.assertEqual(result['outputs'],[])

    def test_tool_stage_failure_remains_failure(self):
        with patch('recipe_agent.dispatch',side_effect=StageFailure('检索步骤未完成')):
            decisions=iter([query(None),({'content':'未找到菜谱'}, {})])
            result=execute(QUESTION,model_call=lambda _:next(decisions))
        self.assertEqual(result['status'],'partial_failure')
        self.assertEqual(result['outputs'][0]['result']['status'],'tool_failed')
        self.assertIn('检索步骤未完成',result['outputs'][0]['result']['short_answer'])

    def test_cancelled_query_does_not_run_tools_or_claim_absence(self):
        with request_budget(cancelled=lambda:True),patch('recipe_agent.dispatch') as tool:
            result=execute(QUESTION,model_call=query)
        tool.assert_not_called()
        self.assertEqual(result['status'],'agent_unavailable')
        self.assertEqual(result['stop_reason'],'deadline')
        self.assertEqual(result['outputs'],[])

    def test_source_version_error_cannot_be_successful_refusal(self):
        with patch('recipe_agent.dispatch',side_effect=ValueError('本地原文与知识库版本不同')):
            result=execute(QUESTION,mode='rag')
        self.assertEqual(result['status'],'partial_failure')
        self.assertEqual(result['outputs'][0]['result']['status'],'tool_failed')

    def test_completed_evidence_query_can_return_genuine_insufficient(self):
        with patch('recipe_agent.dispatch',return_value={'status':'insufficient',
                'short_answer':'知识库未收录所问菜谱，不能提供配方。','sources':[],'evidence':[]}) as tool:
            result=execute(QUESTION,model_call=query)
        self.assertEqual(result['status'],'insufficient')
        self.assertIsNone(result['error'])
        self.assertEqual(tool.call_count,1)
        self.assertEqual(len(result['model_calls']),1)

    def test_search_failure_returns_degradation_not_insufficient(self):
        from recipe_tools import query_recipes
        with patch('recipe_tools._query_recipes',side_effect=StageFailure('服务暂不可用')):
            result=query_recipes(QUESTION)
        self.assertEqual(result['status'],'degraded')
        self.assertIn('尚未生成或核验问题结论',result['short_answer'])


if __name__=='__main__':unittest.main()
