import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from structured_recipe_answer import select_pilot, prepare_answer, validate_structured_answer, prepare_inventory_display
from generate_test_answer import validate_answer


class StructuredRecipeAnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chunks = [json.loads(line) for line in (ROOT / 'data/processed/all_recipes/chunks.jsonl').read_text().splitlines()]
        cls.question = '我有鸡胸脯肉和熟花生，没有其他食材和调料，可以考虑宫保鸡丁吗？请说明还缺什么。'
        cls.recipe = select_pilot(cls.question, cls.chunks)
        cls.refs = []
        for chunk in cls.chunks:
            if chunk['source'] != cls.recipe['source']:
                continue
            original = (ROOT / chunk['source']).read_text().splitlines()
            body = chunk['content'].splitlines()
            start = next(i for i in range(chunk['source_start_line']-1, chunk['source_end_line']) if original[i:i+len(body)] == body)
            cls.refs.append({**chunk, 'reference_id': len(cls.refs)+1, 'content_start_line': start+1})

    def test_named_recipe_and_scope(self):
        self.assertIsNotNone(self.recipe)
        self.assertIsNone(select_pilot('我有鸡胸脯肉和熟花生，可以推荐什么？', self.chunks))
        self.assertIsNone(select_pilot('我有鸡胸脯肉和熟花生，可以考虑宫保鸡丁和凉拌黄瓜吗？', self.chunks))
        self.assertIsNone(select_pilot('我有鸡胸脯肉和熟花生，可以考虑宫保鸡丁复杂版本吗？', self.chunks))
        self.assertIsNone(select_pilot('我有鸡胸脯肉和熟花生，花生过敏可以考虑宫保鸡丁吗？', self.chunks))

    def test_answer_and_citations(self):
        answer = prepare_answer(self.question, self.recipe, self.refs)
        short, evidence, _ = validate_answer(json.dumps(answer, ensure_ascii=False), self.refs)
        validate_structured_answer(json.dumps(answer, ensure_ascii=False), self.question, self.recipe, self.refs)
        self.assertIn('已匹配：鸡胸脯肉、熟花生', short)
        self.assertIn('老抽酱油', short)
        self.assertIn('两个版本步骤未明确使用', short)
        self.assertTrue(evidence)
        self.assertNotIn('还缺：手枪腿', short)

    def test_incomplete_inventory_is_unknown(self):
        answer = prepare_answer(self.question.replace('没有其他食材和调料，', ''), self.recipe, self.refs)
        self.assertIn('未说明是否有', answer['claims'][0]['text'])
        self.assertNotIn('还需补充', answer['claims'][0]['text'])

    def test_inventory_display_has_direct_sections_and_separate_audit(self):
        q='我只有鸡肉、盐和食用油，能做宫保鸡丁吗？还缺哪些材料？'
        result=prepare_inventory_display(q,self.recipe,self.refs)
        self.assertEqual(result['answer_layout'],'inventory_check')
        headings=[b['heading'] for b in result['display_blocks']]
        self.assertIn('能不能做',headings);self.assertIn('还缺哪些材料',headings)
        self.assertNotIn('原料表差异',result['short_answer'])
        self.assertNotIn('替代组',result['short_answer'])
        missing=next(b['text'] for b in result['display_blocks'] if b['heading']=='还缺哪些材料')
        self.assertIn('熟花生',missing);self.assertNotIn('植物油',missing);self.assertNotIn('手枪腿',missing)
        self.assertIn('已有鸡肉需确认部位',result['short_answer'])
        self.assertIn('已有食用油需确认种类',result['short_answer'])
        self.assertTrue(any('原料表差异' in n for n in result['audit_notes']))
        self.assertTrue(all(b['anchors'] for b in result['display_blocks']))
        unknown=prepare_inventory_display(q.replace('只有','有'),self.recipe,self.refs)
        self.assertIn('还需确认的材料',unknown['short_answer'])
        self.assertNotIn('还缺哪些材料',unknown['short_answer'])

    def test_missing_or_tampered_evidence_rejected(self):
        with self.assertRaises(ValueError):
            prepare_answer(self.question, self.recipe, self.refs[:1])
        answer = prepare_answer(self.question, self.recipe, self.refs)
        answer['claims'][0]['evidence'] = []
        with self.assertRaises(ValueError):
            validate_structured_answer(json.dumps(answer), self.question, self.recipe, self.refs)

    def cucumber_answer(self, question):
        recipe = select_pilot(question, self.chunks)
        self.assertEqual(recipe['title'], '凉拌黄瓜')
        refs = []
        for chunk in self.chunks:
            if chunk['source'] != recipe['source']:
                continue
            original = (ROOT / chunk['source']).read_text().splitlines()
            body = chunk['content'].splitlines()
            start = next(i for i in range(chunk['source_start_line']-1, chunk['source_end_line']) if original[i:i+len(body)] == body)
            refs.append({**chunk, 'reference_id':len(refs)+1, 'content_start_line':start+1})
        answer = prepare_answer(question, recipe, refs)
        short, evidence, _ = validate_answer(json.dumps(answer), refs)
        validate_structured_answer(json.dumps(answer), question, recipe, refs)
        self.assertTrue(evidence)
        return answer, short

    def test_cucumber_named_recipe_and_garlic_alias(self):
        answer, short = self.cucumber_answer('我有黄瓜和大蒜，没有其他食材和调料，可以考虑凉拌黄瓜吗？请说明还缺什么。')
        self.assertIn('已匹配：黄瓜、蒜', short)
        self.assertIn('还需补充：醋、酱油、盐、香油、蚝油、白糖', short)
        self.assertNotIn('凉皮', short)
        self.assertIn('白糖仅在操作中出现', short)

    def test_cucumber_inventory_not_complete(self):
        _, short = self.cucumber_answer('我有黄瓜和大蒜，可以考虑凉拌黄瓜吗？请说明还缺什么。')
        self.assertIn('未说明是否有：醋', short)
        self.assertNotIn('还需补充', short)

    def test_cucumber_similar_name_not_alias(self):
        _, short = self.cucumber_answer('我有黄瓜和蒜苗，没有其他食材和调料，可以考虑凉拌黄瓜吗？请说明还缺什么。')
        self.assertIn('还需补充：醋、酱油、蒜', short)
        self.assertNotIn('已匹配：黄瓜、蒜', short)

    def bread_answer(self, question):
        recipe = select_pilot(question, self.chunks)
        self.assertEqual(recipe['title'], '基础牛奶面包')
        refs = []
        for chunk in self.chunks:
            if chunk['source'] != recipe['source']:
                continue
            original = (ROOT / chunk['source']).read_text().splitlines()
            body = chunk['content'].splitlines()
            start = next(i for i in range(chunk['source_start_line']-1, chunk['source_end_line']) if original[i:i+len(body)] == body)
            refs.append({**chunk, 'reference_id':len(refs)+1, 'content_start_line':start+1})
        answer = prepare_answer(question, recipe, refs)
        short, evidence, _ = validate_answer(json.dumps(answer), refs)
        validate_structured_answer(json.dumps(answer), question, recipe, refs)
        self.assertTrue(evidence)
        return answer, short

    def test_bread_water_and_fat_condition(self):
        answer, short = self.bread_answer('我有中筋面粉和牛奶，没有其他食材和调料，可以考虑基础牛奶面包吗？请说明还缺什么。')
        self.assertIn('已匹配：中筋面粉、牛奶', short)
        self.assertIn('还需补充：鸡蛋、糖或糖浆、干酵母、盐', short)
        self.assertIn('30℃温水', short)
        self.assertIn('只有“牛奶”这一名称不足以判定', short)
        self.assertNotIn('谷朊粉', answer['claims'][0]['text'])
        self.assertNotIn('烤箱', answer['claims'][0]['text'])
        self.assertIn('中间产物', answer['claims'][2]['text'])

    def test_bread_existing_water_does_not_satisfy_temperature(self):
        _, short = self.bread_answer('我有高筋面粉和奶粉和水和黄油，没有其他食材和调料，可以考虑基础牛奶面包吗？请说明还缺什么。')
        self.assertIn('即使已有水，也需要核对温度', short)
        self.assertIn('已匹配：黄油', short)

    def test_bread_low_gluten_flour_is_not_allowed_alternative(self):
        _, short = self.bread_answer('我有低筋面粉和牛奶，没有其他食材和调料，可以考虑基础牛奶面包吗？请说明还缺什么。')
        self.assertIn('还需补充：高筋面粉或中筋面粉', short)
        self.assertNotIn('已匹配：低筋面粉', short)


if __name__ == '__main__':
    unittest.main()
