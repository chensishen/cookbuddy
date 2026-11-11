import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

import langchain_service_worker as worker
from cookbuddy.session import SessionState


class MemoryCommandFlowTests(unittest.TestCase):
    def test_explicit_preference_write_and_read_bypass_recipe_routing(self):
        from cookbuddy.ltm.management import parse_command

        read='你记得我哪些饮食偏好?'
        for question in ('请记住，我喜欢微辣，不吃香菜。',read):
            with self.subTest(question=question):
                session=SessionState('u','s')
                management={'decision':'UPDATE','message':'微辣；不吃香菜','needs_confirmation':False}
                runtime=SimpleNamespace(enabled=True,level=5,session=session,previous_task=None,
                    hard=[],events=[],confirmation=None,management=management,notice='',candidates=[],
                    apply_soft=lambda *args:None,context_items=lambda **kwargs:[])
                with patch('cookbuddy.store.Store'),\
                        patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=runtime),\
                        patch('src.nlu.router.understand',side_effect=AssertionError('recipe routing called')):
                    prepared=worker.prepare({'user_id':'u','session_id':'s','request_id':'job','question':question})
                self.assertEqual(prepared['task']['action'],'none')
                self.assertEqual(prepared['management'],management)
        self.assertEqual(parse_command(read),('read',''))

    def test_management_command_skips_recipe_reduction_and_preserves_slots(self):
        session=SessionState('u','s')
        session.slots['temp_constraints']={'tastes':['辣味']}
        runtime=SimpleNamespace(
            enabled=True,level=5,session=session,previous_task=None,hard=[],events=[],
            confirmation=None,management={'decision':'NOOP','message':'已处理记忆请求','needs_confirmation':False},
            notice='',candidates=[],apply_soft=lambda *args:None,context_items=lambda **kwargs:[])
        store=MagicMock()
        store.session_context.return_value=session
        with patch('cookbuddy.store.Store',return_value=store),\
                patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=runtime),\
                patch('src.nlu.router.understand') as understand:
            prepared=worker.prepare({'user_id':'u','session_id':'s','request_id':'job','question':'查看我的偏好'})

        understand.assert_not_called()
        store.save_task_context.assert_not_called()
        self.assertEqual(prepared['task']['action'],'none')
        self.assertIsNone(prepared['task']['state'])
        self.assertEqual(prepared['nlu']['intent'],'preference_update')
        self.assertEqual(session.slots['temp_constraints'],{'tastes':['辣味']})
        self.assertEqual(session.messages[-1],{'role':'user','content':'查看我的偏好'})

    def test_memory_confirmation_skips_recipe_understanding(self):
        session=SessionState('u','s')
        confirmation={'decision':'UPDATE','message':'已按确认更新限制。','needs_confirmation':False}
        runtime=SimpleNamespace(
            enabled=True,level=5,session=session,previous_task=None,hard=[],events=[],
            confirmation=confirmation,management=None,notice='',candidates=[],
            apply_soft=lambda *args:None,context_items=lambda **kwargs:[])
        store=MagicMock()
        store.session_context.return_value=session
        with patch('cookbuddy.store.Store',return_value=store),\
                patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=runtime),\
                patch('src.nlu.router.understand') as understand:
            prepared=worker.prepare({'user_id':'u','session_id':'s','request_id':'job',
                                     'question':'确认忘记对花生过敏这条记录。'})

        understand.assert_not_called()
        self.assertEqual(prepared['task']['action'],'none')
        self.assertEqual(prepared['nlu']['intent'],'preference_update')
        self.assertEqual(prepared['management'],confirmation)

    def test_memory_only_turn_does_not_schedule_background_extraction(self):
        from src.agent.config import Config
        from src.agent.context import AppContext
        from src.agent.runtime import Run
        queue=MagicMock()
        run=Run(Config(),AppContext('u','s'),MagicMock(),'确认忘记对花生过敏这条记录。',None,queue)
        run.prepared={'enabled':True,'task':{'action':'none'},
                      'nlu':{'answer_mode':'memory','intent':'preference_update'}}
        run.submit_memory()
        queue.submit.assert_not_called()


if __name__=='__main__':unittest.main()
