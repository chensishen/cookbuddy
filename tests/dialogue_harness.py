"""Exercise real prepare/tool/persist transitions with an in-memory tenant store."""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from cookbuddy.session import SessionState
from src.agent.config import load_config
from src.agent.engine import ChatEngine
from src.nlu.router import understand
import langchain_service_worker as worker


class DialogueHarness:
    def __init__(self):
        self.session=SessionState('u','s');self.state=None;self.count=0
        self.cfg=load_config();self.cfg=self.cfg.model_copy(update={'memory':self.cfg.memory.model_copy(update={'mode':'M5'})})
        self.store=MagicMock();self.store.session_context.side_effect=lambda *a:self.session
        self.store.save_task_context.side_effect=self.save_task

    def save_task(self,*args):self.state=copy.deepcopy(args[-1]['state'])

    async def turn(self,question,model=None,management=None):
        self.count+=1
        runtime=SimpleNamespace(enabled=True,level=5,session=self.session,previous_task=copy.deepcopy(self.state),
            hard=[],events=[],confirmation=None,management=management,notice='',candidates=[],
            apply_soft=lambda *a:None,context_items=lambda **kw:[])
        def nlu(q,context,**kw):
            return understand(q,context,use_model=bool(model),model_call=model)
        async def service(op,args,timeout):
            return worker.execute(dict(operation=op,arguments=args,user_id='u',session_id='s',
                request_id=str(self.count),hard=[],config=self.cfg.model_dump()))
        async def guard(compact,full):return compact,full
        async def emit(*args):pass
        with patch('cookbuddy.store.Store',return_value=self.store),patch('cookbuddy.ltm.runtime.MemoryRuntime',return_value=runtime),patch('src.nlu.router.understand',side_effect=nlu),patch('service_runtime.RuntimeStore') as cache:
            cache.return_value.get.return_value=None
            prepared=worker.prepare(dict(user_id='u',session_id='s',request_id=str(self.count),question=question))
            run=SimpleNamespace(question=question,prepared=prepared,cfg=self.cfg,context=SimpleNamespace(remaining=lambda n:n),
                tool_calls=0,evidence={},degraded=False,stages=[],service=service,guard=guard,emit=emit)
            if prepared.get('management'):
                run.evidence['management']={'name':'memory_management','full':{'status':'verified_source',
                    'short_answer':prepared['management']['message'],'sources':[],'evidence':[]}}
                handled=True
            else:handled=ChatEngine._conversation(None,run) or await ChatEngine._direct(None,run)
            result={'status':'completed','outputs':[{'tool':e['name'],'result':e['full']} for e in run.evidence.values()]}
            await service('persist',{'question':question,'prepared':prepared,'result':result,'text':''},5)
        return prepared,result,handled


def run_persisted(uid,sid,job,question,epoch,progress):
    """Real PostgreSQL/recipes/engine; provider calls are excluded from DB tests."""
    import asyncio
    from langgraph.checkpoint.memory import InMemorySaver
    from src.agent.context import AppContext
    from agent.fakes import ScriptedModel
    from cookbuddy.store import Store
    prepared={};operations=[]
    cfg=load_config()
    class Services:
        async def call(self,op,args,context,**kw):
            operations.append(op)
            value=worker.execute(dict(operation=op,arguments=args,user_id=uid,session_id=sid,
                request_id=job,hard=kw.get('hard',[]),config=cfg.model_dump()))
            if op=='prepare':prepared.update(value)
            return value
    async def execute():
        engine=ChatEngine(cfg,Services(),InMemorySaver(),model=ScriptedModel())
        try:return await engine.answer(question,AppContext(uid,sid,job))
        finally:await engine.memory_queue.close()
    def offline(q,context,**kw):return understand(q,context,use_model=False)
    def save(store,uid,sid,job,q,result,epoch=0):store.append(uid,sid,job,'assistant','集成测试结果',result)
    with patch('src.nlu.router.understand',side_effect=offline),patch.object(Store,'save_result',new=save),patch('src.agent.runtime.Run.submit_memory'):
        result=asyncio.run(execute())
    return {**result,'prepared':prepared,'operations':operations}
