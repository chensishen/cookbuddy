import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from source_process_answer import prepare_process_answer


class ProcessExcerptTests(unittest.TestCase):
    def rows(self):
        return [{'reference_id':4,'source':'a.md','section':'操作','content_start_line':20,
                 'content':'## 操作\n1. 鸡翅冷水入锅，煮开沥水。\n  * 冰鲜鸡翅先焯水。\n2. 鸡翅改刀腌制。\n3. 鸡翅煎至金黄。\n4. 鸡翅金黄后倒入可乐。\n5. 收汁。'}]
    def test_contiguous_original_steps_and_event(self):
        result=prepare_process_answer('可乐鸡翅原文如何处理鸡翅、什么时候加入可乐？',self.rows(),'a.md')
        claim=result['claims'][0]
        self.assertIn('1. 鸡翅冷水入锅',claim['text'])
        self.assertIn('冰鲜鸡翅先焯水',claim['text'])
        self.assertIn('4. 鸡翅金黄后倒入可乐',claim['text'])
        self.assertNotIn('5. 收汁',claim['text'])
        self.assertEqual(claim['evidence'],[{'reference_id':4,'start_line':21,'end_line':25}])
    def test_ambiguous_or_missing_event_falls_back(self):
        self.assertIsNone(prepare_process_answer('可乐鸡翅如何处理鸡翅、什么时候加入啤酒？',self.rows(),'a.md'))
        self.assertIsNone(prepare_process_answer('可乐鸡翅如何处理鸡翅、什么时候加入可乐？',self.rows(),None))
        self.assertIsNone(prepare_process_answer('可乐鸡翅如何处理鸡翅、什么时候加入可乐？',self.rows()*2,'a.md'))

if __name__=='__main__':unittest.main()
