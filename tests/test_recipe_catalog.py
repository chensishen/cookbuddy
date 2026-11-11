import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_catalog import build_catalog, catalog_page, bind_source
from recipe_tools import load_chunks, clarification
from recipe_agent import execute
from serve_recipe_app import RecipeServer
from evaluate_quality_batch import evidence_checks
from langchain_multi_agent import ToolCollector


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.chunks=load_chunks()

    def test_catalog_keeps_each_source_and_deduplicates_chunks(self):
        items=build_catalog(self.chunks)
        self.assertEqual(len(items),len({row['source'] for row in self.chunks}))
        self.assertEqual(sum(row['chunk_count'] for row in items),len(self.chunks))
        duplicates=[row for row in items if row['title']=='陈皮排骨汤']
        self.assertEqual(len(duplicates),2)
        self.assertTrue(all(row['same_title_sources']==2 for row in duplicates))
        self.assertEqual(len({row['source_label'] for row in duplicates}),2)

    def test_pages_have_no_duplicates_or_missing_sources(self):
        first=catalog_page(self.chunks,limit=50)
        all_items=[]
        for offset in range(0,first['total'],50):
            all_items.extend(catalog_page(self.chunks,offset=offset,limit=50)['items'])
        self.assertEqual(len(all_items),first['source_count'])
        self.assertEqual(len({item['source'] for item in all_items}),first['source_count'])
        self.assertEqual(catalog_page(self.chunks,offset=first['total'])['items'],[])

    def test_search_sections_and_category_do_not_search_ingredients(self):
        page=catalog_page(self.chunks,'蒸水蛋 操作',category='breakfast')
        self.assertEqual([item['title'] for item in page['items']],['蒸水蛋'])
        self.assertEqual(catalog_page(self.chunks,'不存在的菜谱XYZ')['total'],0)
        for values in ({'offset':-1},{'limit':51},{'limit':True},{'query':'x'*101},{'category':'../.env'}):
            with self.assertRaises(ValueError):catalog_page(self.chunks,**values)

    def test_choices_can_select_each_original_without_changing_user_question(self):
        question='查看陈皮排骨汤的原文'
        choices=clarification(question)['source_choices']
        for item in choices:
            result=execute(question,mode='recipe',source=item['source'])
            self.assertEqual(result['question'],question)
            self.assertEqual(result['selected_source'],item['source'])
            self.assertEqual(result['status'],'completed')
            self.assertTrue(evidence_checks(result)['all_exact'])
            self.assertTrue(all(row['source']==item['source'] for row in result['outputs'][0]['result']['sources']))

    def test_wrong_title_source_and_conflicting_existing_source_are_rejected(self):
        choices=clarification('陈皮排骨汤')['source_choices']
        for question,title,source in (
            ('蒸水蛋怎么做？','',choices[0]['source']),
            ('陈皮排骨汤怎么做？','蒸水蛋',choices[0]['source']),
            (choices[0]['source'],'',choices[1]['source']),
            ('陈皮排骨汤怎么做？','','../.env'),
            ('问'*2000,'',choices[0]['source'])):
            with self.assertRaises(ValueError):bind_source(question,title,source,self.chunks)

    def test_rag_preserves_question_constraints_and_binds_exact_source(self):
        item=clarification('陈皮排骨汤')['source_choices'][0]
        question='陈皮排骨汤没有石斛怎么办？不要猜替代品。'
        with patch('recipe_agent.dispatch',return_value={'status':'insufficient','short_answer':'待核验','sources':[]}) as dispatch:
            result=execute(question,mode='rag',source=item['source'])
        actual=dispatch.call_args.args[1]['question']
        self.assertTrue(actual.startswith(question+'\n'))
        self.assertIn(item['source'],actual)
        self.assertEqual(dispatch.call_args.kwargs['original_question'],actual)
        self.assertEqual(result['question'],question)

    def test_explicit_selected_path_is_not_misread_as_another_directory_title(self):
        source='data/HowToCook/dishes/vegetable_dish/鸡蛋羹/微波炉鸡蛋羹.md'
        self.assertIn(source,bind_source('查看'+source,'',source,self.chunks))

    def test_single_agent_restores_selected_source_after_wrong_model_title(self):
        source=clarification('陈皮排骨汤')['source_choices'][1]['source']
        question='查看陈皮排骨汤原文'
        calls=iter([({'tool_calls':[{'id':'one','type':'function','function':{
                        'name':'read_recipe','arguments':json.dumps({'title':'蒸水蛋'})}}]},{}),
                    ({'tool_calls':[]},{})])
        result=execute(question,source=source,model_call=lambda _:next(calls))
        self.assertEqual(result['question'],question)
        self.assertEqual(result['outputs'][0]['arguments']['title'],source)
        self.assertEqual(result['outputs'][0]['requested_arguments']['title'],'蒸水蛋')
        self.assertTrue(evidence_checks(result)['all_exact'])

    def test_multi_agent_collector_keeps_selected_source(self):
        source=clarification('陈皮排骨汤')['source_choices'][1]['source']
        question=bind_source('查看陈皮排骨汤原文','',source,self.chunks)
        with patch('recipe_tools.dispatch') as dispatch:
            # 使用真实collector的规范化逻辑，执行器只记录入参。
            def record(name,args,progress,original_question=None):
                dispatch(name,args,original_question=original_question)
                return {'status':'verified_source','short_answer':'来源','sources':[]}
            collector=ToolCollector(question,lambda _:None,record)
            collector.call('read_recipe',{'title':'蒸水蛋'},worker='source_media')
        self.assertEqual(dispatch.call_args.args[1]['title'],source)
        self.assertEqual(dispatch.call_args.kwargs['original_question'],question)


class CatalogHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'RECIPE_JOBS_DB':str(Path(self.tmp.name)/'jobs.sqlite'),
                                      'RECIPE_WORKERS':'0','RECIPE_ACCESS_TOKEN':'catalog-test-token-123'})
        self.env.start();self.addCleanup(self.env.stop)
        self.server=RecipeServer(('127.0.0.1',0));self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close);self.base=f'http://127.0.0.1:{self.server.server_port}'
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,path,data=None):
        headers={'X-Recipe-Token':'catalog-test-token-123'}
        if data is not None:headers['Content-Type']='application/json';data=json.dumps(data).encode()
        return Request(self.base+path,data=data,headers=headers)

    def test_card_details_require_auth_and_bind_exact_source(self):
        source=clarification('陈皮排骨汤')['source_choices'][1]['source']
        path='/api/recipe?'+urlencode({'source':source})
        with self.assertRaises(HTTPError) as error:urlopen(self.base+path)
        self.assertEqual(error.exception.code,401)
        with urlopen(self.request(path)) as response:data=json.load(response)
        self.assertEqual(data['status'],'verified_source')
        self.assertTrue(data['evidence'])
        self.assertTrue(all(r['source']==source for r in data['evidence']))
        for source in ('../.env','missing.md'):
            with self.assertRaises(HTTPError) as error:urlopen(self.request('/api/recipe?'+urlencode({'source':source})))
            self.assertEqual(error.exception.code,404)
        self.assertEqual(self.server.queue.metrics()['states'],{})

    def test_catalog_requires_auth_and_returns_distinct_duplicate_sources(self):
        with self.assertRaises(HTTPError) as error:urlopen(self.base+'/api/catalog')
        self.assertEqual(error.exception.code,401)
        with urlopen(self.request('/api/catalog?'+urlencode({'q':'陈皮排骨汤'}))) as response:page=json.load(response)
        self.assertEqual(page['total'],2)
        self.assertEqual(len({item['source'] for item in page['items']}),2)

    def test_invalid_paging_and_conflicting_source_fail_before_queue(self):
        for query in ('limit=100','offset=-1','limit=1&limit=2','unknown=1'):
            with self.assertRaises(HTTPError) as error:urlopen(self.request('/api/catalog?'+query))
            self.assertEqual(error.exception.code,400)
        source=clarification('陈皮排骨汤')['source_choices'][0]['source']
        for payload in ({'question':'蒸水蛋怎么做','source':source},
                        {'question':'陈皮排骨汤怎么做','source':'../.env'}):
            with self.assertRaises(HTTPError) as error:urlopen(self.request('/api/ask',payload))
            self.assertEqual(error.exception.code,400)
        self.assertEqual(self.server.queue.metrics()['states'],{})

    def test_selected_source_survives_persistent_queue_submission(self):
        source=clarification('陈皮排骨汤')['source_choices'][1]['source']
        payload={'question':'陈皮排骨汤缺石斛怎么办？不要猜替代品。','mode':'rag','source':source}
        with urlopen(self.request('/api/ask',payload)) as response:
            self.assertEqual(response.status,202);ident=json.load(response)['job_id']
        with self.server.queue.connect() as db:
            saved=json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(ident,)).fetchone()[0])
        self.assertEqual(saved,payload)


if __name__=='__main__':unittest.main()
