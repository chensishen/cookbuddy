"""Reported recommendation -> replacement -> ingredients -> chapter sequence."""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.session import SessionState
from cookbuddy.task_context import advance_task
from recipe_tools import load_chunks,resolve_title
from src.nlu.router import understand


class LongDialogueTests(unittest.TestCase):
    def test_ten_turns_cross_history_window_without_losing_recipe_or_stock(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from src.agent.engine import ChatEngine
        session=SessionState('u','s');checkpoint={'state':None}
        cfg=load_config();cfg=cfg.model_copy(update={'memory':cfg.memory.model_copy(update={'mode':'M5'})})
        store=MagicMock();store.session_context.side_effect=lambda *a:session
        store.task_context.side_effect=lambda *a:copy.deepcopy(checkpoint['state'])
        def save_task(*args):checkpoint['state']=copy.deepcopy(args[-1]['state'])
        store.save_task_context.side_effect=save_task
        def runtime(*args):
            return SimpleNamespace(enabled=True,level=5,session=session,previous_task=copy.deepcopy(checkpoint['state']),
                hard=[],events=[],confirmation=None,management=None,notice='',candidates=[],
                apply_soft=lambda *a:None,context_items=lambda **kw:[])
        def offline(question,context,**kw):return understand(question,context,use_model=False)
        queries=['我有番茄和鸡蛋，推荐两道菜，说明还缺什么。',
                 '我有番茄和鸡蛋，推荐三道菜，说明还缺什么。',
                 '第三道怎么做？请给我用料和完整步骤。','换一道',
                 '这道还需要准备哪些食材？','操作','谢谢','这道还需要准备哪些食材？','计算','操作']
        async def check():
            replacement=None
            for index,q in enumerate(queries):
                previous_titles=[r['dish_name'] for r in session.slots['last_recommendations']]
                p=worker.prepare(dict(user_id='u',session_id='s',request_id='j'+str(index),question=q))
                async def service(op,args,timeout):
                    return worker.execute(dict(operation=op,arguments=args,user_id='u',session_id='s',
                        request_id='j'+str(index),hard=[],config=cfg.model_dump()))
                async def guard(compact,full):return compact,full
                async def emit(*args):pass
                run=SimpleNamespace(question=q,prepared=p,cfg=cfg,context=SimpleNamespace(remaining=lambda n:n),
                    tool_calls=0,evidence={},degraded=False,stages=[],service=service,guard=guard,emit=emit)
                self.assertTrue(await ChatEngine._direct(None,run),q)
                result={'status':'completed','outputs':[{'tool':e['name'],'result':e['full']} for e in run.evidence.values()]}
                self.assertTrue(result['outputs'],(index,q,p))
                data=result['outputs'][-1]['result']
                self.assertNotEqual(p['task']['action'],'clarify',q)
                if index==3:
                    self.assertEqual(len(data['recommendations']),1)
                    row=data['recommendations'][0];replacement=row['source']
                    self.assertNotIn(row['title'],previous_titles)
                    self.assertTrue(set(row['matched']) & {'番茄','鸡蛋'})
                if index in (4,7):
                    self.assertEqual(p['task']['action'],'ingredients')
                    self.assertEqual(p['task']['selected_source'],replacement)
                    self.assertEqual(data['answer_layout'],'inventory_check')
                if index in (5,8,9):
                    self.assertEqual(p['nlu']['intent'],'recipe_section')
                    self.assertTrue(data['evidence'])
                    self.assertTrue(all(e['source']==replacement and q in e['section'] for e in data['evidence']))
                worker.execute(dict(operation='persist',arguments={'question':q,'prepared':p,'result':result,'text':''},
                    user_id='u',session_id='s',request_id='j'+str(index),hard=[],config=cfg.model_dump()))
                self.assertEqual(session.slots['ingredients_on_hand'],['番茄','鸡蛋'])
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',side_effect=runtime),\
                patch('src.nlu.router.understand',side_effect=offline):
            asyncio.run(check())

    def context(self,title='酱炖蟹'):
        name,source=resolve_title(title,load_chunks())
        state=advance_task('我有番茄和鸡蛋，推荐一道菜，说明还缺什么。',None)['state']
        state['choices']=[{'title':name,'source':source}]
        session=SessionState('u','s')
        session.slots['last_recommendations']=[dict(idx=1,dish_name=name,source_path=source)]
        session.slots['ingredients_on_hand']=['番茄','鸡蛋']
        return session,state

    def test_replacement_uses_same_inventory_matching_as_initial_recommendation(self):
        from src.agent.engine import ChatEngine
        from src.agent.config import load_config
        import langchain_service_worker as worker
        session,state=self.context('炒滑蛋')
        state['limit']=1
        cfg=load_config();cfg=cfg.model_copy(update={'memory':cfg.memory.model_copy(update={'mode':'M5'})})
        task={'action':'recommend','state':state,'exclude_dishes':['鸡蛋花','西红柿炒鸡蛋','炒滑蛋']}
        calls=[]
        async def check():
            async def service(op,args,timeout):
                calls.append((op,args))
                return worker.execute(dict(operation=op,arguments=args,user_id='u',session_id='s',request_id='j',hard=[],config=cfg.model_dump()))
            async def guard(compact,full):return compact,full
            async def emit(*args):pass
            run=SimpleNamespace(question='换一道',prepared={'task':task,'slots':session.slots},cfg=cfg,
                context=SimpleNamespace(remaining=lambda n:n),tool_calls=0,evidence={},degraded=False,stages=[],
                service=service,guard=guard,emit=emit)
            self.assertTrue(await ChatEngine._direct(None,run))
            return list(run.evidence.values())[-1]['full']
        result=asyncio.run(check())
        self.assertEqual(calls[0][0],'recommend')
        self.assertTrue(result['recommendations'])
        for row in result['recommendations']:
            self.assertNotIn(row['title'],task['exclude_dishes'])
            self.assertTrue(set(row['matched']) & {'番茄','鸡蛋'})

    def test_tomato_sauce_does_not_prove_tomato_requirement(self):
        from preference_recommendation import recommend_preferences
        session,state=self.context()
        rows=[r for r in load_chunks() if r['title']=='酱炖蟹']
        with patch('cookbuddy.ltm.guard.current',return_value=[]):
            result=recommend_preferences(state,rows)
        for row in result.get('recommendations',[]):
            self.assertNotIn('番茄',row['inventory_matches'])
            self.assertNotIn('番茄',row['matched'])
            self.assertNotIn('用料包含番茄',row['reason'])
        self.assertIn('本次推荐尚未用到你提供的：番茄',result['short_answer'])

    def test_ingredients_followup_survives_model_section_misclassification(self):
        from langchain_service_worker import prepare
        from src.agent.config import load_config
        from src.nlu.router import validate
        session,state=self.context()
        runtime=SimpleNamespace(enabled=True,level=6,session=session,previous_task=state,hard=[],events=[],
            confirmation=None,management=None,notice='',candidates=[],apply_soft=lambda *a:None,context_items=lambda **kw:[])
        store=MagicMock();store.session_context.side_effect=lambda *a:session;store.task_context.return_value=state
        model=validate({'intent':'recipe_section','confidence':.95,'slots':{'dish':'酱炖蟹','section':'计算'},
            'needs_clarification':True,'clarify_question':'你想查看哪个章节？','options':['简介','计算','操作']})
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=runtime),\
                patch('cookbuddy.ltm.profile_store.ProfileStore') as prefs,patch('service_runtime.RuntimeStore') as cache,\
                patch('src.nlu.router._model',return_value=model):
            prefs.return_value.enabled.return_value=True;cache.return_value.get.return_value=None
            result=prepare(dict(user_id='u',session_id='s',request_id='j',question='这道还需要准备哪些食材？',config=load_config().model_dump()))
        self.assertEqual(result['nlu']['answer_mode'],'grounded_recipe')
        self.assertEqual(result['task']['action'],'ingredients')
        self.assertEqual(result['task']['selected_source'],state['choices'][0]['source'])

    def test_bare_chapter_after_single_replacement_uses_that_recipe(self):
        session,state=self.context()
        session.slots['nlu_pending']={'slot':'section','intent':'recipe_section','options':['简介','计算','操作']}
        result=understand('操作',{'slots':session.slots,'previous_task':state},use_model=False)
        self.assertEqual(result['answer_mode'],'grounded_recipe',result)
        self.assertEqual(result['intent'],'recipe_section')
        self.assertEqual(result['slots']['dish'],'酱炖蟹')
        self.assertEqual(result['slots']['section'],'操作')

    def test_bare_chapter_without_pending_question_keeps_single_recipe(self):
        session,state=self.context()
        result=understand('操作',{'slots':session.slots,'previous_task':state},use_model=False)
        self.assertEqual(result['answer_mode'],'grounded_recipe')
        self.assertEqual(result['slots']['dish'],'酱炖蟹')
        self.assertEqual(result['slots']['section'],'操作')

    def test_ingredient_followup_drops_model_invented_recipe_and_section(self):
        session,state=self.context()
        with patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            result=understand('这道还需要准备哪些食材？',{'slots':session.slots,'previous_task':state},
                model_call=lambda *a:dict(intent='recipe_section',confidence=.95,
                    slots={'dish':'不存在的食谱名称','section':'未知章节'}))
        self.assertEqual(result['answer_mode'],'grounded_recipe')
        self.assertEqual(result['intent'],'follow_up')
        self.assertNotIn('unmatched_dish',result)
        self.assertEqual(result['slots']['dish'],'酱炖蟹')

    def test_multiple_unselected_recipes_do_not_become_a_guessed_target(self):
        session,state=self.context()
        name,source=resolve_title('炒滑蛋',load_chunks())
        session.slots['last_recommendations'].append(dict(idx=2,dish_name=name,source_path=source))
        result=understand('这道还需要准备哪些食材？',{'slots':session.slots,'previous_task':state},use_model=False)
        self.assertEqual(result['intent'],'follow_up')
        self.assertEqual(result['slots']['dish'],'')


if __name__=='__main__':unittest.main()
