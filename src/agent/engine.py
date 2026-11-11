"""Common online application lifecycle; async graph only when planning is useful."""
import asyncio
import json
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from langchain_core.messages import RemoveMessage, AIMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langsmith import tracing_context
from .build import build_agent
from .runtime import Run,MemoryQueue
from .tools import invoke_tool,compact_result
from .degrade import fallback,coalesce_originals
from ..llm.factory import make_model


async def no_events(event,data):pass


class BusyError(RuntimeError):pass


class ChatEngine:
    def __init__(self,cfg,services,checkpointer,*,model=None,fallback_model=None,memory_queue=None):
        self.cfg=cfg;self.services=services;self.checkpointer=checkpointer
        self.model=model;self.fallback_model=fallback_model
        self.memory_queue=memory_queue or MemoryQueue(cfg.memory.queue_capacity)
        self.semaphore=asyncio.Semaphore(cfg.resilience.concurrency)
        self.sessions={}

    async def answer(self,question,context,emit=no_events):
        if not isinstance(question,str) or not 1<=len(question.strip())<=2000:raise ValueError('invalid_question')
        context=replace(context,deadline=min(context.deadline,time.time()+self.cfg.agent.request_timeout_s))
        try:await asyncio.wait_for(self.semaphore.acquire(),context.remaining(self.cfg.resilience.queue_timeout_s))
        except TimeoutError:raise BusyError('queue_timeout') from None
        return await self.answer_admitted(question,context,emit)

    async def answer_admitted(self,question,context,emit=no_events):
        """API reserves admission before sending SSE headers; exactly one release."""
        # Session entries are reference counted, so this registry does not grow
        # forever and a departing request cannot remove another waiter's lock.
        key=context.thread_id
        entry=self.sessions.setdefault(key,[asyncio.Lock(),0]);entry[1]+=1
        try:
            try:await asyncio.wait_for(entry[0].acquire(),context.remaining(self.cfg.resilience.queue_timeout_s))
            except TimeoutError:raise BusyError('session_busy') from None
            try:
                async with AsyncExitStack() as stack:
                    if hasattr(self.services,'session_lock'):
                        await stack.enter_async_context(self.services.session_lock(context,self.cfg.resilience.queue_timeout_s))
                    # Configuration wins over any ambient LANGSMITH_TRACING
                    # environment variable; no conversation is exported.
                    with tracing_context(enabled=False):
                        return await self._answer(question,context,emit)
            finally:entry[0].release()
        finally:
            entry[1]-=1
            if not entry[1]:self.sessions.pop(key,None)
            self.semaphore.release()

    async def _answer(self,question,context,emit):
        started=time.monotonic();run=Run(self.cfg,context,self.services,question,emit,self.memory_queue)
        run.actual_counting=self.model is None
        models=[]
        try:
            run.prepared=await run.service('prepare',{'question':question},35)
            nlu=run.prepared.get('nlu')
            if nlu:
                if nlu.get('errors') and nlu['path'] not in ('model','cache','fast'):run.degraded=True
                run.stages.append({'phase':'understanding','intent':nlu['intent'],
                    'confidence':nlu['confidence'],'path':nlu['path'],'fallback':nlu.get('fallback'),
                    'matches':nlu.get('matches',[]),'elapsed_ms':nlu['elapsed_ms'],
                    'errors':nlu.get('errors',[])})
            management=run.prepared.get('management')
            memory_confirmation=run.prepared.get('memory_confirmation')
            if management:
                outputs=[{'tool':'memory_management','result':{'status':'verified_source',
                    'short_answer':management.get('message','已处理记忆请求'),'sources':[],'evidence':[]}}]
            else:
                public=self._conversation(run)
                direct=public or (await self._direct(run) if self.cfg.agent.direct and self.cfg.agent.enabled else False)
                if not direct:
                    if not self.cfg.agent.enabled:
                        await invoke_tool(run,'search_recipes',{'query':question,'top_k':self.cfg.retrieval.top_k})
                    else:
                        model=self.model or make_model(self.cfg,self.services,context,meter=run.meter_planner)
                        alternate=self.fallback_model or (make_model(self.cfg,self.services,context,fallback=True,meter=run.meter_planner) if self.model is None else None)
                        if self.model is None:models=[m for m in (model,alternate) if m]
                        agent=build_agent(self.cfg,run,model,self.checkpointer,fallback=alternate)
                        messages=run.prepared.get('messages',[]) if self.cfg.memory.mode!='M0' else []
                        if not messages or messages[-1].get('content')!=question:messages=[*messages,{'role':'user','content':question}]
                        # The existing session store is authoritative. Replace old
                        # graph history rather than replaying stale tool text.
                        inputs={'messages':[RemoveMessage(id=REMOVE_ALL_MESSAGES),*messages],
                                'slots':run.prepared.get('slots',{}),'hard_constraints':run.hard,
                                'degraded':False,'filtered_out_total':0}
                        graph_config={'configurable':{'thread_id':context.thread_id},
                                      'recursion_limit':self.cfg.agent.max_model_calls*3+3}
                        async with asyncio.timeout(context.remaining()):
                            async for _ in agent.astream(inputs,graph_config,context=context,stream_mode='updates'):
                                # Graph updates are deliberately not forwarded:
                                # they contain private messages and model drafts.
                                pass
                        snapshot=await agent.aget_state(graph_config)
                        # Keep only short, validated graph history. Agent's final
                        # draft is not a recipe answer and is not retained.
                        values=dict(snapshot.values)
                        messages=values.get('messages',[])
                        safe=[m for m in messages if m.type!='ai' or getattr(m,'tool_calls',None)]
                        safe.append(AIMessage(content='工具核对已结束，回答由来源服务呈现。'))
                        await agent.aupdate_state(graph_config,{'messages':[RemoveMessage(id=REMOVE_ALL_MESSAGES),*safe],
                            'slots':run.prepared.get('slots',{}),'hard_constraints':[],
                            'degraded':run.degraded,'filtered_out_total':run.filtered_out})
                        run.checkpoint_bytes=len(json.dumps({**values,'messages':[m.model_dump() for m in safe]},ensure_ascii=False,default=str).encode())
                await self._complete_scope(run)
                outputs=await self._outputs(run)
            if run.prepared.get('input_notice') and outputs:
                data=outputs[0]['result']
                data['notice']='\n'.join(v for v in (run.prepared['input_notice'],data.get('notice','')) if v)
            from ..capabilities import degradation
            metadata=degradation(nlu or {},outputs,context.request_id,
                tool_degraded=run.quality_limited,fallback_used=run.degraded,
                tool_failed=any(o['result'].get('status')=='tool_failed' for o in outputs),
                limited_reason=run.limited_reason or ('rerank_unavailable' if any(s.get('operation')=='search' and s.get('status')=='degraded' for s in run.stages) else None))
            result={'question':question,'mode':'langchain' if self.cfg.agent.enabled else 'fixed_workflow',
                'status':'degraded' if run.degraded else 'completed','outputs':outputs,
                'steps':[r for r in run.stages if r['phase']=='tool'],
                'elapsed_seconds':time.monotonic()-started,**metadata}
            if memory_confirmation:result['memory_confirmation']=memory_confirmation
            if nlu and nlu.get('assumptions') and outputs:
                outputs[0]['result']['assumption_notice']='\n'.join(dict.fromkeys(nlu['assumptions']))
            text=render(outputs)
            if memory_confirmation:text=memory_confirmation+'\n\n'+text
            await run.service('persist',{'question':question,'prepared':run.prepared,'result':result,'text':text},5)
            run.submit_memory()
            if metadata['service_notice']:await emit('notice',{'message':metadata['service_notice']})
            # Only verified/template text reaches the public token stream.
            if self.cfg.agent.stream:
                for offset in range(0,len(text),64):
                    await emit('token',{'text':text[offset:offset+64]})
                    await asyncio.sleep(0)
            else:await emit('token',{'text':text})
            citations=[r for output in outputs for r in output['result'].get('sources',[])]
            images=list({(r.get('image_path'),r.get('source'),r.get('source_line')):r
                for output in outputs for r in output['result'].get('images',[])}.values())
            await emit('citations',{'items':citations,'images':images})
            options=[v for o in outputs for v in o['result'].get('clarification_options',[])]
            if options:await emit('choices',{'items':list(dict.fromkeys(options))[:10]})
            run.stages.append({'phase':'overall','elapsed_ms':round((time.monotonic()-started)*1000,3)})
            trace=run.trace();run.log()
            await emit('done',{**metadata,'elapsed_seconds':result['elapsed_seconds']})
            return {**result,'text':text,'trace':trace}
        except asyncio.CancelledError:
            run.log('cancelled');raise
        except Exception:
            # Never turn a failed store/identity preparation into a successful
            # conversation or bypass authentication. Failure details stay private.
            if not run.prepared:
                run.stages.append({'phase':'overall','elapsed_ms':round((time.monotonic()-started)*1000,3)})
                run.log('failed');raise
            outputs=fallback(run);text=render(outputs)
            run.stages.append({'phase':'overall','elapsed_ms':round((time.monotonic()-started)*1000,3)})
            from ..capabilities import degradation
            metadata=degradation(run.prepared.get('nlu',{}),outputs,context.request_id,tool_degraded=True,
                                 tool_failed=any(o['result'].get('status')=='tool_failed' for o in outputs))
            result={'question':question,'mode':'langchain','status':'degraded',**metadata,'outputs':outputs,
                    'elapsed_seconds':time.monotonic()-started,'trace':run.trace()}
            await emit('notice',{'message':metadata['service_notice']})
            await emit('token',{'text':text})
            await emit('citations',{'items':[s for o in outputs for s in o['result'].get('sources',[])]})
            await emit('done',{**metadata,'elapsed_seconds':result['elapsed_seconds']})
            run.log('degraded')
            return {**result,'text':text}
        finally:
            for model in models:
                if getattr(model,'http_async_client',None):await model.http_async_client.aclose()

    def _conversation(self,run):
        from ..dialogue import public_reply
        plan=run.prepared.get('nlu')
        if not plan:return False
        text=public_reply(plan)
        if text is None:return False
        run.evidence['conversation']={'name':plan['answer_mode'],'compact':{'status':'ok','items':[]},
            'full':{'status':'verified_source','short_answer':text,'sources':[],'evidence':[],
                    'clarification_options':plan.get('options',[])}}
        return True

    async def _direct(self,run):
        if ChatEngine._conversation(self,run):return True
        task=run.prepared.get('task',{});action=task.get('action')
        if run.prepared.get('recipe_conflict'):
            run.evidence['conflict']={'name':'hard_constraint_guard','compact':{'status':'empty','items':[]},
                'full':run.prepared['recipe_conflict']}
            return True

        if action=='clarify':
            run.evidence['clarification']={'name':'clarify','compact':{'status':'empty','items':[]},
                'full':{'status':'insufficient','short_answer':task.get('clarification','请说明菜名或食材。'),'sources':[],'evidence':[]}}
            return True
        if action=='meal_plan':
            await invoke_tool(run,'plan_meal',{'question':run.question});return True
        if action=='ingredients' and task.get('selected_source'):
            await invoke_tool(run,'recipe_ingredients',{'source_path':task['selected_source'],
                'ingredients':run.prepared.get('slots',{}).get('ingredients_on_hand',[])})
            return True
        if action=='followup' and task.get('selected_source'):
            await invoke_tool(run,'get_recipe_detail',{'source_path':task['selected_source']});return True
        if action=='recommend' and task.get('state'):
            state=task['state']
            if (state.get('mode')=='inventory' and state.get('ingredients') and not
                    any(state.get(k) for k in ('tastes','excluded_methods','excluded_ingredients','extra_constraints'))):
                await invoke_tool(run,'recommend_by_ingredients',{'ingredients':state['ingredients'],'top_k':min(8,state.get('limit',5))})
            else:
                await invoke_tool(run,'task_recommend',{'state':state,
                    'exclude_dishes':task.get('exclude_dishes',[])})
            return True
        # This optimization uses the same source guard as all other tools.
        named=run.prepared.get('named_recipe')
        plan=run.prepared.get('nlu',{})
        kind=plan.get('slots',{}).get('request_kind')
        if named and kind in ('source','images','bundle'):
            section=plan['slots']['section']
            # A direct recipe lookup has no recommendation batch to infer the
            # active recipe from. Persist the resolved source so a follow-up
            # such as “只看酵头制作的图片” stays in the same recipe context.
            run.prepared.setdefault('slots',{})['selected_dish']={
                'idx':0,'dish_name':named['dish_name'],'source_path':named['source_path']}
            if kind in ('source','bundle'):await invoke_tool(run,'get_recipe_detail',{'source_path':named['source_path'],'section':section})
            if kind in ('images','bundle'):await invoke_tool(run,'find_recipe_images',{'dish_name':named['source_path'],'section':section})
            return True
        if named and kind=='facts':
            found=await run.service('fact',{'question':task.get('question') or run.question},5)
            if found.get('result'):
                full=found['result'];compact,full=await run.guard(compact_result(full),full)
                run.evidence['fact']={'name':'source_fact','compact':compact,'full':full}
                return True
            await invoke_tool(run,'search_recipes',{'query':task.get('question') or run.question,'top_k':run.cfg.retrieval.top_k})
            return True
        return False

    async def _outputs(self,run):
        outputs=[]
        for entry in run.evidence.values():
            full=entry['full']
            if full.get('artifact'):
                if not full.get('rows'):
                    full={'status':'insufficient','short_answer':'知识库中没有找到可核实的依据。','sources':[],'evidence':[]}
                else:
                    try:
                        generated=await run.service('answer',{'artifact':full['artifact']},min(20,run.context.remaining()))
                        compact,generated=await run.guard(compact_result(generated),generated)
                        if generated.get('status')=='validation_failed':raise ValueError('answer_not_verified')
                        full=generated
                    except Exception as error:
                        run.limited_reason='answer_not_verified' if isinstance(error,ValueError) else 'dependency_unavailable'
                        outputs.extend(fallback(run,[entry]))
                        continue
            # Details are retained for display, never fed back into checkpoints.
            if entry['name']=='get_recipe_detail':
                original='\n\n'.join(e.get('quote','') for e in full.get('evidence',[]))
                text=original or full.get('short_answer','')
                if full.get('complete') is False:text+='\n（本次展示部分原文，完整内容请打开菜谱来源链接。）'
                full={**full,'short_answer':text}
            outputs.append({'tool':entry['name'],'result':full})
        return coalesce_originals(outputs) or fallback(run)

    async def _complete_scope(self,run):
        """A model stopping early cannot silently drop half a source bundle."""
        plan=run.prepared.get('nlu',{})
        if plan and plan.get('answer_mode')!='grounded_recipe':return
        if not run.evidence and not run.tool_calls:
            await invoke_tool(run,'search_recipes',{'query':run.question,'top_k':self.cfg.retrieval.top_k})
        named=run.prepared.get('named_recipe')
        if not named:return
        if plan.get('slots',{}).get('request_kind')!='bundle':return
        completed={e['name'] for e in run.evidence.values()}
        for tool,args in [('get_recipe_detail',{'source_path':named['source_path']}),
                          ('find_recipe_images',{'dish_name':named['source_path']})]:
            if tool not in completed:
                await invoke_tool(run,tool,args)


def render(outputs):
    parts=[]
    for output in outputs:
        result=output['result']
        text=result.get('short_answer') or result.get('answer','')
        if result.get('assumption_notice'):text=result['assumption_notice']+'\n'+text
        for n,row in enumerate(result.get('recommendations',[]),1):
            text+='\n\n'+str(n)+'. **'+row['title']+'**'
            if row.get('description'):text+='\n'+row['description']
            if row.get('reason'):text+='\n推荐理由：'+row['reason']
            if 'matched' in row:text+='\n已有：'+('、'.join(row['matched']) or '尚未确认')+'。'
            if 'missing' in row:text+='\n还需确认或准备：'+('、'.join(row['missing']) or '表内必备原料已齐')+'。'
            if row.get('optional'):text+='\n可选：'+'、'.join(row['optional'])+'。'
            if row.get('unresolved'):text+='\n待核对：'+'；'.join(row['unresolved'])+'。'
        if result.get('notice'):text=result['notice']+'\n\n'+text
        sources=result.get('sources',[])
        citations='\n'.join(f"《{s.get('title','')}》· {s.get('section','')} · {s.get('source','')}"+
            (f" · {s['line_range']}" if s.get('line_range') else f" · L{s['line']}" if s.get('line') else '') for s in sources)
        parts.append(text+('\n\n来源：\n'+citations if citations else ''))
    return '\n\n'.join(parts)
