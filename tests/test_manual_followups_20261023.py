"""Regression coverage for the four manual feedback screenshots."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_browser_repair_20261022 as browser_tests
from recipe_tools import load_chunks


class ManualFollowupTests(unittest.TestCase):
    def test_chapter_only_continuation_keeps_source(self):
        import langchain_service_worker as worker
        source=next(r['source'] for r in load_chunks() if r['title']=='基础牛奶面包')
        for q in ('只看酵头制作。','只看面团制作'):
            store,memory=browser_tests.BrowserRepairTests().runtime(q)
            memory.session.slots['selected_dish']=dict(idx=0,dish_name='基础牛奶面包',source_path=source)
            with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
                prepared=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
            self.assertIsNotNone(prepared['named_recipe'],q)
            self.assertEqual(prepared['named_recipe']['source_path'],source)

    def test_chapter_direct_route_returns_only_exact_original_and_images(self):
        import asyncio
        from types import SimpleNamespace
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from src.agent.engine import ChatEngine as AgentEngine
        from recipe_tools import read_recipe
        source=next(r['source'] for r in load_chunks() if r['title']=='基础牛奶面包')
        async def check():
            for chapter,count in (('酵头制作',3),('面团制作',None),('不存在的章节',0)):
                q='只看'+chapter+'。'
                store,memory=browser_tests.BrowserRepairTests().runtime(q)
                memory.session.slots['selected_dish']=dict(idx=0,dish_name='基础牛奶面包',source_path=source)
                with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
                    prepared=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
                cfg=load_config()
                async def emit(*args):pass
                async def guard(compact,full):return compact,full
                async def service(op,args,timeout):
                    return worker.execute(dict(operation=op,arguments=args,user_id='u',session_id='s',request_id='j',hard=[],config=cfg.model_dump()))
                run=SimpleNamespace(question=q,prepared=prepared,cfg=cfg,tool_calls=0,evidence={},degraded=False,stages=[],
                    context=SimpleNamespace(remaining=lambda n:n),service=service,guard=guard,emit=emit)
                self.assertTrue(await AgentEngine._direct(None,run))
                entry=next(iter(run.evidence.values()))
                if count==0:
                    self.assertEqual(entry['name'],'clarify')
                    self.assertTrue(entry['full']['clarification_options'])
                    continue
                self.assertEqual(entry['name'],'get_recipe_detail')
                expected=read_recipe(source,chapter)
                self.assertEqual(entry['full'],expected)
                if count is not None:self.assertEqual(len(expected.get('images',[])),count)
                if expected['status']=='verified_source':
                    self.assertTrue(expected['complete'])
                    self.assertTrue(all('简介'!=e['section'] and chapter[:2] in e['section'] for e in expected['evidence']))
                    self.assertIn('1.',expected['evidence'][0]['quote'])
                else:self.assertEqual(expected['status'],'insufficient')
        asyncio.run(check())

    def test_suffix_replacement_continues_and_excludes_egg_without_ack(self):
        import langchain_service_worker as worker
        from cookbuddy.task_context import advance_task
        from src.agent.config import load_config
        q='我不要鸡蛋换一下'
        previous=advance_task('推荐两道菜，分别说明推荐理由。',None)['state']
        store,memory=browser_tests.BrowserRepairTests().runtime(q,previous)
        memory.session.slots['last_recommendations']=[dict(idx=1,dish_name='洋葱炒鸡蛋',source_path='old.md')]
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory):
            prepared=worker.prepare(dict(user_id='u',session_id='s',request_id='j',question=q))
        self.assertIsNone(prepared['management'])
        self.assertEqual(prepared['task']['action'],'recommend')
        self.assertEqual(prepared['task']['state']['excluded_ingredients'],['鸡蛋'])
        self.assertEqual(prepared['hard'][0]['value'],'鸡蛋')
        cfg=load_config().model_dump();cfg['memory']['mode']='M5'
        result=worker.execute(dict(operation='task_recommend',arguments={'state':prepared['task']['state'],'exclude_dishes':prepared['task']['exclude_dishes']},
            user_id='u',session_id='s',request_id='j',hard=prepared['hard'],config=cfg))
        self.assertEqual(result['status'],'verified',result['short_answer'])
        self.assertEqual(len(result['recommendations']),1)
        self.assertNotIn('鸡蛋',result['recommendations'][0]['title'])

    def test_replace_egg_is_a_temporary_task_not_global_memory(self):
        from src.agent.turn_intents import alternative_request
        from cookbuddy.ltm.extractor import rule_extract
        q='我不要鸡蛋换一下'
        candidates=rule_extract(q)
        self.assertEqual([(c.value,c.scope) for c in candidates],[('鸡蛋','session')])
        self.assertIsNotNone(alternative_request(q))

    def test_meal_candidate_pool_respects_egg_exclusion(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from cookbuddy.ltm.guard import source_allowed
        hard=[dict(dimension='avoid_ingredient',value='鸡蛋',polarity='avoid',strength='hard')]
        result=worker.execute(dict(operation='meal',arguments={'question':'帮我安排2人的晚餐，有30分钟，想做2菜1汤。'},
            user_id='u',session_id='s',request_id='j',hard=hard,config=load_config().model_dump()))
        # A constrained menu now returns the safe courses it can establish,
        # together with missing roles, rather than discarding all courses.
        self.assertEqual(result['status'],'verified')
        self.assertFalse(result['meal_plan']['complete'])
        self.assertTrue(result['meal_plan']['missing_roles'])
        self.assertLessEqual(result['meal_plan']['estimated_minutes'],30)
        self.assertTrue(all(source_allowed(r['source'],hard) for r in result['recommendations']))
        result=worker.execute(dict(operation='meal',arguments={'question':'帮我安排2人的晚餐，有45分钟，想做2菜1汤。'},
            user_id='u',session_id='s',request_id='j',hard=hard,config=load_config().model_dump()))
        self.assertEqual(result['status'],'verified',result['short_answer'])
        self.assertTrue(all(source_allowed(r['source'],hard) for r in result['recommendations']))

    def test_heavy_taste_uses_explicit_intro_and_keeps_all_requested_slots(self):
        from cookbuddy.ltm.retriever import personalize
        profile={'soft_prefs':[dict(dimension='flavor',value='重口',polarity='like')]}
        rows=[dict(title='甲',source='a',description='口味浓郁。',score=0,reason=''),
              dict(title='乙',source='b',description='麻辣鲜香。',score=0,reason=''),
              dict(title='丙',source='c',description='口味清淡。',score=.01,reason='')]
        result=personalize(rows,profile,k=2,strict_flavor=True)
        self.assertEqual({r['source'] for r in result},{'a','b'})
        self.assertTrue(all('重口' in r['reason'] for r in result))
        self.assertTrue(all(any(word in r['reason'] for word in ('浓郁','麻辣')) for r in result))

if __name__=='__main__':unittest.main()
