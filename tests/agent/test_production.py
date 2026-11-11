import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import httpx
import uvicorn

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from serve_http_pool import create_app,origin


class PoolLifecycleTests(unittest.TestCase):
    def test_real_process_socket_is_private_and_shutdown_reaps_it(self):
        import stat
        from src.retrieval.http_pool import NetworkPool
        previous=os.environ.get('RECIPE_HTTP_POOL_SOCKET')
        with NetworkPool() as pool:
            self.assertEqual(stat.S_IMODE(Path(pool.socket).stat().st_mode),0o600)
            self.assertIsNone(pool.process.poll())
        self.assertIsNotNone(pool.process.poll())
        self.assertFalse(Path(pool.socket).exists())
        self.assertEqual(os.environ.get('RECIPE_HTTP_POOL_SOCKET'),previous)


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections=0;self.headers=[];self.slow=asyncio.Event();self.disconnected=asyncio.Event()
        self.writers=set()
        async def upstream(reader,writer):
            self.connections+=1;self.writers.add(writer)
            try:
                while True:
                    head=await reader.readuntil(b'\r\n\r\n')
                    self.headers.append(head.decode())
                    length=next((int(line.split(b':',1)[1]) for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:')),0)
                    await reader.readexactly(length)
                    if b'/slow ' in head:
                        self.slow.set();await reader.read();self.disconnected.set();return
                    status=b'429 Too Many Requests' if b'/limited ' in head else b'200 OK'
                    writer.write(b'HTTP/1.1 '+status+b'\r\nContent-Length: 2\r\nSet-Cookie: private=first\r\nRetry-After: 7\r\n\r\n{}')
                    await writer.drain()
            except (asyncio.IncompleteReadError,ConnectionError):pass
            finally:writer.close();self.writers.discard(writer)
        self.upstream=await asyncio.start_server(upstream,'127.0.0.1',0)
        self.url='http://127.0.0.1:'+str(self.upstream.sockets[0].getsockname()[1])
        self.tmp=tempfile.TemporaryDirectory(prefix='cb-pool-',dir='/tmp');self.socket=self.tmp.name+'/p.sock'
        self.server=uvicorn.Server(uvicorn.Config(create_app({origin(self.url)}),uds=self.socket,
                                                  log_level='critical',access_log=False))
        self.task=asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.task.done():await self.task
            await asyncio.sleep(.01)

    async def asyncTearDown(self):
        self.server.should_exit=True;await self.task
        self.upstream.close();await self.upstream.wait_closed()
        for writer in list(self.writers):writer.close()
        self.tmp.cleanup()

    def payload(self,path='/',auth='one'):
        return {'url':self.url+path,'method':'POST','headers':{'Authorization':'Bearer '+auth},
                'body':base64.b64encode(b'{}').decode(),'timeout':2}

    async def test_separate_clients_reuse_upstream_connection_without_cookies(self):
        for auth in ('one','two'):
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket)) as client:
                result=(await client.post('http://pool/request',json=self.payload(auth=auth))).json()
                self.assertEqual(result['status'],200)
        self.assertEqual(self.connections,1)
        self.assertIn('Bearer two',self.headers[1])
        self.assertNotIn('Cookie:',self.headers[1])

    async def test_disconnect_closes_inflight_upstream(self):
        reader,writer=await asyncio.open_unix_connection(self.socket)
        body=json.dumps(self.payload('/slow')).encode()
        writer.write(b'POST /request HTTP/1.1\r\nHost: pool\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        await writer.drain();await asyncio.wait_for(self.slow.wait(),2)
        writer.close();await writer.wait_closed()
        await asyncio.wait_for(self.disconnected.wait(),2)

    async def test_worker_exit_preserves_shared_upstream_connection(self):
        code="from urllib.request import Request; from pooled_http import urlopen; import sys; response=urlopen(Request(sys.argv[1],data=b'{}')); response.read(); response.close()"
        env=dict(os.environ,RECIPE_HTTP_POOL_SOCKET=self.socket,PYTHONPATH=str(ROOT/'scripts'))
        for _ in range(2):
            process=await asyncio.create_subprocess_exec(sys.executable,'-c',code,self.url,
                env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await process.communicate()
            self.assertEqual(process.returncode,0,err.decode()[-400:])
        self.assertEqual(self.connections,1)

    async def test_urllib_adapter_preserves_retry_after_and_origin_allowlist(self):
        from pooled_http import urlopen
        from urllib.request import Request
        from urllib.error import HTTPError
        def call():
            with patch.dict(os.environ,{'RECIPE_HTTP_POOL_SOCKET':self.socket}):
                with self.assertRaises(HTTPError) as raised:urlopen(Request(self.url+'/limited',data=b'{}'))
                self.assertEqual(raised.exception.code,429)
                self.assertEqual(raised.exception.headers.get('Retry-After'),'7')
        await asyncio.to_thread(call)
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket)) as client:
            payload=self.payload();payload['url']='http://127.0.0.1:1/private'
            self.assertEqual((await client.post('http://pool/request',json=payload)).status_code,403)


@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','requires local PostgreSQL')
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_recovery_and_rls_rejects_foreign_thread(self):
        import uuid
        from langgraph.checkpoint.base import empty_checkpoint
        from src.agent.checkpoints import checkpoint_store,database_settings
        from src.agent.config import Config
        from src.retrieval.session import checkpoint_thread
        cfg=Config.model_validate({'checkpoint':{'backend':'postgres'}})
        thread=uuid.uuid4().hex;key={'configurable':{'thread_id':thread,'checkpoint_ns':''}}
        token=checkpoint_thread.set(thread)
        try:
            async with checkpoint_store(cfg) as saver:
                checkpoint=empty_checkpoint();checkpoint['channel_values']={'result':'verified'}
                await saver.aput(key,checkpoint,{'source':'input','step':0,'parents':{}},{})
            # An independent process and pool must see the committed checkpoint.
            code="""import asyncio,json,sys
from src.agent.config import Config
from src.agent.checkpoints import checkpoint_store
from src.retrieval.session import checkpoint_thread
async def main():
 checkpoint_thread.set(sys.argv[1])
 async with checkpoint_store(Config.model_validate({'checkpoint':{'backend':'postgres'}})) as saver:
  result=await saver.aget({'configurable':{'thread_id':sys.argv[1],'checkpoint_ns':''}})
  print(json.dumps(result['channel_values']))
asyncio.run(main())"""
            process=await asyncio.create_subprocess_exec(sys.executable,'-c',code,thread,cwd=ROOT,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await process.communicate()
            self.assertEqual(process.returncode,0,err.decode()[-400:])
            self.assertEqual(json.loads(out),{'result':'verified'})
            checkpoint_thread.set('another-user-thread')
            async with checkpoint_store(cfg) as saver:self.assertIsNone(await saver.aget(key))
            from psycopg import AsyncConnection
            async with await AsyncConnection.connect(database_settings()['COOKBUDDY_DATABASE_URL'],autocommit=True) as conn:
                row=await (await conn.execute('SELECT count(*) FROM cookbuddy_checkpoint.checkpoints WHERE thread_id=%s',(thread,))).fetchone()
                self.assertEqual(row[0],0)
        finally:
            checkpoint_thread.reset(token)
            async with await AsyncConnection.connect(database_settings()['COOKBUDDY_ADMIN_DATABASE_URL'],autocommit=True) as conn:
                for table in ('checkpoints','checkpoint_blobs','checkpoint_writes'):
                    await conn.execute('DELETE FROM cookbuddy_checkpoint.'+table+' WHERE thread_id=%s',(thread,))

    async def test_postgres_lock_blocks_other_process_and_releases_on_cancel(self):
        from src.agent.config import Config
        from src.agent.context import AppContext
        from src.retrieval.pipeline import RecipeServices
        cfg=Config.model_validate({'checkpoint':{'backend':'postgres'}})
        services=RecipeServices(cfg);ctx=AppContext('postgres-lock-test','s')
        ready=asyncio.Event()
        async def hold():
            async with services.session_lock(ctx,1):ready.set();await asyncio.Event().wait()
        task=asyncio.create_task(hold());await ready.wait()
        try:
            code="""import asyncio
from src.agent.config import Config
from src.agent.context import AppContext
from src.retrieval.pipeline import RecipeServices
async def main():
 try:
  async with RecipeServices(Config.model_validate({'checkpoint':{'backend':'postgres'}})).session_lock(AppContext('postgres-lock-test','s'),.05):raise AssertionError('lock not held')
 except TimeoutError:print('bounded')
asyncio.run(main())"""
            process=await asyncio.create_subprocess_exec(sys.executable,'-c',code,cwd=ROOT,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await process.communicate();self.assertEqual(process.returncode,0,err.decode()[-400:]);self.assertEqual(out.strip(),b'bounded')
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        async with services.session_lock(ctx,.2):pass
