"""Inventory utterances must reach grounded recipe recommendations."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import test_browser_repair_20261022 as browser_tests


class InventoryNameTests(unittest.TestCase):
    def prepare(self,q):
        import langchain_service_worker as worker
        store,memory=browser_tests.BrowserRepairTests().runtime(q)
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
            return worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))

    def answer(self,q):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from src.agent.engine import ChatEngine
        cfg=load_config();cfg=cfg.model_copy(update={'memory':cfg.memory.model_copy(update={'mode':'M5'})})
        prepared=self.prepare(q)
        async def check():
            async def emit(*args):pass
            async def guard(compact,full):return compact,full
            async def service(op,args,timeout):
                return worker.execute(dict(operation=op,arguments=args,user_id='u',session_id='s',request_id='j',hard=prepared['hard'],config=cfg.model_dump()))
            run=SimpleNamespace(question=q,prepared=prepared,cfg=cfg,tool_calls=0,evidence={},degraded=False,stages=[],
                context=SimpleNamespace(remaining=lambda n:n),service=service,guard=guard,emit=emit)
            self.assertTrue(await ChatEngine._direct(None,run))
            return list(run.evidence.values())[-1]['full']
        return prepared,asyncio.run(check())

    def test_screenshot_chicken_thigh_inventory_returns_two_grounded_recipes(self):
        p,result=self.answer('我有鸡腿肉，推荐两道菜，说明还缺什么')
        self.assertEqual(result['status'],'verified',result.get('short_answer'))
        self.assertEqual(p['task']['state']['ingredients'],['鸡腿肉'])
        self.assertEqual(len(result['recommendations']),2)
        for row in result['recommendations']:
            self.assertIn('鸡腿肉',row['matched'])
            self.assertIsInstance(row['missing'],list)
            self.assertTrue(row['evidence'])

    def test_catalog_ingredients_outside_old_list_are_recognized(self):
        for name in ('猪里脊','五花肉','牛腩','西兰花','胡萝卜','豆芽','洋葱'):
            with self.subTest(name=name):
                p,result=self.answer('我有'+name+'，推荐两道菜，说明还缺什么')
                self.assertEqual(p['task']['state']['ingredients'],[name])
                self.assertEqual(p['task']['state']['mode'],'inventory')
                self.assertEqual(result['status'],'verified',result.get('short_answer'))
                self.assertEqual(len(result['recommendations']),2)
                self.assertTrue(all(name in r['matched'] for r in result['recommendations']))

    def test_inventory_alias_uses_same_names_as_missing_ingredients(self):
        p,result=self.answer('我有马铃薯，推荐两道菜，说明还缺什么')
        self.assertEqual(p['task']['state']['ingredients'],['土豆'])
        self.assertTrue(all('土豆' in r['matched'] and '土豆' not in r['missing'] for r in result['recommendations']))

    def test_multiple_inventory_names_keep_session_scope(self):
        from cookbuddy.ltm.extractor import rule_extract
        q='我有鸡腿肉和土豆，推荐两道菜，说明还缺什么'
        p,result=self.answer(q)
        self.assertEqual(p['task']['state']['ingredients'],['鸡腿肉','土豆'])
        self.assertEqual(result['status'],'verified')
        self.assertEqual([(c.value,c.scope) for c in rule_extract(q) if c.dimension=='inventory'],[('鸡腿肉','session'),('土豆','session')])

    def test_unknown_stock_and_other_constraints_are_not_ignored(self):
        for q,condition in (('我有火星肉，推荐两道菜，说明还缺什么','我有火星肉'),
                            ('我有鸡腿肉，推荐两道菜，不要太咸','不要太咸')):
            with self.subTest(q=q):
                p,result=self.answer(q)
                self.assertEqual(result['status'],'insufficient')
                self.assertIn(condition,result['short_answer'])
                self.assertFalse(result.get('recommendations'))
                self.assertIn(condition,p['task']['state']['extra_constraints'])

    def test_specific_cuts_are_not_normalized_to_different_ingredients(self):
        from ingredient_recommendation import normalize_ingredient
        self.assertEqual(normalize_ingredient('鸡腿肉'),'鸡腿肉')
        self.assertNotEqual(normalize_ingredient('鸡腿肉'),normalize_ingredient('鸡腿'))

    def test_shortfall_explains_requested_count_and_active_dietary_limits(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        hard=[dict(dimension='avoid_ingredient',value=name,polarity='avoid',strength='hard')
              for name in ('辣椒','香菜')]
        result=worker.execute(dict(operation='recommend',arguments={'ingredients':['鸡腿肉'],'top_k':2},
            user_id='u',session_id='s',request_id='j',hard=hard,config=load_config().model_dump()))
        self.assertEqual([r['title'] for r in result['recommendations']],['意式烤鸡'])
        self.assertIn('你要的2道',result['short_answer'])
        self.assertIn('仅找到1道',result['short_answer'])
        self.assertIn('辣椒',result['short_answer'])
        self.assertIn('香菜',result['short_answer'])

    def test_failed_legacy_stock_clause_is_recovered_in_existing_conversation(self):
        from cookbuddy.task_context import advance_task
        previous=advance_task('推荐两道菜，说明还缺什么',None)['state']
        previous['extra_constraints']=['我有鸡腿肉']
        turn=advance_task('我有鸡腿肉，推荐两道菜，说明还缺什么',previous)
        self.assertEqual(turn['state']['extra_constraints'],[])
        self.assertEqual(turn['state']['ingredients'],['鸡腿肉'])
        self.assertEqual(turn['state']['mode'],'inventory')

    def test_constraint_route_also_reports_missing_ingredients(self):
        p,result=self.answer('我有鸡腿肉，推荐两道菜，不要香菜，说明还缺什么')
        self.assertEqual(result['status'],'verified',result.get('short_answer'))
        self.assertTrue(result['recommendations'])
        for row in result['recommendations']:
            self.assertTrue('鸡腿肉' in row['matched'] or any('鸡腿肉' in s for s in row['unresolved']))
            self.assertTrue(any('鸡腿肉' in e['quote'] and ('必备原料' in e['section'] or e['section'].split(' > ')[0]=='计算') for e in row['evidence']))
            self.assertIsInstance(row['missing'],list)
