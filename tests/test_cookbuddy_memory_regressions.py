import json
import os
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.memory import MemoryService
from cookbuddy.store import Store


def completed_record():
    return {'status':'completed','outputs':[{'result':{'status':'verified',
        'short_answer':'蒸至凝固。','evidence':[{'source':'data/HowToCook/dishes/breakfast/蒸水蛋.md','quote':'5. 中火烧至水开，转最小的火继续蒸 4 分钟','start_line':27,'end_line':27}]}}]}


class TaskWorkerTests(unittest.TestCase):
    def test_extraction_failure_keeps_saved_task_and_reports_retryable_failure(self):
        from cookbuddy.worker import run_once
        store=Mock();store.memory_epoch.return_value=0
        store.connect=MagicMock()
        store.connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value=[]
        row={'user_id':'test','job_id':'job','epoch':0,'question':'怎么做','record':completed_record(),'attempts':1}
        with patch('cookbuddy.ltm.worker.process_memory_task',side_effect=ValueError('extraction_failed')) as process, \
                patch('cookbuddy.worker.claim',side_effect=[row,None]), \
                patch('cookbuddy.worker.finish') as finish:
            counts=run_once(store)
        process.assert_called_once_with(store,row)
        finish.assert_called_once()
        self.assertIsInstance(finish.call_args.args[3],ValueError)
        self.assertEqual(counts['errors'],1)

    def test_stale_epoch_skips_both_task_save_and_extraction(self):
        from cookbuddy.worker import run_once
        store=Mock();store.memory_epoch.return_value=1
        row={'user_id':'test','job_id':'job','epoch':0,'question':'怎么做','record':completed_record(),'attempts':1}
        with patch('cookbuddy.ltm.worker.process_memory_task') as service, \
                patch('cookbuddy.worker.claim',side_effect=[row,None]),patch('cookbuddy.worker.finish'):
            run_once(store)
        service.assert_not_called()


class ContextBudgetTests(unittest.TestCase):
    def test_budget_includes_full_serialized_prompt_and_keeps_complete_facts(self):
        class FakeStore:
            def active(self,*args):
                return [dict(id=str(i),kind='profile',version=1,content='我喜欢'+str(i)+'清淡口味'*190,
                    evidence={'quote':'很长的逐字证据'*160,'origin':'user_message'},status='active',expires_at=None) for i in range(8)]
            @contextmanager
            def connect(self,*args):
                class DB:
                    def execute(self,*args):pass
                yield DB()
        class NoVectors:
            def search(self,*args):return []
        with patch('cookbuddy.memory.embed',return_value=[1]),patch('cookbuddy.memory.rerank',return_value=[]):
            result=MemoryService(FakeStore(),NoVectors()).retrieve(uuid.uuid4().hex,'推荐一道菜')
        serialized=json.dumps(result['items'],ensure_ascii=False)
        self.assertLessEqual(len(serialized),3500)
        self.assertEqual(result['characters'],len(serialized))
        self.assertTrue(result['items'])
        self.assertTrue(all(item['content'].endswith('清淡口味') for item in result['items']))


class ContextExpansionTests(unittest.TestCase):
    def test_context_is_available_for_all_expressions_without_a_keyword_gate(self):
        from src.nlu.router import _context
        from cookbuddy.session import SessionState
        session=SessionState('u','s');session.slots['ingredients_on_hand']=['鸡蛋']
        session.slots['selected_dish']={'dish_name':'基础牛奶面包','source_path':'bread.md'}
        result=_context({'slots':session.slots,'summary':'用户刚才选了面包','messages':[{'role':'user','content':'之前的问题'}]})
        self.assertEqual(result['slots']['selected_dish']['dish_name'],'基础牛奶面包')
        self.assertEqual(result['slots']['ingredients_on_hand'],['鸡蛋'])
        self.assertEqual(result['summary'],'用户刚才选了面包')
        self.assertTrue(result['messages'])

    def test_named_original_does_not_take_a_model_invented_previous_source(self):
        from test_nlu_regressions import prepared
        plan=prepared('查看基础牛奶面包的完整原文和图片。')
        self.assertEqual(plan['named_recipe']['dish_name'],'基础牛奶面包')
        self.assertEqual(plan['nlu']['slots']['request_kind'],'bundle')


@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','requires real PostgreSQL')
class MemoryOrderingTests(unittest.TestCase):
    def setUp(self):
        self.s=Store();self.uid=uuid.uuid4().hex;self.s.ensure_user(self.uid)
        self.cid=self.s.create_conversation(self.uid,'隔离的记忆回归测试')
    def tearDown(self):
        with self.s.connect(admin=True) as db:
            for table in ('messages','conversations','outbox','memory_revisions','memories','memory_tasks','intent_audit'):
                db.execute(f'DELETE FROM cookbuddy.{table} WHERE user_id=%s',(self.uid,))
            db.execute('DELETE FROM cookbuddy.tenants WHERE id=%s',(self.uid,))
    def test_human_preference_sequence_across_conversations(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from cookbuddy.ltm.models import CONFIG_OVERRIDE
        cfg=load_config().model_dump();cfg['memory']['mode']='M6'
        old=CONFIG_OVERRIDE.get()
        try:
            worker.configure(cfg)
            def prepare(q,cid,job=None):
                return worker.prepare(dict(user_id=self.uid,session_id=cid,request_id=job or uuid.uuid4().hex,question=q))
            first=prepare('请记住：我喜欢清淡口味。',self.cid)
            repeated=prepare('请记住：我喜欢清淡口味。',self.cid)
            for result in (first,repeated):self.assertIn('清淡',result['management']['message'])
            other=self.s.create_conversation(self.uid,'新会话偏好回忆')
            recall=prepare('我之前告诉过你什么口味偏好？',other)
            self.assertIn('口味：清淡',recall['management']['message'])
            job=uuid.uuid4().hex
            selected=prepare('按我的口味推荐两道菜，分别说明推荐理由。',other,job)
            self.assertEqual(selected['task']['state']['extra_constraints'],[])
            result=worker.execute(dict(operation='task_recommend',arguments={'state':selected['task']['state']},
                user_id=self.uid,session_id=other,request_id=job,hard=selected['hard'],config=cfg))
            self.assertEqual(result['status'],'verified')
            self.assertEqual(len(result['recommendations']),2)
            self.assertTrue(any('清淡' in row['reason'] for row in result['recommendations']))
        finally:CONFIG_OVERRIDE.set(old)

    def test_inventory_is_short_term_and_taste_changes_are_long_term(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from cookbuddy.ltm.models import CONFIG_OVERRIDE
        from cookbuddy.ltm.profile_store import ProfileStore
        cfg=load_config().model_dump();cfg['memory']['mode']='M6'
        old=CONFIG_OVERRIDE.get();prefs=ProfileStore(self.s)
        try:
            worker.configure(cfg)
            def prepare(q,cid):
                return worker.prepare(dict(user_id=self.uid,session_id=cid,request_id=uuid.uuid4().hex,question=q))
            stock=prepare('我有番茄和鸡蛋',self.cid)
            self.assertEqual(stock['slots']['ingredients_on_hand'],['番茄','鸡蛋'])
            self.assertIn('不会保存为长期',stock['management']['message'])
            recall=prepare('我有什么食材',self.cid)
            self.assertIn('番茄、鸡蛋',recall['management']['message'])
            selected=prepare('按现有食材推荐两道菜',self.cid)
            self.assertEqual(selected['task']['state']['ingredients'],['番茄','鸡蛋'])
            self.assertEqual(selected['task']['state']['extra_constraints'],[])
            self.assertFalse(any(p.dimension=='inventory' for p in prefs.prefs(self.uid)))
            replaced=prepare('我现在有豆腐',self.cid)
            self.assertEqual(replaced['slots']['ingredients_on_hand'],['豆腐'])
            prepare('我喜欢清淡口味',self.cid)
            prepare('我不吃香菜',self.cid)
            relaxing=prepare('我现在可以吃香菜了',self.cid)
            self.assertTrue(relaxing['management']['needs_confirmation'])
            prepare('保留',self.cid)
            other=self.s.create_conversation(self.uid,'检查短期与长期隔离')
            blank=prepare('我有什么食材？',other)
            self.assertIn('还没有记录',blank['management']['message'])
            missing=prepare('按现有食材推荐两道菜',other)
            self.assertEqual(missing['task']['action'],'clarify')
            long=prepare('我的饮食偏好有哪些？',other)
            self.assertIn('清淡',long['management']['message']);self.assertIn('不吃香菜',long['management']['message'])
            changed=prepare('我的口味改成重口味了',other)
            self.assertIn('重口',changed['management']['message'])
            third=self.s.create_conversation(self.uid,'检查更新后的长期口味')
            long=prepare('我的口味偏好是什么？',third)
            self.assertIn('重口',long['management']['message']);self.assertNotIn('清淡',long['management']['message'])
            self.assertTrue(any(p['value']=='香菜' for p in prefs.load_profile(self.uid)['hard_constraints']))
            prepare('今天想吃清淡口味',third)
            self.assertEqual([p['value'] for p in prefs.load_profile(self.uid)['soft_prefs'] if p['dimension']=='flavor'],['重口'])
        finally:CONFIG_OVERRIDE.set(old)
    def test_legacy_recipe_scope_repair_is_audited_and_idempotent(self):
        from repair_recipe_preference_scope import repair,QUESTION,REASON
        from cookbuddy.ltm.profile_store import ProfileStore
        from cookbuddy.ltm.models import Candidate
        prefs=ProfileStore(self.s)
        self.s.append(self.uid,self.cid,'scope-repair','user',QUESTION)
        turn=self.cid+':'+str(self.s.user_message_id(self.uid,'scope-repair'))
        result=prefs.apply(self.uid,self.cid,turn,Candidate('avoid_ingredient','放鸡蛋','avoid','hard','explicit','global',confidence=.95))
        self.assertEqual(repair(self.s,self.uid)['eligible'],1)
        self.assertTrue(prefs.load_profile(self.uid)['hard_constraints'])
        self.assertEqual(repair(self.s,self.uid,apply=True)['archived'],1)
        self.assertEqual(repair(self.s,self.uid,apply=True)['archived'],0)
        self.assertEqual(prefs.load_profile(self.uid)['hard_constraints'],[])
        with prefs.connect(self.uid) as db:
            item=db.execute('SELECT status,version FROM user_prefs WHERE id=?',(result['id'],)).fetchone()
            self.assertEqual(item['status'],'archived');self.assertEqual(item['version'],2)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM memory_events WHERE reason=?',(REASON,)).fetchone()['n'],1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM cookbuddy.messages WHERE user_id=? AND content=?',(self.uid,QUESTION)).fetchone()['n'],1)
        # Similar values from a different utterance are outside this repair.
        self.s.append(self.uid,self.cid,'legitimate-preference','user','以后做菜不要放鸡蛋。')
        other_turn=self.cid+':'+str(self.s.user_message_id(self.uid,'legitimate-preference'))
        other=prefs.apply(self.uid,self.cid,other_turn,Candidate('avoid_ingredient','放鸡蛋','avoid','hard','explicit','global',confidence=.95))
        self.assertEqual(repair(self.s,self.uid,apply=True)['archived'],0)
        with prefs.connect(self.uid) as db:
            self.assertEqual(db.execute('SELECT status FROM user_prefs WHERE id=?',(other['id'],)).fetchone()['status'],'active')

    def test_replacement_scope_repair_retains_real_avoidance_and_history(self):
        from repair_replacement_preference_scope import repair,QUESTION,REASON,VALUE
        from cookbuddy.ltm.profile_store import ProfileStore
        from cookbuddy.ltm.models import Candidate
        prefs=ProfileStore(self.s)
        self.s.append(self.uid,self.cid,'replacement-repair','user',QUESTION)
        turn=self.cid+':'+str(self.s.user_message_id(self.uid,'replacement-repair'))
        bad=prefs.apply(self.uid,self.cid,turn,Candidate('avoid_ingredient',VALUE,'avoid','hard','explicit','global',confidence=.95))
        self.s.append(self.uid,self.cid,'real-avoidance','user','我一直不吃鸡蛋')
        other=self.cid+':'+str(self.s.user_message_id(self.uid,'real-avoidance'))
        good=prefs.apply(self.uid,self.cid,other,Candidate('avoid_ingredient','鸡蛋','avoid','hard','explicit','global',confidence=.95))
        self.assertEqual(repair(self.s,self.uid),{'eligible':1,'archived':0})
        self.assertEqual(repair(self.s,self.uid,apply=True)['archived'],1)
        self.assertEqual(repair(self.s,self.uid,apply=True)['archived'],0)
        self.assertEqual([p['value'] for p in prefs.load_profile(self.uid)['hard_constraints']],['鸡蛋'])
        with prefs.connect(self.uid) as db:
            self.assertEqual(db.execute('SELECT status FROM user_prefs WHERE id=?',(bad['id'],)).fetchone()['status'],'archived')
            self.assertEqual(db.execute('SELECT status FROM user_prefs WHERE id=?',(good['id'],)).fetchone()['status'],'active')
            self.assertEqual(db.execute('SELECT count(*) AS n FROM memory_events WHERE reason=?',(REASON,)).fetchone()['n'],1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM cookbuddy.messages WHERE content=?',(QUESTION,)).fetchone()['n'],1)

    def test_manual_profile_change_reaches_recommendation_and_replacement(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from cookbuddy.ltm.models import CONFIG_OVERRIDE
        from cookbuddy.ltm.profile_store import ProfileStore
        cfg=load_config().model_dump();cfg['memory']['mode']='M6'
        old=CONFIG_OVERRIDE.get();worker.configure(cfg)
        def prepare(q):
            job=uuid.uuid4().hex
            result=worker.prepare(dict(user_id=self.uid,session_id=self.cid,request_id=job,question=q))
            return {**result,'_test_job':job}
        def recommend(prepared):
            return worker.execute(dict(operation='task_recommend',arguments={'state':prepared['task']['state']},
                user_id=self.uid,session_id=self.cid,request_id=prepared['_test_job'],hard=prepared['hard'],config=cfg))
        try:
            prepare('我喜欢清淡口味')
            first=recommend(prepare('按我的口味推荐两道菜，分别说明推荐理由。'))
            self.assertEqual(len(first['recommendations']),2)
            self.assertTrue(all('清淡口味偏好相符' in r['reason'] for r in first['recommendations']))
            prepare('我的口味改成重口味了')
            heavy=recommend(prepare('按我的口味推荐两道菜，分别说明推荐理由。'))
            self.assertEqual(len(heavy['recommendations']),2)
            self.assertTrue(all('重口口味偏好相符' in r['reason'] for r in heavy['recommendations']))
            replaced=prepare('我不要鸡蛋换一下')
            self.assertIsNone(replaced['management'])
            result=recommend(replaced)
            self.assertEqual(len(result['recommendations']),1)
            self.assertIn('重口口味偏好相符',result['recommendations'][0]['reason'])
            from cookbuddy.ltm.guard import source_allowed
            self.assertTrue(source_allowed(result['recommendations'][0]['source'],replaced['hard']))
            self.assertFalse(any(p['value'].startswith('鸡蛋') for p in ProfileStore(self.s).load_profile(self.uid)['hard_constraints']))
            other=self.s.create_conversation(self.uid,'短期换菜隔离')
            blank=worker.prepare(dict(user_id=self.uid,session_id=other,request_id=uuid.uuid4().hex,question='我的口味偏好是什么？'))
            self.assertIn('重口',blank['management']['message'])
            self.assertFalse(any(p['value']=='鸡蛋' for p in blank['hard']))
        finally:CONFIG_OVERRIDE.set(old)

    def test_literal_preparation_request_skips_memory_recall_and_rewrite(self):
        from dialogue_harness import run_persisted as run
        question='做响油鳝丝之前，需要准备哪些食材和器具？'
        self.s.append(self.uid,self.cid,'literal-preparation','user',question)
        with patch('cookbuddy.ltm.runtime.MemoryRuntime.context_items',side_effect=AssertionError('unexpected memory recall')), \
                patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')), \
                patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            result=run(self.uid,self.cid,'literal-preparation',question,self.s.memory_epoch(self.uid),lambda _:None)
        self.assertEqual(result['prepared']['task']['question'],question)
        self.assertEqual(result['outputs'][0]['result']['status'],'verified')
        self.assertEqual(result['outputs'][0]['result']['answer_method'],'source_ingredient_table')
        self.assertEqual(len(result['prepared']['profile']),0)
    def test_older_extractor_finishing_last_cannot_replace_new_preference(self):
        old='我喜欢清淡口味';new='我现在喜欢微辣'
        self.s.append(self.uid,self.cid,'older-job','user',old)
        self.s.append(self.uid,self.cid,'newer-job','user',new)
        service=MemoryService(self.s,object())
        for job,quote in [('newer-job',new),('older-job',old)]:
            response={'memories':[dict(id='p1',key='taste',confidence=.99,importance=.8)]}
            with patch('cookbuddy.memory.json_model',return_value=response):
                service.extract(self.uid,job,quote,{'status':'completed','outputs':[]},self.s.memory_epoch(self.uid))
        self.assertEqual(self.s.active(self.uid,['profile'])[0]['content'],new)

    def test_task_survives_extractor_failure_and_retry_does_not_duplicate_vector_work(self):
        service=MemoryService(self.s,object());epoch=self.s.memory_epoch(self.uid)
        record=completed_record()
        with patch('cookbuddy.memory.json_model',side_effect=ValueError('extraction_failed')):
            mid=service.save_task(self.uid,'task-job','怎么做',record,epoch)
            with self.assertRaises(ValueError):
                service.extract(self.uid,'task-job','怎么做',record,epoch)
        first=self.s.memory(self.uid,mid)
        self.assertEqual(first['kind'],'task')
        self.assertEqual(first['evidence']['sources'],['data/HowToCook/dishes/breakfast/蒸水蛋.md'])
        with patch('cookbuddy.memory.json_model',return_value={'memories':[]}):
            self.assertEqual(service.save_task(self.uid,'task-job','怎么做',record,epoch),mid)
            service.extract(self.uid,'task-job','怎么做',record,epoch)
        self.assertEqual(self.s.memory(self.uid,mid)['version'],first['version'])
        with self.s.connect(self.uid) as db:
            count=db.execute('SELECT count(*) AS count FROM cookbuddy.outbox WHERE memory_id=%s',(mid,)).fetchone()['count']
        self.assertEqual(count,1)

    def test_task_does_not_reappear_after_forget_or_save_unverified_work(self):
        service=MemoryService(self.s,object());epoch=self.s.memory_epoch(self.uid)
        self.s.forget(self.uid,purge=True)
        self.assertIsNone(service.save_task(self.uid,'forgotten-job','怎么做',completed_record(),epoch))
        for status,answer_status,evidence in [('failed','verified',[{'source':'test'}]),
                ('completed','failed',[{'source':'test'}]),('completed','verified',[])]:
            record={'status':status,'outputs':[{'result':{'status':answer_status,'evidence':evidence}}]}
            self.assertIsNone(service.save_task(self.uid,'invalid-job','怎么做',record,self.s.memory_epoch(self.uid)))
        self.assertEqual(self.s.active(self.uid),[])
    def test_forget_during_compression_cannot_create_a_new_summary(self):
        for i in range(41):
            self.s.upsert_memory(self.uid,'task','task-'+str(i),'测试任务'+str(i),{'sources':['verified-recipe']},ttl_days=90)
        original=self.s.upsert_memory
        interrupted=False
        def forget_then_save(*args,**kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted=True
                self.s.forget(self.uid,purge=True)
            return original(*args,**kwargs)
        with patch.object(self.s,'upsert_memory',side_effect=forget_then_save):
            result=MemoryService(self.s,object()).maintain(self.uid)
        self.assertTrue(interrupted)
        self.assertEqual(result['compressed'],0)
        self.assertEqual(self.s.active(self.uid),[])

    def test_reconfirming_expired_memory_reactivates_and_emits_new_version(self):
        mid=self.s.upsert_memory(self.uid,'profile','taste','我喜欢清淡',{'quote':'我喜欢清淡'},ttl_days=-1)
        old=self.s.memory(self.uid,mid)
        self.s.upsert_memory(self.uid,'profile','taste',old['content'],old['evidence'],ttl_days=365)
        rows=self.s.active(self.uid,['profile'])
        self.assertEqual(len(rows),1)
        self.assertGreater(rows[0]['version'],old['version'])

if __name__=='__main__':unittest.main()
