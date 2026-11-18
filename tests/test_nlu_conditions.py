import copy
import unittest
from unittest.mock import patch
from test_nlu_regressions import prepared
from src.nlu.router import understand,validate
from src.dialogue import reduce_turn
from cookbuddy.session import SessionState
from cookbuddy.task_context import advance_task
from recipe_tools import load_chunks
from preference_recommendation import recommend_preferences

class ConditionTests(unittest.TestCase):
    def test_exclusive_stock_and_missing_ingredients_are_not_clarifications(self):
        for question in ('我只有鸡腿肉，另外推荐两道菜，告诉我还缺什么。',
                         '手头只剩鸡腿肉，推荐两道菜，列出缺料。'):
            with self.subTest(question=question):
                plan=understand(question,use_model=False)
                self.assertEqual(plan['answer_mode'],'grounded_recipe')
                task,_=reduce_turn(plan,SessionState('u','s'),None,load_chunks())
                state=task['state']
                self.assertEqual(state['ingredients'],['鸡腿肉'])
                self.assertEqual(state['limit'],2)
                self.assertEqual(state['response_fields'],['missing_ingredients'])
                self.assertEqual(state['extra_constraints'],[])
                self.assertEqual(state['mode'],'inventory')

    def test_chicken_leg_recipe_missing_ingredients_exclude_quantity_text(self):
        from check_embedding import ROOT
        from ingredient_recommendation import recipe_ingredient_fields
        for title in ('意式烤鸡','姜葱捞鸡'):
            with self.subTest(title=title):
                rows=[row for row in load_chunks() if row['title']==title]
                required,_,_,_,_=recipe_ingredient_fields(rows,(ROOT/rows[0]['source']).read_text().splitlines())
                self.assertIn('盐',required)
                self.assertIn('鸡腿肉',required)
                self.assertNotIn('鸡腿',required)
                self.assertNotIn('约',required)
                self.assertFalse(any('用量' in item for item in required))

    def test_complete_profile_request_clears_model_invented_unknown(self):
        q='今天临时不要香菜，按我的口味推荐两道菜。'
        model=validate({'intent':'ingredient_recommend','confidence':.95,'slots':{
            'limit':2,'constraints':{'unresolved':['按我的口味']}}})
        result=understand(q,use_model=True,model_call=lambda *a:model)
        self.assertFalse(result['slots']['constraints'].get('unresolved'))
        self.assertIn('香菜',result['slots']['constraints']['excluded_ingredients'])
        unknown=understand('今天临时不要香菜，按我的口味推荐两道菜，预算不超过5元。',
                           use_model=True,model_call=lambda *a:model)
        self.assertTrue(unknown['slots']['constraints'].get('unresolved'))

    def test_ordinal_forms_refer_to_same_list_entry(self):
        for q in ['不要第二道','别要第 2 个','第二个不要了','不要刚才第二道1']:
            with self.subTest(q=q):
                p=prepared(q)
                self.assertEqual(p['task']['action'],'recommend')
                self.assertIn('宫保鸡丁',p['task']['exclude_dishes'])

    def test_out_of_range_one_question_and_choices(self):
        p=prepared('不要第五道')
        self.assertEqual(p['nlu']['answer_mode'],'clarify')
        self.assertIn('3道',p['nlu']['clarify_question'])
        self.assertGreaterEqual(len(p['nlu']['options']),2)
        self.assertEqual(p['nlu']['clarify_question'].count('？'),1)

    def test_unknown_condition_is_a_useful_result_not_none(self):
        state=advance_task('推荐鸡肉菜，不要太咸',None)['state']
        r=recommend_preferences(state,load_chunks())
        self.assertIsInstance(r,dict);self.assertEqual(r['answer_mode'],'clarify')
        self.assertEqual(r['pending_constraints'],state['extra_constraints'])
        self.assertEqual(len(r['clarification_options']),2)

    def test_independent_recommendation_drops_stale_unknown_constraint(self):
        previous=advance_task('推荐鸡肉菜，不要太咸',None)['state']
        task=advance_task('推荐一道鸡肉菜',previous)
        self.assertEqual(task['action'],'recommend')
        self.assertEqual(task['state']['extra_constraints'],[])

        session=SessionState('u','s')
        plan=understand('推荐一道鸡肉菜',{'slots':session.slots,'previous_task':previous},use_model=False)
        reduced,_=reduce_turn(plan,session,previous,load_chunks())
        self.assertEqual(reduced['action'],'recommend')
        self.assertEqual(reduced['state']['extra_constraints'],[])

    def test_continuation_keeps_unresolved_constraint(self):
        previous=advance_task('推荐鸡肉菜，不要太咸',None)['state']
        task=advance_task('继续推荐',previous)
        self.assertEqual(task['state']['extra_constraints'],['不要太咸'])

    def test_understanding_cannot_drop_literal_exclusions_or_response_fields(self):
        p=prepared('我有鸡肉，推荐两道菜，不要花生，说明推荐理由')
        session=SessionState('u','s')
        plan=validate({'intent':'ingredient_recommend','confidence':.95,'slots':{'ingredients':['鸡肉'],'mode':'inventory','limit':2}})
        plan.update(path='model',normalized='我有鸡肉，推荐两道菜，不要花生，说明推荐理由',answer_mode='grounded_recipe')
        plan=understand(plan['normalized'],use_model=True,model_call=lambda *a:plan)
        task,_=reduce_turn(plan,session,None,load_chunks())
        self.assertIn('花生',task['state']['excluded_ingredients']);self.assertIn('reason',task['state']['response_fields'])

    def test_no_recipe_context_needs_one_question(self):
        r=understand('只看酵头制作',use_model=False)
        self.assertEqual(r['answer_mode'],'clarify');self.assertTrue(r['options'])

    def test_hard_constraint_confirmation_is_still_authoritative(self):
        from cookbuddy.ltm.management import parse_command
        from langchain_service_worker import memory_authorized
        self.assertFalse(memory_authorized('forget_preference',{'dimension':'allergy','value':'花生'},'推荐不辣的菜'))
        self.assertTrue(parse_command('忘记我的花生过敏'))

    def test_ignoring_same_low_risk_clarification_does_not_loop(self):
        from recipe_tools import resolve_title
        title,source=resolve_title('基础牛奶面包',load_chunks())
        context={'slots':{'selected_dish':{'dish_name':title,'source_path':source}}}
        first=understand('只看制作',context,use_model=False)
        self.assertEqual(first['answer_mode'],'clarify')
        context['slots']['nlu_pending']={'slot':'section','options':first['options']}
        second=understand('只看制作',context,use_model=False)
        self.assertEqual(second['answer_mode'],'grounded_recipe')
        self.assertTrue(second['assumptions'])

    def test_explicit_unknown_fragment_choice_keeps_separate_hard_guard(self):
        session=SessionState('u','s');session.slots['nlu_pending']={'slot':'conditions','options':['保留条件并补充说明','先按已理解条件推荐']}
        previous=advance_task('推荐鸡肉菜，不要太咸',None)['state']
        plan=understand('先按已理解条件推荐',{'slots':session.slots,'previous_task':previous},use_model=False)
        plan['path']='model'
        task,_=reduce_turn(plan,session,previous,load_chunks())
        self.assertEqual(task['action'],'recommend');self.assertEqual(task['state']['extra_constraints'],[])
        self.assertEqual(previous['extra_constraints'],['不要太咸'])

    def test_model_copied_taste_is_not_a_new_explicit_memory_override(self):
        session=SessionState('u','s');previous=advance_task('推荐3道辣菜',None)['state']
        previous['memory_tastes']=['辣味']
        plan=validate({'intent':'constraint_refine','confidence':.9,'slots':{'constraints':{'taste':'辣味'}}})
        plan.update(path='model',normalized='继续推荐',original='继续推荐',answer_mode='grounded_recipe')
        task,_=reduce_turn(plan,session,previous,load_chunks())
        self.assertFalse(any(e['slot']=='tastes' for e in task['edits']))
        self.assertEqual(task['state']['memory_tastes'],task['state']['tastes'])

    def test_condition_clarification_preserves_literal_requested_count(self):
        previous=advance_task('推荐一道清淡的菜。',None)['state']
        session=SessionState('u','s')
        session.slots['nlu_pending']={'slot':'conditions',
            'options':['保留条件并补充说明','先按已理解条件推荐'],'state':copy.deepcopy(previous)}
        plan=understand('先按已理解条件推荐',
            {'slots':session.slots,'previous_task':previous},use_model=False)
        task,_=reduce_turn(plan,session,previous,load_chunks())
        self.assertEqual(task['action'],'recommend')
        self.assertEqual(task['state']['limit'],1)
        self.assertEqual(task['state']['extra_constraints'],[])
