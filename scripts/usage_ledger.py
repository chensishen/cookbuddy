"""可选跨进程API用量账本；无用量的调用记为未知，不记零成本。"""
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime,format_datetime
import fcntl
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlparse


def provider_diagnostics(error,body=None):
    """仅保留受限错误码及限流数字；不记录错误消息或任意响应头。"""
    result={};headers={}
    allowed={'retry-after'}|{f'x-ratelimit-{kind}-{unit}'
        for kind in ('limit','remaining','reset') for unit in ('requests','tokens')}
    for key,value in (getattr(error,'headers',None) or {}).items():
        key=key.lower();value=str(value).strip()
        if key not in allowed or len(value)>64:continue
        if re.fullmatch(r'\d{1,15}(?:\.\d{1,6})?(?:ms|s|m|h)?',value):
            if key!='retry-after' or value[-1].isdigit():headers[key]=value
        elif key=='retry-after':
            try:headers[key]=format_datetime(parsedate_to_datetime(value),usegmt=True)
            except (ValueError,TypeError,OverflowError):pass
    if headers:result['headers']=headers
    try:
        if body is None and isinstance(error,HTTPError) and error.fp is not None:
            try:body=error.read(8193)
            finally:error.close()
        if not isinstance(body,bytes) or len(body)>8192:return result
        payload=json.loads(body)
        detail=payload.get('error',payload) if isinstance(payload,dict) else {}
        code=detail.get('code') if isinstance(detail,dict) else None
        if type(code) is int and 0<=code<10**12:
            result['error_code']=code
        elif isinstance(code,str) and (re.fullmatch(r'\d{1,12}',code) or code in {
                'rate_limit_exceeded','insufficient_quota','too_many_requests',
                'requests_limit_exceeded','tokens_limit_exceeded'}):
            result['error_code']=code
    except (OSError,ValueError,TypeError,RecursionError):pass
    return result


def record_usage(url,model,phase,response=None,error=None,error_body=None):
    target=os.environ.get('RECIPE_USAGE_LEDGER')
    if not target:return
    parsed=urlparse(url);response=response if isinstance(response,dict) else {}
    record={'at':datetime.now(timezone.utc).isoformat(),'provider':parsed.netloc,
        'endpoint':parsed.path,'model':model,'phase':phase,
        'outcome':'http_success' if error is None else type(error).__name__,
        'http_code':getattr(error,'code',None),
        'usage':response.get('usage'),
        'billed_units':response.get('meta',{}).get('billed_units') if isinstance(response.get('meta'),dict) else None,
        'usage_status':'reported' if response.get('usage') else 'unknown'}
    if error is not None:record['provider_diagnostics']=provider_diagnostics(error,error_body)
    path=Path(target);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
        fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def summarize(path):
    rows=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return {'calls':len(rows),'unknown_usage_calls':sum(r['usage_status']=='unknown' for r in rows),
        'models':sorted({r['model'] for r in rows}),'cost':None,
        'status':'pending: 部分供应商无计费用量/价格或预算，不能用预估token充当完整账单',
        'scope':'实际HTTP调用；缓存命中不新增调用；独立评审SDK用量另列，不计入产品请求'}
