import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from evaluate_chunk_ablations import h2_chunks, experiment_rows, rank_batch, build_index
from naive_baseline import digest


class ChunkAblations(unittest.TestCase):
    def test_h2_keeps_subheadings_and_fenced_fake_headings_with_exact_offsets(self):
        raw = '# 菜谱\n\n介绍\n\n## 原料\n- 鸡蛋\n### 调料\n- 盐\n```md\n## 不是边界\n```\n\n## 操作\n1. 炒熟\n'
        rows = h2_chunks('recipe.md','菜谱',raw)
        self.assertEqual([r['section'] for r in rows],['简介','原料','操作'])
        self.assertIn('### 调料',rows[1]['content'])
        self.assertIn('## 不是边界',rows[1]['content'])
        self.assertEqual(''.join(r['content'] for r in rows),raw)
        for row in rows:
            self.assertEqual(raw[row['start_char']:row['end_char']],row['content'])
            lines = raw.splitlines(keepends=True)
            self.assertEqual(''.join(lines[row['source_start_line']-1:row['source_end_line']]),row['content'])

    def test_prefix_changes_only_embedding_input_not_content_ids_or_gold_spans(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'recipe.md').write_text('# 菜谱\n介绍\n## 操作\n炒熟\n')
            chunks = [{'source':'recipe.md','title':'菜谱','chunk_id':'active','content':'炒熟','text':'旧前缀炒熟'}]
            with patch('evaluate_chunk_ablations.ROOT',root):
                plain,hashes = experiment_rows(chunks,'h2_plain')
                prefixed,other_hashes = experiment_rows(chunks,'h2_prefix')
                bounded,_ = experiment_rows(chunks,'bounded_plain')
            self.assertEqual(hashes,other_hashes)
            self.assertEqual(bounded[0]['text'],'炒熟')
            for left,right in zip(plain,prefixed):
                self.assertEqual({k:v for k,v in left.items() if k!='text'},
                    {k:v for k,v in right.items() if k!='text'})
                self.assertEqual(left['text'],left['content'])
                self.assertTrue(right['text'].endswith(left['content']))
                self.assertIn('原文路径：recipe.md',right['text'])

    def test_exact_cosine_handles_scale_negative_scores_and_deterministic_ties(self):
        rows = [{'chunk_id':'b','vector':[2.,0.]},{'chunk_id':'a','vector':[1.,0.]},
            {'chunk_id':'c','vector':[-1.,0.]},{'chunk_id':'d','vector':[0.,1.]}]
        self.assertEqual(rank_batch([[8.,0.],[0.,2.]],rows,4),
            [['a','b','d','c'],['d','a','b','c']])
        for invalid in ([0.,0.],[float('nan'),1.]):
            with self.assertRaises(ValueError):
                rank_batch([invalid],rows)

    def test_build_resumes_existing_rows_and_manifest_checks_actual_file(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            chunks = [{'chunk_id':'a','title':'菜谱','section':'操作','source':'source.md',
                'content':'炒熟','text':'炒熟'}]
            settings = {'EMBEDDING_MODEL':'test','EMBEDDING_DIMENSIONS':'2',
                'EMBEDDING_BASE_URL':'https://example.invalid/v1','EMBEDDING_API_KEY':'secret'}
            with patch('evaluate_chunk_ablations.load_chunks',return_value=[]), \
                patch('evaluate_chunk_ablations.experiment_rows',return_value=(chunks,{})), \
                patch('evaluate_chunk_ablations.config',return_value=settings), \
                patch('evaluate_chunk_ablations.request_json',return_value={'data':[{'index':0,'embedding':[1.,0.]}]}) as call:
                first = build_index('bounded_plain',folder)
                second = build_index('bounded_plain',folder)
                self.assertEqual(call.call_count,1)
                self.assertEqual(first['vector_file_sha256'],second['vector_file_sha256'])
                vectors = folder/'bounded_plain/vectors.jsonl'
                self.assertEqual(second['vector_file_sha256'],digest(vectors.read_bytes()))
                self.assertFalse(second['production_enabled'])
                changed = {**settings,'EMBEDDING_MODEL':'different'}
                with patch('evaluate_chunk_ablations.config',return_value=changed):
                    with self.assertRaises(ValueError):
                        build_index('bounded_plain',folder)


if __name__ == '__main__':
    unittest.main()
