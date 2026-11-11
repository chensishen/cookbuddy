"""Conversation behavior regressions, including the exact failed user transcript."""
import copy
import itertools
import os
from pathlib import Path
import sys
import unittest
import uuid
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.task_context import advance_task,replay_task,reduce_edits,valid_state
from cookbuddy.recommendation_turn import interpret,Edit


def initial():
    state=advance_task('我喜欢吃辣的，给我推荐3道菜。',None)['state']
    return advance_task('我有鸡肉。',state)['state']


class TurnBehaviorTests(unittest.TestCase):
    def test_non_spicy_candidates_use_whole_source_including_optional_seasonings(self):
        from preference_recommendation import recommend_preferences
        chunks=[]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            for name,note in [('清蒸鸡','直接出锅。'),('可选胡椒鸡','* 白胡椒粉（可选）'),
                              ('辣椒鸡','* 干辣椒'),('不明配料鸡','* 调味料')]:
                source='data/HowToCook/dishes/meat_dish/'+name+'.md'
                lines=['# '+name,'这道菜以鸡肉为主，制作简单。','## 必备原料和工具',
                       '* 鸡肉','* 盐','## 操作','1. 蒸熟鸡肉。','## 备注',note]
                path=root/source;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('\n'.join(lines))
                for start,end,section in [(2,2,'简介'),(4,5,'必备原料和工具'),(7,7,'操作')]:
                    chunks.append({'source':source,'title':name,'section':section,
                                   'source_start_line':start,'source_end_line':end,
                                   'content':'\n'.join(lines[start-1:end])})
            state=advance_task('推荐3道不辣的鸡肉菜',None)['state']
            with patch('preference_recommendation.ROOT',root),patch('cookbuddy.ltm.guard.current',return_value=[]):
                result=recommend_preferences(state,chunks)
            self.assertIn('recommendations',result,(state,result))
            self.assertEqual([row['title'] for row in result['recommendations']],['清蒸鸡'])
            self.assertIn('原文配方及步骤',result['recommendations'][0]['reason'])
            self.assertTrue(any(e['section']=='操作' for e in result['recommendations'][0]['evidence']))

    def test_explicit_taste_and_chicken_dish_phrases_are_complete_conditions(self):
        for q in ('推荐3道不辣的鸡肉菜，不要辣椒。','那就不辣，继续推荐3道鸡肉菜'):
            task=advance_task(q,initial())
            self.assertEqual(task['action'],'recommend')
            self.assertEqual(task['state']['ingredients'],['鸡肉'])
            self.assertEqual(task['state']['tastes'],['不辣'])
            self.assertEqual(task['state']['extra_constraints'],[])
        self.assertTrue(interpret('推荐3道低脂不辣的鸡肉菜').unresolved)

    def test_profile_selection_is_not_an_unknown_recipe_filter(self):
        from preference_recommendation import recommend_preferences
        from recipe_tools import load_chunks
        for q in ('根据我的喜欢推荐','根据我的偏好推荐3道菜','按我的口味推荐','结合我的饮食喜好推荐'):
            task=advance_task(q,None)
            self.assertEqual(task['action'],'recommend')
            self.assertEqual(task['state']['extra_constraints'],[])
            self.assertIsNotNone(recommend_preferences(task['state'],load_chunks()))
        old=initial();old['extra_constraints']=['根据我的喜欢推荐']
        self.assertEqual(advance_task('继续推荐',old)['state']['extra_constraints'],[])
        unknown=advance_task('根据我的偏好推荐低嘌呤菜',None)
        self.assertTrue(unknown['state']['extra_constraints'])

    def test_output_requirements_are_not_recipe_filters(self):
        from preference_recommendation import recommend_preferences
        from recipe_tools import load_chunks
        for suffix in ('并介绍各自特点和推荐原因','介绍每道菜的特点与推荐理由',
                       '说明各自的特色以及为什么推荐','给出简介、推荐理由和主要食材'):
            with self.subTest(suffix=suffix):
                task=advance_task('我有鸡肉，推荐3道辣菜，'+suffix+'。',None)
                self.assertEqual(task['state']['extra_constraints'],[])
                self.assertIn('description',task['state']['response_fields'])
                self.assertIn('reason',task['state']['response_fields'])
                result=recommend_preferences(task['state'],load_chunks())
                self.assertEqual(result['status'],'verified')
                self.assertEqual(len(result['recommendations']),3)
                for row in result['recommendations']:
                    self.assertTrue(row['description']);self.assertTrue(row['reason']);self.assertTrue(row['evidence'])

    def test_screenshot_sequence_keeps_food_constraints_without_output_pollution(self):
        from preference_recommendation import recommend_preferences
        from recipe_tools import load_chunks
        first=advance_task('我有鸡肉，推荐3道辣菜，并介绍各自特点和推荐原因。',None)['state']
        second=advance_task('我有鸡肉和土豆，推荐几道菜，不要油炸。',first)['state']
        self.assertEqual(second['ingredients'],['鸡肉','土豆'])
        self.assertEqual(second['tastes'],['辣味'])
        self.assertEqual(second['excluded_methods'],['油炸'])
        self.assertEqual(second['extra_constraints'],[])
        # Both fresh and contextual requests must yield source-backed candidates.
        fresh=advance_task('我有鸡肉和土豆，推荐几道菜，不要油炸。',None)['state']
        for state in (second,fresh):
            result=recommend_preferences(state,load_chunks())
            self.assertEqual(result['status'],'verified')
            self.assertTrue(result['recommendations'])
            for row in result['recommendations']:
                for ingredient in row['inventory_matches']:self.assertIn(ingredient,row['reason'])
                self.assertIn('未列油炸',row['reason'])
                operations=[e['quote'] for e in row['evidence'] if any(v in e['section'] for v in ('操作','做法','制作'))]
                self.assertTrue(operations);self.assertNotIn('炸','\n'.join(operations))
            self.assertTrue({'鸡肉','土豆'}<={s for row in result['recommendations'] for s in row['inventory_matches']})

    def test_output_only_followup_and_inline_request(self):
        prior=initial()
        for q in ('介绍各自特点和推荐原因','请说明每道菜的推荐理由','说明为什么推荐'):
            task=advance_task(q,prior)
            self.assertEqual(task['action'],'recommend')
            self.assertEqual(task['state']['ingredients'],['鸡肉'])
            self.assertEqual(task['state']['extra_constraints'],[])
        task=advance_task('推荐3道辣菜并介绍各自特点和推荐原因',None)
        self.assertEqual(task['state']['tastes'],['辣味'])
        self.assertEqual(task['state']['extra_constraints'],[])
        self.assertFalse(interpret('推荐原因').recommend)

    def test_describe_each_dish_is_output_not_a_filter_and_unknown_nutrition_survives(self):
        task=advance_task('我有鸡肉，推荐3道辣菜，介绍一下每道菜，并说明为什么推荐。',None)
        self.assertEqual(task['state']['extra_constraints'],[])
        self.assertEqual(task['state']['response_fields'],['description','reason'])
        second=advance_task('我有鸡肉和土豆，推荐几道菜，不要油炸。',task['state'])
        self.assertEqual(second['state']['ingredients'],['鸡肉','土豆'])
        self.assertEqual(second['state']['excluded_methods'],['油炸'])
        self.assertEqual(second['state']['extra_constraints'],[])
        legacy=copy.deepcopy(task['state']);legacy['extra_constraints']=['介绍一下每道菜']
        self.assertEqual(advance_task('继续推荐',legacy)['state']['extra_constraints'],[])
        unknown=advance_task('推荐3道菜，介绍每道菜的热量',None)
        self.assertEqual(unknown['state']['extra_constraints'],['介绍每道菜的热量'])

    def test_legacy_output_constraint_migrates_without_dropping_real_constraints(self):
        prior=initial();prior['extra_constraints']=['并介绍各自特点和推荐原因','不要太咸']
        original=copy.deepcopy(prior)
        task=advance_task('不要油炸',prior)
        self.assertEqual(task['state']['extra_constraints'],['不要太咸'])
        self.assertEqual(task['state']['response_fields'],['description','reason'])
        self.assertEqual(prior,original)
        for suffix in ('并介绍特点和热量','并介绍特点，不要太咸','解释推荐原因，但不能含花生'):
            task=advance_task('推荐3道菜，'+suffix,None)
            self.assertTrue(task['state']['extra_constraints'])
        invalid=copy.deepcopy(prior);invalid['response_fields']=['nutrition']
        self.assertFalse(valid_state(invalid))

    def test_subject_discourse_verb_suffix_combinations(self):
        prior=initial();unchanged=copy.deepcopy(prior)
        for prefix,subject,verb,suffix in itertools.product(('', '这次','现在'),('', '我','我们','我家'),
                ('不吃','不要带','不放','不能吃','不喜欢吃','别加'),('','。','的')):
            q=prefix+subject+verb+'辣椒'+suffix
            with self.subTest(question=q):
                task=advance_task(q,prior)
                self.assertEqual(task['action'],'recommend')
                self.assertEqual(task['state']['ingredients'],['鸡肉'])
                self.assertEqual(task['state']['excluded_ingredients'],['辣椒'])
                self.assertEqual(task['state']['tastes'],[])
                self.assertEqual(task['state']['extra_constraints'],[])
                self.assertEqual(task['state']['limit'],3)
        self.assertEqual(prior,unchanged)
        for q in ('我现在不吃辣椒','我这次不吃辣椒','现在我不吃有辣椒的菜','辣椒都不要'):
            self.assertEqual(advance_task(q,prior)['state']['excluded_ingredients'],['辣椒'],q)

    def test_topical_order_and_clause_composition(self):
        for q in ('辣椒我不吃','辣椒就别放了','这次我不吃辣椒，不要油炸，改成2道'):
            task=advance_task(q,initial())
            self.assertEqual(task['action'],'recommend',q)
            self.assertEqual(task['state']['excluded_ingredients'],['辣椒'])
        task=advance_task('我不吃辣椒和花生，而且我不吃香菜。',initial())
        self.assertEqual(task['state']['excluded_ingredients'],['辣椒','花生','香菜'])
        turn=interpret('我不吃辣椒')
        self.assertEqual(turn.unresolved,[])
        self.assertEqual(turn.edits[0],Edit('add','excluded_ingredients',('辣椒',),'我不吃辣椒'))

    def test_incomplete_or_invalid_update_is_atomic(self):
        prior=initial();prior['choices']=[{'title':'可乐鸡翅','source':'real.md'}]
        for q in ('我不吃辣椒，三个人吃','我不吃八角','我不吃辣椒，改成0道','我不吃辣椒，改成11道'):
            task=advance_task(q,prior)
            self.assertEqual(task['action'],'clarify',q)
            self.assertEqual(task['state'],prior)
        with self.assertRaises(ValueError):
            reduce_edits(prior,[Edit('set','surprise',('鸡肉',),'鸡肉')])
        self.assertEqual(prior['tastes'],['辣味'])

    def test_legacy_replay_uses_user_text_not_failed_model_expansion(self):
        messages=[]
        for q in ('我喜欢吃辣的，给我推荐3道菜。','我有鸡肉。','我不吃辣椒','不要带辣椒'):
            messages.extend([{'role':'user','content':q}, {'role':'assistant','content':'失败',
                'record':{'task_context':None,'context_action':'none','context_question':'我有鸡肉，不吃香菜，我是新手'}}])
        state=replay_task(messages)
        self.assertTrue(valid_state(state));self.assertEqual(state['ingredients'],['鸡肉'])
        self.assertEqual(state['excluded_ingredients'],['辣椒']);self.assertEqual(state['extra_constraints'],[])
        self.assertEqual(state['tastes'],[])
        # A real user topic change remains a boundary even if an old state exists.
        messages.append({'role':'user','content':'换个话题，蒸水蛋怎么做？'})
        self.assertIsNone(replay_task(messages))

    def test_quick_request_uses_explicit_whole_recipe_duration(self):
        from cookbuddy.meal_planning import duration_estimate
        from preference_recommendation import recommend_preferences
        from recipe_tools import load_chunks
        state=advance_task('推荐一道快手菜',None)['state']
        result=recommend_preferences(state,load_chunks())
        self.assertEqual(result['status'],'verified')
        self.assertEqual(len(result['recommendations']),1)
        row=result['recommendations'][0]
        estimate=duration_estimate([r for r in load_chunks() if r['source']==row['source']])
        self.assertLessEqual(estimate['minutes'],30)
        self.assertIn('分钟',row['reason'])
        self.assertTrue(any(item['start_line']<=estimate['line']<=item['end_line']
                            for item in row['evidence']))
        self.assertEqual(advance_task('推荐一道快手菜',None)['state']['extra_constraints'],['快手'])

    def test_simple_request_maps_to_conservative_difficulty_limit(self):
        for q in ('推荐一道简单的菜','推荐两道容易做的鸡肉菜','推荐一道适合新手的菜'):
            with self.subTest(question=q):
                turn=interpret(q)
                self.assertEqual(turn.max_difficulty,2)
                self.assertEqual(turn.unresolved,[])
                task=advance_task(q,None)
                self.assertEqual(task['action'],'recommend')
                self.assertEqual(task['state']['extra_constraints'],[])
                self.assertEqual(task['filters'],{'max_difficulty':2})
                self.assertIn('难度不超过2星',task['question'])


@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','requires PostgreSQL')
class TaskStatePersistenceTests(unittest.TestCase):
    def setUp(self):
        from cookbuddy.store import Store
        self.store=Store();self.uid=uuid.uuid4().hex;self.other=uuid.uuid4().hex
        self.store.ensure_user(self.uid);self.store.ensure_user(self.other)
        self.cid=self.store.create_conversation(self.uid,'task reducer regression')
        self.second=self.store.create_conversation(self.uid,'isolated conversation')
        # These tests exercise task persistence, not asynchronous memory
        # extraction. Do not enqueue test records for a running app worker.
        self.result_patch=patch('cookbuddy.store.Store.save_result',
            new=lambda store,uid,cid,job,q,record,epoch=0:store.append(uid,cid,job,'assistant',
                record['outputs'][0]['result'].get('short_answer',''),record))
        self.result_patch.start();self.addCleanup(self.result_patch.stop)
    def tearDown(self):
        with self.store.connect(admin=True) as db:
            for table in ('memory_tasks','intent_audit','messages','conversations'):
                db.execute('DELETE FROM cookbuddy.'+table+' WHERE user_id=ANY(%s)',([self.uid,self.other],))
            db.execute('DELETE FROM cookbuddy.tenants WHERE id=ANY(%s)',([self.uid,self.other],))
    def user(self,q):
        job=uuid.uuid4().hex;self.store.append(self.uid,self.cid,job,'user',q);return job

    def test_output_polluted_saved_event_recovers_and_routes_without_generation(self):
        from dialogue_harness import run_persisted as run
        first='我有鸡肉，推荐3道辣菜，并介绍各自特点和推荐原因。'
        job=self.user(first)
        legacy=advance_task('我有鸡肉，推荐3道辣菜',None)
        legacy['state']['extra_constraints']=['并介绍各自特点和推荐原因']
        legacy['state'].pop('response_fields',None)
        self.store.save_task_context(self.uid,self.cid,job,legacy)
        for q in ('我有鸡肉和土豆，推荐几道菜，不要油炸。','介绍各自特点和推荐原因'):
            job=self.user(q)
            with patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')),patch('src.nlu.router._model',side_effect=AssertionError('database regression must not call the provider')):
                result=run(self.uid,self.cid,job,q,0,lambda _:None)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['prepared']['task']['action'],'recommend')
            self.assertEqual(result['trace']['model_calls'],0)
            self.assertEqual(result['prepared']['task']['state']['extra_constraints'],[])
            self.assertEqual(result['prepared']['task']['state']['ingredients'],['鸡肉','土豆'])
            self.assertEqual(result['prepared']['task']['state']['excluded_methods'],['油炸'])

    def test_failure_does_not_erase_state_and_restart_reads_it(self):
        from dialogue_harness import run_persisted as run
        from cookbuddy.store import Store
        q='我喜欢吃辣的，给我推荐3道菜。';job=self.user(q)
        with patch('src.agent.engine.invoke_tool',side_effect=RuntimeError('simulated tool failure')):
            failed=run(self.uid,self.cid,job,q,0,lambda _:None)
        self.assertEqual(failed['status'],'degraded')
        self.store.append(self.uid,self.cid,job,'assistant','本次失败',{'task_context':None,'context_action':'none'})
        next_job=self.user('我有鸡肉。')
        state=Store().task_context(self.uid,self.cid,next_job)
        self.assertEqual(state['tastes'],['辣味'])
        result=run(self.uid,self.cid,next_job,'我有鸡肉。',0,lambda _:None)
        self.assertEqual(result['status'],'completed')
        last=self.user('我不吃辣椒')
        result=run(self.uid,self.cid,last,'我不吃辣椒',0,lambda _:None)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['prepared']['task']['state']['ingredients'],['鸡肉'])
        self.assertEqual(result['prepared']['task']['state']['excluded_ingredients'],['辣椒'])

    def test_legacy_actual_failure_chain_recovers_within_same_chat(self):
        for q in ('我喜欢吃辣的，给我推荐3道菜。','我有鸡肉。','我不吃辣椒','不要带辣椒'):
            job=self.user(q)
            self.store.append(self.uid,self.cid,job,'assistant','资料不足',
                {'task_context':None,'context_action':'none','context_question':'推荐不要香菜适合新手的菜'})
        next_job=self.user('不要带辣椒')
        state=self.store.task_context(self.uid,self.cid,next_job)
        self.assertEqual(state['ingredients'],['鸡肉']);self.assertEqual(state['tastes'],[])
        self.assertEqual(state['excluded_ingredients'],['辣椒']);self.assertEqual(state['extra_constraints'],[])
        self.assertIsNone(self.store.task_context(self.uid,self.second,next_job))
        self.assertIsNone(self.store.task_context(self.other,self.cid,next_job))

    def test_user_turn_cutoff_and_explicit_close_are_respected(self):
        first=self.user('推荐3道辣菜');start=advance_task('推荐3道辣菜',None)
        self.store.save_task_context(self.uid,self.cid,first,start)
        second=self.user('我有鸡肉');updated=advance_task('我有鸡肉',start['state'])
        third=self.user('换个话题，蒸水蛋怎么做？')
        self.store.save_task_context(self.uid,self.cid,third,advance_task('换个话题，蒸水蛋怎么做？',updated['state'],True))
        # A delayed earlier write cannot overwrite the later close event.
        self.store.save_task_context(self.uid,self.cid,second,updated)
        self.assertEqual(self.store.task_context(self.uid,self.cid,second),start['state'])
        self.assertEqual(self.store.task_context(self.uid,self.cid,third),updated['state'])
        self.assertIsNone(self.store.task_context(self.uid,self.cid,uuid.uuid4().hex))
