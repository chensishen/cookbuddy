import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from search_filters import filter_chunks
from search_test_recipes import main
from cookbuddy.ltm.guard import policy


def chunks_for(name,category,difficulty):
    source=f'data/HowToCook/dishes/{category}/{name}.md'
    return [{'chunk_id':name+str(i),'title':name,'source':source,'section':section,
        'content':content,'source_start_line':i*10+1,'source_end_line':i*10+5,
        'embedding_model':'test','distance':.5}
        for i,(section,content) in enumerate((('简介',difficulty),('必备原料和工具','- 鸡蛋')))]


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.rows=chunks_for('素菜甲','vegetable_dish','预估烹饪难度：★★')+chunks_for('荤菜乙','meat_dish','预估烹饪难度：★')

    def test_category_and_difficulty_intersect_without_losing_other_sections(self):
        rows,expression,metadata=filter_chunks(self.rows,'vegetable_dish',2)
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['title']=='素菜甲' for row in rows))
        self.assertEqual(json.loads(expression.removeprefix('source in ')),[rows[0]['source']])
        self.assertEqual(metadata[rows[0]['source']]['difficulty'],2)

    def test_unknown_conflicting_out_of_range_and_fenced_difficulty_excluded(self):
        for value in ('','预估烹饪难度：★★★★★★','预估烹饪难度：★★\n预估烹饪难度：★',
                      '```md\n预估烹饪难度：★\n```','预估烹饪难度：★★★★'):
            rows,expression,_=filter_chunks(chunks_for('素菜甲','vegetable_dish',value),max_difficulty=2)
            self.assertEqual(rows,[])
            self.assertEqual(expression,'source == "__no_allowed_recipe__"')

    def test_no_filter_preserves_existing_candidates(self):
        rows,expression,metadata=filter_chunks(self.rows)
        self.assertIs(rows,self.rows);self.assertEqual(expression,'');self.assertEqual(metadata,{})

    def test_invalid_filters_are_rejected(self):
        for value in (True,0,6,2.5,'2'):
            with self.assertRaises(ValueError):filter_chunks(self.rows,max_difficulty=value)
        for value in ('unknown','vegetable_dish" or true',True,[]):
            with self.assertRaises(ValueError):filter_chunks(self.rows,category=value)


class SearchFilterPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.rows=chunks_for('素菜甲','vegetable_dish','预估烹饪难度：★★')+chunks_for('荤菜乙','meat_dish','预估烹饪难度：★')
        (self.folder/'chunks.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in self.rows))
        metadata={'requested_model':'test','dimensions':2,'api_base_url':'https://example.invalid','source_sha256':'test'}
        (self.folder/'embedding-metadata.json').write_text(json.dumps(metadata))
        self.calls=[]
        def milvus(operation,payload):
            if operation=='collections/describe':return {'fields':[{'name':'vector','params':[{'key':'dim','value':'2'}]}]}
            self.calls.append(payload)
            matches=re.findall(r'source in (\[[^\]]*\])',payload['filter'])
            self.assertTrue(matches)
            allowed=set(json.loads(matches[0]))
            rows=[r for r in self.rows if r['source'] in allowed]
            ids=re.search(r'chunk_id in (\[[^\]]*\])',payload['filter'])
            if ids:rows=[r for r in rows if r['chunk_id'] in json.loads(ids[1])]
            return rows[:payload['limit']]
        for patcher in (patch('search_test_recipes.FOLDER',self.folder),
                        patch('search_test_recipes.milvus_api',side_effect=milvus),
                        patch('search_test_recipes.config',return_value={'EMBEDDING_MODEL':'test','EMBEDDING_DIMENSIONS':'2','EMBEDDING_BASE_URL':'https://example.invalid','EMBEDDING_API_KEY':'private'}),
                        patch('search_test_recipes.request_json',return_value={'data':[{'embedding':[1,0]}]}),
                        patch.dict(os.environ,{'RAG_AUGMENT':'1','RAG_RETRIEVAL':'hybrid','COOKBUDDY_HARD_CONTEXT':''})):
            patcher.start();self.addCleanup(patcher.stop)

    def test_all_four_recall_channels_share_filter(self):
        report=main('素菜甲和荤菜乙，我有鸡蛋。',category='vegetable_dish',max_difficulty=2)
        self.assertEqual(len(self.calls),4)
        self.assertEqual(report['allowed_source_count'],1)
        self.assertTrue(report['metadata_filter_applied'])
        self.assertEqual({r['title'] for r in report['results']},{'素菜甲'})
        self.assertEqual(set(report['results'][0]['retrieval_channels']),{'vector','title','ingredient','bm25'})

    def test_no_matching_sources_stop_before_embedding_or_milvus(self):
        with patch('search_test_recipes.request_json') as embedding,patch('search_test_recipes.milvus_api') as milvus:
            with self.assertRaisesRegex(ValueError,'no_allowed_recipes'):main('鸡蛋',category='soup')
        embedding.assert_not_called();milvus.assert_not_called()

    def test_memory_hard_constraints_intersect_with_metadata(self):
        with policy([{'dimension':'avoid_ingredient','value':'鸡蛋','polarity':'avoid'}]),patch('cookbuddy.ltm.guard.source_allowed',return_value=False):
            with patch('search_test_recipes.request_json') as embedding:
                with self.assertRaisesRegex(ValueError,'no_allowed_recipes'):main('鸡蛋',category='vegetable_dish')
        embedding.assert_not_called()

    def test_unexpected_source_from_backend_is_rejected(self):
        with patch('search_test_recipes.milvus_api',side_effect=[{'fields':[{'name':'vector','params':[{'key':'dim','value':'2'}]}]},self.rows]):
            with self.assertRaisesRegex(ValueError,'违反元数据过滤'):main('鸡蛋',category='vegetable_dish')

    def test_cli_passes_explicit_filters(self):
        with patch('sys.argv',['search_test_recipes.py','鸡蛋','--category','vegetable_dish','--max-difficulty','2']):
            report=main()
        self.assertEqual(report['metadata_filters'],{'category':'vegetable_dish','max_difficulty':2})

    def test_search_inherits_agent_context(self):
        from search_filters import filter_policy
        with filter_policy(category='vegetable_dish',max_difficulty=2):
            report=main('鸡蛋')
        self.assertEqual(report['metadata_filters'],{'category':'vegetable_dish','max_difficulty':2})
        self.assertTrue(all(row['title']=='素菜甲' for row in report['results']))


if __name__=='__main__':unittest.main()
