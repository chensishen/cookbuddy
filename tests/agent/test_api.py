import asyncio
import hashlib
import hmac
import json
import socket
import unittest
import uuid
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from src.api.chat_routes import create_app
from src.agent.engine import ChatEngine
from src.agent.context import AppContext
from .test_online import configuration,Services
from .fakes import ScriptedModel,tool_call

SECRET='test-only-session-secret-with-32-chars'


def cookie(uid):return uid+'.'+hmac.new(SECRET.encode(),uid.encode(),hashlib.sha256).hexdigest()


class APIValidationTests(unittest.TestCase):
    def test_TL22_body_identity_ignored_and_cookie_actor_used(self):
        cfg=configuration(agent={'direct':False});services=Services()
        engine=ChatEngine(cfg,services,InMemorySaver(),model=ScriptedModel(replies=[tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束']))
        app=create_app(cfg,engine=engine,secret=SECRET)
        uid='a'*32;sid='b'*32
        with TestClient(app) as client:
            client.cookies.set('cookbuddy_session',cookie(uid))
            response=client.post('/chat',json={'session_id':sid,'message':'查看原文','user_id':'c'*32})
            self.assertEqual(response.status_code,200)
            self.assertEqual([c[2] for c in services.calls if c[0]=='detail'],[uid])
            self.assertTrue(response.text.rstrip().endswith('}'))
            self.assertLess(response.text.index('event: token'),response.text.index('event: done'))
            self.assertNotIn('未核验结论',response.text)

    def test_auth_origin_and_payload_validation(self):
        cfg=configuration();services=Services()
        engine=ChatEngine(cfg,services,InMemorySaver(),model=ScriptedModel())
        with TestClient(create_app(cfg,engine=engine,secret=SECRET)) as client:
            self.assertEqual(client.post('/chat',json={'session_id':'b'*32,'message':'问题'}).status_code,401)
            response=client.post('/sessions')
            self.assertEqual(response.status_code,200)
            self.assertIn('HttpOnly',response.headers['set-cookie'])
            self.assertEqual(client.post('/sessions',headers={'Origin':'https://other.invalid'}).status_code,403)
            self.assertEqual(client.post('/chat',json={'session_id':'bad','message':''}).status_code,422)


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import uvicorn
        self.cfg=configuration(agent={'direct':False},resilience={'concurrency':1,'queue_timeout_s':.03})
        self.services=Services();self.services.delay=.05
        self.engine=ChatEngine(self.cfg,self.services,InMemorySaver(),
            model=ScriptedModel(replies=[tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束']))
        self.app=create_app(self.cfg,engine=self.engine,secret=SECRET)
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen(32)
        self.url='http://127.0.0.1:'+str(self.sock.getsockname()[1])
        self.server=uvicorn.Server(uvicorn.Config(self.app,log_level='critical',access_log=False,lifespan='on'))
        self.server.install_signal_handlers=lambda:None
        self.task=asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(200):
            if self.server.started:break
            await asyncio.sleep(.005)
        self.assertTrue(self.server.started)

    async def asyncTearDown(self):
        self.server.should_exit=True
        await asyncio.wait_for(self.task,5)
        self.sock.close()

    async def test_TL21_network_stream_is_incremental_and_done_last(self):
        import httpx
        async with httpx.AsyncClient(base_url=self.url,cookies={'cookbuddy_session':cookie('a'*32)},trust_env=False) as client:
            async with client.stream('POST','/chat',json={'session_id':'b'*32,'message':'查看原文'}) as response:
                self.assertEqual(response.status_code,200)
                events=[]
                async for line in response.aiter_lines():
                    if line.startswith('event: '):events.append(line[7:])
                self.assertEqual(events[-1],'done')
                self.assertLess(events.index('token'),events.index('done'))
                self.assertIn('tool',events)

    async def test_TL21_disconnect_cancels_underlying_work(self):
        import httpx
        async with httpx.AsyncClient(base_url=self.url,cookies={'cookbuddy_session':cookie('a'*32)},trust_env=False) as client:
            async with client.stream('POST','/chat',json={'session_id':'b'*32,'message':'查看原文'}) as response:
                async for line in response.aiter_lines():
                    if line=='event: tool':break
            for _ in range(100):
                if not self.engine.sessions:break
                await asyncio.sleep(.01)
            self.assertFalse(self.engine.sessions)
            self.assertEqual(self.engine.semaphore._value,1)

    async def test_TL16_http_queue_timeout_is_429(self):
        import httpx
        await self.engine.semaphore.acquire()
        try:
            async with httpx.AsyncClient(base_url=self.url,cookies={'cookbuddy_session':cookie('a'*32)},trust_env=False) as client:
                response=await client.post('/chat',json={'session_id':'b'*32,'message':'查看原文'})
                self.assertEqual(response.status_code,429)
                self.assertIn('retry-after',response.headers)
        finally:self.engine.semaphore.release()
