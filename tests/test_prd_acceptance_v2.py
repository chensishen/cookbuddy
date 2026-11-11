from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ingredient_recommendation import recommendation_answer, recommend
from rerank_test_recipes import bounded_rerank
from accept_prd import acceptance_matrix


class FilteredRecommendations(unittest.TestCase):
    def test_equal_coverage_keeps_recipe_matching_more_inventory(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);chunks=[]
            for name,body in [('small','- 鸡蛋\n- 牛奶'),('both','- 鸡蛋\n- 番茄\n- 盐\n- 油')]:
                (root/(name+'.md')).write_text(body)
                chunks.append({'title':name,'source':name+'.md','section':'必备原料和工具',
                    'content':body,'source_start_line':1,'source_end_line':len(body.splitlines())})
            with patch('ingredient_recommendation.ROOT',root):
                self.assertEqual(recommend(['鸡蛋','番茄'],chunks,limit=1)[0]['title'],'both')

    def test_category_difficulty_unknown_and_all_citations(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);chunks=[]
            for name,category,stars in [('easy','vegetable_dish','★'),('hard','vegetable_dish','★★★'),
                                        ('unknown','vegetable_dish',''),('meat','meat_dish','★')]:
                source=f'data/HowToCook/dishes/{category}/{name}.md'
                path=root/source;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(f'# {name}\n预估烹饪难度：{stars}\n## 必备原料和工具\n- 鸡蛋\n- 盐\n')
                chunks.append({'title':name,'source':source,'section':'必备原料和工具',
                    'content':'- 鸡蛋\n- 盐','source_start_line':4,'source_end_line':5})
            with patch('ingredient_recommendation.ROOT',root):
                result=recommendation_answer('我有鸡蛋，推荐3道素菜，难度不超过2星',chunks)
                self.assertEqual([r['title'] for r in result['recommendations']],['easy'])
                self.assertEqual(result['filters'],{'category':'vegetable_dish','max_difficulty':2})
                self.assertIn('[1][2]',result['short_answer'])
                from acceptance_closeout import exact_evidence
                self.assertTrue(exact_evidence(result,root)['all_exact'])
                self.assertEqual(len(recommend(['鸡蛋'],chunks,category='vegetable_dish')),3)
                with self.assertRaises(ValueError):recommend(['鸡蛋'],chunks,max_difficulty=True)
                with self.assertRaises(ValueError):recommend(['鸡蛋'],chunks,category='invented')

    def test_unsupported_constraints_are_preserved_for_full_workflow(self):
        for question in ['我有鸡蛋，推荐不辣的素菜','我有鸡蛋，推荐热量低的素菜',
                         '我有鸡蛋，推荐素菜，难度不超过2.5星','我有鸡蛋，推荐不要汤羹',
                         '我有鸡蛋，推荐素菜和荤菜','我有鸡蛋，推荐素食菜谱']:
            with self.subTest(question=question):self.assertIsNone(recommendation_answer(question,[]))


class RerankFailurePolicy(unittest.TestCase):
    def test_transient_http_fallback_preserves_order(self):
        for code in (408,429,500,502,503,504):
            with patch('rerank_test_recipes.request_json',side_effect=HTTPError('https://example.invalid',code,'error',{},None)):
                result,reason=bounded_rerank(None,[{'chunk_id':'a'},{'chunk_id':'b'}])
                self.assertEqual(reason,'HTTP'+str(code))
                self.assertEqual([r['index'] for r in result['results']],[0,1])

    def test_auth_errors_are_not_silently_hidden(self):
        with patch('rerank_test_recipes.request_json',side_effect=HTTPError('https://example.invalid',401,'error',{},None)):
            with self.assertRaises(HTTPError):bounded_rerank(None,[{}])


class AcceptanceGate(unittest.TestCase):
    def test_empty_and_offline_reports_cannot_pass(self):
        for report in ({},{'corpus':{'passed':True}}):
            result=acceptance_matrix(report)
            self.assertEqual(result['overall'],'partial')
            self.assertEqual(result['counts']['passed'],0)

    def test_measured_failure_and_missing_evidence_are_distinct(self):
        report={'corpus':{'sources':2,'passed_sources':2,'passed':True},
            'aggregates':{'hybrid_rerank':{'metrics':{'hit':{'mean':1.,'n':8}}}},
            'rerank_significance':{'significant':False,'mean_delta':0},
            'rerank_comparison_coverage':{'expected':8,'paired':7},
            'answers':[{'variant':'full_agent','result':{'status':'agent_unavailable','short_answer':'稍后'},'refusal_correct':False}],
            'improvements':[{'from':'naive_rag','to':'full_agent','p95_delta_seconds':10}],
            'code_changed_during_run':False}
        checks={r['criterion']:r['status'] for r in acceptance_matrix(report)['checks']}
        self.assertEqual(checks['完整系统Hit@5≥90%'],'passed')
        self.assertEqual(checks['重排MRR显著提升且覆盖完整'],'failed')
        self.assertEqual(checks['库外拒答正确率≥90%'],'failed')
        self.assertEqual(checks['样本P95延迟增量≤1.5秒'],'failed')
        self.assertEqual(checks['人工盲评与judge一致性校准'],'pending')

    def test_suite_pins_report_and_writes_gate_even_if_judge_fails(self):
        import evaluate_prd_suite as suite
        report={'report_path':'run/report.json','failures':[],'answers':[],'corpus':{'passed':True}}
        with patch.object(suite,'run',return_value=report),patch.object(suite,'review',side_effect=ValueError),\
             patch.object(suite.subprocess,'run') as process,patch.object(suite,'write_acceptance',return_value={'overall':'partial'}) as gate:
            process.return_value.returncode=0
            self.assertFalse(suite.run_suite('/python'))
            self.assertEqual(process.call_args.args[0][-1],str(suite.ROOT/'run/report.json'))
            gate.assert_called_once_with(suite.ROOT/'run/report.json')
