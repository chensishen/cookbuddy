from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from service_runtime import request_json,request_budget,BudgetExceeded,RuntimeStore,generation_with_fallback
from recipe_tools import query_recipes,source_candidates
from ask_test_recipes import StageFailure
from recipe_agent import execute
from langchain_multi_agent import ToolCollector
from evaluate_engineering import ranking_metrics
from job_queue import JobQueue,stop_process
from serve_recipe_app import RecipeServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        raw=self.rfile.read(int(self.headers['Content-Length']))
        self.server.bodies.append(json.loads(raw))
        code,body,delay=self.server.scenarios.pop(0)
        self.server.calls+=1
        time.sleep(delay)
        self.send_response(code);self.send_header('Retry-After',getattr(self.server,'retry_after','0'));self.end_headers()
        try:self.wfile.write(json.dumps(body).encode())
        except (BrokenPipeError,ConnectionResetError):pass


class NetworkFaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'RECIPE_RUNTIME_DB':str(Path(self.tmp.name)/'runtime.sqlite')});self.env.start();self.addCleanup(self.env.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.calls=0
        self.server.bodies=[]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self):
        return Request(f'http://127.0.0.1:{self.server.server_port}/embeddings',data=json.dumps({'model':'test','input':'盐','dimensions':2}).encode(),headers={'Content-Type':'application/json'})
    def test_real_http_429_retry_and_versioned_cache(self):
        self.server.scenarios=[(429,{},0),(200,{'data':[{'embedding':[.1,.2]}]},0)]
        with request_budget(3):
            result=request_json(self.request(),phase='embedding',cache_namespace='v1')
            self.assertEqual(request_json(self.request(),phase='embedding',cache_namespace='v1'),result)
        self.assertEqual(self.server.calls,2)
        self.assertTrue(any(r['outcome']=='cache_hit' for r in RuntimeStore().metrics()))
    def test_real_http_retry_after_blocks_another_phase_without_an_http_request(self):
        self.server.retry_after='10'
        self.server.scenarios=[(429,{},0),(200,{'data':[{'embedding':[.1,.2]}]},0)]
        with self.assertRaises(HTTPError):request_json(self.request(),phase='first',attempts=1)
        with self.assertRaises(BudgetExceeded):request_json(self.request(),phase='second',timeout=.2,attempts=1)
        self.assertEqual(self.server.calls,1)
        self.assertEqual(len(self.server.scenarios),1)
        code='''import sys
sys.path.insert(0,sys.argv[1])
from urllib.request import Request
from service_runtime import request_json,BudgetExceeded
try:
    request_json(Request(sys.argv[2],data=b'{"model":"test","input":"salt","dimensions":2}'),
                 phase='child',timeout=.2,attempts=1)
except BudgetExceeded:
    sys.exit(0)
sys.exit(1)
'''
        child=subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]/'scripts'),
            self.request().full_url],capture_output=True,text=True,timeout=5)
        self.assertEqual(child.returncode,0,child.stderr)
        self.assertEqual(self.server.calls,1)
        metrics=RuntimeStore().metrics()
        self.assertTrue(any('provider_retry_after' in r.get('admission_reasons',{}) for r in metrics))
    def test_real_socket_timeout_respects_request_budget(self):
        self.server.scenarios=[(200,{'data':[{'embedding':[.1,.2]}]},.1)]
        started=time.monotonic()
        with request_budget(.03),self.assertRaises(BudgetExceeded):request_json(self.request(),timeout=.02)
        self.assertLess(time.monotonic()-started,.2)

    def test_model_timeout_preserves_parent_budget_for_source_fallback(self):
        from service_runtime import remaining
        from evaluate_quality_batch import evidence_checks
        self.server.scenarios=[(200,{'choices':[]},.3)]
        RuntimeStore()  # 数据库建表不计入用于故意触发socket超时的短预算。
        request=Request(f'http://127.0.0.1:{self.server.server_port}/chat/completions',
                        data=json.dumps({'model':'test','messages':[]}).encode(),
                        headers={'Content-Type':'application/json'})
        def failed_stage(*args):
            try:
                with request_budget(deadline=time.time()+.1):
                    request_json(request,timeout=.1,phase='generation',attempts=1)
            except TimeoutError as error:
                raise StageFailure('模型阶段超时') from error
        with request_budget(2),patch('recipe_tools.run_stage',side_effect=failed_stage),patch('recipe_fact_answer.source_fact',return_value=None):
            result=query_recipes('美式炒蛋的做法')
            self.assertGreater(remaining(),1)
        self.assertEqual(self.server.calls,1)
        self.assertEqual(result['status'],'degraded')
        self.assertEqual(result['answer_method'],'source_only_fallback')
        self.assertTrue(evidence_checks({'outputs':[{'result':result}]})['all_exact'])
    def test_invalid_embedding_never_cached(self):
        self.server.scenarios=[(200,{'data':[{'embedding':[0,0]}]},0),(200,{'data':[{'embedding':[1,2]}]},0)]
        with self.assertRaises(ValueError):request_json(self.request(),cache_namespace='v1')
        result=request_json(self.request(),cache_namespace='v1')
        self.assertEqual(self.server.calls,2);self.assertEqual(result['data'][0]['embedding'],[1,2])
    def test_real_http_generation_fallback_recomputes_body_length(self):
        self.server.scenarios=[(503,{},0)]*3+[(200,{'model':'secondary-long-name','choices':[]},0)]
        payload=json.dumps({'model':'primary','messages':[]}).encode()
        request=Request(f'http://127.0.0.1:{self.server.server_port}/chat/completions',data=payload,
                        headers={'Content-Type':'application/json','Content-Length':str(len(payload))})
        with patch.dict(os.environ,{'RECIPE_GENERATION_FALLBACK':'secondary-long-name'}),request_budget(5):
            result=generation_with_fallback(request)
        self.assertEqual(result['model'],'secondary-long-name')
        self.assertEqual(self.server.bodies[-1]['model'],'secondary-long-name')
        self.assertEqual(self.server.calls,4)


class SourceAndAgentIntegrationTests(unittest.TestCase):
    def test_duplicate_recipe_title_clarifies_and_explicit_path_selects(self):
        from recipe_tools import read_recipe
        choices=read_recipe('陈皮排骨汤')
        self.assertEqual(choices['status'],'insufficient');self.assertEqual(len(choices['sources']),2)
        self.assertEqual(choices['evidence'],[])
        selected=read_recipe(choices['sources'][0]['source'])
        self.assertEqual(selected['status'],'verified_source')
        self.assertTrue(all(r['source']==choices['sources'][0]['source'] for r in selected['sources']))
        nested=read_recipe('data/HowToCook/dishes/vegetable_dish/鸡蛋羹/微波炉鸡蛋羹.md')
        self.assertEqual(nested['status'],'verified_source')
    def test_answer_cache_keeps_full_inventory_question_and_source_hash(self):
        from recipe_tools import read_recipe
        with TemporaryDirectory() as directory,patch.dict(os.environ,{'RECIPE_ANSWER_CACHE':'1','RECIPE_RUNTIME_DB':str(Path(directory)/'cache.sqlite')}):
            original=read_recipe('蒸水蛋','操作');original['status']='verified'
            with patch('recipe_tools._query_recipes',return_value=original) as query:
                a=query_recipes('蒸水蛋怎么做，没有盐')
                b=query_recipes('蒸水蛋怎么做，没有盐')
                c=query_recipes('蒸水蛋怎么做，有盐')
            self.assertNotIn('cache_hit',a);self.assertTrue(b['cache_hit'])
            self.assertNotIn('cache_hit',c);self.assertEqual(query.call_count,2)

    def test_answer_cache_does_not_mix_execution_profiles(self):
        from recipe_tools import read_recipe
        with TemporaryDirectory() as directory,patch.dict(os.environ,{
                'RECIPE_ANSWER_CACHE':'1','RECIPE_RUNTIME_DB':str(Path(directory)/'cache.sqlite')}):
            original=read_recipe('蒸水蛋','操作');original['status']='verified'
            with patch('recipe_tools._query_recipes',return_value=original) as query:
                with patch.dict(os.environ,{'RECIPE_QA_PROFILE':'serial'}):
                    query_recipes('蒸水蛋操作原文')
                    cached=query_recipes('蒸水蛋操作原文')
                with patch.dict(os.environ,{'RECIPE_QA_PROFILE':'balanced'}):
                    fresh=query_recipes('蒸水蛋操作原文')
            self.assertTrue(cached['cache_hit']);self.assertNotIn('cache_hit',fresh)
            self.assertEqual(query.call_count,2)
    def test_generation_fallback_is_explicit_and_auth_errors_do_not_switch(self):
        request=Request('https://example.invalid/chat/completions',data=json.dumps({'model':'primary','messages':[]}).encode(),headers={'Authorization':'Bearer fake-test-key'})
        with patch.dict(os.environ,{'RECIPE_GENERATION_FALLBACK':'secondary'}):
            with patch('service_runtime.request_json',side_effect=[HTTPError(request.full_url,503,'busy',{},None),{'model':'secondary','choices':[]}]) as api:
                result=generation_with_fallback(request)
            self.assertEqual(result['model'],'secondary')
            self.assertEqual(json.loads(api.call_args.args[0].data)['model'],'secondary')
            with patch('service_runtime.request_json',side_effect=HTTPError(request.full_url,401,'bad',{},None)) as api:
                with self.assertRaises(HTTPError):generation_with_fallback(request)
                self.assertEqual(api.call_count,1)
    def test_deadline_expiry_never_starts_fallback(self):
        request=Request('https://example.invalid/chat/completions',data=b'{"model":"primary"}')
        with patch.dict(os.environ,{'RECIPE_GENERATION_FALLBACK':'secondary'}),patch('service_runtime.request_json',side_effect=BudgetExceeded()) as api:
            with self.assertRaises(BudgetExceeded):generation_with_fallback(request)
            self.assertEqual(api.call_count,1)
    def test_rerank_outage_returns_quotes_without_unverified_answer(self):
        with patch('recipe_tools.run_stage',side_effect=StageFailure('rerank unavailable')):
            result=query_recipes('蒸水蛋用什么盖住碗？')
        self.assertEqual(result['status'],'degraded')
        self.assertTrue(result['evidence']);self.assertEqual(result['answer_method'],'source_only_fallback')
        self.assertNotIn('防止水滴',result['short_answer'])
        for row in result['evidence']:
            lines=(Path(__file__).resolve().parents[1]/row['source']).read_text().splitlines()
            self.assertEqual(row['quote'],'\n'.join(lines[row['start_line']-1:row['end_line']]))
    def test_multi_source_parallelism_and_scope_guard(self):
        counts=[]
        barrier=threading.Barrier(2)
        def dispatch(name,args,progress,original_question=None):
            barrier.wait(timeout=1);counts.append(name)
            return {'status':'verified_source','short_answer':'来源','sources':[]}
        collector=ToolCollector('请同时查看基础牛奶面包酵头原文和图片',lambda _:None,dispatch)
        collector.source_bundle('基础牛奶面包','酵头')
        self.assertEqual(set(counts),{'read_recipe','find_recipe_images'})
        other=ToolCollector('蒸水蛋需要哪些调料？',lambda _:None,dispatch)
        other.source_bundle('蒸水蛋');self.assertEqual(other.outputs,[])
    def test_source_only_bundle_rag_proposal_is_normalized_and_audited(self):
        def decision(name,args):
            return {'tool_calls':[{'type':'function','id':'call','function':{'name':name,'arguments':json.dumps(args)}}]},{}
        calls=iter([decision('query_recipes',{'question':'看原文'}),decision('find_recipe_images',{'title':'基础牛奶面包酵头'})])
        result=execute('请同时查看基础牛奶面包酵头的原文和图片',model_call=lambda _:next(calls))
        self.assertEqual(result['status'],'completed')
        self.assertEqual([r['tool'] for r in result['outputs']],['read_recipe','find_recipe_images'])
        self.assertEqual(result['outputs'][0]['requested_tool'],'query_recipes')
        self.assertTrue(all(r['arguments']['section']=='酵头' for r in result['outputs']))
    def test_binary_ranking_metrics_and_complete_evidence(self):
        metrics=ranking_metrics(['noise','a','a','b'],['a','b'],3)
        self.assertEqual(metrics['recall'],1);self.assertEqual(metrics['mrr'],.5)
        self.assertTrue(metrics['all_evidence']);self.assertLess(metrics['ndcg'],1)
    def test_process_group_cancellation_stops_running_work(self):
        process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        stop_process(process);self.assertIsNotNone(process.poll())


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.token='test_access_token_1234'
        self.env=patch.dict(os.environ,{'RECIPE_JOBS_DB':str(Path(self.tmp.name)/'jobs.sqlite'),'RECIPE_ACCESS_TOKEN':self.token})
        self.env.start();self.addCleanup(self.env.stop)
        self.server=RecipeServer(('127.0.0.1',0));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close);self.base=f'http://127.0.0.1:{self.server.server_port}'
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def test_access_token_required_and_not_returned_in_status(self):
        with self.assertRaises(HTTPError) as error:urlopen(self.base+'/api/status')
        self.assertEqual(error.exception.code,401)
        with urlopen(Request(self.base+'/api/status',headers={'X-Recipe-Token':self.token})) as response:
            self.assertNotIn(self.token,response.read().decode())
        with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+'/api/status',headers={'X-Recipe-Token':'wrong'}))
        self.assertEqual(error.exception.code,401)
    def test_login_cookie_is_httponly_and_origin_checked(self):
        body=json.dumps({'token':self.token}).encode()
        with urlopen(Request(self.base+'/api/login',data=body,headers={'Content-Type':'application/json'})) as response:
            self.assertTrue(json.load(response)['ok']);cookie=response.headers['Set-Cookie']
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        with urlopen(Request(self.base+'/api/status',headers={'Cookie':cookie.split(';')[0]})) as response:self.assertEqual(response.status,200)
        with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+'/api/login',data=body,headers={'Origin':'https://outside.invalid'}))
        self.assertEqual(error.exception.code,403)
    def test_rate_limit_does_not_count_idempotent_resubmit(self):
        queue=self.server.queue
        ident,_=queue.submit({'question':'a'},'key',client='one',rate=1)
        repeated,reused=queue.submit({'question':'a'},'key',client='one',rate=1)
        self.assertTrue(reused);self.assertEqual(ident,repeated)
        from job_queue import QueueFull
        with self.assertRaises(QueueFull):queue.submit({'question':'b'},client='one',rate=1)


class CheckpointTests(unittest.TestCase):
    def test_langgraph_sqlite_survives_new_connection_and_isolates_threads(self):
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
            from langgraph.graph import StateGraph,START,END
        except ImportError:self.skipTest('需要 conda 中的 LangGraph SQLite 依赖')
        import sqlite3
        from typing import TypedDict
        class State(TypedDict):value:int
        with TemporaryDirectory() as directory:
            path=Path(directory)/'checkpoint.sqlite'
            graph=StateGraph(State);graph.add_node('increment',lambda state:{'value':state['value']+1});graph.add_edge(START,'increment');graph.add_edge('increment',END)
            with sqlite3.connect(path,check_same_thread=False) as db:
                compiled=graph.compile(checkpointer=SqliteSaver(db))
                compiled.invoke({'value':1},config={'configurable':{'thread_id':'a'}})
            with sqlite3.connect(path,check_same_thread=False) as db:
                compiled=graph.compile(checkpointer=SqliteSaver(db))
                self.assertEqual(compiled.get_state({'configurable':{'thread_id':'a'}}).values['value'],2)
                self.assertEqual(compiled.get_state({'configurable':{'thread_id':'b'}}).values,{})


if __name__=='__main__':unittest.main()
