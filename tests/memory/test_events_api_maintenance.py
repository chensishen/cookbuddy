import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from cookbuddy.ltm.models import Candidate,now,stamp
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.episodic_store import EpisodicStore,event_from_user
from cookbuddy.ltm.memory_routes import handle
from cookbuddy.ltm.tools_memory import call,memory_tools
from cookbuddy.ltm.maintenance import maintain,clear_user
from cookbuddy.ltm.retriever import personalize

class EventAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=ProfileStore(path=Path(self.tmp.name)/'memory.db')
        self.v=Mock();self.v.search.return_value=[]
        self.e=EpisodicStore(self.p,self.v,embedding=lambda text:[1.,0.] if '红烧肉' in text else [0.,1.])
    def test_e1_e2_e3_e4_feedback_recall_threshold_dedup(self):
        event=event_from_user('上次做的红烧肉太咸了')
        self.assertEqual(event['importance'],.8)
        first=self.e.write('u',event,'s:1')
        self.assertEqual(self.e.write('u',event,'s:2'),first)
        self.assertEqual(len(self.e.rows('u')),1)
        hits=self.e.recall('u','我上次做的红烧肉怎么调整')
        self.assertEqual(hits[0]['mem_id'],first)
        self.assertIn('太咸',hits[0]['text'])
        self.assertEqual(self.e.recall('u','面包怎么发酵'),[])
    def test_a1_owner_scoped_api_and_a2_tool_identity_injection(self):
        self.p.apply('A','s','s:1',Candidate('flavor','清淡'))
        self.p.apply('B','s','s:1',Candidate('flavor','酸甜'))
        code,result=handle(self.p,'A','GET','/memory')
        self.assertEqual([p['value'] for p in result['preferences']],['清淡'])
        with memory_tools(self.p,'A','chatA'):
            result=call('get_user_profile',{'user_id':'B','session_id':'chatB'})
        self.assertEqual(result['profile']['soft_prefs'][0]['value'],'清淡')
        with self.assertRaises(ValueError):handle(self.p,'A','PATCH','/memory/x',{'user_id':'B'})
        with self.assertRaises(KeyError):handle(self.p,'A','DELETE','/memory/'+self.p.prefs('B')[0].id)
    def test_a3_clear_receipt_rows_vector_cache_and_no_resurrection(self):
        self.p.apply('u','s','s:1',Candidate('allergy','花生','avoid','hard'))
        event=event_from_user('上次做的红烧肉太咸了');self.e.write('u',event,'s:1')
        receipt=clear_user(self.p,'u',vectors=self.v)
        self.assertTrue(receipt['cleared']);self.assertEqual(receipt['vector_deletion'],'complete')
        self.assertFalse(self.p.prefs('u'));self.assertFalse(self.e.rows('u'))
        self.v.delete.assert_called_once_with('u')
        self.assertEqual(self.p.apply('u','s','late',Candidate('flavor','清淡'),expected_epoch=0)['decision'],'REJECT')
        def cleared_during_embedding(text):
            clear_user(self.p,'u',vectors=self.v);return [1.,0.]
        e=EpisodicStore(self.p,self.v,cleared_during_embedding)
        self.assertIsNone(e.write('u',event,'race'))
        self.assertFalse(self.e.rows('u'))
    def test_a4_disabled_reads_and_writes_and_a5_audit_redaction(self):
        self.p.apply('u','s','s:1',Candidate('allergy','花生','avoid','hard'))
        handle(self.p,'u','PUT','/memory/settings',{'enabled':False})
        self.assertEqual(handle(self.p,'u','GET','/memory')[1]['preferences'],[])
        self.assertIsNone(self.e.write('u',event_from_user('做的红烧肉太咸'),'s:2'))
        with self.p.connect('u') as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM memory_events WHERE user_id=?',('u',)).fetchall()]
        self.assertNotIn('花生',json.dumps(rows,ensure_ascii=False))
    def test_patch_versions_restore_and_hard_delete_confirmation(self):
        self.p.apply('u','s','s:1',Candidate('spice_level','1'));first=self.p.prefs('u')[0]
        code,result=handle(self.p,'u','PATCH','/memory/'+first.id,{'value':'2','version':1})
        self.assertEqual(code,200);active=self.p.prefs('u')[0]
        self.assertEqual((active.version,active.value,active.supersedes),(2,'2',first.id))
        handle(self.p,'u','PATCH','/memory/'+active.id,{'restore_version':1})
        self.assertEqual(self.p.prefs('u')[0].value,'1');self.assertEqual(self.p.prefs('u')[0].version,3)
        self.p.apply('u','s','s:2',Candidate('allergy','花生','avoid','hard'))
        hard=next(p for p in self.p.prefs('u') if p.strength=='hard')
        self.assertEqual(handle(self.p,'u','DELETE','/memory/'+hard.id)[0],409)
        self.assertTrue(self.p.load_profile('u')['hard_constraints'])
        self.assertEqual(handle(self.p,'u','DELETE','/memory/'+hard.id,query={'confirm':['true']})[0],200)
        self.assertFalse(self.p.load_profile('u')['hard_constraints'])
    def test_m1_decay_archive_implicit_not_hard(self):
        self.p.apply('u','s','s:1',Candidate('flavor','清淡',source='implicit'))
        self.p.apply('u','s','s:2',Candidate('allergy','花生','avoid','hard'))
        with self.p.connect('u') as db:
            db.execute('UPDATE user_prefs SET updated_at=?,created_at=? WHERE user_id=?',(stamp(now()-timedelta(days=200)),stamp(now()-timedelta(days=200)),'u'))
        result=maintain(self.p,'u')
        self.assertEqual(result['archived'],1)
        self.assertEqual([p.dimension for p in self.p.prefs('u')],['allergy'])
    def test_m2_event_capacity_only_evicts_low_utility(self):
        self.p.apply('u','s','s:hard',Candidate('allergy','花生','avoid','hard'))
        with self.p.connect('u') as db:
            for n in range(205):
                db.execute('INSERT INTO episodic_memory(mem_id,user_id,text,type,dish_refs,importance,created_at,last_accessed) VALUES(?,?,?,?,?,?,?,?)',
                    (str(n),'u','历史'+str(n),'cooked','[]',n/205,stamp(),stamp()))
        result=maintain(self.p,'u')
        self.assertEqual(result['evicted'],5);self.assertEqual(len(self.e.rows('u')),200)
        self.assertTrue(self.p.load_profile('u')['hard_constraints'])
    def test_soft_rank_explicit_override_and_exploration(self):
        rows=[{'title':'a','description':'清淡','score':1},{'title':'b','description':'酸甜','score':1.05},{'title':'c','description':'清淡','score':1.04}]
        profile={'soft_prefs':[{'dimension':'flavor','value':'清淡'}]}
        ranked=personalize(rows,profile,k=2)
        self.assertIn('b',[r['title'] for r in ranked])
        override=personalize(rows,profile,{'flavor':'酸甜'},k=2)
        self.assertEqual(override[0]['title'],'b')

    def test_confirmation_targets_requested_record(self):
        for value in ('花生','牛奶'):self.p.apply('u','s','s:'+value,Candidate('allergy',value,'avoid','hard'))
        rows={p.value:p for p in self.p.prefs('u')}
        handle(self.p,'u','DELETE','/memory/'+rows['花生'].id)
        handle(self.p,'u','DELETE','/memory/'+rows['牛奶'].id,query={'confirm':['true']})
        self.assertEqual([p.value for p in self.p.prefs('u')],['花生'])

    def test_opposite_feedback_is_not_deduplicated(self):
        self.e.write('u',event_from_user('上次做的红烧肉太咸了'),'s:1')
        self.e.write('u',event_from_user('上次做的红烧肉很好吃'),'s:2')
        self.assertEqual(len(self.e.rows('u')),2)

    def test_compression_preserves_feedback_and_is_idempotent(self):
        with self.p.connect('u') as db:
            for n,fact in enumerate(('太咸','太淡','太辣','很好吃','太油')):
                db.execute('INSERT INTO episodic_memory(mem_id,user_id,text,type,dish_refs,importance,created_at,last_accessed,vector) VALUES(?,?,?,?,?,?,?,?,?)',
                    (str(n),'u','用户反馈红烧肉：'+fact+'。','feedback','["红烧肉"]',.8,stamp(),stamp(),'[1.0,0.0]'))
        with patch('cookbuddy.ltm.episodic_store.embed',return_value=[1.,0.]):
            # Default arguments bind at definition time, so provide a service
            # factory with an explicit deterministic embedding at this boundary.
            service=lambda prefs:EpisodicStore(prefs,self.v,lambda _:[1.,0.])
            with patch('cookbuddy.ltm.maintenance.EpisodicStore',side_effect=service):
                first=maintain(self.p,'u',compression=True)
                second=maintain(self.p,'u',compression=True)
        self.assertEqual(first['compressed'],1);self.assertEqual(second['compressed'],0)
        summaries=[r for r in self.e.rows('u') if r['type']=='summary']
        self.assertEqual(len(summaries),1)
        self.assertIn('太咸',summaries[0]['text']);self.assertIn('很好吃',summaries[0]['text'])
