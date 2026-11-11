import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from cookbuddy.store import Store
from cookbuddy.ltm.models import Candidate
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.guard import source_allowed
from cookbuddy.ltm.worker import process_memory_task
from cookbuddy.ltm.episodic_store import EpisodicStore,MemoryVectors,event_from_user
from cookbuddy.ltm.maintenance import clear_user,flush_idle_sessions
from dialogue_harness import run_persisted as run

@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','requires PostgreSQL + Milvus')
class LiveMemoryTests(unittest.TestCase):
    def setUp(self):
        self.store=Store();self.uid=uuid.uuid4().hex;self.other=uuid.uuid4().hex
        for uid in (self.uid,self.other):self.store.ensure_user(uid)
        self.cid=self.store.create_conversation(self.uid,'isolated versioned memory acceptance')
        self.prefs=ProfileStore(self.store)
    def tearDown(self):
        # Every test actor is disposable; no application user's data is touched.
        with self.store.connect(admin=True) as db:
            for table in ('memory_tasks','outbox','memory_revisions','memories','intent_audit','messages','conversations'):
                db.execute('DELETE FROM cookbuddy.'+table+' WHERE user_id=ANY(%s)',([self.uid,self.other],))
            db.execute('DELETE FROM cookbuddy.tenants WHERE id=ANY(%s)',([self.uid,self.other],))
    def message(self,text):
        job=uuid.uuid4().hex;self.store.append(self.uid,self.cid,job,'user',text);return job
    def test_meal_parameters_stay_in_conversation_after_worker_replay(self):
        from cookbuddy.ltm.runtime import MemoryRuntime
        q='请记住：我们家2人，30分钟以内'
        job=self.message(q)
        runtime=MemoryRuntime(self.store,self.uid,self.cid,job,q)
        self.assertEqual(runtime.management['decision'],'SESSION_ONLY')
        self.assertEqual(runtime.session.slots['temp_constraints']['profile_overrides'],
                         {'household_size':'2','time_budget':'30'})
        self.store.save_session_context(self.uid,self.cid,job,runtime.session)
        row={'user_id':self.uid,'job_id':job,'epoch':self.store.memory_epoch(self.uid),
             'profile_epoch':self.prefs.epoch(self.uid)}
        self.assertEqual(process_memory_task(self.store,row),0)
        self.assertEqual(process_memory_task(self.store,row),0)
        self.assertEqual(self.prefs.prefs(self.uid),[])
        follow=self.message('继续')
        saved=self.store.session_context(self.uid,self.cid,follow)
        self.assertEqual(saved.slots['temp_constraints']['profile_overrides'],
                         {'household_size':'2','time_budget':'30'})
        other=self.store.create_conversation(self.uid,'new conversation without meal defaults')
        other_job=uuid.uuid4().hex
        self.store.append(self.uid,other,other_job,'user','我有哪些饮食偏好？')
        fresh=MemoryRuntime(self.store,self.uid,other,other_job,'我有哪些饮食偏好？')
        self.assertEqual(fresh.management['message'],'目前没有已保存的饮食偏好。')
        self.assertEqual(fresh.session.slots['temp_constraints'].get('profile_overrides',{}),{})

    def test_natural_memory_delete_and_read_bypass_recipe_router(self):
        self.prefs.apply(self.uid,self.cid,'seed:1',Candidate('avoid_ingredient','香菜','dislike','soft'))
        self.prefs.apply(self.uid,self.cid,'seed:2',Candidate('spice_level','1'))
        with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            for q in ('忘记我不喜欢香菜这条偏好。','你还记得我的哪些饮食偏好？'):
                result=run(self.uid,self.cid,self.message(q),q,self.store.memory_epoch(self.uid),lambda _:None)
                self.assertEqual(result['outputs'][0]['tool'],'memory_management')
                self.assertFalse(any(op in result['operations'] for op in ('search','answer')))
                text=result['outputs'][0]['result']['short_answer']
                self.assertNotIn('我是 cookbuddy',text)
            self.assertIn('微辣',text);self.assertNotIn('香菜',text)
        self.assertEqual([p.value for p in self.prefs.prefs(self.uid)],['1'])

    def test_personal_recommendation_bypasses_llm_and_generic_rag(self):
        self.prefs.apply(self.uid,self.cid,'seed:spice',Candidate('spice_level','3'))
        for q in ('根据我的喜欢推荐','根据我的偏好推荐3道菜','按我的口味推荐'):
            with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
                result=run(self.uid,self.cid,self.message(q),q,self.store.memory_epoch(self.uid),lambda _:None)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['prepared']['task']['state']['tastes'],['辣味'])
            rows=result['outputs'][0]['result']['recommendations']
            self.assertEqual(len(rows),3)
            self.assertTrue(all(row['description'] and row['reason'] for row in rows))

    def test_literal_baking_question_bypasses_router_and_generation(self):
        q='基础牛奶面包要烤多久，温度是多少？'
        with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            result=run(self.uid,self.cid,self.message(q),q,self.store.memory_epoch(self.uid),lambda _:None)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['outputs'][0]['result']['answer_method'],'literal_baking_fact')

    def test_procedures_and_ordinal_followup_bypass_remote_qa(self):
        with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            for q in ('番茄炒蛋怎么做？请给出用量和步骤。',
                      '基础牛奶面包怎么做？简洁说明，但保留用量、温度、时间和关键注意事项。',
                      '两个人吃晚饭，30分钟内做好，推荐两菜一汤。','第二道菜怎么做？'):
                result=run(self.uid,self.cid,self.message(q),q,self.store.memory_epoch(self.uid),lambda _:None)
                self.assertEqual(result['status'],'completed',(q,result['prepared']['task'],result['operations']))
                if '两菜一汤' in q:
                    selected=result['outputs'][0]['result']['recommendations'][1]['source']
                    continue
                data=result['outputs'][0]['result']
                self.assertIn(data['status'],('verified','verified_source'))
                self.assertTrue(data['sources'])
                if q=='第二道菜怎么做？':
                    self.assertEqual({r['source'] for r in data['sources']},{selected})

    def test_chili_exclusion_survives_soft_memory_injection_and_followups(self):
        self.prefs.apply(self.uid,self.cid,'seed:spice',Candidate('spice_level','3'))
        self.prefs.apply(self.uid,self.cid,'seed:allergy',Candidate('allergy','花生','avoid','hard'))
        for q in ('我喜欢吃辣的，给我推荐3道菜','我有鸡肉','不要带辣椒的','继续推荐',
                  '那就不辣，继续推荐3道鸡肉菜'):
            with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
                result=run(self.uid,self.cid,self.message(q),q,self.store.memory_epoch(self.uid),lambda _:None)
            if q in ('我喜欢吃辣的，给我推荐3道菜','我有鸡肉'):continue
            state=result['prepared']['task']['state']
            self.assertEqual(state['ingredients'],['鸡肉'])
            self.assertEqual(state['excluded_ingredients'],['辣椒'])
            self.assertNotIn('辣味',state['tastes'])
            self.assertNotIn('微辣',state['tastes'])
            data=result['outputs'][0]['result']
            self.assertIn('recommendations',data,(q,data))
            rows=data['recommendations']
            self.assertTrue(rows)
            hard=self.prefs.load_profile(self.uid)['hard_constraints']
            self.assertTrue(all(source_allowed(row['source'],hard) for row in rows))
        self.assertEqual(result['prepared']['task']['state']['tastes'],['不辣'])
        self.assertEqual(next(p.value for p in self.prefs.prefs(self.uid) if p.dimension=='spice_level'),'3')

    def test_current_chili_exclusion_beats_session_spice_and_preserves_unrelated_filters(self):
        self.prefs.apply(self.uid,self.cid,'seed:spice',Candidate('spice_level','3'))
        first='推荐3道菜';job=self.message(first)
        session=self.store.session_context(self.uid,self.cid,job)
        session.slots['temp_constraints']['profile_overrides']={'spice_level':'3'}
        self.store.save_session_context(self.uid,self.cid,job,session)
        with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            run(self.uid,self.cid,job,first,0,lambda _:None)
            q='不要带辣椒的'
            excluded=run(self.uid,self.cid,self.message(q),q,0,lambda _:None)
        self.assertNotIn('辣味',excluded['prepared']['task']['state']['tastes'])
        self.assertTrue(excluded['outputs'][0]['result']['recommendations'])
        q='推荐3道不辣的鸡肉菜，不要辣椒，不要油炸。'
        with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            result=run(self.uid,self.cid,self.message(q),q,0,lambda _:None)
        state=result['prepared']['task']['state']
        self.assertEqual(state['tastes'],['不辣'])
        self.assertEqual(state['excluded_methods'],['油炸'])
        self.assertEqual(state['excluded_ingredients'],['辣椒'])
        self.assertTrue(result['outputs'][0]['result']['recommendations'])

    def test_meal_plan_bypasses_rag_and_preserves_structure_with_stored_spice(self):
        self.prefs.apply(self.uid,self.cid,'seed:spice',Candidate('spice_level','3'))
        q='两个人吃晚饭，30分钟内做好，推荐两菜一汤。';job=self.message(q)
        with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            result=run(self.uid,self.cid,job,q,self.store.memory_epoch(self.uid),lambda _:None)
        self.assertEqual(result['outputs'][0]['tool'],'plan_meal')
        self.assertEqual(result['prepared']['task']['action'],'meal_plan')
        self.assertFalse(any(op in result['operations'] for op in ('search','answer')))
        data=result['outputs'][0]['result'];self.assertEqual(data['status'],'verified')
        self.assertEqual(len(data['recommendations']),3)
        self.assertEqual(data['meal_plan']['soups'],1)
        self.assertNotIn('口味：辣味',result['prepared']['task']['question'])
        next_job=self.message('第二道菜怎么做？')
        session=self.store.session_context(self.uid,self.cid,next_job)
        self.assertEqual(len(session.slots['last_recommendations']),3)

    def test_async_only_user_text_and_noop_delivery(self):
        q='我不吃香菜';job=self.message(q)
        row={'user_id':self.uid,'job_id':job,'epoch':self.store.memory_epoch(self.uid),'profile_epoch':0,
             'question':'助手说我不吃辣','record':{'tool':'原文：我不吃花生'}}
        with patch('cookbuddy.ltm.worker.json_model',side_effect=AssertionError('rule extraction needs no model')):
            process_memory_task(self.store,row);process_memory_task(self.store,row)
        prefs=self.prefs.prefs(self.uid)
        self.assertEqual([(p.dimension,p.value,p.evidence_count) for p in prefs],[('avoid_ingredient','香菜',1)])
        self.assertFalse(self.prefs.prefs(self.other))
    def test_hard_guard_cross_session_current_soft_override_and_disable(self):
        self.prefs.apply(self.uid,self.cid,'s:1',Candidate('allergy','花生','avoid','hard'))
        self.prefs.apply(self.uid,self.cid,'s:2',Candidate('spice_level','0'))
        q='推荐3道辣菜';job=self.message(q)
        with patch('cookbuddy.store.Store.save_result',new=lambda store,uid,cid,job,q,result,epoch:store.append(uid,cid,job,'assistant','完成',result)),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            result=run(self.uid,self.cid,job,q,0,lambda _:None)
        dishes=result['outputs'][0]['result']['recommendations']
        self.assertTrue(dishes)
        hard=self.prefs.load_profile(self.uid)['hard_constraints']
        self.assertTrue(all(source_allowed(r['source'],hard) for r in dishes))
        self.assertEqual(result['prepared']['task']['state']['tastes'],['辣味'])
        self.prefs.set_enabled(self.uid,False)
        q='推荐3道菜';job=self.message(q)
        with patch('cookbuddy.store.Store.save_result'),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')),patch('cookbuddy.store.Store.session_context',side_effect=AssertionError('disabled session read')):
            result=run(self.uid,self.cid,job,q,0,lambda _:None)
        self.assertEqual(len(result['prepared']['profile']),0)
    def test_milvus_projection_search_isolation_and_clear_all(self):
        vector=[1.]+[0.]*1023;vectors=MemoryVectors();vectors.ensure()
        event=event_from_user('上次做的红烧肉太咸了')
        e=EpisodicStore(self.prefs,vectors,lambda _:vector)
        mid=e.write(self.uid,event,'s:1');e.sync(self.uid)
        self.assertTrue(vectors.ids(self.uid))
        self.assertFalse(vectors.ids(self.other))
        self.assertEqual(e.recall(self.uid,'我上次做的红烧肉怎么调整')[0]['mem_id'],mid)
        self.assertFalse(e.recall(self.other,'红烧肉'))
        self.prefs.apply(self.uid,self.cid,'s:2',Candidate('allergy','花生','avoid','hard'))
        q='推荐3道辣菜';job=self.message(q)
        self.store.save_session_context(self.uid,self.cid,job,self.store.session_context(self.uid,self.cid,job))
        receipt=clear_user(self.prefs,self.uid,vectors)
        self.assertEqual(receipt['vector_deletion'],'complete')
        self.assertFalse(vectors.ids(self.uid));self.assertFalse(self.prefs.prefs(self.uid))
        job=self.message('换一个')
        self.assertEqual(self.store.session_context(self.uid,self.cid,job).messages,[])
        self.assertIsNone(self.store.task_context(self.uid,self.cid,job))
    def test_idle_flush_is_once_and_does_not_enqueue_tool_records(self):
        job=self.message('我喜欢清淡')
        self.store.append(self.uid,self.cid,job,'assistant','我是工具回答',{'secret_tool_text':'不能进记忆'})
        with self.store.connect(self.uid) as db:
            db.execute("UPDATE cookbuddy.conversations SET updated_at=now()-interval '31 minutes' WHERE id=%s",(self.cid,))
        flush_idle_sessions(self.store,[self.uid]);flush_idle_sessions(self.store,[self.uid])
        with self.store.connect(self.uid) as db:
            rows=db.execute('SELECT record FROM cookbuddy.memory_tasks WHERE user_id=%s',(self.uid,)).fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['record'],{})

    def test_temporary_hard_scope_survives_recipe_change_and_requires_confirmation(self):
        from cookbuddy.ltm.runtime import MemoryRuntime
        from cookbuddy.session import update_slots
        def runtime(q):
            job=self.message(q)
            r=MemoryRuntime(self.store,self.uid,self.cid,job,q)
            self.store.save_session_context(self.uid,self.cid,job,r.session)
            return r
        first=runtime('今天不吃花生')
        self.assertTrue(any(h['value']=='花生' for h in first.hard))
        named=runtime('基础牛奶面包怎么做')
        update_slots(named.session,{'boundary':True,'state':None})
        job=self.message('保持当前会话')
        self.store.save_session_context(self.uid,self.cid,job,named.session)
        pending=runtime('花生的事不用管了')
        self.assertTrue(pending.management['needs_confirmation'])
        retained=runtime('保留')
        self.assertTrue(any(h['value']=='花生' for h in retained.hard))
        runtime('花生的事不用管了');confirmed=runtime('确认')
        self.assertFalse(any(h['value']=='花生' for h in confirmed.hard))
        self.assertFalse(self.prefs.prefs(self.uid))

    def test_derived_profile_taste_refreshes_without_overriding_current_request(self):
        self.prefs.apply(self.uid,self.cid,'pref:1',Candidate('spice_level','3'))
        def ask(text):
            job=self.message(text)
            with patch('cookbuddy.store.Store.save_result',new=lambda store,uid,cid,job,q,result,epoch:store.append(uid,cid,job,'assistant','完成',result)),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
                return run(self.uid,self.cid,job,text,0,lambda _:None)
        self.assertEqual(ask('推荐3道菜')['prepared']['task']['state']['tastes'],['辣味'])
        self.prefs.apply(self.uid,self.cid,'pref:2',Candidate('spice_level','0'))
        self.assertEqual(ask('继续推荐')['prepared']['task']['state']['tastes'],['不辣'])
        self.assertEqual(ask('推荐3道辣菜')['prepared']['task']['state']['tastes'],['辣味'])
        self.assertEqual(ask('我不太能吃辣')['prepared']['task']['state']['tastes'],['微辣'])
