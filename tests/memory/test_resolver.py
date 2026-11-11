import concurrent.futures
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from cookbuddy.ltm.models import Candidate
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.extractor import extract

class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=ProfileStore(path=Path(self.tmp.name)/'memory.db');self.n=0
    def apply(self,c,sid='s',turn=None):
        self.n+=1
        return self.s.apply('u',sid,turn or sid+':'+str(self.n),c)
    def test_r1_r2_r8_scope_rules(self):
        for text,scope,dim,value in [('我不吃香菜','global','avoid_ingredient','香菜'),('今天想吃辣的','session','spice_level','3'),('最近想少吃油','temporary','flavor','少油')]:
            candidates=extract(text);self.assertEqual(len(candidates),1)
            c=candidates[0];self.assertEqual((c.scope,c.dimension,c.value),(scope,dim,value))
            result=self.apply(c)
            self.assertEqual(result['decision'],'SESSION_ONLY' if scope=='session' else 'ADD')
        self.assertFalse(any(p.dimension=='spice_level' for p in self.s.prefs('u')))
    def test_r3_version_chain_and_r4_soft_delete(self):
        self.apply(Candidate('spice_level','1'))
        candidates=extract('我现在能吃辣了，中辣可以');self.assertEqual(len(candidates),1)
        result=self.apply(candidates[0])
        self.assertEqual(result['decision'],'UPDATE')
        history=self.s.prefs('u',('active','superseded'))
        active=next(p for p in history if p.status=='active');old=next(p for p in history if p.status=='superseded')
        self.assertEqual((active.value,active.supersedes,active.version),('2',old.id,2))
        self.apply(Candidate('spice_level','2',op='revoke'))
        self.assertFalse(self.s.prefs('u'))
        self.assertTrue(self.s.prefs('u',('deleted',)))
    def test_r5_confirmation_no_silence_expiry(self):
        self.apply(Candidate('allergy','花生','avoid','hard'))
        self.assertEqual(self.apply(Candidate('allergy','花生','avoid','hard',op='revoke'))['decision'],'ASK')
        self.assertIsNone(self.s.confirm('u','s','推荐别的菜'))
        self.assertTrue(self.s.load_profile('u')['hard_constraints'])
        self.s.confirm('u','s','保留');self.assertTrue(self.s.load_profile('u')['hard_constraints'])
        self.apply(Candidate('allergy','花生','avoid','hard',op='revoke'))
        self.s.confirm('u','s','确认');self.assertFalse(self.s.load_profile('u')['hard_constraints'])
    def test_r6_duplicates_and_r11_idempotence(self):
        c=Candidate('avoid_ingredient','香菜','avoid','hard')
        self.apply(c,turn='s:1');self.apply(c,turn='s:2');self.apply(c,turn='s:2')
        rows=self.s.prefs('u');self.assertEqual(len(rows),1);self.assertEqual(rows[0].evidence_count,2)
    def test_r7_pending_evidence_sessions_and_explicit_conflict(self):
        c=Candidate('spice_level','3',source='implicit')
        self.apply(c,'a');self.apply(c,'a')
        self.assertEqual(self.s.prefs('u')[0].status,'pending')
        self.assertEqual(self.apply(c,'b')['decision'],'UPDATE')
        self.assertEqual(self.s.prefs('u')[0].status,'active')
        self.apply(Candidate('spice_level','1'))
        self.apply(c,'c');self.apply(c,'c')
        self.assertEqual(self.apply(c,'d')['decision'],'ASK')
        self.assertEqual([p.value for p in self.s.prefs('u') if p.status=='active'],['1'])
    def test_r9_multivalued_no_conflict_and_r10_concurrency(self):
        self.apply(Candidate('avoid_ingredient','香菜','avoid','hard'))
        self.apply(Candidate('avoid_ingredient','葱','avoid','hard'))
        self.assertEqual(len(self.s.prefs('u')),2)
        def update(n):return self.s.apply('u','s','s:thread'+str(n),Candidate('spice_level',str(n)))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:list(pool.map(update,[1,2]))
        active=[p for p in self.s.prefs('u') if p.dimension=='spice_level']
        self.assertEqual(len(active),1)
        allrows=[p for p in self.s.prefs('u',('active','superseded')) if p.dimension=='spice_level']
        self.assertEqual(sorted(p.version for p in allrows),[1,2])
    def test_r12_only_user_source_and_no_audit_plaintext(self):
        for role in ('assistant','tool','document'):
            self.assertEqual(extract({'role':role,'content':'我不吃辣'}),[])
        self.apply(Candidate('allergy','花生','avoid','hard',evidence_text='我对花生过敏'))
        with self.s.connect('u') as db:
            audit=[dict(r) for r in db.execute('SELECT * FROM memory_events WHERE user_id=?',('u',)).fetchall()]
        self.assertNotIn('花生',json.dumps(audit,ensure_ascii=False))
        self.assertEqual(extract('我朋友不吃花生'),[])
        self.assertEqual(extract('我不是素食主义者'),[])
        c=extract('我对花生不过敏')[0]
        self.assertEqual((c.value,c.op),('花生','revoke'))
    def test_disabled_and_stale_epoch_cannot_read_or_write(self):
        self.apply(Candidate('flavor','清淡'))
        epoch=self.s.epoch('u');self.s.set_enabled('u',False)
        self.assertEqual(self.s.load_profile('u'),{'hard_constraints':[],'soft_prefs':[]})
        self.assertEqual(self.apply(Candidate('flavor','酸甜'))['decision'],'REJECT')
        self.s.set_enabled('u',True)
        self.assertEqual(self.s.apply('u','s','stale',Candidate('flavor','重口'),expected_epoch=epoch)['decision'],'REJECT')

    def test_temporary_idempotence_and_expired_single_value_reconfirmation(self):
        from datetime import timedelta
        from cookbuddy.ltm.models import now,stamp
        self.apply(extract('最近想少吃油')[0],turn='s:1')
        self.apply(extract('最近想少吃油')[0],turn='s:1')
        self.assertEqual(self.s.prefs('u')[0].evidence_count,1)
        self.apply(Candidate('spice_level','1',scope='temporary',valid_to=stamp(now()-timedelta(days=1))),turn='s:2')
        self.apply(Candidate('spice_level','2'),turn='s:3')
        active=next(p for p in self.s.prefs('u') if p.dimension=='spice_level')
        self.assertEqual((active.value,active.version),('2',2))

    def test_late_older_extraction_and_hard_confirmation_epoch_barrier(self):
        self.apply(Candidate('spice_level','2'),turn='s:20')
        self.apply(Candidate('spice_level','1'),turn='s:10')
        self.assertEqual(self.s.prefs('u')[0].value,'2')
        self.apply(Candidate('allergy','花生','avoid','hard'),turn='s:21')
        epoch=self.s.epoch('u')
        self.apply(Candidate('allergy','花生','avoid','hard',op='revoke'),turn='s:22')
        self.s.confirm('u','s','确认')
        result=self.s.apply('u','s','s:19',Candidate('allergy','花生','avoid','hard'),expected_epoch=epoch)
        self.assertEqual(result['decision'],'REJECT')

    def test_llm_schema_retry_and_only_recent_user_context(self):
        row={'dimension':'cuisine','value':'粤','polarity':'like','strength':'soft','source':'explicit',
             'scope':'global','op':'set','confidence':.9,'evidence':'我更偏向粤菜','valid_days':None}
        model=Mock(side_effect=[{'candidates':'invalid'},{'candidates':[row]}])
        recent=[{'role':'assistant','content':'我不吃辣'},{'role':'tool','content':'我花生过敏'},
                {'role':'user','content':'平常家常'}]
        result=extract('我更偏向粤菜',recent,model)
        self.assertEqual(model.call_count,2)
        self.assertEqual((result[0].dimension,result[0].value,result[0].confidence),('cuisine','粤',.7))
        self.assertEqual(model.call_args.args[1]['recent_user_messages'],['平常家常'])
        invalid={**row,'dimension':'password'}
        with self.assertRaises(ValueError):extract('我更偏向粤菜',model=Mock(return_value={'candidates':[invalid]}))
