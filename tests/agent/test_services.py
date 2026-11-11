import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from src.agent.config import Config
from src.agent.context import AppContext
from src.retrieval.pipeline import RecipeServices
from service_runtime import RuntimeStore,CircuitOpen


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_timeout_and_cancel_reap_worker(self):
        original=asyncio.create_subprocess_exec;created=[]
        async def launch(*args,**kwargs):
            process=await original(sys.executable,'-c','import time; time.sleep(30)',**kwargs)
            created.append(process);return process
        services=RecipeServices(Config())
        with patch('src.retrieval.pipeline.asyncio.create_subprocess_exec',side_effect=launch):
            with self.assertRaises(TimeoutError):
                await services.call('search',{},AppContext('A','s'),timeout=.03)
            self.assertIsNotNone(created[-1].returncode)
            task=asyncio.create_task(services.call('search',{},AppContext('A','s')))
            while len(created)<2:await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertIsNotNone(created[-1].returncode)

    async def test_child_deadline_obeys_operation_timeout(self):
        from unittest.mock import AsyncMock
        process=SimpleNamespace(pid=99999999,returncode=0,communicate=AsyncMock(return_value=(b'{}',None)))
        async def launch(*args,**kwargs):
            deadline=float(kwargs['env']['RECIPE_DEADLINE_EPOCH'])
            self.assertLessEqual(deadline-time.time(),2)
            self.assertGreater(deadline-time.time(),1)
            return process
        with patch('src.retrieval.pipeline.asyncio.create_subprocess_exec',side_effect=launch):
            await RecipeServices(Config()).call('answer',{},AppContext('A','budget',deadline=time.time()+30),timeout=2)

    async def test_worker_failure_keeps_only_bounded_reason(self):
        from unittest.mock import AsyncMock
        process=SimpleNamespace(pid=99999999,returncode=0,communicate=AsyncMock(
            return_value=(b'{"_error":"TimeoutExpired","private":"do-not-log"}',None)))
        with patch('src.retrieval.pipeline.asyncio.create_subprocess_exec',return_value=process):
            with self.assertRaises(RuntimeError) as caught:
                await RecipeServices(Config()).call('answer',{},AppContext('A','reason'),timeout=2)
        self.assertEqual(caught.exception.reason,'stage_deadline')
        self.assertNotIn('do-not-log',str(caught.exception))

    async def test_cross_process_session_lock_is_bounded(self):
        first=RecipeServices(Config());second=RecipeServices(Config());ctx=AppContext('test-lock','s')
        async with first.session_lock(ctx,1):
            with self.assertRaises(TimeoutError):
                async with second.session_lock(ctx,.03):pass
        async with second.session_lock(ctx,.03):pass


class GovernorTests(unittest.TestCase):
    def test_tool_text_cannot_authorize_memory_writes(self):
        from langchain_service_worker import memory_authorized
        self.assertFalse(memory_authorized('remember_preference',{'dimension':'allergy','value':'花生'},'查看宫保鸡丁原文'))
        self.assertFalse(memory_authorized('remember_preference',{'dimension':'allergy','value':'花生'},'记住我喜欢花生'))
        self.assertTrue(memory_authorized('remember_preference',{'dimension':'allergy','value':'花生'},'记住我对花生过敏'))
        self.assertFalse(memory_authorized('forget_preference',{'dimension':'allergy','value':'花生'},'我对花生过敏'))

    def test_memory_off_does_not_write_slots_or_task_memory(self):
        from langchain_service_worker import prepare
        memory=SimpleNamespace(enabled=False,level=0,session=None,previous_task=None,hard=[],events=[],
            confirmation=None,management=None,notice='',apply_soft=lambda *_:None,context_items=lambda **_:[])
        with patch('cookbuddy.store.Store') as store,patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=memory),patch('recipe_tools.load_chunks',return_value=[]):
            result=prepare({'user_id':'a'*32,'session_id':'b'*32,'request_id':'job','question':'你好'})
            store.return_value.save_task_context.assert_not_called()
            store.return_value.save_session_context.assert_not_called()
            self.assertFalse(result['enabled'])

    def test_inventory_soft_taste_is_not_a_new_hard_filter(self):
        from langchain_service_worker import keep_inventory_tastes_explicit
        from cookbuddy.recommendation_schema import new_state
        state=new_state();state.update(mode='inventory',ingredients=['鸡蛋'],tastes=['辣味'],memory_tastes=['辣味'])
        task={'state':state};keep_inventory_tastes_explicit(task)
        self.assertEqual(state['tastes'],[])
        state['tastes']=['不辣'];keep_inventory_tastes_explicit(task)
        self.assertEqual(state['tastes'],['不辣'])
    def test_TL15_shared_circuit_open_half_open_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=RuntimeStore(Path(tmp)/'runtime.sqlite3')
            for _ in range(2):
                ident=store.acquire('model',threshold=2,cooldown=30)
                store.release(ident,'model',False)
            with self.assertRaises(CircuitOpen):store.acquire('model',threshold=2,cooldown=30)
            future=time.time()+31
            with patch('service_runtime.time.time',return_value=future):
                probe=store.acquire('model',threshold=2,cooldown=30)
                self.assertTrue(probe)
                self.assertIsNone(store.acquire('model',threshold=2,cooldown=30))
                store.release(probe,'model',True)
                healthy=store.acquire('model',threshold=2,cooldown=30)
                self.assertTrue(healthy);store.release(healthy,'model',True)

    def test_TL23_disabled_rerank_never_calls_rerank_stage(self):
        self._search(False)

    def test_rerank_timeout_preserves_original_order_and_generation_schema(self):
        self._search(True)

    def _search(self,enabled):
        import langchain_service_worker as worker
        from cookbuddy.ltm.models import CONFIG_OVERRIDE
        old=CONFIG_OVERRIDE.get()
        with tempfile.TemporaryDirectory() as tmp:
            # The worker's artifacts must remain under ROOT by contract.
            folder=ROOT/'data/processed/langchain-test-artifacts';folder.mkdir(exist_ok=True)
            name=folder/(Path(tmp).name+'.json')
            rows=[{'chunk_id':str(i),'title':'菜'+str(i),'source':'source'+str(i),
                'content':'原文','source_start_line':1,'source_end_line':1,'distance':1-i*.1,'section':'操作'} for i in range(3)]
            report={'question':'原始问题','results':rows};name.write_text(json.dumps(report))
            cfg=Config().model_dump();cfg['retrieval']['rerank']['enabled']=enabled
            payload={'operation':'search','arguments':{'query':'原始问题','top_k':3},'user_id':'A','session_id':'s','request_id':'job','hard':[],'config':cfg}
            calls=[]
            def stage(script,*args):
                calls.append(script)
                if script=='search_test_recipes.py':return name,report
                raise TimeoutError('rerank timed out')
            try:
                with patch.dict(os.environ,{},clear=False),patch('ask_test_recipes.run_stage',side_effect=stage):result=worker.execute(payload)
                self.assertEqual([r['chunk_id'] for r in result['rows']],['0','1','2'])
                self.assertEqual([r['rerank_rank'] for r in result['rows']],[1,2,3])
                self.assertEqual(calls.count('rerank_test_recipes.py'),int(enabled))
                self.assertEqual(result['degraded'],enabled)
            finally:
                CONFIG_OVERRIDE.set(old)
                for path in folder.glob(name.stem+'*'):path.unlink()
