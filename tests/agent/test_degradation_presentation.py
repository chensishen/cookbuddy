import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from src.agent.engine import ChatEngine
from src.agent.context import AppContext
from src.api.chat_routes import create_app
from .fakes import ScriptedModel,tool_call
from .test_online import Services,configuration


class DegradationPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_generation_preserves_literal_evidence_and_explains_quality_limit(self):
        from recipe_tools import checked_chunks
        from src.agent.degrade import literal_evidence
        services=Services();original=services.call
        row=checked_chunks(services.source)[0]
        self.assertIsNotNone(literal_evidence(row))
        self.assertIsNone(literal_evidence({**row,'content':'invented'}))
        async def call(op,*args,**kwargs):
            result=await original(op,*args,**kwargs)
            if op=='search':result['rows']=[row]
            if op=='answer':result['status']='validation_failed'
            return result
        services.call=call
        engine=ChatEngine(configuration(agent={'direct':False}),services,InMemorySaver(),
            model=ScriptedModel(replies=[tool_call('search_recipes',{'query':'问题'}),'结束']))
        try:result=await engine.answer('问题',AppContext('presentation_user','invalid-answer'))
        finally:await engine.memory_queue.close()
        self.assertEqual(result['reason'],'answer_not_verified')
        self.assertIn('依据核验',result['service_notice'])
        self.assertEqual(result['outputs'][0]['result']['evidence'][0]['quote'],row['content'])

    async def test_router_failure_with_complete_local_answer_has_no_warning(self):
        services=Services();services.task={'action':'meal_plan'}
        services.nlu.update(intent='meal_plan',path='rule',errors=['circuit_open'])
        engine=ChatEngine(configuration(),services,InMemorySaver(),model=ScriptedModel())
        events=[]
        async def emit(event,data):events.append((event,data))
        try:result=await engine.answer('安排早餐',AppContext('presentation_user','session'),emit)
        finally:await engine.memory_queue.close()
        self.assertTrue(result['degraded']);self.assertEqual(result['reason'],'router_unavailable')
        self.assertFalse(result['quality_limited']);self.assertEqual(result['service_notice'],'')
        self.assertNotIn('notice',[event for event,_ in events])
        self.assertTrue(result['request_id']);self.assertGreater(result['elapsed_seconds'],0)
        self.assertTrue(any(s['phase']=='understanding' for s in result['trace']['stages']))

    async def test_unknown_fallback_has_alternative_and_request_id(self):
        services=Services();services.nlu.update(intent='unclear',answer_mode='general_chat',path='general_chat',errors=['timeout'])
        engine=ChatEngine(configuration(),services,InMemorySaver(),model=ScriptedModel())
        try:result=await engine.answer('那个弄一下',AppContext('presentation_user','unknown'))
        finally:await engine.memory_queue.close()
        self.assertTrue(result['quality_limited']);self.assertTrue(result['unsupported_in_fallback'])
        self.assertIn(result['request_id'],result['service_notice']);self.assertIn('菜名',result['service_notice'])
        self.assertNotIn('circuit_open',result['service_notice'])

    async def test_rerank_loss_explains_actual_limit(self):
        services=Services();services.degraded=True
        engine=ChatEngine(configuration(agent={'direct':False}),services,InMemorySaver(),
            model=ScriptedModel(replies=[tool_call('search_recipes',{'query':'问题'}),'结束']))
        try:result=await engine.answer('问题',AppContext('presentation_user','rerank'))
        finally:await engine.memory_queue.close()
        self.assertTrue(result['quality_limited']);self.assertEqual(result['reason'],'rerank_unavailable')
        self.assertIn('精排',result['service_notice']);self.assertTrue(result['outputs'][0]['result']['sources'])


class ResilienceHealthTests(unittest.TestCase):
    def test_health_is_sanitized_and_degraded_ratio_alert_is_configured(self):
        from service_runtime import RuntimeStore
        services=Services();engine=ChatEngine(configuration(),services,InMemorySaver(),model=ScriptedModel())
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'RECIPE_RUNTIME_DB':str(Path(tmp)/'health.sqlite')}):
            store=RuntimeStore()
            for i in range(10):store.record_request(str(i),i<3)
            with TestClient(create_app(configuration(),engine=engine,secret='test-only-session-secret-with-32-chars')) as client:
                response=client.get('/health');self.assertEqual(response.status_code,200)
                data=response.json()
            self.assertEqual(data['requests']['degraded_ratio'],.3)
            self.assertIn('degraded_ratio',data['alerts'])
            self.assertEqual(set(data['dependencies']),{'router_model','main_model','memory_model','embedding','rerank','milvus'})
            self.assertNotIn('api_key',str(data));self.assertNotIn('https://',str(data))
