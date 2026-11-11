import asyncio
from unittest.mock import patch
import ast
import copy
import json
import logging
from pathlib import Path
import time
import unittest
from dataclasses import replace
from langgraph.checkpoint.memory import InMemorySaver
from src.agent.config import Config,merge
from src.agent.context import AppContext
from src.agent.engine import ChatEngine,BusyError
from src.agent.runtime import Run,MemoryQueue
from src.agent.tools import make_tools,invoke_tool
from src.agent.build import build_agent
from .fakes import ScriptedModel,tool_call


class Services:
    def __init__(self):
        self.calls=[];self.saved=[];self.hard=[];self.task={'action':'none'}
        self.nlu={'intent':'recipe_lookup','confidence':1.,'path':'fixture','elapsed_ms':0,'answer_mode':'grounded_recipe','slots':{'request_kind':'facts','section':''}}
        self.slots={};self.messages=[];self.delay=0;self.fail=set();self.degraded=False
        self.source='data/HowToCook/dishes/vegetable_dish/蒸水蛋.md'

    async def call(self,op,args,context,**kwargs):
        self.calls.append((op,copy.deepcopy(args),context.user_id,context.session_id))
        if self.delay:await asyncio.sleep(self.delay)
        if op in self.fail:raise TimeoutError('private-sensitive-error')
        if op=='auth':return {'session_id':'b'*32} if args.get('create') else {'ok':True}
        if op=='prepare':return {'enabled':True,'hard':self.hard,'slots':copy.deepcopy(self.slots),
            'nlu':copy.deepcopy(self.nlu),'task':copy.deepcopy(self.task),'messages':self.messages,'summary':'','profile':[]}
        if op=='persist':self.saved.append(args);return {'ok':True}
        if op=='fact':return {'result':None}
        if op=='summarize':return {'summary':'用户要求不辣，已选蒸水蛋，等待确认'}
        if op=='guard':return {'allowed':[] if self.hard else [self.source]}
        if op=='enqueue':return {'ok':True}
        row={'title':'蒸水蛋','source':self.source,'section':'操作','line':12,'source_start_line':12,
             'source_end_line':14,'content':'原文事实。'*100,'coverage':.5,'missing':['盐'],'optional':[]}
        if op=='search':return {'status':'degraded' if self.degraded else 'ok','rows':[row],
                               'artifact':'private-artifact','degraded':self.degraded}
        if op in ('detail','answer','recommend','task_recommend','images','meal'):
            return {'status':'verified_source','short_answer':'已核对的原文事实','sources':[row],
                'evidence':[{'source':self.source,'quote':row['content']}],
                **({'recommendations':[row]} if op in ('recommend','task_recommend') else {})}
        if op in ('remember_preference','forget_preference'):return {'message':'已处理偏好','decision':'ADD'}
        if op=='recall_history':return {'events':[{'id':'event1','text':'过去的用户反馈'}]}
        raise AssertionError(op)


def configuration(**changes):return Config.model_validate(merge(Config().model_dump(),changes))


class OnlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.services=Services();self.saver=InMemorySaver();self.engines=[]

    def engine(self,replies=None,cfg=None,alternate=None):
        engine=ChatEngine(cfg or configuration(agent={'direct':False}),self.services,self.saver,
                          model=ScriptedModel(replies=replies or []),fallback_model=alternate)
        self.engines.append(engine);return engine

    async def asyncTearDown(self):
        for engine in self.engines:await engine.memory_queue.close()

    async def test_TL1_build_memory_off_and_fixed_branch(self):
        cfg=configuration(memory={'mode':'M0'},agent={'enabled':False,'direct':False})
        engine=self.engine(cfg=cfg)
        result=await engine.answer('蒸水蛋怎么做',AppContext('A','s'))
        self.assertEqual(result['mode'],'fixed_workflow')
        self.assertEqual(result['trace']['model_calls'],0)
        run=Run(cfg,AppContext('A','s'),self.services,'问题',lambda *_:None)
        agent=build_agent(cfg,run,ScriptedModel(),self.saver)
        self.assertNotIn('remember_preference',[t.name for t in make_tools(run)])

    async def test_TL2_schema_errors_are_safe(self):
        for name,args in [('search_recipes',{'query':'问题','top_k':50}),('recommend_by_ingredients',{'ingredients':[]})]:
            engine=self.engine([tool_call(name,args),'结束'])
            result=await engine.answer('测试',AppContext('A',name))
            saved=await self.saver.aget_tuple({'configurable':{'thread_id':AppContext('A',name).thread_id}})
            messages=saved.checkpoint['channel_values']['messages']
            response=next(m for m in messages if m.type=='tool')
            self.assertEqual(json.loads(response.content)['status'],'error')
            self.assertNotIn('ValidationError',response.content)

    async def test_TL3_inventory_calls_recommender(self):
        engine=self.engine([tool_call('recommend_by_ingredients',{'ingredients':['番茄','鸡蛋']}),'结束'])
        result=await engine.answer('我有番茄和鸡蛋，能做什么',AppContext('A','s'))
        self.assertIn('recommend',[c[0] for c in self.services.calls])
        self.assertEqual(result['outputs'][0]['result']['recommendations'][0]['title'],'蒸水蛋')

    async def test_no_tool_schema_exposes_identity(self):
        run=Run(Config(),AppContext('A','s'),self.services,'问题',None)
        for tool in make_tools(run):
            self.assertFalse({'user_id','session_id','runtime'} & tool.args.keys())

    async def test_service_generation_usage_is_included_in_trace(self):
        engine=self.engine()
        run=Run(engine.cfg,AppContext('A','s'),self.services,'问题',None)
        self.services.call=lambda *a,**k:self._metered_result()
        await run.service('answer',{})
        self.assertEqual(run.trace()['model_calls'],2)
        self.assertEqual(run.trace()['usage'],{'input_tokens':100,'output_tokens':20})
        self.assertEqual(run.trace()['service_api_calls'],3)

    async def _metered_result(self):
        return {'_service_usage':{'api_calls':3,'model_calls':2,'input_tokens':100,'output_tokens':20}}

    async def test_TL20_actual_middleware_model_wrapper_order(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from src.agent.middleware.trace_mw import TraceMiddleware
        from src.agent.middleware.resilience_mw import ResilienceMiddleware
        from src.agent.middleware.memory_mw import MemoryMiddleware
        from src.agent.middleware.guard_mw import GuardMiddleware
        marks=[]
        def wrap(original,label):
            async def method(instance,request,handler):
                marks.append(label+':enter')
                result=await original(instance,request,handler)
                marks.append(label+':exit')
                return result
            return method
        with ExitStack() as stack:
            for cls,label in [(TraceMiddleware,'trace'),(ResilienceMiddleware,'resilience'),(MemoryMiddleware,'memory'),(GuardMiddleware,'guard')]:
                stack.enter_context(patch.object(cls,'awrap_model_call',wrap(cls.awrap_model_call,label)))
            await self.engine(['结束']).answer('问题',AppContext('A','s'))
        self.assertEqual(marks,['trace:enter','resilience:enter','memory:enter','guard:enter',
                                'guard:exit','memory:exit','resilience:exit','trace:exit'])

    async def test_TL4_ordinal_uses_selected_source_without_model(self):
        self.services.task={'action':'followup','selected_source':self.services.source}
        result=await self.engine(cfg=Config()).answer('第二个怎么做',AppContext('A','s'))
        call=next(c for c in self.services.calls if c[0]=='detail')
        self.assertEqual(call[1]['source_path'],self.services.source)
        self.assertEqual(result['trace']['model_calls'],0)

    async def test_TL5_change_excludes_previous_recommendations(self):
        self.services.slots={'last_recommendations':[{'dish_name':'旧菜'}]}
        self.services.task={'action':'recommend','state':{'ingredients':['鸡蛋']},'exclude_dishes':['旧菜']}
        for question in ('换一个','再来一个'):
            self.services.calls.clear()
            await self.engine(cfg=Config()).answer(question,AppContext('A','s'))
            call=next(c for c in self.services.calls if c[0]=='task_recommend')
            self.assertEqual(call[1]['exclude_dishes'],['旧菜'])

    async def test_TL6_compact_checkpoint_no_recipe_full_text(self):
        engine=self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'未核验结论'])
        await engine.answer('看原文',AppContext('A','s'))
        saved=await self.saver.aget_tuple({'configurable':{'thread_id':AppContext('A','s').thread_id}})
        messages=saved.checkpoint['channel_values']['messages']
        tool=next(m for m in messages if m.type=='tool')
        self.assertLessEqual(len(json.loads(tool.content)['items'][0]['snippet']),200)
        serialized=json.dumps([m.model_dump() for m in messages],ensure_ascii=False)
        self.assertNotIn('原文事实。'*100,serialized)
        self.assertNotIn('未核验结论',serialized)

    async def test_TL7_forged_identity_cannot_select_other_user(self):
        engine=self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋','user_id':'B','session_id':'other'}),'结束'])
        await engine.answer('看原文',AppContext('A','s'))
        call=next(c for c in self.services.calls if c[0]=='detail')
        self.assertEqual(call[2:4],('A','s'))
        self.assertNotIn('user_id',call[1])

    async def test_TL8_and_TL9_hard_guard_before_model_and_detail_display(self):
        self.services.hard=[{'dimension':'allergy','value':'花生','polarity':'avoid'}]
        engine=self.engine([tool_call('get_recipe_detail',{'dish_name':'违规菜'}),'结束'])
        result=await engine.answer('查看违规菜原文',AppContext('A','s'))
        self.assertEqual(result['trace']['filtered_out'],1)
        self.assertNotIn('原文事实',result['text'])
        saved=await self.saver.aget_tuple({'configurable':{'thread_id':AppContext('A','s').thread_id}})
        message=next(m for m in saved.checkpoint['channel_values']['messages'] if m.type=='tool')
        value=json.loads(message.content)
        self.assertEqual(value['status'],'empty');self.assertEqual(value['items'],[])

    async def test_TL10_threads_and_users_are_isolated(self):
        for uid,sid in [('A','one'),('A','two'),('B','one')]:
            await self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束']).answer(uid+sid,AppContext(uid,sid))
        for uid,sid in [('A','one'),('A','two'),('B','one')]:
            saved=await self.saver.aget_tuple({'configurable':{'thread_id':AppContext(uid,sid).thread_id}})
            users=[m.content for m in saved.checkpoint['channel_values']['messages'] if m.type=='human']
            self.assertEqual(users,[uid+sid])

    async def test_TL11_rerank_degradation_has_notice_and_sources(self):
        self.services.degraded=True;events=[]
        async def emit(event,data):events.append((event,data))
        result=await self.engine([tool_call('search_recipes',{'query':'完整问题'}),'结束']).answer('完整问题',AppContext('A','s'),emit)
        self.assertTrue(result['degraded'])
        self.assertIn('notice',[e[0] for e in events])
        self.assertTrue(result['outputs'][0]['result']['sources'])

    async def test_TL12_primary_timeout_uses_configured_fallback(self):
        alternate=ScriptedModel(replies=[tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束'])
        result=await self.engine([TimeoutError()],alternate=alternate).answer('看原文',AppContext('A','s'))
        self.assertTrue(result['degraded']);self.assertIn('原文事实',result['text'])

    async def test_TL13_both_models_fail_without_evidence(self):
        result=await self.engine([TimeoutError()],alternate=ScriptedModel(replies=[TimeoutError()])).answer('问题',AppContext('A','s'))
        self.assertTrue(result['degraded']);self.assertEqual(result['text'],'服务繁忙，请稍后重试')
        self.assertNotIn('private-sensitive',str(result))

    async def test_TL13_failed_generation_keeps_checked_citations(self):
        self.services.fail.add('answer')
        result=await self.engine([tool_call('search_recipes',{'query':'问题'}),'结束']).answer('问题',AppContext('A','s'))
        self.assertTrue(result['degraded']);self.assertIn(self.services.source,result['text'])

    async def test_TL14_tool_loop_is_bounded(self):
        replies=[tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'},str(i)) for i in range(20)]
        result=await self.engine(replies,cfg=configuration(agent={'direct':False,'max_tool_calls':2,'max_model_calls':4})).answer('问题',AppContext('A','s'))
        self.assertTrue(result['degraded'])
        self.assertEqual(sum(c[0]=='detail' for c in self.services.calls),2)

    async def test_TL16_admission_times_out_without_interrupting_running_call(self):
        cfg=configuration(agent={'direct':False},resilience={'concurrency':1,'queue_timeout_s':.01})
        self.services.delay=.05
        engine=self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束'],cfg=cfg)
        first=asyncio.create_task(engine.answer('一',AppContext('A','s1')))
        await asyncio.sleep(.005)
        with self.assertRaises(BusyError):await engine.answer('二',AppContext('B','s2'))
        self.assertIn('原文事实',(await first)['text'])
        self.assertFalse(engine.sessions)

    async def test_TL17_queue_submit_is_nonblocking_and_failure_isolated(self):
        class BrokenQueue:
            def submit(self,run):raise RuntimeError('private-preference')
            async def close(self):pass
        engine=self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束'])
        engine.memory_queue=BrokenQueue()
        result=await engine.answer('问题',AppContext('A','s'))
        self.assertIn('原文事实',result['text'])
        entered=asyncio.Event();release=asyncio.Event()
        async def blocked_call(*args,**kwargs):
            entered.set();await release.wait();return {}
        queue=MemoryQueue();run=Run(Config(),AppContext('A','s'),self.services,'问题',None)
        with patch.object(self.services,'call',blocked_call):
            queue.submit(run)
            try:
                await asyncio.wait_for(entered.wait(),1)
                self.assertFalse(queue.worker.done())
                self.assertFalse(release.is_set())
            finally:
                release.set();await queue.close()

    async def test_TL18_service_layers_have_no_framework_imports(self):
        root=Path(__file__).resolve().parents[2]
        paths=list((root/'src/retrieval').rglob('*.py'))+list((root/'scripts/cookbuddy/ltm').rglob('*.py'))
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text())):
                names=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
                self.assertFalse(any(n.startswith(('langchain','langgraph')) for n in names),str(path))
        for path in (root/'src').rglob('*.py'):
            self.assertNotIn('langchain_classic',path.read_text())
            self.assertNotIn('initialize_agent',path.read_text())

    async def test_TL19_and_TL25_trace_contains_metrics_not_content(self):
        with self.assertLogs('cookbuddy.langchain',logging.INFO) as logs:
            result=await self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束']).answer('敏感用户问题和偏好',AppContext('A','s'))
        combined='\n'.join(logs.output)
        self.assertNotIn('敏感用户问题',combined);self.assertNotIn('原文事实',combined)
        trace=result['trace']
        self.assertTrue({'request_id','stages','model_calls','tool_calls','usage','degraded','filtered_out','checkpoint_bytes'}<=trace.keys())
        json.dumps(trace)

    async def test_TL24_all_seven_memory_modes_select_expected_tools(self):
        for level in range(7):
            cfg=configuration(memory={'mode':'M'+str(level)})
            run=Run(cfg,AppContext('A','s'),self.services,'问题',None)
            tools=[t.name for t in make_tools(run)]
            self.assertEqual('remember_preference' in tools,level>=3)

    async def test_summary_is_structured_separate_and_preserves_slots(self):
        self.services.messages=[{'role':'user','content':'之前的问题'*50} for _ in range(3)]
        self.services.slots={'selected_dish':{'dish_name':'蒸水蛋'}}
        result=await self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),'结束'],
            cfg=configuration(agent={'direct':False},memory={'window_max_tokens':100})).answer('现在的问题',AppContext('A','s'))
        self.assertIn('summarize',[c[0] for c in self.services.calls])
        self.assertEqual(self.services.saved[-1]['prepared']['slots']['selected_dish']['dish_name'],'蒸水蛋')

    async def test_model_cannot_drop_original_query_conditions(self):
        question='请推荐不辣且难度不超过2的汤，不要花生'
        await self.engine([tool_call('search_recipes',{'query':'推荐汤'}),'结束']).answer(question,AppContext('A','s'))
        call=next(c for c in self.services.calls if c[0]=='search')
        self.assertEqual(call[1]['query'],question)

    async def test_source_bundle_cannot_stop_after_only_images_or_change_recipe(self):
        self.services.nlu['slots']['request_kind']='bundle'
        original=self.services.call
        async def call(op,args,context,**kwargs):
            result=await original(op,args,context,**kwargs)
            if op=='prepare':result['named_recipe']={'dish_name':'蒸水蛋','source_path':self.services.source}
            return result
        self.services.call=call
        for first in ('find_recipe_images','get_recipe_detail'):
            self.services.calls.clear()
            result=await self.engine([tool_call(first,{'dish_name':'另一道菜'}),'结束']).answer(
                '查看蒸水蛋原文和图片',AppContext('A',first))
            self.assertEqual({r['tool'] for r in result['outputs']},{'get_recipe_detail','find_recipe_images'})
            image=next(c for c in self.services.calls if c[0]=='images')
            self.assertEqual(image[1]['dish_name'],self.services.source)

    async def test_cancellation_propagates_and_releases_slots(self):
        self.services.delay=10;engine=self.engine()
        task=asyncio.create_task(engine.answer('问题',AppContext('A','s')))
        await asyncio.sleep(.02);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(engine.sessions);self.assertEqual(engine.semaphore._value,2)
