import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

LOGGER=logging.getLogger('cookbuddy.langchain')


def configure_logging():
    if not LOGGER.handlers:
        handler=logging.StreamHandler();handler.setFormatter(logging.Formatter('%(message)s'))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO);LOGGER.propagate=False


@dataclass
class Run:
    cfg: object
    context: object
    services: object
    question: str
    emit: object
    memory_queue: object=None
    prepared: dict=field(default_factory=dict)
    evidence: dict=field(default_factory=dict)
    model_calls: int=0
    tool_calls: int=0
    filtered_out: int=0
    degraded: bool=False
    quality_limited: bool=False
    limited_reason: str | None=None
    stages: list=field(default_factory=list)
    usage: dict=field(default_factory=lambda:{'input_tokens':0,'output_tokens':0})
    checkpoint_bytes: int=0
    memory_submitted: bool=False
    service_model_calls: int=0
    service_api_calls: int=0
    unknown_usage_calls: int=0
    api_usage: list=field(default_factory=list)
    planner_http_calls: int=0
    actual_counting: bool=False
    metering_incomplete: bool=False

    @property
    def hard(self): return self.prepared.get('hard',[])

    async def service(self, operation, args, timeout=8):
        from scripts.stage_diagnostics import normalize_reason,sanitized
        started=time.monotonic();outcome='ok';diagnostics={}
        try:
            result=await self.services.call(operation,args,self.context,hard=self.hard,timeout=timeout)
            if result.get('degraded'):
                outcome='degraded'
                diagnostics['reason']=normalize_reason(result.get('degraded_reason'))
            if operation=='search' and result.get('rerank_diagnostics'):
                diagnostics['rerank']=sanitized(result['rerank_diagnostics'])
            metering=result.pop('_service_usage',{})
            self.service_model_calls+=metering.get('model_calls',0)
            self.service_api_calls+=metering.get('api_calls',0)
            self.unknown_usage_calls+=metering.get('unknown_usage_calls',0)
            for key in self.usage:self.usage[key]+=metering.get(key,0)
            self.api_usage.extend(metering.get('phases',[]))
            return result
        except BaseException as error:
            outcome='error';self.metering_incomplete=True
            diagnostics['reason']=normalize_reason(getattr(error,'reason',type(error).__name__))
            raise
        finally:self.stages.append({'phase':'service','operation':operation,'status':outcome,
                                   'elapsed_ms':round((time.monotonic()-started)*1000,3),**diagnostics})

    async def guard(self, compact, full):
        if not self.hard: return compact,full
        checked=await self.service('guard',{'result':compact},5)
        allowed=set(checked['allowed'])
        items=compact.get('items',[])
        good=[r for r in items if r['source_path'] in allowed]
        removed=len(items)-len(good)
        self.filtered_out+=removed
        if removed:
            # A free-text answer may describe rejected dishes. Drop the whole
            # answer instead of retaining prose after filtering its citations.
            compact={'status':'empty','items':[],'filtered_out':removed,
                     'message':'在你的限制下没有找到合适的菜谱'}
            full={'status':'insufficient','short_answer':compact['message'],'sources':[],'evidence':[]}
        return compact,full

    def trace(self, outcome='done'):
        return {'request_id':self.context.request_id,'outcome':outcome,'stages':self.stages,
            'model_calls':(self.planner_http_calls if self.actual_counting else self.model_calls)+self.service_model_calls,
            'model_call_count_scope':'http_attempts' if self.actual_counting else 'scripted_agent_invocations_plus_service_http',
            'agent_model_calls':self.model_calls,'agent_http_calls':self.planner_http_calls,
            'service_model_calls':self.service_model_calls,'service_api_calls':self.service_api_calls,
            'unknown_usage_calls':self.unknown_usage_calls,'api_usage':self.api_usage,
            'metering_incomplete':self.metering_incomplete,
            'tool_calls':self.tool_calls,'usage':self.usage,
            'degraded':self.degraded,'filtered_out':self.filtered_out,
            'checkpoint_bytes':self.checkpoint_bytes,
            'checkpoint_oversize':self.checkpoint_bytes>self.cfg.trace.checkpoint_warn_bytes}

    def log(self, outcome='done'):
        if self.cfg.trace.structured_log:LOGGER.info(json.dumps(self.trace(outcome),ensure_ascii=False))

    def submit_memory(self):
        if (self.memory_submitted or not self.memory_queue or not self.prepared.get('enabled')
                or self.cfg.memory.mode in ('M0','M1','M2')):return
        task=self.prepared.get('task') or {};nlu=self.prepared.get('nlu') or {}
        if task.get('action')=='none' and nlu.get('answer_mode')=='memory':return
        self.memory_submitted=True
        try:self.memory_queue.submit(self)
        except Exception:
            self.stages.append({'phase':'memory_enqueue','status':'error','elapsed_ms':0})
            LOGGER.warning(json.dumps({'request_id':self.context.request_id,'phase':'memory_enqueue','outcome':'error'}))

    def meter_planner(self,metering):
        self.planner_http_calls+=metering.get('model_calls',0)
        self.unknown_usage_calls+=metering.get('unknown_usage_calls',0)
        self.api_usage.extend(metering.get('phases',[]))
        self.metering_incomplete|=metering.get('incomplete',False)


class MemoryQueue:
    """Bounded application queue; worker writes the existing durable DB outbox."""
    def __init__(self, capacity=64):
        self.queue=asyncio.Queue(capacity)
        self.worker=None

    def submit(self, run):
        self.queue.put_nowait(run)
        if self.worker is None or self.worker.done():self.worker=asyncio.create_task(self._drain())

    async def _drain(self):
        from dataclasses import replace
        while not self.queue.empty():
            run=await self.queue.get()
            try:
                # Extraction is independent of the completed answer's deadline.
                ctx=replace(run.context,deadline=time.time()+10)
                await run.services.call('enqueue',{'question':run.question},ctx,timeout=8)
            except Exception:
                LOGGER.warning(json.dumps({'request_id':run.context.request_id,'phase':'memory_enqueue','outcome':'error'}))
            finally:self.queue.task_done()

    async def close(self):
        if self.worker:await self.worker
