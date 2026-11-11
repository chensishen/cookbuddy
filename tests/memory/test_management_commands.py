import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from cookbuddy.ltm.management import parse_command,handle_command
from cookbuddy.ltm.extractor import extract
from cookbuddy.ltm.models import Candidate
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.runtime import MemoryRuntime
from cookbuddy.session import SessionState


class ManagementCommandTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.prefs=ProfileStore(path=Path(tmp.name)/'memory.db')

    def seed(self,c,turn='seed',uid='u'):
        return self.prefs.apply(uid,'s',turn,c)

    def handle(self,q,turn='command'):
        return handle_command(parse_command(q),self.prefs,'u','s',turn)

    def test_read_variants_and_noncommands(self):
        for q in ('你还记得我的哪些饮食偏好？','你记得我的偏好吗？','查看我的画像',
                  '我的饮食偏好有哪些？','请列出我的忌口','告诉我你记得的我的偏好',
                  '我有哪些饮食偏好？','我还保留了哪些偏好？'):
            with self.subTest(q=q):self.assertEqual(parse_command(q),('read',''))
        for q in ('按照我的饮食偏好推荐三道菜','你记得我的偏好吗？推荐三道菜',
                  '取消后还能继续吗？推荐一道快手菜。','取消花生过敏',
                  '忘记所有长期记忆','清空我的记忆','香菜可以不放吗？'):
            with self.subTest(q=q):self.assertIsNone(parse_command(q))

    def test_forget_soft_then_read_preserves_other_user_and_other_facts(self):
        self.seed(Candidate('avoid_ingredient','香菜','dislike','soft'))
        self.seed(Candidate('spice_level','1'),'taste')
        self.seed(Candidate('avoid_ingredient','香菜','dislike','soft'),uid='other')
        result=self.handle('忘记我不喜欢香菜这条偏好。')
        self.assertEqual(result['decision'],'DELETE');self.assertIn('已忘记',result['message'])
        result=self.handle('你还记得我的哪些饮食偏好？','read')
        self.assertIn('微辣',result['message']);self.assertNotIn('香菜',result['message'])
        self.assertEqual(len(self.prefs.prefs('other')),1)
        self.assertEqual(self.prefs.prefs('u',('deleted',))[0].value,'香菜')

    def test_hard_limit_confirmation_and_empty_read(self):
        self.assertIn('没有已保存',self.handle('查看我的偏好')['message'])
        self.seed(Candidate('allergy','花生','avoid','hard'))
        result=self.handle('忘记我对花生过敏这条记录')
        self.assertTrue(result['needs_confirmation'])
        self.assertEqual(self.prefs.prefs('u')[0].value,'花生')
        confirmed=self.prefs.confirm('u','s','确认忘记对花生过敏这条记录')
        self.assertEqual(confirmed['decision'],'UPDATE')
        self.assertEqual(self.prefs.prefs('u'),[])

    def test_runtime_natural_confirmation_does_not_readd_hard_limit(self):
        self.seed(Candidate('allergy','花生','avoid','hard'))
        ask=handle_command(('forget','花生'),self.prefs,'u','s','ask')
        self.assertTrue(ask['needs_confirmation'])
        store=MagicMock()
        store.session_context.return_value=SessionState('u','s')
        store.user_message_id.return_value=1
        store.task_context.return_value=None
        store.memory_epoch.return_value=0
        store.connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value=[]
        with patch('cookbuddy.ltm.runtime.ProfileStore',return_value=self.prefs):
            runtime=MemoryRuntime(store,'u','s','job','确认忘记对花生过敏这条记录')
        self.assertEqual(runtime.management['decision'],'UPDATE')
        self.assertEqual(runtime.candidates,[])
        self.assertEqual(self.prefs.prefs('u'),[])

    def test_forget_one_with_explicit_preservation_keeps_other_preference(self):
        self.seed(Candidate('avoid_ingredient','香菜','dislike','soft'))
        self.seed(Candidate('spice_level','1'),'taste')
        result=self.handle('请忘记我不喜欢香菜这条偏好，保留微辣。')
        self.assertEqual(result['decision'],'DELETE')
        self.assertEqual([(p.dimension,p.value) for p in self.prefs.prefs('u')],[('spice_level','1')])

    def test_conflicting_preservation_never_deletes(self):
        self.seed(Candidate('avoid_ingredient','香菜','dislike','soft'))
        result=self.handle('忘记我不喜欢香菜这条偏好，保留香菜。')
        self.assertEqual(result['decision'],'NOOP')
        self.assertEqual(len(self.prefs.prefs('u')),1)

    def test_missing_ambiguous_and_disabled_do_not_write(self):
        self.assertIn('没有找到',self.handle('删除香菜偏好')['message'])
        self.seed(Candidate('allergy','花生','avoid','hard'))
        self.seed(Candidate('avoid_ingredient','花生','dislike','soft'),'second')
        self.assertIn('多条',self.handle('忘记花生')['message'])
        blocked=Mock()
        result=handle_command(('read',''),blocked,'u','s','x',enabled=False)
        self.assertIn('已关闭',result['message']);blocked.prefs.assert_not_called()
        self.assertEqual(len(self.prefs.prefs('u')),2)

    def test_commands_never_become_extracted_facts(self):
        model=Mock(side_effect=AssertionError('commands must bypass LLM extraction'))
        for q in ('忘记我不喜欢香菜这条偏好。','你还记得我的哪些饮食偏好？'):
            self.assertEqual(extract(q,model=model),[])
        model.assert_not_called()
        c=extract('我不喜欢香菜')[0]
        self.assertEqual((c.value,c.strength,c.polarity),('香菜','soft','dislike'))
