import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from eval_judge import has_evaluable_answer,execution_completed
from review_prd_eval import review,execution_summary
from accept_prd import acceptance_matrix


class UnavailableAnswerEvaluation(unittest.TestCase):
    def test_completed_tool_does_not_hide_a_later_agent_planning_failure(self):
        row={'variant':'full_agent','seconds':2.,'result':{'status':'verified','short_answer':'已核对原文'},
            'agent_record':{'status':'partial','stop_reason':'agent_unavailable'}}
        self.assertTrue(has_evaluable_answer(row))
        self.assertFalse(execution_completed(row))
        summary=execution_summary([row])
        self.assertEqual(summary['successful_answers'],0)
        self.assertEqual(summary['failures'],1)
        checks={r['criterion']:r['status'] for r in acceptance_matrix({'answers':[row]})['checks']}
        self.assertEqual(checks['完整系统执行无失败或降级'],'failed')

    def test_empty_failed_and_unavailable_answers_are_not_evaluable(self):
        for row in ({}, {'result':{'status':'verified','short_answer':'  '}},
            {'result':{'status':'agent_unavailable','short_answer':'稍后重试'}},
            {'failure_type':'HTTPError','result':{'status':'verified','short_answer':'候选'}},
            {'result':{'status':'verified','short_answer':None}}):
            self.assertFalse(has_evaluable_answer(row))
        self.assertTrue(has_evaluable_answer({'result':{'status':'degraded','short_answer':'已核对原文候选'}}))

    def test_blank_answer_cannot_inherit_a_positive_judge_score(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);prices=root/'data/evaluation';prices.mkdir(parents=True)
            (prices/'prices-20261014.json').write_text(json.dumps({'model':'other'}))
            fixture=root/'fixture.json';fixture.write_text(json.dumps({'cases':[
                {'id':'good','question':'q','sources':[],'gold':[]},
                {'id':'bad','question':'q','sources':[],'gold':[]}]}))
            path=root/'report.json';path.with_suffix('.md').write_text('# Report\n')
            judge={'scores':{'correct':True,'complete':True},'same_model_bias':False}
            rows=[{'case':'good','variant':'full_agent','seconds':1.,'result':{'status':'verified','short_answer':'原文回答'},'llm_judge':judge},
                {'case':'bad','variant':'full_agent','seconds':.01,'result':{'status':'agent_unavailable','short_answer':''},'llm_judge':judge}]
            report={'fixture_path':str(fixture),'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
                'configuration':{'llm_model':'production'},'answers':rows,
                'answer_aggregates':{'full_agent':{'n':2,'p50_seconds':.5,'p95_seconds':1.}},'acceptance':{}}
            path.write_text(json.dumps(report))
            with patch('review_prd_eval.ROOT',root),patch('review_prd_eval.request_json',side_effect=AssertionError('不应重评')):
                result=review(path,judge=False)
            aggregate=result['answer_aggregates']['full_agent']
            self.assertEqual(aggregate['judge_pass_rate']['mean'],.5)
            self.assertEqual(aggregate['judge_coverage'],{'reviewed':1,'expected':2,'execution_ineligible':1})
            self.assertEqual(result['answers'][1]['llm_judge'],judge)
            self.assertIn('judge_ineligible',result['answers'][1])
            report['answers']=rows[1:];report['answer_aggregates']={'full_agent':{'n':1,'p50_seconds':.01,'p95_seconds':.01}}
            path.write_text(json.dumps(report))
            with patch('review_prd_eval.ROOT',root):
                result=review(path,judge=False)
            self.assertEqual(result['answer_aggregates']['full_agent']['judge_pass_rate']['mean'],0.)


if __name__=='__main__':unittest.main()
