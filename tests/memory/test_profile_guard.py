import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from cookbuddy.ltm.models import Candidate
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.guard import build_milvus_filter,post_check,source_allowed,policy,request_conflict
from recipe_tools import load_chunks,query_recipes
from preference_recommendation import recommend_preferences
from cookbuddy.task_context import advance_task

class ProfileGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=ProfileStore(path=Path(self.tmp.name)/'prefs.db')
        self.hard=[{'dimension':'allergy','value':'花生','polarity':'avoid','strength':'hard'}]
    def test_h1_search_filter_and_return_check(self):
        chunks=load_chunks();expr=build_milvus_filter(self.hard,chunks)
        self.assertTrue(expr.startswith('source in '))
        good,bad=post_check(chunks,self.hard)
        self.assertTrue(good);self.assertTrue(bad)
        for row in good:self.assertTrue(source_allowed(row['source'],self.hard))
        state=advance_task('推荐3道辣菜',None)['state']
        with policy(self.hard):result=recommend_preferences(state,chunks)
        self.assertTrue(result['recommendations'])
        self.assertTrue(all(source_allowed(r['source'],self.hard) for r in result['recommendations']))
    def test_h2_alias_and_h3_empty_never_relax(self):
        hard=[{'dimension':'avoid_ingredient','value':'香菜','polarity':'avoid'}]
        with patch('cookbuddy.ltm.guard.full_text',return_value='原料：芫荽'):
            self.assertFalse(source_allowed('s.md',hard))
            self.assertEqual(post_check([{'source':'s.md'}],hard)[0],[])
    def test_h4_conflicting_request_does_not_call_rag(self):
        self.assertTrue(request_conflict('推荐一道含花生的菜',self.hard))
        with policy(self.hard),patch('recipe_tools._query_recipes_with_cache') as rag:
            result=query_recipes('推荐一道含花生的菜')
        rag.assert_not_called();self.assertFalse(result['recommendations'])
        for question in ('推荐不含花生的菜','不要放花生','避免用花生油','别加落花生'):
            self.assertFalse(request_conflict(question,self.hard),question)
    def test_new_profile_and_hard_revoke_require_confirmation(self):
        self.assertEqual(self.store.apply('A','s','s:1',Candidate('allergy','花生','avoid','hard'))['decision'],'ADD')
        self.assertEqual(self.store.load_profile('B')['hard_constraints'],[])
        self.assertEqual(self.store.apply('A','s','s:2',Candidate('allergy','花生','avoid','hard',op='revoke'))['decision'],'ASK')
        self.store.confirm('A','s','保留')
        self.assertTrue(self.store.load_profile('A')['hard_constraints'])
        self.store.apply('A','s','s:3',Candidate('allergy','花生','avoid','hard',op='revoke'))
        self.store.confirm('A','s','确认')
        self.assertFalse(self.store.load_profile('A')['hard_constraints'])

    def test_postcheck_retries_recall_once_without_unsafe_prose(self):
        from cookbuddy.ltm.guard import blocked_result
        unsafe={'status':'verified','short_answer':'不能泄露的旧结论','sources':[{'source':'bad.md'}],'evidence':[],'images':[]}
        safe={'status':'insufficient','short_answer':'没有找到合适的菜谱','sources':[],'evidence':[],'images':[]}
        with policy(self.hard),patch('recipe_tools._query_recipes_with_cache',side_effect=[unsafe,safe]) as recall:
            result=query_recipes('推荐一些菜')
        self.assertEqual(recall.call_count,2)
        self.assertNotIn('旧结论',result['short_answer'])
