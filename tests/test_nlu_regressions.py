"""The reported failures exercise the public engine and real prepare/detail seams."""
import copy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_tools import read_recipe
from cookbuddy.session import SessionState
from cookbuddy.task_context import advance_task


def prepared(question,selected=False):
    from langchain_service_worker import prepare
    state=advance_task('我喜欢吃辣的，推荐3道菜',None)['state']
    state['choices']=[{'title':v,'source':'data/HowToCook/dishes/meat_dish/'+v+'.md'} for v in ['红烧肉','宫保鸡丁','可乐鸡翅']]
    session=SessionState('test-user','test-session')
    session.slots['last_recommendations']=[{'idx':i,'dish_name':v['title'],'source_path':v['source']} for i,v in enumerate(state['choices'],1)]
    if selected:
        from recipe_tools import resolve_title,load_chunks
        title,source=resolve_title('基础牛奶面包',load_chunks())
        session.slots['selected_dish']={'idx':0,'dish_name':title,'source_path':source}
    runtime=SimpleNamespace(enabled=True,level=6,session=session,previous_task=state,hard=[],events=[],
        confirmation=None,management=None,notice='',apply_soft=lambda *a:None,context_items=lambda **kw:[])
    store=MagicMock();store.session_context.return_value=session;store.task_context.return_value=state;store.memory_epoch.return_value=0
    with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=runtime):
        return prepare({'user_id':'test-user','session_id':'test-session','request_id':'test-request','question':question})


class ReportedRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_identity_never_searches(self):
        from agent.test_online import Services,configuration
        from agent.fakes import ScriptedModel
        from src.agent.engine import ChatEngine
        from src.agent.context import AppContext
        from langgraph.checkpoint.memory import InMemorySaver
        services=Services();original=services.call
        async def call(op,args,*a,**kw):
            if op=='prepare':return prepared(args['question'])
            return await original(op,args,*a,**kw)
        services.call=call
        engine=ChatEngine(configuration(),services,InMemorySaver(),model=ScriptedModel())
        try:
            result=await engine.answer('你是谁',AppContext('test-user','test-session'))
            self.assertIn('cookbuddy',result['text'])
            self.assertFalse(any(c[0]=='search' for c in services.calls))
        finally:await engine.memory_queue.close()

    async def test_section_filler_keeps_same_evidence(self):
        from src.agent.turn_intents import chapter_request
        def detail(q):
            p=prepared(q,True)
            section=(p.get('nlu',{}).get('slots',{}).get('section') or chapter_request(q))
            return read_recipe('基础牛奶面包',section)
        clean=detail('只看酵头制作');noisy=detail('只看酵头制作部分')
        self.assertEqual(noisy['status'],'verified_source')
        self.assertEqual(clean['evidence'],noisy['evidence'])

    async def test_rejected_ordinal_with_noise(self):
        clean=prepared('不要刚才第二道');noisy=prepared('不要刚才第二道1')
        self.assertEqual(noisy['task']['action'],'recommend')
        self.assertIn('宫保鸡丁',noisy['task'].get('exclude_dishes',[]))
        self.assertEqual(clean['task']['exclude_dishes'],noisy['task']['exclude_dishes'])
        self.assertIn('1',noisy.get('nlu',{}).get('ignored_tokens',[]))

    async def test_trace_contains_no_user_text(self):
        from agent.test_online import Services,configuration
        from agent.fakes import ScriptedModel
        from src.agent.engine import ChatEngine
        from src.agent.context import AppContext
        from src.agent.runtime import LOGGER
        from langgraph.checkpoint.memory import InMemorySaver
        from src.nlu.router import understand
        services=Services();original=services.call
        secret='你是谁-private-input-marker'
        async def call(op,args,*a,**kw):
            if op=='prepare':return {'task':{'action':'none'},'enabled':False,'nlu':understand(secret,use_model=False)}
            return await original(op,args,*a,**kw)
        services.call=call
        engine=ChatEngine(configuration(),services,InMemorySaver(),model=ScriptedModel())
        try:
            with self.assertLogs(LOGGER,level='INFO') as logs:
                r=await engine.answer(secret,AppContext('u','s'))
            self.assertNotIn(secret,'\n'.join(logs.output))
            self.assertNotIn(secret,str(r['trace']))
            self.assertEqual(r['trace']['tool_calls'],0)
        finally:await engine.memory_queue.close()
