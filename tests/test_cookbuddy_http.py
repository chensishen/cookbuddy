import json
import sys
import os
import tempfile
import threading
import hashlib
import hmac
import unittest
from pathlib import Path
from unittest.mock import patch
from http.cookiejar import CookieJar
from urllib.request import Request,build_opener,HTTPCookieProcessor
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from serve_recipe_app import RecipeServer
from cookbuddy.store import Store
from cookbuddy.models import settings


class JobPollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'RECIPE_WORKERS':'0','RECIPE_JOBS_DB':str(Path(self.tmp.name)/'jobs.sqlite'),'COOKBUDDY_ENABLED':'1','RECIPE_ACCESS_TOKEN':''});self.env.start()
        self.worker=patch('serve_recipe_app.MemoryWorker');self.worker.start()
        self.secret='polling-test-secret-with-at-least-32-characters'
        self.config=patch('serve_recipe_app.cookbuddy_settings',return_value={'COOKBUDDY_ENABLED':'1','COOKBUDDY_SESSION_SECRET':self.secret});self.config.start()
        self.store=patch('serve_recipe_app.Store');self.mock_store=self.store.start().return_value
        self.server=RecipeServer(('127.0.0.1',0));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.uid='a'*32
        self.job=self.server.queue.submit({'user_id':self.uid,'question':'你好','mode':'auto'})[0]

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        self.store.stop();self.config.stop();self.worker.stop();self.env.stop();self.tmp.cleanup()

    def cookie(self,uid):
        signature=hmac.new(self.secret.encode(),uid.encode(),hashlib.sha256).hexdigest()
        return 'cookbuddy_session='+uid+'.'+signature

    def test_owned_job_remains_pollable_when_postgres_is_unavailable(self):
        self.mock_store.ensure_user.side_effect=TimeoutError('database temporarily unavailable')
        for _ in range(3):
            req=Request(self.base+'/api/job?id='+self.job,headers={'Cookie':self.cookie(self.uid)})
            with build_opener().open(req) as response:self.assertEqual(json.load(response)['state'],'queued')
        self.mock_store.ensure_user.assert_not_called()

    def test_other_signed_user_cannot_poll_or_cancel_job(self):
        for path,data in [('/api/job?id='+self.job,None),('/api/cancel',{'job_id':self.job})]:
            req=Request(self.base+path,data=json.dumps(data).encode() if data else None,
                headers={'Cookie':self.cookie('b'*32),'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as error:build_opener().open(req)
            self.assertEqual(error.exception.code,404)

    def test_invalid_signature_creates_a_different_tenant(self):
        self.mock_store.conversations.return_value=[]
        req=Request(self.base+'/api/conversations',headers={'Cookie':'cookbuddy_session='+self.uid+'.forged'})
        with build_opener().open(req) as response:
            self.assertIn('HttpOnly',response.headers['Set-Cookie'])
            self.assertNotIn(self.uid,response.headers['Set-Cookie'])
        self.mock_store.ensure_user.assert_called_once()

    def test_failed_tenant_creation_does_not_issue_a_session_cookie(self):
        self.mock_store.ensure_user.side_effect=[TimeoutError('database temporarily unavailable'),None]
        self.mock_store.conversations.return_value=[]
        jar=CookieJar();client=build_opener(HTTPCookieProcessor(jar))
        with self.assertRaises(HTTPError) as error:client.open(self.base+'/api/conversations')
        self.assertEqual(error.exception.code,503)
        self.assertIsNone(error.exception.headers.get('Set-Cookie'))
        self.assertEqual(len(jar),0)
        with client.open(self.base+'/api/conversations') as response:self.assertEqual(response.status,200)
        self.assertEqual(len(jar),1)

@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','real PostgreSQL integration opt-in')
class CookbuddyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'RECIPE_WORKERS':'0','RECIPE_JOBS_DB':str(Path(self.tmp.name)/'jobs.sqlite'),'COOKBUDDY_ENABLED':'1','RECIPE_ACCESS_TOKEN':''});self.env.start()
        self.worker=patch('serve_recipe_app.MemoryWorker');self.worker.start()
        self.server=RecipeServer(('127.0.0.1',0));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port);self.cookies=[CookieJar(),CookieJar()];self.clients=[build_opener(HTTPCookieProcessor(jar)) for jar in self.cookies]
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        users=[cookie.value.split('.')[0] for jar in self.cookies for cookie in jar if cookie.name=='cookbuddy_session']
        with Store().connect(admin=True) as db:
            for uid in users:
                for table in ('messages','conversations','outbox','memory_revisions','memories','memory_tasks','intent_audit'):
                    db.execute(f'DELETE FROM cookbuddy.{table} WHERE user_id=%s',(uid,))
                db.execute('DELETE FROM cookbuddy.tenants WHERE id=%s',(uid,))
        self.worker.stop();self.env.stop();self.tmp.cleanup()
    def call(self,who,path,data=None):
        req=Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        with self.clients[who].open(req) as r:return json.load(r)
    def test_conversation_and_job_are_owner_scoped(self):
        cid=self.call(0,'/api/conversations',{'title':'隔离验收'})['id'];self.call(1,'/api/conversations')
        with self.assertRaises(HTTPError) as error:self.call(1,'/api/conversation?id='+cid)
        self.assertEqual(error.exception.code,404)
        job=self.call(0,'/api/ask',{'question':'看蒸水蛋原文','mode':'auto','conversation_id':cid})['job_id']
        self.assertEqual(self.call(0,'/api/job?id='+job)['state'],'queued')
        with self.assertRaises(HTTPError) as error:self.call(1,'/api/job?id='+job)
        self.assertEqual(error.exception.code,404)
        with self.assertRaises(HTTPError):self.call(1,'/api/cancel',{'job_id':job})
        self.assertTrue(self.call(0,'/api/cancel',{'job_id':job})['cancelled'])
    def test_client_cannot_inject_user_identity(self):
        cid=self.call(0,'/api/conversations',{'title':'参数验收'})['id']
        with self.assertRaises(HTTPError) as error:self.call(0,'/api/ask',{'question':'你好','mode':'auto','conversation_id':cid,'user_id':'a'*32})
        self.assertEqual(error.exception.code,400)
    def test_history_is_server_persisted_not_browser_storage(self):
        cid=self.call(0,'/api/conversations',{'title':'新对话'})['id']
        self.call(0,'/api/ask',{'question':'蒸水蛋怎么做','mode':'auto','conversation_id':cid})
        result=self.call(0,'/api/conversation?id='+cid)
        self.assertEqual(result['messages'][0]['content'],'蒸水蛋怎么做')
        self.assertEqual(self.call(0,'/api/conversations')[0]['title'],'蒸水蛋怎么做')
        cookie=next(iter(self.cookies[0]))
        self.assertIn('HttpOnly',cookie._rest)
        self.assertEqual(len(cookie.value.split('.')[1]),64)
    def test_duplicate_submission_does_not_duplicate_turn(self):
        cid=self.call(0,'/api/conversations',{'title':'新对话'})['id'];payload={'question':'你好','mode':'auto','conversation_id':cid}
        ids=[]
        for _ in range(2):
            req=Request(self.base+'/api/ask',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Idempotency-Key':'cookbuddy-idempotency-test'})
            with self.clients[0].open(req) as r:ids.append(json.load(r)['job_id'])
        self.assertEqual(ids[0],ids[1]);self.assertEqual(len(self.call(0,'/api/conversation?id='+cid)['messages']),1)

if __name__=='__main__':unittest.main()
