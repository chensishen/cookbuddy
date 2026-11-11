import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import generate_test_answer as module

class AbsenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = 'data/HowToCook/dishes/egg.md'
        self.path = self.root / self.source
        self.path.parent.mkdir(parents=True)
        self.path.write_text('# 蒸水蛋\n蒸4分钟\n水温20~30\n')
        self.rows = [{'reference_id': 1, 'title': '蒸水蛋', 'section': '操作',
                     'source': self.source, 'content_lines': [{'line': 2, 'text': '蒸4分钟'}]}]
        self.calls = []
    def tearDown(self): self.tmp.cleanup()
    def fake(self, supported):
        def call(messages, reviewing=False):
            self.calls.append(json.loads(messages[1]['content']))
            return {'choices': [{'message': {'content': json.dumps({'supported_absence': supported, 'reason': '全文核对'})}}]}
        return call
    def run_check(self, answer='原文未给出成品中心温度。', source=True, supported=True):
        with patch.object(module, 'ROOT', self.root):
            return module.review_source_absence(self.fake(supported), '蒸水蛋的成品中心温度？', answer, self.rows, self.source if source else None)
    def test_full_file_including_unretrieved_lines_is_checked(self):
        result = self.run_check()
        self.assertTrue(result['supported_absence'])
        self.assertFalse(result['answerable'])
        self.assertEqual(result['absence_source']['line_count'], 3)
        self.assertEqual(self.calls[0]['complete_source']['content_lines'][-1]['text'], '水温20~30')
    def test_unspecified_brand_uses_full_source_check(self):
        self.assertTrue(self.run_check(answer='原文没有指定食用油品牌。')['supported_absence'])
    def test_no_named_source_cannot_prove_absence(self):
        self.assertIsNone(self.run_check(source=False))
        self.assertEqual(self.calls, [])
    def test_positive_answer_does_not_enter_absence_review(self):
        self.assertIsNone(self.run_check(answer='中心温度80度。'))
        self.assertEqual(self.calls, [])
    def test_negative_cooking_state_is_not_a_missing_source_attribute(self):
        self.assertIsNone(self.run_check(answer='若30分钟后没有明显变色，说明温度过低，可提高5~10℃。'))
        self.assertEqual(self.calls, [])
    def test_mixed_or_false_negative_rejected_by_independent_review(self):
        result = self.run_check(answer='原文未给出品牌，蒸10分钟。', supported=False)
        self.assertFalse(result['passed'])
        self.assertEqual(result['verdict'], 'incomplete_evidence')
    def test_stale_context_rejected(self):
        self.path.write_text('# 蒸水蛋\n蒸8分钟\n')
        with self.assertRaises(ValueError): self.run_check()
    def test_truncated_large_source_not_used(self):
        self.path.write_text('# 蒸水蛋\n蒸4分钟\n' + '水'*14001)
        self.assertIsNone(self.run_check())
        self.assertEqual(self.calls, [])
    def test_cited_absence_routes_to_full_source_verification(self):
        with patch.object(module, 'ROOT', self.root):
            result = module.review_answer(self.fake(True), '蒸水蛋的成品中心温度？',
                '原文未给出成品中心温度。', [{'reference_id': 1, 'quote': '蒸4分钟'}], json.dumps(self.rows), self.source)
        self.assertTrue(result['passed'])
        self.assertEqual(result['verdict'], 'insufficient')
        self.assertEqual(len(self.calls), 1)

    def test_mixed_section_absence_does_not_bypass_positive_evidence_review(self):
        calls = []
        responses = [
            {'supported_absence': True, 'absence_only': False, 'reason': '计算章未说明温度，操作章有温度'},
            {'verdict': 'incomplete_evidence', 'reason': '蒸10分钟不受选中摘录支持'},
        ]
        def fake(messages, reviewing=False):
            calls.append(json.loads(messages[1]['content']))
            return {'choices': [{'message': {'content': json.dumps(responses.pop(0))}}]}
        with patch.object(module, 'ROOT', self.root):
            result = module.review_answer(fake, '章节差异？',
                '计算章未说明温度；操作蒸10分钟。',
                [{'reference_id': 1, 'quote': '蒸4分钟'}], json.dumps(self.rows), self.source)
        self.assertFalse(result['passed'])
        self.assertEqual(len(calls), 2)
        self.assertIn('verified_absence', calls[1])
        self.assertNotIn('complete_source', calls[1])
        self.assertEqual(calls[1]['selected_evidence'][0]['quote'], '蒸4分钟')

    def test_wrong_model_absence_only_label_cannot_bypass_positive_review(self):
        calls=[]
        responses=[{'supported_absence': True, 'absence_only': True, 'reason': '模型误标纯缺失'},
                   {'verdict': 'incomplete_evidence', 'reason': '虚构了用户已有食材'}]
        def fake(messages, reviewing=False):
            calls.append(messages)
            return {'choices':[{'message':{'content':json.dumps(responses.pop(0))}}]}
        with patch.object(module,'ROOT',self.root):
            result=module.review_answer(fake,'是否可选？',
                '原文未说明品牌。已有牛奶和盐，可以做。',
                [{'reference_id':1,'quote':'蒸4分钟'}],json.dumps(self.rows),self.source)
        self.assertFalse(result['passed']);self.assertEqual(len(calls),2)
        self.assertFalse(result['absence_verification']['supported_absence'])

if __name__ == '__main__': unittest.main()
