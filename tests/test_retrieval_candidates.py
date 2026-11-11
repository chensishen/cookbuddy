import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from retrieval_candidates import match_titles, merge_candidates

class CandidateTests(unittest.TestCase):
    def test_explicit_title(self):
        self.assertEqual(match_titles('可乐鸡翅需要什么？', ['可乐鸡翅','红烧鸡翅']),['可乐鸡翅'])
    def test_no_invented_titles(self):
        self.assertEqual(match_titles('只有鸡蛋能做什么？',['可乐鸡翅','炒饭']),[])
    def test_long_name_and_comparison(self):
        self.assertEqual(set(match_titles('酸梅汤（半成品加工）和可乐鸡翅', ['酸梅汤','酸梅汤（半成品加工）','可乐鸡翅'])),{'酸梅汤（半成品加工）','可乐鸡翅'})
    def test_two_explicit_names_are_kept(self):
        self.assertEqual(set(match_titles("酸梅汤和酸梅汤（半成品加工）有什么区别？", ["酸梅汤", "酸梅汤（半成品加工）"])), {"酸梅汤", "酸梅汤（半成品加工）"})
    def test_merge_deduplicates_and_keeps_scores(self):
        rows=merge_candidates([{'chunk_id':'a','distance':.7}], [{'chunk_id':'a','distance':.7},{'chunk_id':'b','distance':.4}])
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['retrieval_channels'],['vector','title'])
        self.assertEqual(rows[1]['distance'],.4)


class IngredientTests(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            {'source':'cola','section':'必备原料和工具','content':'* 鸡翅中\n* 可乐\n* 盐'},
            {'source':'roast','section':'必备原料和工具','content':'- 鸡翅\n- 盐'},
            {'source':'egg','section':'必备原料和工具','content':'- 鸡蛋\n- 可乐（可选）'},
        ]
    def test_inventory_two_matches(self):
        from retrieval_candidates import ingredient_candidates
        terms,sources=ingredient_candidates('我有鸡翅和可乐，没有其他食材。',self.chunks)
        self.assertEqual(terms,['鸡翅','可乐']);self.assertEqual(sources,['cola'])
    def test_negation_not_inventory(self):
        from retrieval_candidates import ingredient_candidates
        terms,sources=ingredient_candidates('我有鸡蛋，没有可乐。',self.chunks)
        self.assertEqual(terms,['鸡蛋']);self.assertEqual(sources,['egg'])
    def test_complex_inventory_not_guessed(self):
        from retrieval_candidates import ingredient_candidates
        self.assertEqual(ingredient_candidates('我有鸡蛋但没有可乐。',self.chunks),([],[]))
    def test_optional_does_not_boost(self):
        from retrieval_candidates import ingredient_candidates
        _,sources=ingredient_candidates('我有可乐。',self.chunks)
        self.assertEqual(sources,['cola'])
    def test_single_character_seasoning(self):
        from retrieval_candidates import ingredient_candidates
        terms,_=ingredient_candidates('我有鸡蛋和盐，可以做什么？', self.chunks)
        self.assertEqual(terms,['鸡蛋','盐'])
    def test_three_channels_deduplicated(self):
        rows=merge_candidates([{'chunk_id':'a','distance':.7}],[],[{'chunk_id':'a','distance':.7}])
        self.assertEqual(rows[0]['retrieval_channels'],['vector','ingredient'])

if __name__=='__main__':unittest.main()
