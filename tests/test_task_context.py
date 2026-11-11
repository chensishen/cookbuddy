import copy
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.task_context import advance_task,remember_choices,valid_state
from preference_recommendation import recommend_preferences
from recipe_tools import load_chunks


class TaskContextTests(unittest.TestCase):
    def initial(self):return advance_task('我喜欢吃辣的，给我推荐几道菜',None)['state']
    def test_add_replace_exclude_and_reference_actual_list(self):
        state=self.initial();original=copy.deepcopy(state)
        state=advance_task('鸡肉',state)['state']
        self.assertEqual(state['ingredients'],['鸡肉']);self.assertEqual(state['tastes'],['辣味'])
        self.assertEqual(state['mode'],'preference');self.assertEqual(original,self.initial())
        state=advance_task('不要油炸',state)['state']
        self.assertEqual(state['excluded_methods'],['油炸'])
        state=remember_choices(state,{'outputs':[{'result':{'status':'verified','recommendations':[{'title':'菜A','source':'a.md'},{'title':'菜B','source':'b.md'}]}}]})
        turn=advance_task('第二道怎么做',state)
        self.assertEqual(turn['question'],'菜B怎么做？');self.assertEqual(turn['selected_source'],'b.md')
        state=advance_task('改成牛肉',turn['state'])['state']
        self.assertEqual(state['ingredients'],['牛肉']);self.assertEqual(state['tastes'],['辣味']);self.assertEqual(state['excluded_methods'],['油炸'])
        self.assertEqual(state['choices'],[])
    def test_missing_list_and_out_of_range_require_specific_clarification(self):
        self.assertEqual(advance_task('第二道怎么做',None)['action'],'clarify')
        self.assertEqual(advance_task('第九道怎么做',self.initial())['action'],'clarify')
    def test_independent_recipe_and_new_topic_close_old_task(self):
        self.assertIsNone(advance_task('蒸水蛋怎么做',self.initial(),named_recipe=True)['state'])
        self.assertIsNone(advance_task('换个话题，面粉如何保存？',self.initial())['state'])
        self.assertEqual(advance_task('鸡肉',None)['action'],'none')
    def test_inventory_is_explicit_and_replacement_does_not_imply_new_stock(self):
        state=advance_task('我有鸡肉，推荐三道菜',None)['state']
        self.assertEqual(state['mode'],'inventory')
        self.assertEqual(advance_task('改成牛肉',state)['state']['mode'],'preference')
    def test_stock_declaration_continues_existing_recommendation(self):
        initial=advance_task('我喜欢吃辣的，给我推荐3道菜',None)['state']
        initial=advance_task('不要油炸',initial)['state']
        original=copy.deepcopy(initial)
        for q in ('我有鸡肉','我有鸡肉。','冰箱里有鸡肉','手边有鸡肉','现有食材：鸡肉'):
            turn=advance_task(q,initial)
            self.assertEqual(turn['action'],'recommend',q)
            self.assertEqual(turn['state']['ingredients'],['鸡肉'])
            self.assertEqual(turn['state']['mode'],'inventory')
            self.assertEqual(turn['state']['tastes'],['辣味'])
            self.assertEqual(turn['state']['excluded_methods'],['油炸'])
            self.assertEqual(turn['state']['limit'],3)
            again=advance_task(q,turn['state'])
            self.assertEqual(again['state'],turn['state'])
        self.assertEqual(initial,original)
        self.assertEqual(advance_task('我有鸡肉和土豆',initial)['state']['ingredients'],['鸡肉','土豆'])
        restricted=advance_task('我只有鸡肉',initial)['state']
        self.assertIn('我只有鸡肉',restricted['extra_constraints'])
        for q in ('我有鸡肉', '我有鸡肉过敏怎么办？', '我有鸡肉，能保存多久？'):
            turn=advance_task(q,None if q=='我有鸡肉' else initial)
            self.assertEqual(turn['action'],'none',q)
    def test_negation_replaces_taste_and_unknown_conditions_are_not_dropped(self):
        state=advance_task('不辣了',self.initial())['state'];self.assertEqual(state['tastes'],['不辣'])
        state=advance_task('鸡肉',state)['state']
        state=advance_task('不要香菜',state)['state']
        self.assertIn('香菜',state['excluded_ingredients']);self.assertEqual(state['ingredients'],['鸡肉'])
        turn=advance_task('不要太咸',state)
        self.assertEqual(turn['action'],'clarify');self.assertEqual(turn['state'],state)
        state=advance_task('推荐几道菜，不要太咸',None)['state']
        self.assertIn('不要太咸',state['extra_constraints'])
        self.assertEqual(recommend_preferences(state,load_chunks())['answer_mode'],'clarify')
        for q in ('我不喜欢吃辣，推荐几道菜','我不想吃辣，推荐几道菜','推荐几道不辣的菜'):
            self.assertEqual(advance_task(q,None)['state']['tastes'],['不辣'])
    def test_serialization_and_failed_results_do_not_create_choices(self):
        state=json.loads(json.dumps(self.initial()));self.assertTrue(valid_state(state))
        state=remember_choices(state,{'outputs':[{'result':{'status':'validation_failed','recommendations':[{'title':'fake','source':'fake'}]}}]})
        self.assertEqual(state['choices'],[])
    def test_exclusion_updates_are_canonical_reversible_and_latest_wins(self):
        for wording in ('不要带辣椒的','不要辣椒','不放辣椒','别加辣椒','不吃辣椒','去掉辣椒','避开辣椒','不要小米辣','不能吃辣椒','不喜欢辣椒','不要有辣椒的','不要辣椒，其他不变','不要辣椒，给我推荐3道菜'):
            with self.subTest(wording=wording):
                prior=advance_task('我有鸡肉',self.initial())['state']
                turn=advance_task(wording,prior)
                state=turn['state']
                self.assertEqual(turn['action'],'recommend')
                self.assertEqual(state['ingredients'],['鸡肉'])
                self.assertEqual(state['excluded_ingredients'],['辣椒'])
                self.assertEqual(state['tastes'],[])
                self.assertEqual(state['extra_constraints'],[])
                self.assertIn('排除食材：辣椒',turn['question'])
                self.assertEqual(advance_task(wording,state)['state'],state)
                allowed=advance_task('可以放辣椒了',state)['state']
                self.assertEqual(allowed['excluded_ingredients'],[])
                self.assertEqual(allowed['tastes'],[])  # Old preferences do not resurrect.
        state=advance_task('不要带辣椒的，不要油炸，改成2道',prior)['state']
        self.assertEqual(state['excluded_methods'],['油炸'])
        self.assertEqual(state['limit'],2)
        state=advance_task('不要香菜和花生',state)['state']
        self.assertEqual(state['excluded_ingredients'],['辣椒','香菜','花生'])
        state=advance_task('不要鸡肉',state)['state']
        self.assertEqual(state['ingredients'],[])
        state=advance_task('改成鸡肉',state)['state']
        self.assertNotIn('鸡肉',state['excluded_ingredients'])
        self.assertEqual(state['ingredients'],['鸡肉'])
    def test_exclusion_compatibility_and_task_boundaries(self):
        old=self.initial();old.pop('excluded_ingredients',None)
        self.assertTrue(valid_state(old))
        state=advance_task('不要带辣椒的',old)['state']
        self.assertEqual(state['excluded_ingredients'],['辣椒'])
        self.assertEqual(advance_task('不要带辣椒的',None)['action'],'none')
        self.assertIsNone(advance_task('蒸水蛋怎么做',state,named_recipe=True)['state'])
        invalid=copy.deepcopy(state);invalid['excluded_ingredients']=[{}]
        self.assertFalse(valid_state(invalid))
        for q in ('不喜欢八角','不放八角','不要太咸'):
            turn=advance_task(q,state)
            self.assertEqual(turn['action'],'clarify')
            self.assertEqual(turn['state'],state)
        failed=advance_task('我有鸡肉',self.initial())['state']
        failed.pop('excluded_ingredients',None)
        failed['extra_constraints']=['不要带辣椒的']
        original=copy.deepcopy(failed)
        repaired=advance_task('继续推荐',failed)['state']
        self.assertEqual(repaired['extra_constraints'],[])
        self.assertEqual(repaired['excluded_ingredients'],['辣椒'])
        self.assertEqual(repaired['ingredients'],['鸡肉'])
        self.assertEqual(repaired['tastes'],[])
        self.assertEqual(failed,original)


class PreferenceEvidenceTests(unittest.TestCase):
    def query(self,*questions):
        state=None
        for q in questions:state=advance_task(q,state)['state']
        return state,recommend_preferences(state,load_chunks())
    def test_spicy_recommendations_do_not_require_inventory(self):
        _,result=self.query('我喜欢吃辣的，给我推荐几道菜')
        self.assertEqual(result['status'],'verified');self.assertEqual(len(result['recommendations']),3)
        self.assertNotIn('已有',result['short_answer']);self.assertEqual(result['recommendation_kind'],'preference')
    def test_chicken_exclusion_and_exact_current_source_evidence(self):
        _,result=self.query('我喜欢吃辣的，给我推荐几道菜','鸡肉','不要油炸')
        self.assertEqual(result['status'],'verified');self.assertEqual(len(result['recommendations']),3)
        for item in result['evidence']:
            lines=Path(item['source']).read_text().splitlines()
            self.assertEqual(item['quote'],'\n'.join(lines[item['start_line']-1:item['end_line']]))
        for row in result['recommendations']:
            self.assertIn('鸡肉',row['reason']);self.assertIn('未列油炸',row['reason'])
        self.assertTrue(all(r['spice_match']!='optional' for r in result['recommendations']))
    def test_exact_stock_followup_and_repeat_return_rich_spicy_chicken_cards(self):
        state,result=self.query('我喜欢吃辣的，给我推荐3道菜','我有鸡肉。','我有鸡肉。')
        self.assertEqual(state['tastes'],['辣味'])
        self.assertEqual(result['status'],'verified')
        self.assertEqual(len(result['recommendations']),3)
        for row in result['recommendations']:
            self.assertTrue(row['description'])
            self.assertIn('鸡肉',row['reason'])
            self.assertIn('辣',row['reason'])
    def test_excluded_ingredients_are_checked_in_whole_source_including_condiments(self):
        from preference_recommendation import exclusion_pattern
        state,result=self.query('我喜欢吃辣的，给我推荐3道菜','我有鸡肉。','不要带辣椒的')
        self.assertEqual(result['status'],'verified')
        self.assertEqual(len(result['recommendations']),3)
        self.assertIn('排除辣椒',result['short_answer'])
        for row in result['recommendations']:
            source=Path(row['source']).read_text()
            self.assertIsNone(__import__('re').search(exclusion_pattern('辣椒'),source))
            self.assertIn('鸡肉',row['reason']);self.assertIn('排除条件',row['reason'])
            self.assertNotIn('奥尔良',row['title']);self.assertNotIn('盐焗鸡粉','、'.join(row['main_ingredients']))
            self.assertFalse(any('烤盘' in item or '铝箔' in item for item in row['main_ingredients']))
        # Even if optional ingredients or an operation were not retrieved, the
        # complete current source must still rule the candidate out.
        chicken=advance_task('我有鸡肉',advance_task('推荐3道菜',None)['state'])['state']
        chicken=advance_task('不要辣椒',chicken)['state']
        rows=[r for r in load_chunks() if r['title']=='宫保鸡丁' and '辣' not in r['content']]
        self.assertEqual(recommend_preferences(chicken,rows)['status'],'insufficient')
        rows=[r for r in load_chunks() if r['title']=='奥尔良风味烤鸡腿']
        self.assertTrue(rows)
        self.assertEqual(recommend_preferences(chicken,rows)['status'],'insufficient')
    def test_multiple_exclusions_survive_edits_and_no_match_explains_criteria(self):
        state,result=self.query('推荐3道菜','我有鸡肉','不要辣椒','不要花生和香菜','不要油炸')
        self.assertIsNotNone(result)
        for row in result.get('recommendations',[]):
            from preference_recommendation import exclusion_pattern
            for name in state['excluded_ingredients']:
                self.assertIsNone(__import__('re').search(exclusion_pattern(name),Path(row['source']).read_text()))
        result=recommend_preferences(state,[])
        self.assertEqual(result['status'],'insufficient')
        self.assertIn('鸡肉',result['short_answer']);self.assertIn('排除辣椒',result['short_answer'])
        self.assertNotIn('检索到的章节',result['short_answer'])
    def test_other_constraints_and_inventory_are_not_silently_ignored(self):
        for q in ('我有鸡肉，推荐几道菜，还缺什么','推荐几道菜，我对花生过敏','推荐几道清淡的菜'):
            state=advance_task(q,None)['state'];self.assertEqual(recommend_preferences(state,load_chunks())['answer_mode'],'clarify')
    def test_both_screenshot_questions_have_introduction_reason_and_literal_sources(self):
        from preference_recommendation import plain
        for q in ('我喜欢吃辣的，给我推荐几道菜','我有鸡肉给我推荐几道，辣的','推荐几道辣的，我有鸡肉','鸡肉的辣菜推荐一下'):
            state,result=self.query(q)
            self.assertEqual(result['status'],'verified',q);self.assertEqual(len(result['recommendations']),3)
            for row in result['recommendations']:
                self.assertTrue(row['description']);self.assertIn('辣',row['reason']);self.assertTrue(row['main_ingredients'])
                self.assertIn(row['description'],plain('\n'.join(e['quote'] for e in row['evidence'])))
                self.assertNotIn('/condiment/',row['source']);self.assertNotIn('/breakfast/',row['source'])
                self.assertNotEqual(row['spice_match'],'optional')
            self.assertNotIn('已齐',result['short_answer'])
    def test_optional_add_spice_is_labeled_when_it_is_the_only_candidate(self):
        state=advance_task('推荐几道辣菜',None)['state']
        rows=[r for r in load_chunks() if r['title']=='凉拌鸡丝']
        result=recommend_preferences(state,rows)
        self.assertEqual(result['recommendations'][0]['spice_match'],'optional')
        self.assertIn('并非固定辣味',result['recommendations'][0]['notice'])
    def test_changed_or_forged_chunk_content_is_not_recommended(self):
        state=advance_task('我喜欢吃辣的，给我推荐几道菜',None)['state']
        rows=[{**r,'content':r['content']+'\n伪造干辣椒'} for r in load_chunks()]
        self.assertEqual(recommend_preferences(state,rows)['status'],'insufficient')


class PipelineTaskTests(unittest.TestCase):
    def test_pipeline_keeps_state_across_turns_without_free_text_rewriting(self):
        import asyncio
        from dialogue_harness import DialogueHarness
        async def check():
            chat=DialogueHarness()
            questions=('我喜欢吃辣的，给我推荐3道菜','我有鸡肉。','我有鸡肉。','不要油炸','不要带辣椒的','不要香菜和花生','可以放辣椒了')
            for index,q in enumerate(questions):
                p,r,handled=await chat.turn(q)
                self.assertTrue(handled)
                self.assertEqual(p['task']['action'],'recommend')
                self.assertEqual(p['task']['state']['tastes'],['辣味'] if index<4 else [])
            self.assertEqual(chat.state['excluded_ingredients'],['香菜','花生'])
            self.assertEqual(chat.state['ingredients'],['鸡肉'])
            second=chat.session.slots['last_recommendations'][1]
            p,r,handled=await chat.turn('第二道怎么做')
            self.assertTrue(handled)
            self.assertEqual(p['task']['selected_source'],second['source_path'])
            self.assertTrue(all(e['source']==second['source_path'] for e in r['outputs'][0]['result']['evidence']))
        asyncio.run(check())
    def test_known_task_routing_does_not_call_llm_or_embeddings(self):
        from cookbuddy.router import IntentRouter
        registry=type('Registry',(),{'registry':lambda self:[{'id':ident,'target':target,'version':1} for ident,target in [('ingredient_recommendation','rag'),('recipe_qa','rag'),('unclear','clarify')]]})()
        task=advance_task('我喜欢吃辣的，给我推荐几道菜',None)
        with patch('cookbuddy.router.json_model',side_effect=AssertionError('task reclassified')),patch('cookbuddy.router.embed',side_effect=AssertionError('task embedded')):
            decision=IntentRouter(registry).route_task(task)
        self.assertEqual(decision['target'],'rag');self.assertEqual(decision['route_method'],'resolved_task_context')


@unittest.skipUnless(os.environ.get('COOKBUDDY_INTEGRATION_TESTS')=='1','requires PostgreSQL')
class TaskPersistenceTests(unittest.TestCase):
    def test_selected_source_survives_persist_and_two_specific_followups(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from cookbuddy.store import Store
        store=Store();uid=uuid.uuid4().hex;store.ensure_user(uid)
        sid=store.create_conversation(uid,'source followup regression')
        cfg=load_config().model_dump()
        def payload(job,operation,arguments):
            return dict(user_id=uid,session_id=sid,request_id=job,operation=operation,arguments=arguments,hard=[],config=cfg)
        try:
            source=None
            for q in ('查看基础牛奶面包的完整原文和图片。','只看酵头制作的图片。',
                      '酵头需要多少酵母、水温多少、发酵多久？请区分静置时间和发酵时间，并给出依据。'):
                job=uuid.uuid4().hex
                from src.nlu.router import understand
                def offline(question,context,**kwargs):return understand(question,context,use_model=False)
                with patch('src.nlu.router.understand',side_effect=offline):
                    prepared=worker.execute(payload(job,'prepare',{'question':q}))
                if source is None:source=prepared['named_recipe']['source_path']
                else:self.assertEqual(prepared['named_recipe']['dish_name'],'基础牛奶面包')
                self.assertEqual(prepared['named_recipe']['source_path'],source)
                worker.execute(payload(job,'persist',{'question':q,'prepared':prepared,
                    'result':{'status':'completed','outputs':[]},'text':'验收占位回答，不作菜谱依据'}))
        finally:
            with store.connect(admin=True) as db:
                db.execute('DELETE FROM cookbuddy.messages WHERE user_id=%s',(uid,))
                db.execute('DELETE FROM cookbuddy.conversations WHERE user_id=%s',(uid,))
                db.execute('DELETE FROM cookbuddy.tenants WHERE id=%s',(uid,))

    def test_restart_conversation_and_tenant_isolation_and_task_boundary(self):
        from cookbuddy.store import Store
        store=Store();a=uuid.uuid4().hex;b=uuid.uuid4().hex;store.ensure_user(a);store.ensure_user(b)
        ca=store.create_conversation(a,'task context test');cb=store.create_conversation(a,'other task test')
        try:
            state=advance_task('我喜欢吃辣的，给我推荐几道菜',None)['state']
            store.append(a,ca,uuid.uuid4().hex,'assistant','推荐结果',{'task_context':state})
            job=uuid.uuid4().hex;store.append(a,ca,job,'user','鸡肉')
            self.assertEqual(Store().task_context(a,ca,job),state)
            self.assertIsNone(Store().task_context(a,cb,job));self.assertIsNone(Store().task_context(b,ca,job))
            store.append(a,ca,job,'assistant','新任务',{'task_context':None})
            self.assertIsNone(Store().task_context(a,ca,uuid.uuid4().hex))
        finally:
            with store.connect(admin=True) as db:
                db.execute('DELETE FROM cookbuddy.messages WHERE user_id=ANY(%s)',([a,b],))
                db.execute('DELETE FROM cookbuddy.conversations WHERE user_id=ANY(%s)',([a,b],))
                db.execute('DELETE FROM cookbuddy.tenants WHERE id=ANY(%s)',([a,b],))

if __name__=='__main__':unittest.main()
