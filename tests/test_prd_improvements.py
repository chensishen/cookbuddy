import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from llm_config import llm_settings, require_llm, chat_parameters
from ingredient_recommendation import normalize_ingredient, parse_inventory, recommend
from retrieval_candidates import match_titles
from document_ir import markdown_ir, chunk_ir, ingest_ir, element
from evaluate_prd import metrics, interval, paired_test, summarize_retrieval
from review_prd_eval import llm_cost, matching_prices, execution_summary


class ModelConfigurationTests(unittest.TestCase):
    def test_embedding_secret_is_never_used_for_chat(self):
        with self.assertRaises(ValueError):require_llm({'EMBEDDING_API_KEY':'embedding-only'})
    def test_deepseek_parameters_and_environment(self):
        with patch.dict('os.environ',{'LLM_API_KEY':'example-test-key','LLM_MODEL':'test-chat'}):
            self.assertEqual(require_llm()['model'],'test-chat')
        settings=llm_settings({'LLM_MODEL':'deepseek-flash'})
        params=chat_parameters({'enable_thinking':True,'thinking_budget':100,'response_format':{'type':'json_object'}},settings)
        self.assertNotIn('enable_thinking',params);self.assertNotIn('thinking_budget',params)
        self.assertEqual(params['thinking'],{'type':'disabled'})
        self.assertEqual(params['response_format'],{'type':'json_object'})
    def test_agnes_disables_thinking_with_documented_extension(self):
        cfg=llm_settings({'LLM_BASE_URL':'https://apihub.agnes-ai.com/v1','LLM_MODEL':'agnes-3.0-flash'})
        params=chat_parameters({'thinking':{'type':'enabled'},'enable_thinking':True,'thinking_budget':1024,
                                'chat_template_kwargs':{'custom':1,'enable_thinking':True},'response_format':{'type':'json_object'}},cfg)
        self.assertEqual(params['model'],'agnes-3.0-flash')
        self.assertEqual(params['chat_template_kwargs'],{'custom':1,'enable_thinking':False})
        self.assertFalse(any(k in params for k in ('thinking','enable_thinking','thinking_budget')))
        self.assertEqual(params['response_format'],{'type':'json_object'})

    def test_unsafe_provider_address_rejected(self):
        for base in ('http://example.com','https://user:secret@example.com','https://example.com/?key=secret'):
            with self.assertRaises(ValueError):llm_settings({'LLM_BASE_URL':base})
    def test_loopback_http_model_proxy(self):
        for host in ('127.0.0.1', 'localhost', '[::1]'):
            base=f'http://{host}:3425/v1'
            settings=llm_settings({'LLM_BASE_URL':base,'LLM_MODEL':'workbuddy-ai/deepseek-v4.1-flash'})
            self.assertEqual(settings['base_url'],base)
            self.assertEqual(settings['model'],'workbuddy-ai/deepseek-v4.1-flash')
        for base in ('http://127.0.0.1.example.com/v1','http://192.168.1.1/v1',
                     'http://user:secret@localhost/v1','http://localhost/v1?key=secret'):
            with self.assertRaises(ValueError):llm_settings({'LLM_BASE_URL':base})
    def test_siliconflow_json_defaults_to_nonthinking(self):
        settings=llm_settings({'LLM_BASE_URL':'https://api.siliconflow.cn/v1',
                               'LLM_MODEL':'deepseek-ai/DeepSeek-V4.1-Flash'})
        params=chat_parameters({'thinking':{'type':'disabled'},'response_format':{'type':'json_object'}},settings)
        self.assertFalse(params['enable_thinking'])
        self.assertNotIn('thinking',params)
        self.assertEqual(params['model'],'deepseek-ai/DeepSeek-V4.1-Flash')
        self.assertEqual(params['response_format'],{'type':'json_object'})


class IngredientRecommendationTests(unittest.TestCase):
    def test_alias_quantity_and_tool_boundary(self):
        self.assertEqual(normalize_ingredient('西红柿（2 个）'),'番茄')
        self.assertEqual(normalize_ingredient('2个鸡蛋'),'鸡蛋')
        self.assertEqual(normalize_ingredient('马铃薯 500g'),'土豆')
        self.assertEqual(parse_inventory('有鸡蛋和番茄能做什么'),['鸡蛋','番茄'])
        self.assertIsNone(parse_inventory('我有鸡蛋，没有盐，推荐菜'))
        self.assertIsNone(parse_inventory('我有鸡蛋和锅，推荐三个菜'))
    def test_coverage_sort_missing_optional_and_tools(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);rows=[]
            for name,body in [('a','- 西红柿\n- 鸡蛋\n- 盐\n- 糖（可选）\n- 锅'),('b','- 鸡蛋\n- 盐')]:
                path=root/(name+'.md');path.write_text(body)
                rows.append({'title':name,'section':'必备原料和工具','source':path.name,'content':body,'source_start_line':1,'source_end_line':len(body.splitlines())})
            with patch('ingredient_recommendation.ROOT',root):
                results=recommend(['番茄','鸡蛋'],rows)
                self.assertEqual(results[0]['title'],'a')
                self.assertEqual(results[0]['missing'],['盐'])
                self.assertEqual(results[0]['optional'],['糖'])
                self.assertEqual(results[0]['tools'],['锅'])
                self.assertEqual(len(results[0]['matched']),2)
                self.assertNotIn('coverage',results[0])
                (root/'a.md').write_text('- 盐')
                with self.assertRaises(ValueError):recommend(['鸡蛋'],rows)
    def test_recipe_alias(self):
        self.assertEqual(match_titles('番茄炒蛋要什么原料',{'西红柿炒鸡蛋'}),['西红柿炒鸡蛋'])


class UnifiedIRTests(unittest.TestCase):
    def test_formula_code_and_table_atomicity(self):
        rows=markdown_ir('# 测试\n## 计算\n变量说明\n$$\nx=2\n$$\n```py\n## 假标题\n```\n|食材|用量|\n|---|---|\n|盐|2g|')
        self.assertEqual([r['type'] for r in rows],['title','title','paragraph','formula','code','table'])
        chunks=chunk_ir(rows,max_tokens=60,token_counter=len)
        self.assertTrue(any('变量说明\n$$\nx=2\n$$'==r['text'] for r in chunks))
        self.assertTrue(any('## 假标题' in r['text'] for r in chunks))
        self.assertEqual(chunks[-1]['embedding_text'],'食材: 盐；用量: 2g')
    def test_table_groups_repeat_header_and_track_rows(self):
        row=element('table','',table_rows=[['名','量']]+[[str(i),'2g'] for i in range(9)])
        chunks=chunk_ir([row],max_tokens=45,token_counter=len)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(r['text'].startswith('| 名 | 量 |') for r in chunks))
        self.assertEqual(chunks[0]['row_range'][0],2)
        self.assertEqual(chunks[-1]['row_range'][1],10)
    def test_malformed_empty_duplicate_and_wrong_magic_quarantined(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'bad.csv';path.write_text('食材,用量\n盐\n')
            self.assertEqual(ingest_ir(path)['route'],'quarantine')
            path=root/'empty.md';path.write_text('');self.assertEqual(ingest_ir(path)['chunks'],[])
            path.write_text('# 测试\n## 操作\n蒸熟')
            first=ingest_ir(path);second=ingest_ir(path,known_hashes=[first['source_sha256']])
            self.assertEqual(second['route'],'quarantine');self.assertFalse(first['production_enabled'])
            path=root/'fake.pdf';path.write_text('not pdf');self.assertEqual(ingest_ir(path)['route'],'quarantine')
    def test_html_excludes_navigation_and_serializes_table(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.html';path.write_text('<nav>广告</nav><h1>菜谱</h1><p>配方</p><table><tr><th>食材</th><th>量</th></tr><tr><td>盐</td><td>2g</td></tr></table>')
            result=ingest_ir(path);self.assertEqual(result['route'],'normal')
            self.assertNotIn('广告',json.dumps(result,ensure_ascii=False));self.assertEqual(result['elements'][-1]['type'],'table')
    def test_unclosed_atomic_elements_are_quarantined(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'broken.md'
            for body in ('# 配方\n```py\nx=2', '# 配方\n$$\nx=2', r'# 配方'+ '\n'+r'\(x=2'):
                path.write_text(body)
                result=ingest_ir(path)
                self.assertEqual(result['route'],'quarantine')
                self.assertEqual(result['chunks'],[])
    def test_formula_keeps_variable_explanations_and_locator(self):
        rows=markdown_ir('# 配方\n## 计算\n按人数缩放\n\\(x=2n\\)\nn 是人数')
        chunks=chunk_ir(rows,max_tokens=60,token_counter=len)
        self.assertEqual(len(chunks),1)
        self.assertIn('按人数缩放',chunks[0]['embedding_text'])
        self.assertIn('n 是人数',chunks[0]['embedding_text'])
        self.assertEqual(chunks[0]['elements'][1]['line_start'],4)
    def test_docx_retains_headings_and_real_table_cells(self):
        import zipfile
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'recipe.docx'
            xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>菜谱</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>食材</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>用量</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>盐</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>2g</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('word/document.xml',xml)
            result=ingest_ir(path)
            self.assertEqual(result['route'],'normal')
            self.assertEqual(result['chunks'][0]['heading_path'],['菜谱'])
            self.assertEqual(result['chunks'][0]['embedding_text'],'食材: 盐；用量: 2g')
    def test_xlsx_sheets_rows_and_uncomputed_formulas_are_traceable(self):
        from openpyxl import Workbook
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'recipe.xlsx';book=Workbook();sheet=book.active;sheet.title='用量'
            sheet.append([None,None]);sheet.append(['食材','克数']);sheet.append(['盐',2]);sheet.append(['糖','=B3*2'])
            empty=book.create_sheet('空表');book.save(path);book.close()
            result=ingest_ir(path)
            self.assertEqual(result['route'],'low_quality')
            self.assertEqual(len(result['elements']),1)
            row=result['chunks'][0]['elements'][0]
            self.assertEqual(row['sheet'],'用量');self.assertEqual(row['row_range'],[3,4])
            self.assertEqual(row['formula_cells'],['B4'])
            self.assertIn('=B3*2',result['chunks'][0]['text'])
            self.assertFalse(result['production_enabled'])
    def test_bad_docx_and_missing_file_do_not_abort_ingestion(self):
        import zipfile
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'bad.docx'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('word/document.xml','<invalid')
            self.assertEqual(ingest_ir(path)['route'],'quarantine')
            self.assertEqual(ingest_ir(Path(tmp)/'missing.pdf')['route'],'quarantine')
    def test_csv_multiline_cells_preserve_raw_values(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'recipe.csv';path.write_text('食材,备注\n盐,"分两次\n加入"\n')
            row=ingest_ir(path)['elements'][0]
            self.assertEqual(row['table_rows'][1][1],'分两次\n加入')


class EvaluationMetricTests(unittest.TestCase):
    def test_metrics_use_gold_recall_and_deduplicate_ranks(self):
        result=metrics(['wrong','a','a','b'],['a','b'])
        self.assertEqual(result['recall'],1);self.assertEqual(result['mrr'],.5)
        self.assertEqual(result['precision'],.4);self.assertIsNone(metrics(['wrong'],[]))
    def test_bootstrap_deterministic_and_no_fake_significance(self):
        self.assertEqual(interval([0,1,1]),interval([0,1,1]))
        self.assertEqual(paired_test([1,1],[1,1])['p_value'],1)
        self.assertFalse(paired_test([0],[1])['significant'])
        self.assertTrue(paired_test([0]*8,[1]*8)['significant'])
    def test_cost_counts_cached_and_uncached_tokens_separately(self):
        result=llm_cost([{'prompt_tokens':100,'prompt_cache_hit_tokens':80,'completion_tokens':10}],
            {'off_peak':{'cache_hit':1,'cache_miss':2,'output':3},'peak':{'cache_hit':2,'cache_miss':4,'output':6}})
        self.assertEqual(result['tokens'],{'cache_hit':80,'cache_miss':20,'output':10})
        self.assertAlmostEqual(result['usd_lower'],.00015)
        self.assertAlmostEqual(result['usd_upper'],.0003)
    def test_prices_must_match_both_model_and_provider(self):
        snapshot={'model':'deepseek-flash'}
        report={'configuration':{'llm_model':'deepseek-flash','llm_base_url':'https://api.deepseek.com/v1'}}
        self.assertTrue(matching_prices(report,snapshot))
        report['configuration']['llm_base_url']='https://other.invalid/v1'
        self.assertFalse(matching_prices(report,snapshot))
        report['configuration'].update(llm_base_url='https://api.deepseek.com/v1',llm_model='other')
        self.assertFalse(matching_prices(report,snapshot))
    def test_large_paired_samples_finish_deterministically(self):
        result=paired_test([0]*100,[1]*100)
        self.assertEqual(result,paired_test([0]*100,[1]*100))
        self.assertEqual(result['method'],'monte_carlo_sign_flip')
        self.assertTrue(result['significant'])
        with self.assertRaises(ValueError):paired_test([0],[1,1])
    def test_failed_retrieval_remains_in_metric_denominator(self):
        rows=[{'variant':'full','case':'good','type':'direct','metrics':metrics(['a'],['a'])},
              {'variant':'full','case':'failed','type':'direct','metrics':metrics([],['a']),'failure_type':'TimeoutError'}]
        aggregate=summarize_retrieval(rows)['full']['metrics']['hit']
        self.assertEqual(aggregate['n'],2)
        self.assertEqual(aggregate['mean'],.5)
    def test_live_retrieval_failure_cannot_disappear_from_report(self):
        import evaluate_prd
        with TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'scripts').mkdir();out=root/'run';out.mkdir()
            (root/'data/processed/prd-acceptance').mkdir(parents=True)
            fixture=root/'fixture.json'
            fixture.write_text(json.dumps({'source_hashes':{},'scope':'regression','split':'dev','cases':[
                {'id':'failed','question':'蒸水蛋怎么做','type':'direct','gold':['c'],'answer_eval':False}]}))
            chunks=[{'chunk_id':'c','title':'蒸水蛋','content':'蒸水蛋','section':'操作'}]
            with patch.object(evaluate_prd,'ROOT',root),patch.object(evaluate_prd,'load_chunks',return_value=chunks),\
                 patch.object(evaluate_prd,'sha',return_value='fixed'),patch.object(evaluate_prd,'corpus_audit',return_value={'passed':True}),\
                 patch.object(evaluate_prd.subprocess,'check_output',return_value='commit'),\
                 patch.object(evaluate_prd,'run_stage',side_effect=TimeoutError):
                report=evaluate_prd.run_evaluation(True,fixture,out)
            self.assertEqual(len(report['failures']),1)
            self.assertEqual(report['aggregates']['hybrid_rerank']['metrics']['hit']['mean'],0)
            self.assertIsNone(report['rerank_significance'])
            self.assertEqual(report['rerank_comparison_coverage']['paired'],0)
    def test_eval_shares_quota_but_isolates_cache_and_restores_environment(self):
        import evaluate_prd
        import os
        with TemporaryDirectory() as tmp,patch.object(evaluate_prd,'ROOT',Path(tmp)),\
             patch.dict(os.environ,{'RECIPE_RUNTIME_DB':'existing.sqlite3','RECIPE_CACHE_NAMESPACE':'existing-cache'}),\
             patch.object(evaluate_prd,'run_evaluation',side_effect=lambda *args:dict(os.environ)):
            during=evaluate_prd.run()
            self.assertEqual(during['RECIPE_RUNTIME_DB'],'existing.sqlite3')
            self.assertNotEqual(during['RECIPE_CACHE_NAMESPACE'],'existing-cache')
            self.assertTrue(Path(during['RECIPE_USAGE_LEDGER']).is_relative_to(tmp))
            self.assertEqual(os.environ['RECIPE_RUNTIME_DB'],'existing.sqlite3')
            self.assertEqual(os.environ['RECIPE_CACHE_NAMESPACE'],'existing-cache')
    def test_fast_agent_failure_is_not_a_successful_low_latency_answer(self):
        rows=[{'seconds':.01,'result':{'status':'agent_unavailable','short_answer':'稍后再试'}},
              {'seconds':10,'result':{'status':'verified','short_answer':'有依据的回答'}},
              {'seconds':1,'failure_type':'HTTPError'},
              {'seconds':2,'result':{'status':'degraded','short_answer':'原文候选'}}]
        summary=execution_summary(rows)
        self.assertEqual(summary['failures'],2)
        self.assertEqual(summary['successful_answers'],1)
        self.assertEqual(summary['successful_p50_seconds'],10)
