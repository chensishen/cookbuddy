"""Keep recommendation identities stable while inspecting and replacing recipes."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_browser_repair_20261022 as browser_tests
from cookbuddy.task_context import advance_task,remember_choices
from cookbuddy.session import update_slots
from recipe_tools import load_chunks


class OrdinalFollowupTests(unittest.TestCase):
    def setUp(self):
        self.state=advance_task('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。',None)['state']
        self.state['choices']=[{'title':title,'source':next(r['source'] for r in load_chunks() if r['title']==title)}
                              for title in ('西红柿炒鸡蛋','炒滑蛋')]

    def prepare(self,q,state,session=None):
        import langchain_service_worker as worker
        store,memory=browser_tests.BrowserRepairTests().runtime(q,state)
        if session:memory.session=session
        update_slots(memory.session,{'state':state})
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
            prepared=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
        return prepared,memory.session

    def test_first_steps_then_second_keeps_order(self):
        for first in ('第一道怎么做？','我要第一道的步骤','给我第一道步骤','西红柿炒鸡蛋怎么做？','查看西红柿炒鸡蛋的完整原文和图片。'):
            with self.subTest(first=first):
                p,session=self.prepare(first,copy.deepcopy(self.state))
                self.assertIn(p['task']['action'],('followup','none'))
                self.assertEqual(p['task']['state'],self.state)
                self.assertEqual([r['dish_name'] for r in session.slots['last_recommendations']],['西红柿炒鸡蛋','炒滑蛋'])
                for second in ('第二道怎么做？请给我用料和完整步骤。','第二道的步骤','第二道呢？'):
                    following,_=self.prepare(second,p['task']['state'],copy.deepcopy(session))
                    self.assertEqual(following['task']['action'],'followup',second)
                    self.assertEqual(following['task']['selected_source'],self.state['choices'][1]['source'])

    def test_targeted_replacement_preserves_second_and_excludes_old(self):
        for q in ('换第一道菜','换第一道，不要刚才第二道。','换掉刚才第一道，不要重复。1'):
            with self.subTest(q=q):
                p,session=self.prepare(q,copy.deepcopy(self.state))
                task=p['task']
                self.assertEqual(task['action'],'recommend')
                self.assertEqual(task['replacement_index'],1)
                self.assertEqual(set(task['exclude_dishes']),{'西红柿炒鸡蛋','炒滑蛋'})
                result={'outputs':[{'result':{'status':'verified','recommendations':[{'title':'新菜','source':'new.md'}]}}]}
                state=remember_choices(task['state'],result,replacement_index=task['replacement_index'])
                self.assertEqual(state['limit'],2)
                self.assertEqual(state['choices'],[{'title':'新菜','source':'new.md'},self.state['choices'][1]])
                task['state']=state
                update_slots(session,task,result)
                self.assertEqual([r['dish_name'] for r in session.slots['last_recommendations']],['新菜','炒滑蛋'])
                self.assertEqual(advance_task('第二道怎么做？',state)['selected_source'],self.state['choices'][1]['source'])

    def test_invalid_replacement_retains_choices(self):
        p,_=self.prepare('换第三道菜',copy.deepcopy(self.state))
        self.assertEqual(p['task']['action'],'clarify')
        self.assertEqual(p['task']['state'],self.state)

    def test_counted_replacement_preserves_conditions_and_excludes_old(self):
        for q,count in (('换俩道菜',2),('换两道菜',2),('换2道菜',2),('我要换俩道',2),('请帮我换三道菜',3),('换一道菜',1)):
            with self.subTest(q=q):
                p,_=self.prepare(q,copy.deepcopy(self.state))
                self.assertEqual(p['task']['action'],'recommend')
                self.assertEqual(p['task']['state']['limit'],count)
                self.assertEqual(p['task']['state']['ingredients'],self.state['ingredients'])
                self.assertEqual(p['task']['state']['response_fields'],self.state['response_fields'])
                self.assertEqual(set(p['task']['exclude_dishes']),{'西红柿炒鸡蛋','炒滑蛋'})
                self.assertNotIn('replacement_index',p['task'])
                self.assertIsNone(p['management'])

    def test_counted_replacement_rejects_invalid_counts(self):
        for q in ('换0道菜','换11道菜'):
            p,_=self.prepare(q,copy.deepcopy(self.state))
            self.assertEqual(p['task']['action'],'clarify')
            self.assertEqual(p['task']['state'],self.state)

    def test_counted_replacement_applies_new_constraint_and_updates_count(self):
        state=copy.deepcopy(self.state);state['limit']=1
        p,_=self.prepare('换俩道菜，不要香菜',state)
        self.assertEqual(p['task']['action'],'recommend')
        self.assertEqual(p['task']['state']['limit'],2)
        self.assertEqual(p['task']['state']['excluded_ingredients'],['香菜'])
        self.assertTrue(any(c['value']=='香菜' for c in p['hard']))

    def test_topic_boundary_still_closes_list(self):
        p,session=self.prepare('换个话题，查看基础牛奶面包的完整原文和图片。',copy.deepcopy(self.state))
        self.assertIsNone(p['task']['state'])
        self.assertEqual(session.slots['last_recommendations'],[])
