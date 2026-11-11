import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from eval_dataset import fixture_sources,make_holdout
from evaluate_prd import register_evaluation


class EvaluationIsolationTests(unittest.TestCase):
    def test_non_prd_fixtures_are_excluded_from_new_holdout(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);evaluation=root/'data/evaluation';evaluation.mkdir(parents=True)
            rows=[]
            for name in ('seen','fresh'):
                source='data/HowToCook/dishes/soup/'+name+'.md'
                file=root/source;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(name)
                for section in ('必备原料和工具','计算','操作'):
                    rows.append({'source':source,'title':name,'section':section,'content':'足够长的原文章节用于来源隔离验证','chunk_id':name+section})
            (evaluation/'qa-latency-v1.json').write_text(json.dumps({'cases':[{'source':rows[0]['source']}]}))
            with patch('eval_dataset.ROOT',root),patch('eval_dataset.load_chunks',return_value=rows):
                result=make_holdout(evaluation/'test.json',source_count=1)
            self.assertEqual(set(result['source_hashes']),{'data/HowToCook/dishes/soup/fresh.md'})
            self.assertIn(rows[0]['source'],result['dev_sources'])

    def test_fixture_source_hashes_and_nested_source_lists_are_detected(self):
        source='data/HowToCook/dishes/soup/a.md'
        self.assertEqual(fixture_sources({'source_hashes':{source:'hash'},'cases':[{'sources':[source]}]}),{source})
        self.assertEqual(fixture_sources({'secret':'.env','cases':[]}),set())

    def test_new_unknown_questions_do_not_repeat_consumed_questions(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);evaluation=root/'data/evaluation';evaluation.mkdir(parents=True)
            source='data/HowToCook/dishes/soup/fresh.md';file=root/source;file.parent.mkdir(parents=True);file.write_text('fresh')
            previous=['银河尘埃布丁怎么做？','木星气旋寿司要哪些原料？','请给出电磁波炖星云的做法。','知识库中有暗物质汤圆的配方吗？']
            (evaluation/'consumed.json').write_text(json.dumps({'cases':[{'question':question} for question in previous]}))
            rows=[{'source':source,'title':'fresh','section':section,'content':'足够长的原文段落用于验证新题隔离','chunk_id':section} for section in ('必备原料和工具','计算','操作')]
            with patch('eval_dataset.ROOT',root),patch('eval_dataset.load_chunks',return_value=rows):
                result=make_holdout(evaluation/'new.json',source_count=1)
            unknown=[case['question'] for case in result['cases'] if not case['gold']]
            self.assertEqual(len(unknown),4);self.assertFalse(set(unknown)&set(previous))

    def test_development_run_cannot_replace_formal_pointer_or_erase_history(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'data/processed/prd-acceptance';folder.mkdir(parents=True)
            path=folder/'latest.json';path.write_text(json.dumps({'report':'old.json','implementation_update':'evidence.json'}))
            with patch('evaluate_prd.ROOT',root):
                register_evaluation(folder/'dev')
                self.assertEqual(json.loads(path.read_text())['report'],'old.json')
                register_evaluation(folder/'sealed',heldout=True)
                register_evaluation(folder/'sealed',heldout=True)
            result=json.loads(path.read_text())
            self.assertEqual(result['previous_formal_reports'],['old.json'])
            self.assertEqual(result['implementation_update'],'evidence.json')
            self.assertEqual(result['report'],'data/processed/prd-acceptance/sealed/report.json')


if __name__=='__main__':unittest.main()
