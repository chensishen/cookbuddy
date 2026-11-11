import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from acceptance_closeout import exact_evidence, corpus_audit, retrieval_probe


class EvidenceAcceptanceTests(unittest.TestCase):
    def test_exact_quote_and_invalid_line_ranges(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            source='data/HowToCook/dishes/breakfast/example.md'
            (root/source).parent.mkdir(parents=True)
            (root/source).write_text('# 菜名\n## 操作\n加水\n')
            base={'source':source,'start_line':3,'end_line':3,'quote':'加水'}
            self.assertTrue(exact_evidence({'evidence':[base]},root)['all_exact'])
            for change in ({'start_line':0},{'end_line':4},{'start_line':True},
                           {'quote':'加热水'},{'source':'../../outside.md'}):
                self.assertFalse(exact_evidence({'evidence':[{**base,**change}]},root)['all_exact'])

    def test_empty_evidence_is_not_success(self):
        self.assertEqual(exact_evidence({}),{'count':0,'all_exact':False})

    def test_corpus_records_changed_source_without_aborting_other_recipes(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            rows=[]
            for title in ('正常菜','变更菜'):
                source=f'data/HowToCook/dishes/breakfast/{title}.md'
                (root/source).parent.mkdir(parents=True,exist_ok=True)
                (root/source).write_text('原文')
                rows.append({'source':source,'title':title,'chunk_id':title,'section':'操作','content':'原文'})
            good={'status':'verified_source','complete':True,'evidence':[{
                'source':rows[0]['source'],'start_line':1,'end_line':1,'quote':'原文'}]}
            def original(source):
                if source==rows[1]['source']:raise ValueError('来源变更')
                return good
            with patch('acceptance_closeout.ROOT',root),patch('recipe_tools.load_chunks',return_value=rows),\
                 patch('recipe_tools.read_recipe',side_effect=original),\
                 patch('recipe_tools.find_recipe_images',return_value={'status':'insufficient','images':[]}),\
                 patch('acceptance_closeout.exact_evidence',side_effect=lambda r:exact_evidence(r,root)):
                report=corpus_audit()
            self.assertEqual(report['sources'],2)
            self.assertEqual(report['passed_sources'],1)
            self.assertFalse(report['passed'])
            self.assertTrue(any(r.get('failure_type')=='ValueError' for r in report['records']))

    def test_retrieval_candidate_hit_cannot_hide_rerank_source_loss(self):
        from check_embedding import ROOT
        source='data/HowToCook/dishes/breakfast/测试菜.md'
        rows=[{'source':source,'title':'测试菜','chunk_id':'c','section':'操作','content':'原文'}]
        search=(ROOT/'data/processed/search.json',{'results':[{'source':source}]})
        rerank=(ROOT/'data/processed/rerank.json',{'results':[{'source':'另一来源'}]})
        with patch('recipe_tools.load_chunks',return_value=rows),\
             patch('ask_test_recipes.run_stage',side_effect=[search,rerank]):
            report=retrieval_probe()
        self.assertTrue(report['records'][0]['candidate_source_hit'])
        self.assertFalse(report['records'][0]['rerank_source_hit_at_5'])
        self.assertFalse(report['passed'])


if __name__=='__main__':unittest.main()
