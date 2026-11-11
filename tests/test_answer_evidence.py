"""验证引用拼接与无依据结论的拒绝，不调用付费 API。"""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_test_answer import validate_answer, review_answer


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.references = [
            {"reference_id": 1, "content_start_line": 10, "content": "西红柿\n鸡蛋"},
            {"reference_id": 3, "content_start_line": 20, "content": "糖（可选）"},
        ]

    def test_multiple_sources_get_matching_citations(self):
        payload = {"claims": [{"text": "可做西红柿炒鸡蛋，糖可选。", "evidence": [
            {"reference_id": 1, "start_line": 10, "end_line": 11},
            {"reference_id": 3, "start_line": 20, "end_line": 20},
        ]}]}
        text, evidence, cited = validate_answer(json.dumps(payload), self.references)
        self.assertEqual(cited, [1, 3])
        self.assertTrue(text.endswith("[1][3]"))
        self.assertEqual(evidence[1]["quote"], "糖（可选）")

    def test_each_claim_needs_evidence(self):
        with self.assertRaises(ValueError):
            validate_answer(json.dumps({"claims": [{"text": "先炒鸡蛋。", "evidence": []}]}), self.references)

    def test_out_of_section_lines_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_answer(json.dumps({"claims": [{"text": "糖可选。", "evidence": [
                {"reference_id": 3, "start_line": 19, "end_line": 20}]}]}), self.references)

    def test_insufficient_answer_has_no_citations(self):
        text, evidence, cited = validate_answer(json.dumps({"claims": [],
            "missing_information": "资料中没有宫保鸡丁的做法。"}), self.references)
        self.assertIn("当前检索资料不足以回答", text)
        self.assertEqual((evidence, cited), ([], []))

    def test_review_cannot_use_unselected_original_lines(self):
        captured = []

        def fake_model(messages, reviewing=False):
            captured.append(json.loads(messages[1]["content"]))
            return {"choices": [{"message": {"content": json.dumps({
                "verdict": "incomplete_evidence", "reason": "遗漏后续步骤"})}}]}

        context = json.dumps([{"reference_id": 1, "title": "菜谱", "section": "操作",
                               "content_lines": [{"line": 11, "text": "未选择的步骤"}]}])
        review_answer(fake_model, "先后顺序？", "先炒鸡蛋再炒西红柿 [1]",
                      [{"reference_id": 1, "quote": "先炒鸡蛋"}], context)
        self.assertNotIn("original_references", captured[0])
        self.assertNotIn("未选择的步骤", json.dumps(captured[0], ensure_ascii=False))

    def test_ingredient_excerpt_keeps_whole_list(self):
        references = [{"reference_id": 1, "content_start_line": 10,
                       "content": "* 鸡蛋\n* 干酵母\n* 牛奶"}]
        _, evidence, _ = validate_answer(json.dumps({"claims": [{"text": "缺少鸡蛋、酵母和奶。",
            "evidence": [{"reference_id": 1, "start_line": 10, "end_line": 10}]}]}), references)
        self.assertEqual(evidence[0]["end_line"], 12)
        self.assertIn("干酵母", evidence[0]["quote"])

    def test_order_excerpt_keeps_later_steps(self):
        references = [{"reference_id": 1, "content_start_line": 10,
                       "content": "1. 先炒鸡蛋\n\n2. 盛出鸡蛋\n\n3. 炒西红柿"}]
        _, evidence, _ = validate_answer(json.dumps({"claims": [{"text": "先炒鸡蛋再炒西红柿。",
            "evidence": [{"reference_id": 1, "start_line": 10, "end_line": 10}]}]}), references)
        self.assertEqual(evidence[0]["end_line"], 14)
        self.assertIn("炒西红柿", evidence[0]["quote"])


if __name__ == "__main__":
    unittest.main()
