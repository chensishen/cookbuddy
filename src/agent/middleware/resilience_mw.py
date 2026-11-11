import asyncio
from langchain.agents.middleware import AgentMiddleware


def retryable(error):
    code=getattr(error,'status_code',None)
    return isinstance(error,(TimeoutError,ConnectionError)) or type(error).__name__ in (
        'APITimeoutError','APIConnectionError','CircuitOpen') or code in (408,429,500,502,503,504)


class ResilienceMiddleware(AgentMiddleware):
    def __init__(self, run, fallback=None):self.run=run;self.fallback=fallback

    async def awrap_model_call(self, request, handler):
        async def call(req):
            if self.run.model_calls>=self.run.cfg.agent.max_model_calls:raise RuntimeError('model_call_limit')
            self.run.model_calls+=1
            return await asyncio.wait_for(handler(req),self.run.context.remaining(self.run.cfg.llm.timeout_s))
        try:return await call(request)
        except asyncio.CancelledError:raise
        except Exception as error:
            if not self.fallback or not retryable(error):raise
            self.run.degraded=True
            return await call(request.override(model=self.fallback))
