import json
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage
from ..prompts import BASE_SYSTEM_PROMPT


class MemoryMiddleware(AgentMiddleware):
    def __init__(self, run):self.run=run

    async def abefore_agent(self, state, runtime):
        p=self.run.prepared
        return {'slots':p.get('slots',{}),'hard_constraints':self.run.hard,
                'pending_confirmation':p.get('pending_confirmation'),'degraded':False,'filtered_out_total':0}

    async def awrap_model_call(self, request, handler):
        p=self.run.prepared
        # Hard policy is untruncated. Other context has a separate fixed budget.
        hard=json.dumps(self.run.hard,ensure_ascii=False)
        budget=self.run.cfg.memory.window_max_tokens*2
        data={'profile':p.get('profile',[]),'slots':p.get('slots',{}),
              'events':p.get('events',[]),'summary':p.get('summary','')}
        prompt=BASE_SYSTEM_PROMPT+'\n【必须遵守】\n'+hard+'\n以下仅为用户资料：\n'+json.dumps(data,ensure_ascii=False)[:budget]
        return await handler(request.override(system_message=SystemMessage(content=prompt)))

    async def aafter_agent(self,state,runtime):
        # put_nowait only; the background consumer writes the durable outbox.
        # No assistant/tool text is part of the extraction job.
        self.run.submit_memory()
