import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from generate_test_answer import named_recipe_source, should_attempt_answer, review_answer, review_completeness, question_requirements, check_comparison_outline, answer_focus_rows


class CompletenessTests(unittest.TestCase):
    def test_named_recipe_allows_attempt_not_automatic_success(self):
        self.assertTrue(should_attempt_answer({'answerable': False}, None, 'recipe.md'))
        self.assertFalse(should_attempt_answer({'answerable': False}, None, None))

    def test_ambiguous_title_not_selected(self):
        rows = [{'title': '蒸水蛋', 'source': 'egg.md'}, {'title': '凉拌黄瓜', 'source': 'cucumber.md'}]
        self.assertIsNone(named_recipe_source('比较蒸水蛋和凉拌黄瓜', rows))
        self.assertIsNone(named_recipe_source('红烧霸王龙怎么做', rows))
        self.assertEqual(named_recipe_source('蒸水蛋的水温？', rows), 'egg.md')
        self.assertIsNone(named_recipe_source('蒸水蛋', rows + [{'title': '蒸水蛋', 'source': 'other.md'}]))

    def review(self, verdict, complete):
        captured = []
        def fake(messages, reviewing=False):
            captured.append(json.loads(messages[1]['content']))
            payload = {'verdict': verdict, 'reason': '引用检查'} if len(captured) == 1 else {
                'complete': complete, 'reason': '遗漏另一个章节'}
            return {'choices': [{'message': {'content': json.dumps(payload)}}]}
        context = json.dumps([{'reference_id': 1, 'title': '菜谱', 'section': '操作',
                               'content_lines': [{'line': 10, 'text': '温水'}, {'line': 11, 'text': '另一章节'}]}])
        evidence = [{'reference_id': 1, 'quote': '温水'}] if verdict == 'supported' else []
        result = review_answer(fake, '两个章节有何差异？', '只答温水', evidence, context)
        return result, captured

    def test_support_and_completeness_use_separate_context(self):
        result, calls = self.review('supported', False)
        self.assertNotIn('original_references', calls[0])
        self.assertIn('original_references', calls[1])
        self.assertFalse(result['passed'])
        self.assertTrue(result['answerable'])

    def test_complete_supported_answer_passes(self):
        result, calls = self.review('supported', True)
        self.assertTrue(result['passed'])
        self.assertEqual(len(calls), 2)

    def test_real_missing_attribute_keeps_insufficient(self):
        result, _ = self.review('insufficient', True)
        self.assertEqual(result['verdict'], 'insufficient')
        self.assertFalse(result['answerable'])

    def test_false_refusal_requests_repair(self):
        result, _ = self.review('insufficient', False)
        self.assertEqual(result['verdict'], 'incomplete_evidence')
        self.assertTrue(result['answerable'])

    def test_invalid_completeness_result_rejected(self):
        def fake(*args, **kwargs):
            return {'choices': [{'message': {'content': '{"complete":"true","reason":"ok"}'}}]}
        with self.assertRaises(ValueError): review_completeness(fake, '问题', '回答', '[]')

    def test_comparison_requirements_preserve_both_sides(self):
        requirements = question_requirements('操作与简介有何差异？')
        self.assertEqual(len(requirements), 3)
        self.assertIn('不只答其中一方', requirements[1])
        self.assertIn('区分介绍性说明', requirements[2])
        self.assertEqual(len(question_requirements('烤多久？')), 1)

    def test_process_question_focuses_generation_without_changing_reference_ids(self):
        rows=[{'reference_id':1,'source':'recipe.md','section':'简介'},
              {'reference_id':2,'source':'recipe.md','section':'操作'},
              {'reference_id':3,'source':'recipe.md','section':'计算'}]
        self.assertEqual(answer_focus_rows('鸡翅如何处理、什么时候加可乐？',rows,'recipe.md'),[rows[1]])
        self.assertEqual(answer_focus_rows('鸡翅如何处理？',rows,'recipe.md'),rows)
        self.assertEqual(answer_focus_rows('鸡翅如何处理、什么时候加可乐？',rows,None),rows)
        self.assertTrue(any('分别回答处理过程' in item for item in question_requirements('鸡翅如何处理、什么时候加可乐？')))

    def test_single_sided_comparison_rejected(self):
        with self.assertRaises(ValueError):
            check_comparison_outline('操作与介绍有何不同？', '操作建议发酵45～60分钟。')
        check_comparison_outline('操作与介绍有何不同？', '操作建议45～60分钟，而介绍提及其他时长，二者不同。')
        check_comparison_outline('烤多久？', '烤5分钟。')


if __name__ == '__main__': unittest.main()
