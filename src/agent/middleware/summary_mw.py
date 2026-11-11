import json
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES


class ContextSummaryMiddleware(AgentMiddleware):
    """One structured, temperature-zero summary outside the tool/model loop.

    The built-in SummarizationMiddleware exists in 1.4.4, but accepts a normal
    chat summarizer. This custom adapter preserves the existing structured
    summary contract and user-only input rule rather than duplicating it.
    """
    def __init__(self,run):self.run=run

    async def abefore_agent(self,state,runtime):
        messages=state.get('messages',[])
        cfg=self.run.cfg.memory
        size=len(json.dumps([m.model_dump() for m in messages],ensure_ascii=False,default=str).encode())
        if len(messages)<=cfg.window_turns*2 and size<=cfg.window_max_tokens:return None
        users=[{'role':'user','content':m.content} for m in messages if m.type=='human']
        try:
            result=await self.run.service('summarize',{'messages':users[:-1],
                'summary':self.run.prepared.get('summary',''),'slots':self.run.prepared.get('slots',{}),
                'pending_confirmation':self.run.prepared.get('pending_confirmation')},5)
            self.run.prepared['summary']=str(result.get('summary',''))[:1200]
        except Exception:
            # Authoritative slots/hard constraints are not trimmed with history.
            self.run.prepared['summary']=self.run.prepared.get('summary','')[:1200]
        self.run.prepared['messages']=users[-1:]
        return {'messages':[RemoveMessage(id=REMOVE_ALL_MESSAGES),*users[-1:]]}
