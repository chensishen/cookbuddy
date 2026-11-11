"""Bounded, content-free progress records survive a stage subprocess timeout."""
import json
import math
import os
from pathlib import Path
import time

PHASES={'startup','imports','data_load','prepare','admission','api','finalize','finished'}
REASONS={'stage_deadline','request_deadline','admission_timeout','rate_limited','circuit_open',
         'upstream_timeout','network_error','upstream_http_error','result_validation',
         'child_failed','stage_failure','cache_hit'}


def normalize_reason(value):
    if value is None:return None
    if isinstance(value,str):
        if value in REASONS:return value
        if value=='HTTP429':return 'rate_limited'
        if value.startswith('HTTP') and value[4:].isdigit():return 'upstream_http_error'
        if value.startswith('admission_'):return 'admission_timeout'
        if value in ('TimeoutError','BudgetExceeded'):return 'upstream_timeout'
        if value=='URLError':return 'network_error'
        if value=='CircuitOpen':return 'circuit_open'
    return 'stage_failure'


def sanitized(value):
    if not isinstance(value,dict):return {}
    out={}
    for key in ('budget_ms','elapsed_ms'):
        number=value.get(key)
        if type(number) in (int,float) and math.isfinite(number) and 0<=number<=600000:out[key]=number
    if value.get('last_phase') in tuple(PHASES):out['last_phase']=value['last_phase']
    if type(value.get('finished')) is bool:out['finished']=value['finished']
    out['reason']=normalize_reason(value.get('reason'))
    code=value.get('http_code')
    if type(code) is int and 100<=code<=599:out['http_code']=code
    timings=value.get('timings_ms',{})
    if isinstance(timings,dict):
        out['timings_ms']={k:v for k,v in timings.items() if k in PHASES and
            type(v) in (int,float) and math.isfinite(v) and 0<=v<=600000}
    return out


def record(phase,reason=None,http_code=None):
    target=os.environ.get('RECIPE_STAGE_DIAGNOSTICS')
    if not target or phase not in PHASES:return
    row={'phase':phase,'at':time.monotonic()}
    if reason in REASONS:row['reason']=reason
    if type(http_code) is int and 100<=http_code<=599:row['http_code']=http_code
    try:
        with open(target,'a') as stream:
            stream.write(json.dumps(row)+'\n');stream.flush()
    except OSError:pass  # Observability must not break recipe answering.


def failure_reason(error):
    import subprocess
    from urllib.error import HTTPError,URLError
    from service_runtime import BudgetExceeded,AdmissionTimeout,CircuitOpen
    if isinstance(error,subprocess.TimeoutExpired):return 'stage_deadline'
    if isinstance(error,AdmissionTimeout):return 'admission_timeout'
    if isinstance(error,BudgetExceeded):return 'stage_deadline'
    if isinstance(error,CircuitOpen):return 'circuit_open'
    if isinstance(error,HTTPError):return 'rate_limited' if error.code==429 else 'upstream_http_error'
    if isinstance(error,TimeoutError):return 'upstream_timeout'
    if isinstance(error,(URLError,ConnectionError)):return 'network_error'
    if isinstance(error,(ValueError,KeyError,TypeError)):return 'result_validation'
    if type(error).__name__=='StageFailure':return 'child_failed'
    return 'stage_failure'


def collect(path,started,budget,*,error=None):
    ended=time.monotonic();rows=[]
    try:
        for line in Path(path).read_text()[:32000].splitlines()[:128]:
            try:row=json.loads(line)
            except ValueError:continue
            if (isinstance(row,dict) and isinstance(row.get('phase'),str) and row['phase'] in PHASES and
                    type(row.get('at')) in (float,int) and math.isfinite(row['at']) and
                    started<=row['at']<=ended):rows.append(row)
    except OSError:pass
    timings={};phase='startup';previous=started;reason=None;code=None
    for row in rows:
        if row['at']<previous:continue
        timings[phase]=timings.get(phase,0)+(row['at']-previous)*1000
        phase=row['phase'];previous=row['at']
        if isinstance(row.get('reason'),str) and row['reason'] in REASONS:reason=row['reason']
        if type(row.get('http_code')) is int and 100<=row['http_code']<=599:code=row['http_code']
    timings[phase]=timings.get(phase,0)+(ended-previous)*1000
    if error is not None and (reason is None or reason=='cache_hit'):reason=failure_reason(error)
    result={'budget_ms':round(budget*1000,3),'elapsed_ms':round((ended-started)*1000,3),
            'last_phase':phase,'finished':phase=='finished','reason':reason,
            'timings_ms':{k:round(v,3) for k,v in timings.items()}}
    if code is not None:result['http_code']=code
    return result
