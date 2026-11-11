import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from evaluate_full_matrix import PROFILES,deduplicate,Engine,normalize_query,controlled_scope_complete,failure_diagnostics
from ablation_indexes import overview_rows


class MatrixTests(unittest.TestCase):
    def test_each_leave_one_out_changes_exactly_one_component(self):
        expected={'A-1':'rerank','A-2':'hybrid','A-3':'prefix','A-4':'filter','A-5':'normalize','A-6':'agent'}
        for name,key in expected.items():
            changed={field for field in PROFILES['E8'] if PROFILES[name][field]!=PROFILES['E8'][field]}
            self.assertEqual(changed,{key});self.assertFalse(PROFILES[name][key])
        self.assertFalse(PROFILES['E1']['overview']);self.assertFalse(PROFILES['E1']['hybrid'])
        self.assertTrue(PROFILES['E7']['rerank']);self.assertFalse(PROFILES['E7']['agent'])

    def test_overview_projection_keeps_real_original_as_generation_context(self):
        original={'chunk_id':'real','source':'source','title':'菜谱','section':'必备原料和工具',
                  'content':'原文\n- 西红柿','source_start_line':1,'source_end_line':2,'vector':[1,0]}
        metadata={'source':{'category':'soup','difficulty':2,'ingredients':['番茄']}}
        row=overview_rows([original],metadata,[],True,True)[0]
        self.assertEqual(row['content'],original['content'])
        self.assertEqual(row['evidence_chunk_id'],'real')
        self.assertIn('番茄',row['text']);self.assertNotIn('番茄',row['content'])
        self.assertEqual(len(deduplicate([row,original])),1)

    def test_all_and_any_constraints_are_independent_and_normalization_can_be_removed(self):
        engine=Engine.__new__(Engine)
        engine.metadata={'a':{'category':'soup','difficulty':2,'ingredients':['番茄','鸡蛋']},
                         'b':{'category':'meat_dish','difficulty':1,'ingredients':['鸡蛋']}}
        engine.raw_ingredients={'a':{'西红柿','鸡蛋'},'b':{'鸡蛋'}}
        self.assertEqual(engine.allowed({'ingredients_all':['西红柿','鸡蛋']},True),{'a'})
        self.assertEqual(engine.allowed({'ingredients_all':['番茄']},False),set())
        self.assertEqual(engine.allowed({'ingredients_any':['鸡蛋'],'category':'soup','max_difficulty':1},True),set())

    def test_query_normalization_is_separate_from_original_question(self):
        question='我有马铃薯和西红柿'
        self.assertEqual(normalize_query(question),'我有土豆和番茄')
        self.assertEqual(question,'我有马铃薯和西红柿')

    def test_ragas_scope_keeps_requested_denominator_and_legacy_reports(self):
        from evaluate_ragas import scoped_answers
        rows=[{'case':'a','variant':'E0'},{'case':'a','variant':'E4'},{'case':'a','variant':'E8','failure_type':'failed'}]
        self.assertEqual(scoped_answers({'answers':rows}),rows)
        self.assertEqual(scoped_answers({'answers':rows,'ragas_eval_variants':['E0','E8']}),[rows[0],rows[2]])

    def test_controlled_completion_does_not_relabel_answer_as_verified(self):
        result={'status':'baseline_unreviewed','short_answer':'待评审回答'}
        self.assertTrue(controlled_scope_complete('问题',[{'tool':'query_recipes','result':result}]))
        self.assertEqual(result['status'],'baseline_unreviewed')
        self.assertFalse(controlled_scope_complete('问题',[{'tool':'query_recipes','result':{'status':'tool_failed','short_answer':'失败'}}]))

    def test_failure_diagnostics_records_local_validation_without_remote_message(self):
        try:raise ValueError('基线引用偏移不一致')
        except ValueError as error:detail=failure_diagnostics(error)
        self.assertEqual(detail['failure_reason'],'基线引用偏移不一致')
        self.assertEqual(detail['failure_trace'][-1]['file'],'test_full_matrix.py')
        self.assertNotIn('failure_reason',failure_diagnostics(ValueError('untrusted secret response')))

    def test_overview_loader_rejects_changed_metadata(self):
        from ablation_indexes import load,sha
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);metadata=folder/'source-metadata.json';metadata.write_text('{}')
            rows=folder/'sample.jsonl';rows.write_text('{}\n')
            (folder/'manifest.json').write_text(json.dumps({'state':'ready','variant_hashes':{'sample':sha(rows.read_bytes())},
                'spec':{'source_hashes':{},'metadata_sha256':sha(metadata.read_bytes())}}))
            with patch('ablation_indexes.FOLDER',folder):
                self.assertEqual(load(folder,'sample'),[{}])
                metadata.write_text('{"changed":true}')
                with self.assertRaisesRegex(ValueError,'元数据版本变化'):load(folder,'sample')

    def test_tool_context_preserves_actual_provenance_without_gold_expansion(self):
        from evaluate_ragas import tool_evidence_context
        evidence={'title':'空气炸锅面包片','section':'操作','source':'actual.md','quote':'200°C 烘烤 5 分钟','other':'not context'}
        value=json.loads(tool_evidence_context(evidence))
        self.assertEqual(value,{k:evidence[k] for k in ('title','section','source','quote')})
        self.assertNotIn('other',value)


if __name__=='__main__':unittest.main()
