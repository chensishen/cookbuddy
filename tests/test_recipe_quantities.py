from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import recipe_quantity_answer as quantities
from check_embedding import ROOT
from generate_test_answer import validate_answer


class FormulaTests(unittest.TestCase):
    def test_decimal_is_exact(self):
        self.assertEqual(quantities.evaluate_formula('0.4+0.2*servings',{'servings':3}),Decimal('1.0'))
        self.assertEqual(quantities.evaluate_formula('7.5+4*servings',{'servings':5}),Decimal('27.5'))

    def test_rounding_and_dependency(self):
        eggs=quantities.evaluate_formula('ceil(1.5*servings)',{'servings':1})
        self.assertEqual(eggs,2)
        self.assertEqual(quantities.evaluate_formula('4*eggs',{'eggs':eggs}),8)

    def test_code_and_unsupported_operations_rejected(self):
        for expr in ('__import__("os").system("true")','servings**100','servings.__class__','[1][0]',
                     'ceil(x=1)','True','unknown+1','1/0','1e999','0*1e999','1+'):
            with self.subTest(expr=expr),self.assertRaises(ValueError): quantities.evaluate_formula(expr,{'servings':2})

    def test_counts_not_guessed(self):
        for q,count in [('两份',2),('三人份',3),('三个人',3),('十一份',11),('二十三份',23),('100份',100)]:
            self.assertEqual(quantities.parse_servings(q),count)
        for q in ('半份','1.5份','1/2份','五分之一份','0份','101份','两份或三份','2到3份','2-3份','2～3份','三份和四份','没说几份'):
            self.assertIsNone(quantities.parse_servings(q))


class QuantityAnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chunks=[json.loads(s) for s in (ROOT/'data/processed/all_recipes/chunks.jsonl').read_text().splitlines()]

    def plan(self,q): return quantities.select_quantity_plan(q,self.chunks)

    def answer(self,q):
        plan=self.plan(q);self.assertIsNotNone(plan);refs=[]
        for row in self.chunks:
            if row['source']!=plan['source']:continue
            lines=(ROOT/row['source']).read_text().splitlines();content=row['content'].splitlines()
            start=next(i for i in range(row['source_start_line']-1,row['source_end_line']) if lines[i:i+len(content)]==content)
            refs.append({**row,'reference_id':len(refs)+1,'content_start_line':start+1})
        answer=quantities.prepare_quantity_answer(plan,refs)
        text,_,_=validate_answer(json.dumps(answer,ensure_ascii=False),refs)
        return plan,refs,answer,text

    def test_affine_formula_and_fixed_amount(self):
        _,_,_,text=self.answer('凉拌黄瓜做五份，各种用料需要多少？')
        self.assertIn('醋：7.5 + 4 × 5 = 27.5ml',text)
        self.assertIn('香油：5 + 2 × 5 = 15ml',text)
        self.assertIn('蚝油：5 = 5ml',text)

    def test_unscaled_step_not_multiplied(self):
        plan,_,_,text=self.answer('凉拌黄瓜做三份，白糖需要多少？')
        self.assertEqual(plan['calculations'][0]['values'],[])
        self.assertIn('原文没有按份数',text)
        self.assertNotIn('15克',text)

    def test_egg_then_oil(self):
        _,_,_,text=self.answer('西红柿炒鸡蛋做五份，需要几个鸡蛋和多少油？')
        self.assertIn('= 8个',text)
        self.assertIn('4 × 8 = 32ml',text)

    def test_oil_only_includes_dependency_evidence(self):
        plan,_,_,text=self.answer('西红柿炒鸡蛋做一份，需要多少油？')
        self.assertEqual(len(plan['calculations']),1)
        self.assertIn(25,[e['line'] for e in plan['calculations'][0]['evidence']])
        self.assertIn('向上取整为2个',text)
        self.assertIn('8ml',text)

    def test_interval_and_optional_not_forced_upper_bound(self):
        _,_,_,text=self.answer('西红柿炒鸡蛋做三份，盐和糖需要多少？')
        self.assertIn('4.5～6克',text)
        self.assertIn('0～6克',text)
        self.assertIn('不是必须达到该范围上限',text)

    def test_specific_item_does_not_include_unrequested_items(self):
        plan,_,_,_=self.answer('凉拌黄瓜做两份，醋需要多少？')
        self.assertEqual([e['name'] for e in plan['calculations']],['醋'])

    def test_unsupported_conditions_and_oil_names(self):
        for q in ('西红柿炒鸡蛋做三份，多少酱油？','凉拌黄瓜做两份，换算多少汤匙醋？',
                  '凉拌黄瓜需要多少醋？','西红柿炒鸡蛋做三份，鸡蛋过敏，需要多少油？',
                  '凉拌黄瓜做半份，用料需要多少？','蒸水蛋做两份，需要多少水？',
                  '我有一个鸡蛋，做三份西红柿炒鸡蛋需要多少油？',
                  '西红柿炒鸡蛋和凉拌黄瓜做三份，需多少盐？'):
            self.assertIsNone(self.plan(q))

    def test_source_version_rejected(self):
        data=json.loads(quantities.PROFILES.read_text());data['profiles'][0]['source_sha256']='0'*64
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'profiles.json';p.write_text(json.dumps(data))
            with patch.object(quantities,'PROFILES',p),self.assertRaises(ValueError):
                self.plan('凉拌黄瓜做两份，醋需要多少？')

    def test_fake_evidence_rejected(self):
        data=json.loads(quantities.PROFILES.read_text());data['profiles'][0]['entries'][1]['evidence']['quote']='* 醋 100ml'
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'profiles.json';p.write_text(json.dumps(data))
            with patch.object(quantities,'PROFILES',p),self.assertRaises(ValueError):
                self.plan('凉拌黄瓜做两份，醋需要多少？')

    def test_missing_references_rejected(self):
        with self.assertRaises(ValueError):quantities.prepare_quantity_answer(self.plan('凉拌黄瓜做两份，醋需要多少？'),[])

    def test_tampered_result_rejected(self):
        plan,refs,answer,_=self.answer('凉拌黄瓜做两份，醋需要多少？')
        answer['claims'][0]['text']='醋需要15ml。'
        with self.assertRaises(ValueError):quantities.validate_quantity_answer(json.dumps(answer),plan,refs)


if __name__=='__main__':unittest.main()
