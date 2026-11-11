import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_crosscheck import crosscheck, ingredient_vocabulary, item_name

class CrosscheckTests(unittest.TestCase):
    def prepare(self):
        return {'claims':[{'text':'可考虑鸡翅。','evidence':[{'reference_id':1,'start_line':1,'end_line':3}]}]}
    def refs(self):
        return [
            {'reference_id':1,'source':'a','section':'必备原料和工具','content':'* 鸡翅\n* 生姜\n* 盐','content_start_line':1},
            {'reference_id':2,'source':'a','section':'计算','content':'* 老抽 3 克','content_start_line':5},
            {'reference_id':3,'source':'a','section':'操作','content':'1. 放入姜片、食用盐，然后起油。','content_start_line':8},
            {'reference_id':4,'source':'b','section':'计算','content':'* 辣椒 5 克','content_start_line':20},
        ]
    def test_calc_difference_has_original_line(self):
        r=crosscheck(self.prepare(),self.refs(),['鸡翅'],['姜片','食用盐'])
        self.assertIn('老抽',r['claims'][0]['text'])
        self.assertIn({'reference_id':2,'start_line':5,'end_line':5},r['claims'][0]['evidence'])
        self.assertNotIn('辣椒',r['claims'][0]['text'])
    def test_forms_not_duplicate(self):
        r=crosscheck(self.prepare(),self.refs(),['鸡翅'],['姜片','食用盐'])
        text=r['claims'][0]['text']
        self.assertNotIn('姜片',text);self.assertNotIn('食用盐',text)
        self.assertIn('油（原文未说明种类）',text)
    def test_vocab_drops_quantity_prefix(self):
        words=ingredient_vocabulary([{'section':'必备原料和工具','content':'* 大约 3 个\n* 番茄酱'}])
        self.assertNotIn('大约',words);self.assertIn('番茄酱',words)
    def test_vocab_uses_full_calculation_name_instead_of_fragment(self):
        words=ingredient_vocabulary([{'section':'计算','content':'- 5g 蒜蓉辣酱'}])
        self.assertIn('蒜蓉辣酱',words)
        self.assertNotIn('蒜蓉',words)
        self.assertEqual(item_name('1 盒内脂豆腐'),'内脂豆腐')
        self.assertEqual(item_name('1 枚咸鸭蛋'),'咸鸭蛋')
    def test_optional_step_not_required(self):
        refs=self.refs();refs[2]['content']='1. 可以加入番茄酱。'
        r=crosscheck(self.prepare(),refs,['鸡翅'],['番茄酱'])
        self.assertIn('不能直接当作全部必需',r['claims'][0]['text'])

if __name__=='__main__':unittest.main()
