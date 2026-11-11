"""Regressions for the human preference test sequence."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.ltm.management import parse_command
from cookbuddy.ltm.runtime import MemoryRuntime
from cookbuddy.session import SessionState
from cookbuddy.task_context import advance_task


class MemoryFeedbackTests(unittest.TestCase):
    def test_recall_user_wording_bypasses_recipe_search(self):
        for q in ('我之前告诉过你什么口味偏好？','我之前告诉过你哪些饮食偏好？','我的口味偏好是什么？'):
            self.assertEqual(parse_command(q),('read','taste' if '口味' in q else ''),q)
        self.assertIsNone(parse_command('按我的口味推荐两道菜，分别说明推荐理由。'))

    def test_taste_recall_excludes_inventory_without_changing_memory(self):
        from cookbuddy.ltm.management import handle_command
        prefs=MagicMock();prefs.prefs.return_value=[
            SimpleNamespace(dimension='flavor',value='清淡',status='active'),
            SimpleNamespace(dimension='inventory',value='鸡蛋',status='active')]
        result=handle_command(parse_command('我之前告诉过你什么口味偏好？'),prefs,'u','s','j')
        self.assertIn('清淡',result['message']);self.assertNotIn('鸡蛋',result['message'])
        prefs.apply.assert_not_called()

    def test_recommendation_reason_is_a_response_field(self):
        result=advance_task('按我的口味推荐两道菜，分别说明推荐理由。',None)
        self.assertEqual(result['action'],'recommend')
        self.assertEqual(result['state']['limit'],2)
        self.assertEqual(result['state']['extra_constraints'],[])
        self.assertIn('reason',result['state']['response_fields'])
        unknown=advance_task('按我的口味推荐两道菜，分别说明热量。',None)
        self.assertTrue(unknown['state']['extra_constraints'])

    def test_repeated_explicit_memory_acknowledges_saved_value(self):
        store=MagicMock();store.session_context.return_value=SessionState('u','s')
        store.connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value=[]
        prefs=MagicMock();prefs.enabled.return_value=True;prefs.confirm.return_value=None
        prefs.pending_message.return_value='';prefs.load_profile.return_value={'hard_constraints':[],'soft_prefs':[]}
        prefs.apply.return_value={'decision':'NOOP','id':'p','message':'已处理','needs_confirmation':False}
        prefs.prefs.return_value=[SimpleNamespace(id='p',dimension='flavor',value='清淡',polarity='like',strength='soft',status='active')]
        with patch('cookbuddy.ltm.runtime.ProfileStore',return_value=prefs):
            memory=MemoryRuntime(store,'u','s','j','请记住：我喜欢清淡口味。')
        self.assertIn('清淡',memory.management['message'])
        self.assertIn('记住',memory.management['message'])


if __name__=='__main__':unittest.main()
