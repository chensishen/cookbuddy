import time
from langchain.agents.middleware import AgentMiddleware


class TraceMiddleware(AgentMiddleware):
    def __init__(self, run):self.run=run

    async def awrap_model_call(self, request, handler):
        started=time.monotonic()
        try:
            result=await handler(request)
            for message in getattr(result,'result',[]):
                usage=getattr(message,'usage_metadata',None) or {}
                for key in self.run.usage:self.run.usage[key]+=usage.get(key,0)
            return result
        finally:
            self.run.stages.append({'phase':'model','elapsed_ms':round((time.monotonic()-started)*1000,3)})
