"""验收不能把空引用、错行号或改写后的摘录算为精确原文。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_embedding import ROOT
from evaluate_quality_batch import evidence_checks
import json
from unittest.mock import patch
from ingredient_answer_checks import check_unsupported_restrictions
import recipe_tools


class QualityEvidenceTests(unittest.TestCase):
    def record(self, **changes):
        source = 'data/HowToCook/dishes/condiment/糖醋汁.md'
        lines = (ROOT / source).read_text().splitlines()
        item = dict(source=source, start_line=1, end_line=1, quote=lines[0])
        item.update(changes)
        return {'outputs': [{'result': {'evidence': [item]}}]}

    def test_quote_tampering_and_wrong_line_are_rejected(self):
        self.assertTrue(evidence_checks(self.record())['all_exact'])
        self.assertFalse(evidence_checks(self.record(quote='清水500ml'))['all_exact'])
        self.assertFalse(evidence_checks(self.record(start_line=2, end_line=2))['all_exact'])

    def test_empty_evidence_and_invalid_bounds_are_not_passes(self):
        self.assertFalse(evidence_checks({'outputs': []})['all_exact'])
        for values in ({'start_line': 0}, {'end_line': 999999},
                       {'start_line': '1'}, {'start_line': True}, {'source': '../outside.md'}):
            self.assertFalse(evidence_checks(self.record(**values))['all_exact'])

    def test_suggested_reduction_cannot_support_omission_prohibition(self):
        answer = json.dumps({'claims': [{'text': '草莓酱不能完全不放糖。',
            'evidence': [{'reference_id': 1}]}]})
        evidence = [{'reference_id': 1, 'quote': '白糖400克（可以考虑降低到200g）'}]
        with self.assertRaises(ValueError):
            check_unsupported_restrictions(answer, evidence)
        with self.assertRaises(ValueError):
            check_unsupported_restrictions(json.dumps({'claims': [{'text':
                '原文未说明保存时间。草莓酱不能完全不放糖。',
                'evidence': [{'reference_id': 1}]}]}), evidence)
        for text in ('原文给200g建议，无法判断是否不能完全不放糖。',
                     '原文给400克，低糖可考虑200g，仅凭原文无法判断零糖方案是否可行。'):
            check_unsupported_restrictions(json.dumps({'claims': [{'text': text,
                'evidence': [{'reference_id': 1}]}]}), evidence)
        check_unsupported_restrictions(answer,
            [{'reference_id': 1, 'quote': '不能完全不放糖。'}])

    def test_failed_generation_returns_exact_named_source_not_rejected_claim(self):
        question = '草莓酱能完全不放糖吗？'
        search = ROOT / 'data/processed/search-fixture.json'
        rerank = ROOT / 'data/processed/rerank-fixture.json'
        answer = ROOT / 'data/processed/answer-fixture.json'
        stages = [(search, {'question': question}),
                  (rerank, {'question': question, 'source_search': str(search.relative_to(ROOT))}),
                  (answer, {'question': question, 'source_rerank': str(rerank.relative_to(ROOT)),
                            'status': 'validation_failed', 'short_answer': '不能完全不放糖'})]
        with patch.object(recipe_tools, 'run_stage', side_effect=stages):
            result = recipe_tools._query_recipes(question)
        self.assertEqual(result['status'], 'degraded')
        self.assertNotIn('不能完全不放糖', result['short_answer'])
        self.assertTrue(evidence_checks({'outputs': [{'result': result}]})['all_exact'])
        self.assertTrue(all(item['source'].endswith('/草莓酱/草莓酱.md') for item in result['evidence']))


if __name__ == '__main__':
    unittest.main()
