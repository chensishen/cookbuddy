import json
import os
import sys
import unittest
from pathlib import Path
from urllib.request import Request
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_cookbuddy_http as http_tests
from cookbuddy.store import Store
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.models import Candidate

@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','real PostgreSQL opt-in')
class MemoryHTTPTests(unittest.TestCase):
    setUp=http_tests.CookbuddyHTTPTests.setUp
    tearDown=http_tests.CookbuddyHTTPTests.tearDown
    call=http_tests.CookbuddyHTTPTests.call
    def request(self,who,path,method,data=None):
        req=Request(self.base+path,method=method,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        with self.clients[who].open(req) as response:return response.status,json.load(response)
    def test_http_authorization_masking_versions_and_disable(self):
        self.call(0,'/api/conversations');self.call(1,'/api/conversations')
        uids=[next(c.value.split('.')[0] for c in jar if c.name=='cookbuddy_session') for jar in self.cookies]
        prefs=ProfileStore(Store());prefs.apply(uids[0],'s','s:1',Candidate('allergy','花生','avoid','hard'))
        hard=prefs.prefs(uids[0])[0]
        self.assertEqual(self.call(0,'/memory')['preferences'][0]['value'],'[敏感信息已隐藏]')
        self.assertEqual(self.call(1,'/memory')['preferences'],[])
        with self.assertRaises(HTTPError) as error:self.request(1,'/memory/'+hard.id,'DELETE')
        self.assertEqual(error.exception.code,404)
        with self.assertRaises(HTTPError) as error:self.request(0,'/memory/'+hard.id,'DELETE')
        self.assertEqual(error.exception.code,409)
        self.assertEqual(self.request(0,'/memory/'+hard.id+'?confirm=true','DELETE')[0],200)
        self.assertFalse(prefs.load_profile(uids[0])['hard_constraints'])
        self.assertEqual(self.request(0,'/api/memory/settings','PUT',{'enabled':False})[0],200)
        self.assertFalse(self.call(0,'/memory')['enabled'])
        self.assertEqual(self.call(0,'/memory/export')['preferences'],[])
        with self.assertRaises(HTTPError) as error:self.request(0,'/memory/settings','PUT',{'enabled':True,'user_id':uids[1]})
        self.assertEqual(error.exception.code,400)
        self.assertEqual(self.request(0,'/memory','DELETE')[1]['vector_deletion'],'complete')

    def test_end_session_is_owner_scoped_and_idempotently_queues_user_text(self):
        cid=self.call(0,'/api/conversations',{'title':'新对话'})['id']
        self.call(1,'/api/conversations')
        uid=next(c.value.split('.')[0] for c in self.cookies[0] if c.name=='cookbuddy_session')
        store=Store();store.append(uid,cid,'end-test','user','我喜欢清淡')
        store.append(uid,cid,'end-test','assistant','工具说用户喜欢麻辣',{'tool_text':'禁止提取'})
        with self.assertRaises(HTTPError) as error:self.request(1,'/api/conversation/end','POST',{'id':cid})
        self.assertEqual(error.exception.code,404)
        self.assertEqual(self.request(0,'/api/conversation/end','POST',{'id':cid})[1]['queued'],1)
        self.assertEqual(self.request(0,'/api/conversation/end','POST',{'id':cid})[1]['queued'],0)
        with store.connect(uid) as db:record=db.execute('SELECT record FROM cookbuddy.memory_tasks WHERE job_id=%s',('end-test',)).fetchone()
        self.assertEqual(record['record'],{})
