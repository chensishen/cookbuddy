import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ingredient_condition_answer as conditions
from check_embedding import ROOT
from generate_test_answer import validate_answer


class ConditionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chunks = [json.loads(line) for line in
                      (ROOT / 'data/processed/all_recipes/chunks.jsonl').read_text().splitlines()]
        cls.references = []
        for number, chunk in enumerate(cls.chunks, 1):
            lines = (ROOT / chunk['source']).read_text().splitlines()
            content = chunk['content'].splitlines()
            start = next(n for n in range(chunk['source_start_line']-1, chunk['source_end_line'])
                         if lines[n:n+len(content)] == content)
            cls.references.append({**chunk, 'reference_id': number, 'content_start_line': start+1})

    def plan(self, question):
        return conditions.select_condition_plan(question, self.chunks)

    def text(self, question):
        plan = self.plan(question)
        self.assertIsNotNone(plan)
        answer = conditions.prepare_condition_answer(plan, self.references)
        validate_answer(json.dumps(answer, ensure_ascii=False), self.references)
        return ' '.join(c['text'] for c in answer['claims'])

    def test_optional_items_not_missing(self):
        text = self.text('我有西红柿、鸡蛋、食用油和盐，没有葱花，能按西红柿炒鸡蛋原方做吗？')
        self.assertIn('不列为必补项', text)
        self.assertNotIn('还缺：葱花', text)
        self.assertIn('数量', text)

    def test_only_inventory_marks_missing(self):
        text = self.text('我只有鸡蛋和西红柿，可以做西红柿炒鸡蛋吗？')
        self.assertIn('还缺：食用油、盐', text)

    def test_unstated_is_not_absent(self):
        text = self.text('我有鸡蛋和西红柿，可以做西红柿炒鸡蛋吗？')
        self.assertIn('未说明是否有：食用油、盐', text)
        self.assertNotIn('还缺：', text)

    def test_step_sugar_not_optional(self):
        text = self.text('我没有白糖，需要补买才能按凉拌黄瓜原方做吗？')
        self.assertIn('步骤中明确使用', text)
        self.assertIn('未标为可选', text)

    def test_known_absence_overrides_full_inventory(self):
        text = self.text('我有黄瓜、醋、酱油、蒜、盐、香油和蚝油，没有白糖，能按凉拌黄瓜原方做吗？')
        self.assertIn('还缺：白糖', text)

    def test_no_added_oil_is_recipe_scoped(self):
        text = self.text('我有空气炸锅和面包片，没有油，能按空气炸锅面包片原方做吗？')
        self.assertIn('未要求加油', text)
        self.assertIn('不是对其他做法的判断', text)

    def test_explicit_missing_tool(self):
        text = self.text('我只有面包片，没有空气炸锅，可以按空气炸锅面包片原方做吗？')
        self.assertIn('明确没有该工具', text)
        self.assertIn('不能直接照原方制作', text)

    def test_ingredient_only_does_not_imply_missing_tool(self):
        text = self.text('我只有面包片，可以按空气炸锅面包片原方做吗？')
        self.assertIn('是否拥有仍需确认', text)
        self.assertNotIn('明确没有该工具', text)

    def test_simple_version_step_items_not_optional(self):
        text = self.text('我没有姜片和花椒，做宫保鸡丁简易版本需要补买吗？')
        self.assertIn('简易版本步骤中明确使用', text)
        self.assertIn('未标为可选', text)

    def test_optional_without_full_inventory(self):
        self.assertIn('不需要仅因缺少', self.text('做基础牛奶面包，没有香草精，需要补买吗？'))

    def test_low_gluten_restriction(self):
        self.assertIn('避免低筋', self.text('我只有低筋面粉，能按基础牛奶面包原方做吗？'))

    def test_unsafe_or_unsupported_conditions_not_guessed(self):
        questions = [
            '我有两个鸡蛋和西红柿，能做西红柿炒鸡蛋吗？',
            '我有西红柿和鸡蛋，鸡蛋过敏，能做西红柿炒鸡蛋吗？',
            '我有西红柿和鸡蛋，没有鸡蛋，能做西红柿炒鸡蛋吗？',
            '我有西红柿和鸡蛋，能用酱油替代盐做西红柿炒鸡蛋吗？',
            '我有西红柿和鸡蛋，要多少油做西红柿炒鸡蛋？',
            '我没有老抽，宫保鸡丁进阶版需要补买吗？',
            '我只有鸡翅和可乐，能做可乐鸡翅吗？',
            '我有西红柿和鸡蛋，没有所有调味料，能做西红柿炒鸡蛋吗？',
            '我有西红柿和鸡蛋，除了盐都没有，能做西红柿炒鸡蛋吗？',
            '我有西红柿和鸡蛋，不是没有盐，能做西红柿炒鸡蛋吗？',
            '如果没有盐，可以做西红柿炒鸡蛋吗？',
            '我有西红柿和鸡蛋，不确定有没有盐，能做西红柿炒鸡蛋吗？',
        ]
        for question in questions:
            with self.subTest(question=question): self.assertIsNone(self.plan(question))

    def test_changed_policy_source_fails_closed(self):
        payload = json.loads(conditions.POLICIES.read_text())
        payload['policies'][0]['source_sha256'] = '0' * 64
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'policies.json'; path.write_text(json.dumps(payload))
            with patch.object(conditions, 'POLICIES', path), self.assertRaises(ValueError):
                self.plan('我只有西红柿和鸡蛋，可以做西红柿炒鸡蛋吗？')

    def test_fake_quote_fails_closed(self):
        payload = json.loads(conditions.POLICIES.read_text())
        payload['policies'][0]['entries'][0]['evidence'][0]['quote'] = '* 编造原料'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'policies.json'; path.write_text(json.dumps(payload))
            with patch.object(conditions, 'POLICIES', path), self.assertRaises(ValueError):
                self.plan('我只有西红柿和鸡蛋，可以做西红柿炒鸡蛋吗？')

    def test_missing_context_rejected(self):
        plan = self.plan('我只有西红柿和鸡蛋，可以做西红柿炒鸡蛋吗？')
        with self.assertRaises(ValueError): conditions.prepare_condition_answer(plan, [])

    def test_claim_tampering_rejected(self):
        plan = self.plan('我只有西红柿和鸡蛋，可以做西红柿炒鸡蛋吗？')
        answer = conditions.prepare_condition_answer(plan, self.references)
        answer['claims'][0]['text'] = '盐可选，直接做。'
        with self.assertRaises(ValueError):
            conditions.validate_condition_answer(json.dumps(answer), plan, self.references)

    def test_material_checks_share_direct_heading_layout(self):
        cases=[('我只有鸡蛋和西红柿，可以做西红柿炒鸡蛋吗？','还缺哪些材料','食用油'),
               ('我有鸡蛋和西红柿，可以做西红柿炒鸡蛋吗？','还需确认的材料','盐'),
               ('我没有姜片和花椒，做宫保鸡丁简易版本需要补买吗？','需要补充的材料','姜、花椒'),
               ('我只有面包片，没有空气炸锅，可以按空气炸锅面包片原方做吗？','所需工具','不能直接'),
               ('我有空气炸锅和面包片，没有油，能按空气炸锅面包片原方做吗？','用油要求','不构成')]
        for q,heading,term in cases:
            with self.subTest(q=q):
                plan=self.plan(q);self.assertIsNotNone(plan)
                result=conditions.prepare_condition_display(plan,self.references)
                self.assertEqual(result['answer_layout'],'inventory_check')
                blocks=[b for b in result['display_blocks'] if b['heading']==heading]
                self.assertTrue(blocks);self.assertIn(term,blocks[0]['text'])
                self.assertTrue(all(b['anchors'] for b in result['display_blocks']))
                self.assertNotIn('这里只核对原文',result['short_answer'])
                self.assertTrue(result['audit_notes'])
        result=conditions.prepare_condition_display(self.plan(cases[1][0]),self.references)
        self.assertNotIn('还缺哪些材料',result['short_answer'])


if __name__ == '__main__': unittest.main()
