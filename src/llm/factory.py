"""LangChain model clients reuse the existing cross-process provider governor."""
import os
import sys
from pathlib import Path
import httpx
from langchain_openai import ChatOpenAI

ROOT=Path(__file__).resolve().parents[2]


class ServiceTransport(httpx.AsyncBaseTransport):
    def __init__(self, services, context, timeout, meter=None):
        self.services=services;self.context=context;self.timeout=timeout;self.meter=meter

    async def handle_async_request(self, request):
        import json
        body=json.loads(request.content)
        # Agent streaming uses graph updates. SDK token streaming would expose
        # unverified recipe text and bypass the legacy usage parser.
        body['stream']=False
        try:result=await self.services.call('model',{'body':body},self.context,timeout=self.timeout)
        except BaseException:
            if self.meter:self.meter({'incomplete':True})
            raise
        metering=result.pop('_service_usage',{})
        if self.meter:self.meter(metering)
        if result.get('_error'):
            status=result.get('_status')
            if status:return httpx.Response(status,json={'error':{'message':'model_request_failed'}},request=request)
            if result['_error'] in ('TimeoutError','BudgetExceeded','AdmissionTimeout'):
                raise httpx.ReadTimeout('model_timeout',request=request)
            raise httpx.ConnectError('model_unavailable',request=request)
        return httpx.Response(200,json=result,request=request)


def make_model(cfg, services, context, *, fallback=False, extractor=False,meter=None):
    if str(ROOT/'scripts') not in sys.path:sys.path.insert(0,str(ROOT/'scripts'))
    from llm_config import require_llm,chat_parameters
    settings=require_llm()
    model=os.environ.get(cfg.llm.fallback_env,'') if fallback else settings['model']
    if not model:return None
    # No hidden SDK retries: request_json inside the service process owns retry,
    # shared rate limits, Retry-After, circuit state and usage recording.
    client=httpx.AsyncClient(transport=ServiceTransport(services,context,cfg.llm.timeout_s,meter))
    return ChatOpenAI(model=model,base_url=settings['base_url'],api_key=settings['api_key'],
        temperature=0 if extractor else .2,max_completion_tokens=700,
        max_retries=0,timeout=httpx.Timeout(cfg.llm.timeout_s,connect=5),
        http_async_client=client,
        extra_body={k:v for k,v in chat_parameters({},settings).items() if k!='model'})
