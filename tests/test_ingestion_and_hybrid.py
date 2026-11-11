import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from document_ingestion import markdown_blocks,bounded_chunks,ingest,parse_docx
from lexical_retrieval import BM25,rrf_merge
from knowledge_versions import change_plan,source_manifest,publish_alias,rollback_alias,safe_collection


class IngestionTests(unittest.TestCase):
    def test_setext_headings_code_fences_and_original_locators(self):
        text='测试菜\n======\n简介内容\n原料\n----\n鸡蛋\n```md\n## 非章节\n```\n## 操作\n蒸熟'
        rows=markdown_blocks(text)
        self.assertEqual([r['section'] for r in rows],['简介','原料','操作'])
        self.assertEqual(rows[0]['title'],'测试菜')
        self.assertIn('## 非章节',rows[1]['content'])
    def test_nested_parent_and_bounded_chunks_keep_lines_and_overlap(self):
        text='## 操作\n### 简易版\n'+ '\n'.join('第%d步：鸡蛋蒸熟'%i for i in range(10))
        block=markdown_blocks(text)[0]
        chunks=bounded_chunks(block,max_tokens=30,overlap_lines=1,token_counter=len)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(r['content'])<=30 for r in chunks))
        self.assertEqual(chunks[0]['section'],'操作 > 简易版')
        self.assertEqual(chunks[0]['content'].splitlines()[-1],chunks[1]['content'].splitlines()[0])
    def test_oversized_atomic_line_is_explicit_not_truncated(self):
        with self.assertRaises(ValueError):bounded_chunks({'content':'鸡蛋'*100,'section':'原料'},max_tokens=10,token_counter=len)
    def test_source_hash_stable_ids_and_path_free_embedding(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'test.md';path.write_text('# 测试菜\n\n## 操作\n蒸鸡蛋。\n')
            a=ingest(path);b=ingest(path)
            self.assertEqual(a['chunks'][0]['chunk_id'],b['chunks'][0]['chunk_id'])
            self.assertNotIn(str(path),a['chunks'][0]['text'])
            self.assertEqual(a['source_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            path.write_text('# 测试菜\n\n## 操作\n炒鸡蛋。\n')
            self.assertNotEqual(a['source_sha256'],ingest(path)['source_sha256'])
    def test_docx_tables_and_paragraph_locators(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'recipe.docx'
            xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>菜谱</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>盐</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>2g</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('word/document.xml',xml)
            rows=parse_docx(path)
            self.assertEqual(rows[1]['content'],'盐 | 2g');self.assertEqual(rows[1]['node_index'],2)
            self.assertFalse(ingest(path)['chunks'][0]['production_enabled'])
    def test_blank_pdf_is_marked_needs_ocr(self):
        try:from pypdf import PdfWriter
        except ImportError:self.skipTest('需要 pypdf')
        with TemporaryDirectory() as directory:
            path=Path(directory)/'scan.pdf';writer=PdfWriter();writer.add_blank_page(100,100)
            with path.open('wb') as out:writer.write(out)
            result=ingest(path)
            self.assertEqual(result['blocks'][0]['parse_method'],'needs_ocr')
            self.assertEqual(result['chunks'],[])


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'chunk_id':'egg','title':'蒸水蛋','section':'操作','content':'蒸水蛋用锡纸盖住碗'},
                   {'chunk_id':'noodle','title':'炒面','section':'操作','content':'炒面放入酱油'},
                   {'chunk_id':'oil','title':'凉拌黄瓜','section':'用料','content':'盐 香油 黄瓜'}]
    def test_exact_chinese_terms_find_relevant_source(self):
        self.assertEqual(BM25(self.rows).search('蒸水蛋用什么盖住碗',1)[0]['chunk_id'],'egg')
        self.assertEqual(BM25(self.rows).search('香油',1)[0]['chunk_id'],'oil')
    def test_fusion_uses_ranks_not_raw_score_scales(self):
        a={'chunk_id':'a','distance':.8};b={'chunk_id':'b','distance':.7}
        rows=rrf_merge({'vector':[a,b],'bm25':[{**b,'bm25_score':100000},{**a,'bm25_score':.001}]})
        self.assertEqual(rows[0]['fusion_score'],rows[1]['fusion_score'])
        self.assertEqual(rows[0]['distance'],.8)
        self.assertEqual(rows[1]['retrieval_channels'],['vector','bm25'])
    def test_duplicate_hit_in_channel_is_not_boosted(self):
        row={'chunk_id':'a','distance':.8}
        self.assertEqual(rrf_merge({'a':[row,row]})[0]['fusion_score'],1/61)


class VersionTests(unittest.TestCase):
    def test_incremental_plan_includes_deletion_and_model_rebuild(self):
        plan=change_plan({'a':'1','b':'2','c':'3'},{'a':'1','b':'4','d':'5'},'old','new',1024,1024)
        self.assertEqual(plan['added'],['d']);self.assertEqual(plan['deleted'],['c'])
        self.assertEqual(plan['changed'],['b']);self.assertTrue(plan['embedding_rebuild_required'])
    def test_guard_prevents_other_project_collections(self):
        with self.assertRaises(ValueError):safe_collection('sales_agent_knowledge')
    def test_validated_publish_and_conflict_safe_rollback(self):
        class Client:
            def __init__(self):self.target='recipe_old'
            def describe_alias(self,alias):return {'collection_name':self.target}
            def alter_alias(self,target,alias):self.target=target
        client=Client()
        with self.assertRaises(ValueError):publish_alias('recipe_live','recipe_new',{'status':'prepared'},client)
        change=publish_alias('recipe_live','recipe_new',{'status':'validated','collection':'recipe_new'},client)
        self.assertEqual(client.target,'recipe_new');rollback_alias(change,client);self.assertEqual(client.target,'recipe_old')
        client.target='recipe_other'
        with self.assertRaises(ValueError):rollback_alias(change,client)


if __name__=='__main__':unittest.main()
