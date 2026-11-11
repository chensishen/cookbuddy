import unittest
from src.nlu.normalize import normalize
from src.nlu.matching import fuzzy_match

class MatchingTests(unittest.TestCase):
    def test_normalization_preserves_quantities(self):
        for q in ['第2道','放3个鸡蛋','180度烤20分钟','第12道','鸡蛋3个']:
            self.assertEqual(normalize(q)['text'],q)
        self.assertEqual(normalize('  第２道  ！😊')['text'],'第2道')
        self.assertIn('1',normalize('不要刚才第二道1')['ignored_tokens'])
        self.assertEqual(normalize('不要第二道。1')['text'],'不要第二道')
        self.assertEqual(normalize('酵頭製作')['text'],'酵头制作')

    def test_filler_variants(self):
        for q in ['酵头制作那一段','看看酵头制作','酵头的制作部分吧','酵头制作部分']:
            self.assertEqual(fuzzy_match(q,['酵头的制作','面团的制作'])['value'],'酵头的制作')

    def test_phonetic_and_mixed_script(self):
        for q,c in [('酵投制作','酵头的制作'),('红烧rou','红烧肉'),('西红是炒蛋','西红柿炒鸡蛋')]:
            result=fuzzy_match(q,[c,'可乐鸡翅'])
            self.assertEqual(result['value'],c,result)
            self.assertTrue(result['assumption'])

    def test_ambiguity_and_no_match(self):
        self.assertTrue(fuzzy_match('面包制作',['甜面包制作','咸面包制作'])['ambiguous'])
        result=fuzzy_match('量子引擎',['酵头的制作','面团的制作'])
        self.assertIsNone(result['value']);self.assertEqual(len(result['choices']),2)

    def test_semantic_escalation(self):
        r=fuzzy_match('怎么发酵头',['酵头的制作','面团的制作'],semantic=lambda q,c:{'酵头的制作':.96,'面团的制作':.1})
        self.assertEqual(r['value'],'酵头的制作');self.assertEqual(r['level'],'L5')

    def test_cli_entry_can_load_shared_matching(self):
        import subprocess,sys
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        # A fresh interpreter catches missing repository import paths that an
        # in-process unittest import would conceal.
        code="from recipe_tools import read_recipe; r=read_recipe('基础牛奶面包','酵头制作部分'); assert r['status']=='verified_source'"
        result=subprocess.run([sys.executable,'-c',code],cwd=root/'scripts',capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
