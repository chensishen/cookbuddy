import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.extraction import knowledge_candidates, selected_memories, batches, bounded_passages
from cookbuddy.memory import MemoryService
from cookbuddy.models import ModelOutputError
from cookbuddy.store import Store


class FakeStore:
    def __init__(self):self.progress={};self.rows={};self.epoch=0
    def memory_epoch(self,uid):return self.epoch
    def extraction_progress(self,uid,job):return dict(self.progress)
    def checkpoint_extraction(self,uid,job,epoch,batch,result):
        if epoch!=self.epoch:return False
        self.progress[batch]=result;return True
    def user_message_id(self,*args):return 123
    def upsert_memory(self,uid,**row):
        if row['expected_epoch']!=self.epoch:return None
        self.rows[(row['kind'],row['key'])]=row
        return row['key']


class ExtractionBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.source='data/HowToCook/test.md';self.path=self.root/self.source
        self.path.parent.mkdir(parents=True)
        self.text='\n\n'.join(f'{i}. 保持小火加热 {i} 分钟，直到凝固后关火。' for i in range(1,14))
        self.path.write_text(self.text)
        self.record={'outputs':[{'result':{'status':'verified','evidence':[{
            'source':self.source,'quote':self.text,'start_line':1,'end_line':len(self.text.splitlines())}]}}]}
        self.patch=patch('cookbuddy.extraction.ROOT',self.root);self.patch.start()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()

    def test_all_evidence_and_exact_line_provenance_survive_bounded_batching(self):
        candidates=knowledge_candidates(self.record)
        self.assertEqual(len(candidates),13)
        self.assertEqual([len(b) for b in batches(candidates)],[6,6,1])
        for row in candidates:
            self.assertEqual('\n'.join(self.text.splitlines()[row['start_line']-1:row['end_line']]),row['text'])
        self.assertIn('13 分钟',candidates[-1]['text'])
        # Evidence entries after the previous eight-entry limit are included too.
        many={'outputs':[{'result':{'status':'verified','evidence':[
            {'source':self.source,'quote':line,'start_line':i*2+1,'end_line':i*2+1}
            for i,line in enumerate(self.text.split('\n\n'))]}}]}
        self.assertEqual(len(knowledge_candidates(many)),13)

    def test_model_returns_ids_only_and_program_fills_text_and_source(self):
        candidate=knowledge_candidates(self.record)[0]
        result=selected_memories({'memories':[{'id':candidate['id'],'confidence':.99,'importance':.7}]},
            'knowledge',[candidate],'怎么做')
        self.assertEqual(result[0]['content'],candidate['text'])
        self.assertEqual(result[0]['evidence']['source'],self.source)
        self.assertEqual(result[0]['evidence']['start_line'],1)

    def test_invalid_ids_duplicates_nan_and_echoed_text_are_failures_not_empty_memories(self):
        candidates=knowledge_candidates(self.record)
        valid={'id':'k1','confidence':.99,'importance':.7}
        for rows in [[dict(valid,id='invented')],[valid,valid],[dict(valid,confidence=float('nan'))],
                [dict(valid,quote='invented')]]:
            with self.subTest(rows=rows),self.assertRaises(ModelOutputError):
                selected_memories({'memories':rows},'knowledge',candidates,'怎么做')
        self.assertEqual(selected_memories({'memories':[]},'knowledge',candidates,'怎么做'),[])

    def test_source_changed_or_quote_outside_range_is_rejected(self):
        candidates=knowledge_candidates(self.record)
        self.path.write_text(self.text+'\nchanged')
        with self.assertRaises(ModelOutputError):
            selected_memories({'memories':[{'id':'k1','confidence':.99,'importance':.7}]},'knowledge',candidates,'怎么做')
        self.record['outputs'][0]['result']['evidence'][0]['start_line']=2
        with self.assertRaises(ModelOutputError):knowledge_candidates(self.record)

    def test_multiple_profile_fields_can_reference_one_complete_statement(self):
        question='我喜欢清淡，家里有烤箱。'
        rows=selected_memories({'memories':[
            {'id':'p1','key':key,'confidence':.99,'importance':.7} for key in ('taste','equipment')]},
            'profile',[{'id':'p1','text':question}],question)
        self.assertEqual({r['key'] for r in rows},{'taste','equipment'})
        self.assertTrue(all(r['content']==question for r in rows))

    def test_long_paragraph_keeps_every_complete_sentence_without_character_truncation(self):
        text=('只有面团完全发酵后才能进行下一步。'*90)+'最后关火。'
        pieces=list(bounded_passages(text))
        self.assertEqual(''.join(piece for _,piece in pieces),text)
        self.assertGreater(len(pieces),1)
        self.assertTrue(all(len(piece)<=1000 and piece.endswith('。') for _,piece in pieces))
        with self.assertRaises(ModelOutputError):list(bounded_passages('很'*1001))

    def test_only_failed_batch_is_called_again_success_and_empty_batches_are_checkpointed(self):
        store=FakeStore();service=MemoryService(store,object());calls=[]
        def first(prompt,data,phase,**kwargs):
            ident=data['candidates'][0]['id'];calls.append(ident)
            if ident=='k1':raise TimeoutError()
            if ident=='k7':return {'memories':[]}
            return {'memories':[{'id':ident,'confidence':.99,'importance':.7}]}
        with patch('cookbuddy.memory.json_model',side_effect=first):
            with self.assertRaises(TimeoutError):service.extract('user','job','怎么做',self.record,0)
        self.assertEqual(calls,['k1','k7','k13'])
        states=[v['status'] for v in store.progress.values()]
        self.assertIn('failed',states);self.assertIn('no_memory',states);self.assertIn('saved',states)
        with patch('cookbuddy.memory.json_model',return_value={'memories':[]}) as model:
            service.extract('user','job','怎么做',self.record,0)
        self.assertEqual(model.call_count,1)
        self.assertEqual(model.call_args.args[1]['candidates'][0]['id'],'k1')
        with patch('cookbuddy.memory.json_model') as model:
            service.extract('user','job','怎么做',self.record,0)
        model.assert_not_called()

    def test_profile_failure_does_not_block_knowledge_and_forget_during_call_blocks_write(self):
        store=FakeStore();service=MemoryService(store,object())
        def model(prompt,data,phase,**kwargs):
            if phase=='memory_extract_profile':raise TimeoutError()
            return {'memories':[{'id':data['candidates'][0]['id'],'confidence':.99,'importance':.7}]}
        with patch('cookbuddy.memory.json_model',side_effect=model):
            with self.assertRaises(TimeoutError):service.extract('user','job','我喜欢清淡',self.record,0)
        self.assertEqual(len(store.rows),3)
        store=FakeStore();service=MemoryService(store,object())
        def forget(prompt,data,phase,**kwargs):
            store.epoch=1;store.progress.clear()
            return {'memories':[{'id':'p1','key':'taste','confidence':.99,'importance':.7}]}
        with patch('cookbuddy.memory.json_model',side_effect=forget):
            service.extract('user','job','我喜欢清淡',{},0)
        self.assertEqual(store.rows,{})
        self.assertEqual(store.progress,{})


@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','requires real PostgreSQL')
class CheckpointIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store=Store();self.uid=uuid.uuid4().hex;self.job=uuid.uuid4().hex
        self.store.ensure_user(self.uid)
        # done keeps the application's background worker out of this isolated fixture.
        with self.store.connect(self.uid) as db:
            db.execute("INSERT INTO cookbuddy.memory_tasks(job_id,user_id,question,record,state) VALUES(%s,%s,'test','{}','done')",(self.job,self.uid))
    def tearDown(self):
        with self.store.connect(admin=True) as db:
            db.execute('DELETE FROM cookbuddy.memory_tasks WHERE user_id=%s',(self.uid,))
            db.execute('DELETE FROM cookbuddy.tenants WHERE id=%s',(self.uid,))
    def test_progress_is_durable_tenant_scoped_and_cleared_by_forget(self):
        self.assertTrue(self.store.checkpoint_extraction(self.uid,self.job,0,'batch',{'status':'no_memory','saved':0}))
        self.assertEqual(Store().extraction_progress(self.uid,self.job)['batch']['status'],'no_memory')
        self.assertEqual(self.store.extraction_progress(uuid.uuid4().hex,self.job),{})
        self.store.forget(self.uid,purge=True)
        self.assertEqual(self.store.extraction_progress(self.uid,self.job),{})
        self.assertFalse(self.store.checkpoint_extraction(self.uid,self.job,0,'batch',{'status':'saved'}))
        self.assertEqual(self.store.extraction_progress(self.uid,self.job),{})

    def test_restarted_service_skips_completed_empty_batch_but_retries_invalid_selection(self):
        source='data/HowToCook/dishes/breakfast/蒸水蛋.md'
        record={'outputs':[{'result':{'status':'verified','evidence':[{'source':source,
            'quote':'5. 中火烧至水开，转最小的火继续蒸 4 分钟','start_line':27,'end_line':27}]}}]}
        with patch('cookbuddy.memory.json_model',return_value={'memories':[{'id':'wrong','confidence':.99,'importance':.7}]}):
            with self.assertRaises(ModelOutputError):MemoryService(self.store,object()).extract(self.uid,self.job,'怎么做',record,0)
        progress=self.store.extraction_progress(self.uid,self.job)
        failed=[v for v in progress.values() if v['status']=='failed']
        self.assertEqual(failed[0]['error_code'],'memory_selection_invalid')
        with patch('cookbuddy.memory.json_model',return_value={'memories':[]}) as model:
            MemoryService(Store(),object()).extract(self.uid,self.job,'怎么做',record,0)
        self.assertEqual(model.call_count,1)
        with patch('cookbuddy.memory.json_model') as model:
            MemoryService(Store(),object()).extract(self.uid,self.job,'怎么做',record,0)
        model.assert_not_called()


if __name__=='__main__':unittest.main()
