import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
import os
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qa_execution import complete_named_context, independent_reviews, model_deadline
from service_runtime import request_budget, remaining, BudgetExceeded
from generate_test_answer import review_answer
from ingredient_answer_checks import check_unstated_inventory
from ask_test_recipes import run_stage


class QAExecutionTests(unittest.TestCase):
    def test_only_complete_single_source_can_skip_front_judgement(self):
        chunks = [{'chunk_id': 'a', 'source': 'egg.md'},
                  {'chunk_id': 'b', 'source': 'egg.md'},
                  {'chunk_id': 'c', 'source': 'other.md'}]
        self.assertTrue(complete_named_context('egg.md', chunks[:2], chunks))
        self.assertFalse(complete_named_context('egg.md', chunks[:1], chunks))
        self.assertFalse(complete_named_context('egg.md', chunks, chunks))
        self.assertFalse(complete_named_context(None, chunks, chunks))

    def test_both_reviewers_enter_together_and_inherit_budget(self):
        barrier = threading.Barrier(2)
        observed = []
        def support():
            observed.append(remaining()); barrier.wait(timeout=2)
            return {'verdict': 'supported'}
        def completeness():
            observed.append(remaining()); barrier.wait(timeout=2)
            return {'complete': True}
        with request_budget(3):
            result = independent_reviews(support, completeness, parallel=True)
        self.assertEqual(result, ({'verdict': 'supported'}, {'complete': True}))
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(0 < seconds <= 3 for seconds in observed))

    def test_cancelled_budget_is_not_lost_in_review_threads(self):
        with request_budget(3, cancelled=lambda: True):
            with self.assertRaises(BudgetExceeded):
                independent_reviews(lambda: remaining(), lambda: remaining(), parallel=True)

    def test_serial_and_parallel_require_both_support_and_completeness(self):
        context = json.dumps([{'reference_id': 1, 'title': '菜谱', 'section': '操作',
                              'content_lines': [{'line': 1, 'text': '蒸4分钟'}]}])
        for verdict, complete in (('supported', True), ('supported', False),
                                  ('incomplete_evidence', True), ('insufficient', False)):
            def fake(messages, reviewing=False):
                data = ({'complete': complete, 'reason': '完整性检查'}
                        if '你只检查回答是否完整' in messages[0]['content'] else
                        {'verdict': verdict, 'reason': '证据检查'})
                return {'choices': [{'message': {'content': json.dumps(data)}}]}
            results = [review_answer(fake, '蒸多久？', '蒸4分钟',
                                     [{'reference_id': 1, 'quote': '蒸4分钟'}], context,
                                     parallel=parallel) for parallel in (False, True)]
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[1]['passed'], verdict == 'supported' and complete)

    def test_peer_exception_cannot_publish_supported_answer(self):
        def failure():
            raise ValueError('完整性检查格式错误')
        with self.assertRaises(ValueError):
            independent_reviews(lambda: {'verdict': 'supported'}, failure, parallel=True)

    def test_balanced_generation_has_stage_cap_with_large_parent_budget(self):
        with request_budget(240) as total:
            self.assertLessEqual(model_deadline()-time.time(),40)
        for script,limit in (('generate_test_answer.py',40),('rerank_test_recipes.py',12)):
            with request_budget(240),patch.dict(os.environ,{'RECIPE_QA_PROFILE':'balanced'}):
                with patch('ask_test_recipes.subprocess.run',side_effect=subprocess.TimeoutExpired('stage',limit)) as run:
                    with self.assertRaises(subprocess.TimeoutExpired):run_stage(script,'input.json','unused')
            self.assertLessEqual(run.call_args.kwargs['timeout'],limit)

    def test_rerank_timeout_retains_hybrid_order_but_cancellation_stops(self):
        from rerank_test_recipes import bounded_rerank
        candidates=[{'chunk_id':'first'},{'chunk_id':'second'}]
        with patch('rerank_test_recipes.request_json',side_effect=TimeoutError) as call:
            result,reason=bounded_rerank(None,candidates)
            self.assertEqual(reason,'TimeoutError')
            self.assertEqual([r['index'] for r in result['results']],[0,1])
            self.assertEqual(call.call_args.kwargs['attempts'],1)
            with request_budget(1,cancelled=lambda:True):
                with self.assertRaises(BudgetExceeded):bounded_rerank(None,candidates)
        with patch('rerank_test_recipes.request_json',side_effect=ValueError('invalid result')):
            with self.assertRaises(ValueError):bounded_rerank(None,candidates)

    def test_reserve_leaves_time_for_local_fallback(self):
        with request_budget(30) as total:
            deadline = model_deadline()
            self.assertAlmostEqual(total-deadline, 10, delta=.1)
            with request_budget(deadline=deadline):
                self.assertLessEqual(remaining(), 20)
            self.assertGreater(remaining(), 29)
        with request_budget(3) as total:
            self.assertAlmostEqual(model_deadline(), total, delta=.1)

    def test_real_failed_record_inventory_claim_is_rejected_before_model_review(self):
        # 回放实际对照失败的文本，防止只靠另一个模型对同一错误反复给出乐观意见。
        text='番茄丁和培根丁不是必备原料，而是可选添加物。已有鸡蛋、全脂牛奶/奶油、黄油和盐；可考虑加入番茄丁和培根丁，但按原配方还缺/未说明这些原料。'
        answer=json.dumps({'claims':[{'text':text,'evidence':[]} ]})
        with self.assertRaises(ValueError):
            check_unstated_inventory(answer,'美式炒蛋的番茄丁和培根丁是必备的吗？我不想加这两样。')
        check_unstated_inventory(json.dumps({'claims':[{'text':'番茄丁和培根丁不是必备，是可选配料。'}]}),
                                '我不想加番茄丁和培根丁。')
        check_unstated_inventory(answer,'我有鸡蛋、牛奶、黄油和盐，缺什么？')

    def test_generation_subprocess_hard_timeout_reserves_parent_time(self):
        with request_budget(30), patch.dict(os.environ, {'RECIPE_QA_PROFILE':'balanced'}):
            with patch('ask_test_recipes.subprocess.run', side_effect=subprocess.TimeoutExpired('generate',20)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_stage('generate_test_answer.py','input.json','answer-results')
        options=run.call_args.kwargs
        self.assertGreater(options['timeout'],19);self.assertLessEqual(options['timeout'],20)
        self.assertEqual(options['env']['RECIPE_QA_STAGE_RESERVED'],'10')

    def test_online_answer_reserves_two_seconds_inside_existing_budget(self):
        with request_budget(20),patch.dict(os.environ,{'RECIPE_QA_PROFILE':'balanced'}):
            with patch('ask_test_recipes.subprocess.run',side_effect=subprocess.TimeoutExpired('generate',18)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_stage('generate_test_answer.py','input.json','answer-results',fallback_reserve=2)
        self.assertGreater(run.call_args.kwargs['timeout'],17)
        self.assertLessEqual(run.call_args.kwargs['timeout'],18)
        self.assertEqual(run.call_args.kwargs['env']['RECIPE_QA_STAGE_RESERVED'],'2')

    def test_serial_subprocess_does_not_change_existing_stage_budget(self):
        with request_budget(30), patch.dict(os.environ, {'RECIPE_QA_PROFILE':'serial'}):
            with patch('ask_test_recipes.subprocess.run', side_effect=subprocess.TimeoutExpired('generate',30)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_stage('generate_test_answer.py','input.json','answer-results')
        self.assertGreater(run.call_args.kwargs['timeout'],29)
        self.assertEqual(run.call_args.kwargs['env']['RECIPE_QA_STAGE_RESERVED'],'0')


if __name__ == '__main__':
    unittest.main()
