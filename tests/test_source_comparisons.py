import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import source_comparison_answer as comparisons
from check_embedding import ROOT
from generate_test_answer import validate_answer


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chunks = [json.loads(s) for s in (ROOT/'data/processed/all_recipes/chunks.jsonl').read_text().splitlines()]

    def prepare(self, question):
        plan = comparisons.select_comparison_plan(question, self.chunks)
        refs = []
        for row in self.chunks:
            if row['source'] != plan['source']: continue
            lines = (ROOT/row['source']).read_text().splitlines(); content = row['content'].splitlines()
            start = next(i for i in range(row['source_start_line']-1,row['source_end_line']) if lines[i:i+len(content)] == content)
            refs.append({**row,'reference_id':len(refs)+1,'content_start_line':start+1})
        answer = comparisons.prepare_comparison_answer(plan,refs)
        text, evidence, _ = validate_answer(json.dumps(answer,ensure_ascii=False),refs)
        return plan, refs, answer, text, evidence

    def test_water_difference_no_invented_unit_or_purpose(self):
        _,_,_,text,_ = self.prepare('蒸水蛋热水还是温水，章节有什么差异？')
        self.assertIn('存在差异',text)
        self.assertIn('未注明单位',text)
        self.assertNotIn('℃',text)
        self.assertNotIn('热水是锅水',text)

    def test_original_numeric_mentions_allowed_but_new_temperatures_rejected(self):
        _,_,_,text,_ = self.prepare('蒸水蛋应该直接用热水还是20～30度温水？原文不同章节分别怎么写，请指出差异。')
        self.assertIn('未注明单位', text)
        self.assertIn('260ml', text)
        self.assertIsNone(comparisons.select_comparison_plan('蒸水蛋热水还是50度温水，原文章节差异？', self.chunks))
        self.assertIsNone(comparisons.select_comparison_plan('蒸水蛋热水还是20度温水？', self.chunks))

    def test_sibling_variant_not_assigned_to_fast_method(self):
        _,_,_,text,evidence = self.prepare('西红柿炒鸡蛋普通操作和快速做法有什么区别？')
        self.assertIn('盛盘',text)
        self.assertIn('继续第10步',text)
        self.assertIn('另一同级条目',text)
        self.assertTrue(all(e['quote'].strip() for e in evidence))

    def test_intro_and_step_both_retained(self):
        _,_,_,text,_ = self.prepare('基础牛奶面包酵头发酵操作与介绍有何不同？')
        for value in ('45~60','4天','30分钟'): self.assertIn(value,text)

    def test_actual_baking_not_replaced_by_preheat(self):
        _,_,_,text,_ = self.prepare('基础牛奶面包面团阶段怎样烘烤？')
        for value in ('180℃','30分钟','165℃','10分钟'): self.assertIn(value,text)
        self.assertIn('预热是前面的独立步骤',text)
        self.assertIsNone(comparisons.select_comparison_plan('基础牛奶面包面团阶段烘烤温度如何调整？',self.chunks))

    def test_unsupported_questions_not_routed(self):
        for q in ('蒸水蛋热水温水品牌有何区别？','蒸水蛋和西红柿炒鸡蛋有什么区别？',
                  '基础牛奶面包酵头操作用量是多少？','蒸水蛋用温水吗？'):
            self.assertIsNone(comparisons.select_comparison_plan(q,self.chunks))

    def test_version_change_rejected(self):
        data = json.loads(comparisons.VIEWS.read_text()); data['profiles'][0]['source_sha256']='0'*64
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'views.json';p.write_text(json.dumps(data))
            with patch.object(comparisons,'VIEWS',p),self.assertRaises(ValueError):
                comparisons.select_comparison_plan('蒸水蛋热水还是温水，章节有什么差异？',self.chunks)

    def test_missing_context_and_tampered_claim_rejected(self):
        plan,refs,answer,_,_ = self.prepare('蒸水蛋热水还是温水，章节有什么差异？')
        with self.assertRaises(ValueError): comparisons.prepare_comparison_answer(plan,[])
        answer['claims'][0]['text']='热水是锅水，温水是蛋液。'
        with self.assertRaises(ValueError): comparisons.validate_comparison_answer(json.dumps(answer),plan,refs)


if __name__=='__main__': unittest.main()
