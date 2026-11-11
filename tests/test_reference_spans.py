import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from reference_spans import normalize_reference_spans
from generate_test_answer import validate_answer
from recipe_tools import read_recipe


class ReferenceSpanTests(unittest.TestCase):
    def setUp(self):
        self.source=read_recipe('基础牛奶面包')['sources'][0]['source']
        self.lines=Path(self.source).read_text().splitlines()
        self.refs=[self.row(1,139,164),self.row(2,165,170)]
    def row(self,ident,start,end):
        return {'reference_id':ident,'source':self.source,'section':'操作 > 面团的制作',
                'content_start_line':start,'content':'\n'.join(self.lines[start-1:end])}
    def answer(self,start,end):
        return json.dumps({'claims':[{'kind':'note','text':'保留原文列出的烘烤注意事项。',
            'evidence':[{'reference_id':1,'start_line':start,'end_line':end}]}],
            'missing_information':''},ensure_ascii=False)
    def test_wrong_chunk_for_real_absolute_line_is_rebound(self):
        answer=self.answer(165,165);refs=copy.deepcopy(self.refs)
        with self.assertRaises(ValueError):validate_answer(answer,self.refs)
        normalized,repairs=normalize_reference_spans(answer,self.refs)
        self.assertEqual(json.loads(normalized)['claims'][0]['evidence'][0]['reference_id'],2)
        self.assertEqual(len(repairs),1);self.assertEqual(self.refs,refs)
        _,evidence,_=validate_answer(normalized,self.refs)
        self.assertIn(self.lines[164],evidence[0]['quote'])
        again,repairs=normalize_reference_spans(normalized,self.refs)
        self.assertEqual(again,normalized);self.assertEqual(repairs,[])
    def test_cross_chunk_ranges_split_without_losing_lines(self):
        normalized,repairs=normalize_reference_spans(self.answer(163,167),self.refs)
        spans=json.loads(normalized)['claims'][0]['evidence']
        self.assertEqual([(s['reference_id'],s['start_line'],s['end_line']) for s in spans],[(1,163,164),(2,165,167)])
        validate_answer(normalized,self.refs)
    def test_missing_lines_other_source_and_forged_chunks_are_not_repaired(self):
        answer=self.answer(165,165)
        cases=[self.refs[:1], [self.refs[0],self.row(2,166,170)],
               [self.refs[0],{**self.refs[1],'source':'data/HowToCook/other.md'}],
               [self.refs[0],{**self.refs[1],'content':'伪造引用'}]]
        for refs in cases:
            with self.subTest(refs=refs):
                normalized,repairs=normalize_reference_spans(answer,refs)
                self.assertEqual(normalized,answer);self.assertEqual(repairs,[])
                with self.assertRaises(ValueError):validate_answer(normalized,refs)
        normalized,repairs=normalize_reference_spans(self.answer(9999,9999),self.refs)
        self.assertEqual(repairs,[])
        for ident in (True,1.0,[],999):
            payload=json.loads(answer);payload['claims'][0]['evidence'][0]['reference_id']=ident
            text=json.dumps(payload);normalized,repairs=normalize_reference_spans(text,self.refs)
            self.assertEqual(normalized,text);self.assertEqual(repairs,[])
