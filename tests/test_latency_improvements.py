import io
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from service_runtime import RuntimeStore,guarded_call,request_budget,AdmissionTimeout,BudgetExceeded,request_json
from cookbuddy.router import IntentRouter,local_recipe_intent,DEFAULT_INTENTS
from evaluate_prd import answer_variants,code_fingerprint


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=RuntimeStore(Path(self.tmp.name)/'runtime.sqlite3')

    def test_quota_rejection_reports_reason_and_returns_without_api_or_circuit_failure(self):
        for reason,limits in [('tpm',{'RECIPE_MODEL_TPM':'40'}),('rpm',{'RECIPE_MODEL_RPM':'1'}),
                              ('concurrency',{'RECIPE_MODEL_CONCURRENCY':'1'})]:
            with self.subTest(reason=reason):
                ident=self.store.acquire(reason,tokens=40)
                if reason!='concurrency':self.store.release(ident,reason,True)
                called=[];started=time.monotonic()
                with patch.dict(os.environ,limits),request_budget(2):
                    with self.assertRaises(AdmissionTimeout) as caught:
                        guarded_call(lambda _:called.append(1),phase=reason,scope=reason,tokens=1,
                                     admission_timeout=.03,store=self.store)
                self.assertEqual(caught.exception.reason,reason)
                self.assertFalse(called);self.assertLess(time.monotonic()-started,.3)
                events=[e for e in self.store.events_since() if e['phase']==reason]
                self.assertEqual(events[-1]['reason'],reason)
                self.assertEqual(events[-1]['outcome'],'admission_timeout')
                with self.store.connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM circuits WHERE failures>0').fetchone()[0],0)

    def test_cancellation_is_not_hidden_by_optional_admission_timeout(self):
        with request_budget(2,cancelled=lambda:True),self.assertRaises(BudgetExceeded):
            guarded_call(lambda _:True,phase='cancel',store=self.store,admission_timeout=.1)

    def test_short_admission_cap_does_not_shorten_admitted_api_budget(self):
        waits=[]
        with request_budget(2):
            guarded_call(lambda wait:waits.append(wait),phase='api',timeout=1,store=self.store,admission_timeout=.03)
        self.assertGreater(waits[0],.5)

    def test_old_event_schema_migrates_without_losing_rows(self):
        path=Path(self.tmp.name)/'old.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,created REAL,phase TEXT,model TEXT,attempt INTEGER,elapsed REAL,outcome TEXT,code INTEGER)')
            db.execute("INSERT INTO events VALUES(1,0,'old','test',1,.1,'ok',NULL)")
        store=RuntimeStore(path)
        self.assertEqual(store.events_since()[0]['phase'],'old')
        store.event('new','test',1,.2,'admission_timeout',reason='tpm')
        self.assertEqual(store.events_since(1)[0]['reason'],'tpm')

    def test_cache_namespace_isolated_but_admission_accounting_still_shared(self):
        request=Request('https://example.invalid/chat',data=b'{"model":"test"}')
        with patch('service_runtime.urlopen',side_effect=lambda *a,**k:io.StringIO('{"ok":true}')) as network,\
             patch('service_runtime.RuntimeStore',return_value=self.store):
            for namespace in ('a','a','b'):
                with patch.dict(os.environ,{'RECIPE_CACHE_NAMESPACE':namespace}):
                    self.assertTrue(request_json(request,cache_namespace='version')['ok'])
        self.assertEqual(network.call_count,2)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],2)

    def test_rerank_local_queue_timeout_preserves_candidates(self):
        from rerank_test_recipes import bounded_rerank
        with patch('rerank_test_recipes.request_json',side_effect=AdmissionTimeout('tpm')):
            result,reason=bounded_rerank(None,[{},{}])
        self.assertEqual(reason,'admission_tpm')
        self.assertEqual([r['index'] for r in result['results']],[0,1])
        for value in ('nan','0','9'):
            with patch.dict(os.environ,{'RAG_RERANK_ADMISSION_SECONDS':value}),self.assertRaises(ValueError):
                bounded_rerank(None,[{}])


class LocalRoutingTests(unittest.TestCase):
    def setUp(self):
        rows=[{'id':i,'description':d,'examples':e,'target':t,'version':1} for i,d,e,t in DEFAULT_INTENTS]
        self.store=type('Registry',(),{'registry':lambda self:rows})()
        self.chunks=[{'title':'蒸水蛋','source':'data/HowToCook/dishes/vegetable_dish/蒸水蛋.md'}]

    def test_named_fact_and_inventory_need_no_intent_model(self):
        for question in ('蒸水蛋盐放多少？','我有鸡蛋和番茄，推荐3道素菜，难度不超过2星'):
            with patch('recipe_tools.load_chunks',return_value=self.chunks),\
                 patch('cookbuddy.router.json_model',side_effect=AssertionError('unnecessary model call')),\
                 patch('cookbuddy.router.embed',side_effect=AssertionError('unnecessary embedding')):
                decision=IntentRouter(self.store).route(question)
            self.assertEqual(decision['target'],'rag')
            self.assertEqual(decision['route_method'],'local_single_recipe_task')

    def test_complex_media_memory_unknown_and_multiple_recipes_still_use_router(self):
        chunks=self.chunks+[{'title':'红烧肉','source':'other'}]
        for question in ('同时查看蒸水蛋原文和图片','蒸水蛋和红烧肉分别怎么做','按我的偏好做蒸水蛋',
                         '上次那道菜怎么做','帮我安排一餐','帮我弄一下','烤箱怎么用'):
            with self.subTest(question=question),patch('recipe_tools.load_chunks',return_value=chunks):
                self.assertIsNone(local_recipe_intent(question))

    def test_variant_order_is_deterministic_and_counterbalanced(self):
        orders=[answer_variants(str(i)) for i in range(10)]
        self.assertEqual(orders,[answer_variants(str(i)) for i in range(10)])
        self.assertEqual({o[2:] for o in orders},{('full_agent','fixed_workflow'),('fixed_workflow','full_agent')})

    def test_fingerprint_includes_nested_router_changes(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'scripts/cookbuddy/router.py';path.parent.mkdir(parents=True)
            path.write_text('before');before=code_fingerprint(root)
            path.write_text('after');self.assertNotEqual(before,code_fingerprint(root))

    def test_agent_records_planning_and_tool_durations(self):
        from recipe_agent import execute
        message={'tool_calls':[{'id':'call_1','type':'function','function':{'name':'query_recipes','arguments':'{"question":"蒸水蛋盐放多少"}'}}]}
        with patch('recipe_agent.dispatch',return_value={'status':'verified','short_answer':'有依据的回答','sources':[]}):
            record=execute('蒸水蛋盐放多少',model_call=lambda _:(message,{}))
        self.assertGreaterEqual(record['model_calls'][0]['elapsed_seconds'],0)
        self.assertGreaterEqual(record['steps'][0]['seconds'],0)

    def test_reviews_default_to_json_without_thinking_and_keep_opt_in(self):
        from generate_test_answer import review_request_parameters
        from llm_config import chat_parameters,llm_settings
        settings=llm_settings({'LLM_BASE_URL':'https://api.siliconflow.cn/v1','LLM_MODEL':'test'})
        original={'enable_thinking':True,'thinking_budget':1024,'max_tokens':2500}
        with patch.dict(os.environ,{'RECIPE_REVIEW_THINKING':'0'}):
            result=chat_parameters(review_request_parameters(original),settings)
        self.assertFalse(result['enable_thinking']);self.assertNotIn('thinking_budget',result)
        self.assertEqual(result['response_format'],{'type':'json_object'})
        self.assertEqual(result['max_tokens'],2048)
        self.assertTrue(original['enable_thinking'])
        with patch.dict(os.environ,{'RECIPE_REVIEW_THINKING':'1'}):
            result=chat_parameters(review_request_parameters(original),settings)
        self.assertTrue(result['enable_thinking']);self.assertNotIn('response_format',result)
