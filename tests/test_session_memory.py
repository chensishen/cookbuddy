import copy
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.session import (SessionState,append_message,update_slots,resolve_reference,
    compact_assistant,rewrite_query,build_context,tokens,prompt_slots)
from cookbuddy.task_context import advance_task
from preference_recommendation import recommend_preferences
from recipe_tools import load_chunks


class SessionTests(unittest.TestCase):
    def setUp(self):self.state=SessionState('userA','sessionA')
    def recommendations(self):
        task=advance_task('我有鸡肉，推荐3道辣菜',None)
        data=recommend_preferences(task['state'],load_chunks())
        result={'status':'completed','outputs':[{'result':data}]}
        update_slots(self.state,task,result)
        return task,result

    def test_identity_is_required(self):
        for uid,cid in [('', 'a'),('a',''),(None,'a')]:
            with self.assertRaises(ValueError):SessionState(uid,cid)

    def test_s1_ordinal_resolves_exact_display_order_without_model(self):
        _,result=self.recommendations();model=Mock()
        second=result['outputs'][0]['result']['recommendations'][1]
        self.assertEqual(rewrite_query(self.state,'第二个怎么做',model),second['title']+'怎么做？')
        self.assertEqual(self.state.slots['selected_dish']['source_path'],second['source'])
        model.assert_not_called()
        self.assertIsNone(resolve_reference(self.state,'第九个怎么做'))
        self.assertEqual(resolve_reference(self.state,'它怎么做')['dish_name'],second['title'])

    def test_ambiguous_pronoun_does_not_guess_first_recommendation(self):
        self.recommendations()
        self.assertIsNone(resolve_reference(self.state,'那个怎么做'))

    def test_s2_retrieval_excludes_previous_recommendations(self):
        task,first=self.recommendations()
        excluded=[r['title'] for r in first['outputs'][0]['result']['recommendations']]
        second=recommend_preferences(task['state'],load_chunks(),exclude_titles=excluded)
        self.assertTrue(second['recommendations'])
        self.assertFalse(set(excluded)&{r['title'] for r in second['recommendations']})

    def test_s4_summary_keeps_facts_and_window_is_bounded(self):
        self.recommendations()
        summary=Mock(return_value={'summary':'用户有鸡肉，想吃辣，已推荐宫保鸡丁。'})
        for n in range(16):append_message(self.state,'user','用户想吃辣，有鸡肉。'+str(n),summary)
        self.assertLessEqual(len(self.state.messages),12)
        self.assertLessEqual(tokens(self.state.messages),1500)
        summary.assert_called()
        self.assertIn('鸡肉',self.state.summary)
        self.assertIn('辣',self.state.summary)
        self.assertIn('宫保鸡丁',self.state.summary)
        self.assertEqual(self.state.slots['ingredients_on_hand'],['鸡肉'])
        self.assertLessEqual(tokens(self.state.summary),400)

    def test_summary_failure_does_not_lose_slots_or_fail_answer(self):
        self.recommendations();before=copy.deepcopy(self.state.slots)
        for n in range(15):append_message(self.state,'user','鸡肉'+str(n),Mock(side_effect=TimeoutError))
        self.assertEqual(self.state.slots,before)
        self.assertLessEqual(tokens(self.state.messages),1500)

    def test_s5_compact_history_excludes_all_tool_answer_text(self):
        _,result=self.recommendations()
        result['outputs'][0]['result']['short_answer']='工具长文禁止进入窗口'*300
        text=compact_assistant(result)
        self.assertNotIn('工具长文',text)
        self.assertIn('source_path',text)
        self.assertIn('dish_name',text)
        append_message(self.state,'assistant',text)
        self.assertNotIn('工具长文',json.dumps(self.state.record(),ensure_ascii=False))

    def test_self_contained_question_skips_query_rewriter(self):
        model=Mock();question='基础牛奶面包怎么做？'
        self.assertEqual(rewrite_query(self.state,question,model),question)
        model.assert_not_called()

    def test_omitted_subject_uses_bounded_context(self):
        model=Mock(return_value={'question':'基础牛奶面包需要发酵多久？'})
        self.state.summary='正在讨论基础牛奶面包'
        self.assertEqual(rewrite_query(self.state,'要多久？',model),'基础牛奶面包需要发酵多久？')
        model.assert_called_once()
        self.assertEqual(model.call_args.args[1]['summary'],self.state.summary)

    def test_context_order_hard_constraints_and_current_message_once(self):
        append_message(self.state,'user','鸡肉怎么做')
        hard=['花生过敏'*200]
        messages=build_context(self.state,'鸡肉怎么做',hard_constraints=hard)
        self.assertEqual(json.loads(messages[1]['content'])['must_observe'],hard)
        self.assertEqual(messages[-1],{'role':'user','content':'鸡肉怎么做'})
        self.assertEqual(sum(m['content']=='鸡肉怎么做' for m in messages),1)
        self.assertTrue(messages[2]['content'].startswith('用户画像'))
        self.assertTrue(messages[3]['content'].startswith('会话摘要'))
        self.assertTrue(messages[-2]['content'].startswith('会话槽位'))

    def test_prompt_slots_is_valid_json_and_does_not_mutate_authoritative_slots(self):
        self.recommendations();original=copy.deepcopy(self.state.slots)
        projection=prompt_slots(self.state.slots,150)
        self.assertLessEqual(tokens(projection),150)
        json.loads(json.dumps(projection))
        self.assertEqual(original,self.state.slots)

    def test_new_topic_clears_old_selection_and_constraints(self):
        self.recommendations()
        rewrite_query(self.state,'第二个怎么做')
        update_slots(self.state,{'boundary':True,'state':None})
        self.assertIsNone(resolve_reference(self.state,'它怎么做'))
        self.assertEqual(self.state.slots['last_recommendations'],[])
        self.assertEqual(self.state.slots['temp_constraints'],{})

    def test_profile_projection_keeps_valid_complete_records(self):
        messages=build_context(self.state,'测试',memory_items=[{'content':'长资料'*300},{'content':'我喜欢清淡'}])
        profile=json.loads(messages[2]['content'].split('：',1)[1])
        self.assertEqual(profile,[{'content':'我喜欢清淡'}])


@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','requires PostgreSQL')
class SessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        from cookbuddy.store import Store
        self.store=Store();self.uid=uuid.uuid4().hex;self.other=uuid.uuid4().hex
        self.store.ensure_user(self.uid);self.store.ensure_user(self.other)
        self.cid=self.store.create_conversation(self.uid,'isolated session acceptance')
    def tearDown(self):
        with self.store.connect(admin=True) as db:
            for table in ('intent_audit','messages','conversations','tenants'):
                db.execute('DELETE FROM cookbuddy.'+table+' WHERE '+('id' if table=='tenants' else 'user_id')+'=ANY(%s)',([self.uid,self.other],))
    def user(self,q):
        job=uuid.uuid4().hex;self.store.append(self.uid,self.cid,job,'user',q);return job

    def test_cutoff_isolation_expiry_and_identity(self):
        first=self.user('我有鸡肉');state=self.store.session_context(self.uid,self.cid,first)
        state.summary='第一轮';self.store.save_session_context(self.uid,self.cid,first,state)
        second=self.user('鸡肉怎么做');later=copy.deepcopy(state);later.summary='第二轮'
        self.store.save_session_context(self.uid,self.cid,second,later)
        self.assertEqual(self.store.session_context(self.uid,self.cid,second).summary,'第一轮')
        with self.assertRaises(ValueError):self.store.session_context(self.other,self.cid,second)
        with self.assertRaises(ValueError):self.store.save_session_context(self.other,self.cid,first,state)
        with self.store.connect(self.uid) as db:
            db.execute("UPDATE cookbuddy.session_states SET expires_at=now()-interval '1 second'")
        self.assertEqual(self.store.session_context(self.uid,self.cid,second).summary,'')

    def test_s1_s2_s3_pipeline_consumes_session_and_retriggers_retrieval(self):
        from dialogue_harness import run_persisted as run
        original=self.store.save_result
        def save(uid,cid,job,q,result,epoch):
            self.store.append(uid,cid,job,'assistant','测试已完成',result)
        with patch('cookbuddy.store.Store.save_result',side_effect=save),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
            job=self.user('我有鸡肉，推荐3道辣菜')
            first=run(self.uid,self.cid,job,'我有鸡肉，推荐3道辣菜',0,lambda _:None)
            first_titles=[r['title'] for r in first['outputs'][0]['result']['recommendations']]
            job=self.user('第二个怎么做')
            with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')) as execute:
                detail=run(self.uid,self.cid,job,'第二个怎么做',0,lambda _:None)
            execute.assert_not_called()
            self.assertEqual(detail['prepared']['named_recipe']['dish_name'],first_titles[1])
            answer=detail['outputs'][0]['result']
            self.assertEqual(answer['status'],'verified_source')
            self.assertTrue(all(r['title']==first_titles[1] for r in answer['sources']))
            from acceptance_closeout import exact_evidence
            self.assertTrue(exact_evidence(answer)['all_exact'])
            job=self.user('换一个')
            changed=run(self.uid,self.cid,job,'换一个',0,lambda _:None)
            self.assertFalse(set(first_titles)&{r['title'] for r in changed['outputs'][0]['result']['recommendations']})
            job=self.user('再清淡点')
            with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')) as execute:
                refined=run(self.uid,self.cid,job,'再清淡点',0,lambda _:None)
            execute.assert_not_called()
            self.assertTrue(refined['outputs'][0]['result']['clarification_options'])
            job=self.user('继续推荐')
            state=self.store.session_context(self.uid,self.cid,job)
            self.assertIn('再清淡点',refined['prepared']['task']['state']['extra_constraints'])
