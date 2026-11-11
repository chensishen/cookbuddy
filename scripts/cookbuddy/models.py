import hashlib
import json
import math
import os
from contextvars import ContextVar
from urllib.request import Request
from urllib.parse import urlparse
from check_embedding import config
from llm_config import require_llm, chat_parameters
from service_runtime import request_json, request_budget

MEMORY_USAGE=ContextVar('memory_usage',default=None)


def settings():
    values=config()
    for key in ('COOKBUDDY_ENABLED','COOKBUDDY_ADMIN_DATABASE_URL','COOKBUDDY_DATABASE_URL','COOKBUDDY_MILVUS_URL','COOKBUDDY_MEMORY_TTL_DAYS'):
        if key in os.environ:values[key]=os.environ[key]
    return values


class ModelOutputError(ValueError):
    """Only fixed diagnostic codes may be persisted; never model text."""
    def __init__(self, code):
        if code not in ('model_output_truncated', 'model_output_incomplete',
                        'model_output_invalid_json', 'model_output_not_object',
                        'memory_selection_invalid', 'memory_source_changed', 'memory_evidence_too_long'):
            raise ValueError('invalid_model_error_code')
        self.code = code
        super().__init__(code)


def json_model(system, data, phase, max_tokens=1200):
    with request_budget(40):
        return _json_model(system,data,phase,max_tokens)


def _json_model(system,data,phase,max_tokens):
    cfg=require_llm()
    payload=chat_parameters({'messages':[{'role':'system','content':system},{'role':'user','content':json.dumps(data,ensure_ascii=False)}],
        'temperature':0,'max_tokens':max_tokens,'response_format':{'type':'json_object'}},cfg)
    for attempt in range(2):
        request=Request(cfg['base_url']+'/chat/completions',data=json.dumps(payload,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+cfg['api_key'],'Content-Type':'application/json'})
        response=request_json(request,timeout=40,attempts=2,phase=phase)
        collector=MEMORY_USAGE.get()
        if collector is not None:collector.append({'phase':phase,'usage':response.get('usage') or {}})
        choice=response['choices'][0]
        if choice['finish_reason']=='length':
            if attempt==0 and payload['max_tokens']<8192:
                payload['max_tokens']=min(8192,payload['max_tokens']*2)
                continue
            raise ModelOutputError('model_output_truncated')
        if choice['finish_reason']!='stop':raise ModelOutputError('model_output_incomplete')
        try:
            result=json.loads(choice['message']['content'])
        except (ValueError,TypeError):
            raise ModelOutputError('model_output_invalid_json') from None
        if not isinstance(result,dict):raise ModelOutputError('model_output_not_object')
        return result


def embedding_spec():
    cfg=settings()
    spec={k:cfg.get(k,'') for k in ('EMBEDDING_BASE_URL','EMBEDDING_MODEL','EMBEDDING_DIMENSIONS')}
    return spec, hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:12]


def embed(text, phase='memory_embedding'):
    cfg=settings();base=cfg['EMBEDDING_BASE_URL'].rstrip('/')
    if urlparse(base).scheme!='https':raise ValueError('embedding_requires_https')
    payload={'model':cfg['EMBEDDING_MODEL'],'input':text[:6000], 'dimensions':int(cfg['EMBEDDING_DIMENSIONS']),'encoding_format':'float'}
    request=Request(base+'/embeddings',data=json.dumps(payload,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+cfg['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
    return request_json(request,phase=phase,timeout=30,attempts=2,cache_namespace='cookbuddy-intent-embedding-v1' if phase.startswith('intent_') else None)['data'][0]['embedding']


def rerank(question, documents, phase='memory_rerank'):
    if not documents:return []
    cfg=settings();base=cfg['EMBEDDING_BASE_URL'].rstrip('/')
    if urlparse(base).scheme!='https':raise ValueError('rerank_requires_https')
    request=Request(base+'/rerank',data=json.dumps({'model':'Qwen/Qwen3-Reranker-8B','query':question,
        'documents':documents,'top_n':len(documents),'return_documents':False},ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+cfg['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
    return request_json(request,phase=phase,timeout=30,attempts=2,cache_namespace='cookbuddy-rerank-v1')['results']


def cosine(left,right):
    if len(left)!=len(right) or not left:raise ValueError('vector_space_mismatch')
    a=math.sqrt(sum(x*x for x in left));b=math.sqrt(sum(x*x for x in right))
    if not a or not b:raise ValueError('zero_vector')
    return max(-1,min(1,sum(x*y for x,y in zip(left,right))/(a*b)))
