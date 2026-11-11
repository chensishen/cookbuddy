"""跨进程模型准入、缓存、熔断、重试与请求预算；不记录正文或认证头。"""
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
import math
from functools import wraps, lru_cache
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from pooled_http import urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
_deadline = ContextVar('recipe_deadline', default=None)
_cancel = ContextVar('recipe_cancel', default=None)


class BudgetExceeded(TimeoutError):
    pass


class CircuitOpen(RuntimeError):
    pass


class AdmissionTimeout(TimeoutError):
    """本地排队预算耗尽，父请求仍可继续执行降级。"""
    def __init__(self, reason):
        self.reason=reason
        super().__init__('模型准入等待超时：'+reason)


def remaining(cap=180):
    callback = _cancel.get()
    if callback and callback():
        raise BudgetExceeded('查询已取消')
    deadline = _deadline.get()
    if deadline is None:
        deadline = float(os.environ.get('RECIPE_DEADLINE_EPOCH', '0')) or None
    left = deadline - time.time() if deadline is not None else cap
    if left <= 0:
        raise BudgetExceeded('查询总时间预算已用尽')
    return min(cap, left)


@contextmanager
def request_budget(seconds=180, deadline=None, cancelled=None):
    parent = _deadline.get() or (float(os.environ.get('RECIPE_DEADLINE_EPOCH','0')) or None)
    target = deadline if deadline is not None else time.time() + seconds
    if parent is not None:
        target = min(parent, target)
    a = _deadline.set(target)
    b = _cancel.set(cancelled if cancelled is not None else _cancel.get())
    try:
        yield target
    finally:
        _deadline.reset(a)
        _cancel.reset(b)


def pause(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        left = remaining()
        time.sleep(min(.1, left, max(0, end - time.monotonic())))


def retry_after_seconds(headers):
    value = headers.get('Retry-After') if headers else None
    if value is not None:
        try:
            seconds = float(value)
        except (ValueError,TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0,seconds) if math.isfinite(seconds) else None
    return None


def retry_delay(headers, attempt, rng=random.random):
    seconds = retry_after_seconds(headers)
    if seconds is not None:return seconds
    return min(8, 2 ** attempt) * (.5 + rng())


def budgeted(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with request_budget(float(os.environ.get('RECIPE_REQUEST_SECONDS','180'))):
            return fn(*args, **kwargs)
    return wrapped


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class RuntimeStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('RECIPE_RUNTIME_DB', ROOT / 'data/runtime/service.sqlite3'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, scope TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS calls(scope TEXT, created REAL, tokens INTEGER);
                CREATE TABLE IF NOT EXISTS circuits(scope TEXT PRIMARY KEY, failures INTEGER,
                    opened REAL, probe TEXT);
                CREATE TABLE IF NOT EXISTS provider_cooldowns(scope TEXT PRIMARY KEY, retry_until REAL);
                CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, expires REAL, value TEXT);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, created REAL,
                    phase TEXT, model TEXT, attempt INTEGER, elapsed REAL, outcome TEXT, code INTEGER);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'job' not in {r[1] for r in db.execute('PRAGMA table_info(leases)')}:
                db.execute('ALTER TABLE leases ADD COLUMN job TEXT')
            if 'call_id' not in {r[1] for r in db.execute('PRAGMA table_info(calls)')}:
                db.execute('ALTER TABLE calls ADD COLUMN call_id TEXT')
            if 'reason' not in {r[1] for r in db.execute('PRAGMA table_info(events)')}:
                db.execute('ALTER TABLE events ADD COLUMN reason TEXT')
            if 'breaker_scope' not in {r[1] for r in db.execute('PRAGMA table_info(leases)')}:
                db.execute('ALTER TABLE leases ADD COLUMN breaker_scope TEXT')
            if 'breaker_policy' not in {r[1] for r in db.execute('PRAGMA table_info(leases)')}:
                db.execute('ALTER TABLE leases ADD COLUMN breaker_policy TEXT')
            db.commit()
            from resilience.breaker import schema
            schema(db)

    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None, factory=ClosingConnection)
        db.execute('PRAGMA busy_timeout=5000')
        if db.execute('PRAGMA journal_mode').fetchone()[0]!='wal':
            db.execute('PRAGMA journal_mode=WAL')
        return db

    def acquire(self, scope, tokens=0, concurrency=2, rpm=60, tpm=60000,
                threshold=None, cooldown=None, lease_seconds=120, diagnostics=None,breaker_scope=None):
        if diagnostics is not None:diagnostics.pop('retry_after_seconds',None)
        now = time.time()
        ident = uuid.uuid4().hex
        db = self.connection()
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM leases WHERE expires < ?', (now,))
            db.execute('DELETE FROM calls WHERE created < ?', (now - 60,))
            db.execute('DELETE FROM provider_cooldowns WHERE retry_until <= ?', (now,))
            retry_until = db.execute('SELECT retry_until FROM provider_cooldowns WHERE scope=?', (scope,)).fetchone()
            if retry_until:
                if diagnostics is not None:
                    diagnostics.update(reason='provider_retry_after',retry_after_seconds=retry_until[0]-now)
                db.commit()
                return None
            from resilience.breaker import admit,policy,activate
            circuit_scope=breaker_scope or scope
            circuit_policy=policy(threshold,cooldown)
            admitted=admit(db,circuit_scope,now,circuit_policy)
            if admitted is False:raise CircuitOpen('模型服务暂时熔断，请稍后再试')
            if admitted is None:
                if diagnostics is not None:diagnostics['reason']='half_open_probe'
                db.rollback();return None
            active = db.execute('SELECT count(*) FROM leases WHERE scope=?', (scope,)).fetchone()[0]
            count, consumed = db.execute('SELECT count(*),coalesce(sum(tokens),0) FROM calls WHERE scope=?', (scope,)).fetchone()
            if tokens > tpm:
                raise ValueError('单次模型输入预算超过每分钟配额')
            if active >= concurrency or count >= rpm or consumed + tokens > tpm:
                if diagnostics is not None:
                    diagnostics['reason']='concurrency' if active>=concurrency else 'rpm' if count>=rpm else 'tpm'
                db.rollback()
                return None
            db.execute('INSERT INTO leases(id,scope,expires,job,breaker_scope,breaker_policy) VALUES(?,?,?,?,?,?)',
                       (ident, scope, now + lease_seconds,os.environ.get('RECIPE_JOB_ID'),circuit_scope,json.dumps(circuit_policy)))
            db.execute('INSERT INTO calls(scope,created,tokens,call_id) VALUES(?,?,?,?)', (scope, now, tokens,ident))
            activate(db,circuit_scope,now,ident)
            db.commit()
            return ident
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def settle_usage(self,ident,result):
        usage=result.get('usage') if isinstance(result,dict) else None
        actual=usage.get('total_tokens') if isinstance(usage,dict) else None
        # Missing/invalid provider usage keeps the conservative reservation.
        if not isinstance(actual,int) or isinstance(actual,bool) or actual<0:return
        with self.connection() as db:
            db.execute('UPDATE calls SET tokens=? WHERE call_id=?',(actual,ident))

    def defer_provider(self,scope,seconds):
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<0:
            raise ValueError('供应商冷却时间须为非负有限秒数')
        if seconds==0:return
        with self.connection() as db:
            db.execute('''INSERT INTO provider_cooldowns VALUES(?,?) ON CONFLICT(scope)
                DO UPDATE SET retry_until=max(retry_until,excluded.retry_until)''',(scope,time.time()+seconds))

    def release(self, ident, scope, success=None,category=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT breaker_scope,breaker_policy FROM leases WHERE id=?',(ident,)).fetchone()
            db.execute('DELETE FROM leases WHERE id=?', (ident,))
            from resilience.breaker import finish,policy
            circuit_scope=(row[0] if row else None) or scope
            finish(db,circuit_scope,time.time(),category or ('ok' if success is True else 'connect_error' if success is False else 'client_cancel'),json.loads(row[1]) if row and row[1] else policy(),ident)
            db.commit()

    def release_job(self, job):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE circuits SET probe=NULL WHERE probe IN (SELECT id FROM leases WHERE job=?)',(job,))
            db.execute('DELETE FROM leases WHERE job=?',(job,))
            db.commit()

    def get(self, key):
        with self.connection() as db:
            row = db.execute('SELECT value FROM cache WHERE key=? AND expires>?', (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value, ttl=3600):
        with self.connection() as db:
            db.execute('DELETE FROM cache WHERE expires < ?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)', (key, time.time() + ttl, json.dumps(value, ensure_ascii=False)))

    def event(self, phase, model, attempt, elapsed, outcome, code=None, reason=None):
        with self.connection() as db:
            # 保留最后一个编号，避免空表重用id使逐请求增量诊断丢失新事件。
            db.execute('DELETE FROM events WHERE created < ? AND id < (SELECT max(id) FROM events)', (time.time()-7*86400,))
            db.execute('INSERT INTO events(created,phase,model,attempt,elapsed,outcome,code,reason) VALUES(?,?,?,?,?,?,?,?)',
                       (time.time(), phase, model, attempt, elapsed, outcome, code, reason))

    def events_since(self, ident=0):
        with self.connection() as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE id>? ORDER BY id',(ident,))]

    def metrics(self):
        with self.connection() as db:
            rows = db.execute('SELECT phase,outcome,count(*),avg(elapsed) FROM events GROUP BY phase,outcome').fetchall()
            samples=db.execute('SELECT phase,outcome,elapsed,reason FROM events').fetchall()
        report=[]
        for row in rows:
            values=sorted(r[2] for r in samples if r[:2]==row[:2])
            report.append({'phase':row[0],'outcome':row[1],'count':row[2],'mean_seconds':round(row[3],3),
                           'p95_seconds':values[min(len(values)-1,int(len(values)*.95))],
                           'admission_reasons':{reason:sum(r[:2]==row[:2] and r[3]==reason for r in samples)
                               for reason in {r[3] for r in samples if r[:2]==row[:2] and r[3]}}})
        return report

    def count_failure(self,dependency,category):
        with self.connection() as db:
            db.execute('INSERT INTO failure_counts VALUES(?,?,1) ON CONFLICT(dependency,category) DO UPDATE SET count=count+1',
                       (dependency,category))

    def record_request(self,request_id,degraded):
        from resilience.config import settings
        now=time.time()
        with self.connection() as db:
            db.execute('DELETE FROM request_outcomes WHERE created<?',(now-settings()['alerts']['window_seconds'],))
            db.execute('INSERT OR REPLACE INTO request_outcomes VALUES(?,?,?)',(request_id,now,int(degraded)))

    def resilience_health(self):
        from resilience.config import settings
        now=time.time();cfg=settings();dependencies={}
        with self.connection() as db:
            for name in ('router_model','main_model','memory_model','embedding','rerank','milvus'):
                rows=db.execute('SELECT state,opened,backoff,open_count,total_open FROM circuits WHERE scope LIKE ?',(name+':%',)).fetchall()
                state='open' if any(r[0]=='open' for r in rows) else 'half_open' if any(r[0]=='half_open' for r in rows) else 'closed'
                opened=max((now-r[1] for r in rows if r[0]=='open'),default=0)
                counts=dict(db.execute('SELECT category,count FROM failure_counts WHERE dependency=?',(name,)))
                dependencies[name]={'state':state,'open_count':sum(r[3] for r in rows),
                    'open_seconds':round(opened,3),'total_open_seconds':round(sum(r[4]+(now-r[1] if r[0]=='open' else 0) for r in rows),3),
                    'counts':counts,'alert':opened>cfg['alerts']['open_duration_seconds_threshold'] or bool(counts.get('internal_error') or counts.get('invalid_json') or counts.get('validation_error'))}
            total,degraded=db.execute('SELECT count(*),coalesce(sum(degraded),0) FROM request_outcomes WHERE created>=?',
                                     (now-cfg['alerts']['window_seconds'],)).fetchone()
        ratio=degraded/total if total else 0
        alerts=[k for k,v in dependencies.items() if v['alert']]
        if total>=cfg['alerts']['min_requests'] and ratio>cfg['alerts']['degraded_ratio_threshold']:alerts.append('degraded_ratio')
        return {'dependencies':dependencies,'requests':{'total':total,'degraded':degraded,'degraded_ratio':ratio},'alerts':alerts}


def guarded_call(fn, *, phase, model='', scope=None, tokens=0, timeout=45,
                 store=None, attempts=3, cache_key=None, opener_errors=None, admission_timeout=None):
    """仅重试网络/429/可恢复5xx；预算和取消一直生效，业务校验不重试。"""
    if admission_timeout is not None and (isinstance(admission_timeout,bool) or not math.isfinite(admission_timeout) or admission_timeout<=0):
        raise ValueError('准入等待预算须为有限正数')
    from stage_diagnostics import record as stage_record,failure_reason
    def progress(name,reason=None,code=None):
        if phase=='rerank':stage_record(name,reason,code)
    store = store or RuntimeStore()
    if cache_key:
        hit = store.get(cache_key)
        if hit is not None:
            progress('finalize','cache_hit')
            remaining()
            store.event(phase, model, 0, 0, 'cache_hit')
            return hit
    scope = scope or model or phase
    from resilience.failure import dependency,classify
    circuit_scope=dependency(phase)+':'+scope
    for attempt in range(attempts):
        started = time.monotonic()
        left = remaining(timeout)
        ident = None
        admission={}
        admission_end=started+admission_timeout if admission_timeout is not None else None
        progress('admission')
        try:
            while ident is None:
                remaining()
                if admission_end is not None and time.monotonic()>=admission_end:
                    raise AdmissionTimeout(admission.get('reason','unknown'))
                ident = store.acquire(scope, tokens,
                    concurrency=int(os.environ.get('RECIPE_MODEL_CONCURRENCY', '2')),
                    breaker_scope=circuit_scope,
                    rpm=int(os.environ.get('RECIPE_MODEL_RPM', '60')),
                    tpm=int(os.environ.get('RECIPE_MODEL_TPM', '60000')),
                    lease_seconds=left + 10,diagnostics=admission)
                if ident is None:
                    if admission.get('retry_after_seconds',0)>=remaining():
                        error=BudgetExceeded('供应商冷却等待超过剩余预算')
                        error.reason='provider_retry_after'
                        error.retry_after_seconds=admission['retry_after_seconds']
                        raise error
                    pause(min(.1,max(0,admission_end-time.monotonic())) if admission_end is not None else .1)
                    left = remaining(timeout)
        except (CircuitOpen,BudgetExceeded,AdmissionTimeout) as error:
            progress('finalize',failure_reason(error))
            store.event(phase,model,attempt+1,time.monotonic()-started,
                        'circuit_open' if isinstance(error,CircuitOpen) else 'admission_timeout',reason=admission.get('reason'))
            raise
        admitted=time.monotonic()
        progress('api')
        store.event(phase+'_admission',model,attempt+1,admitted-started,'ok',reason=admission.get('reason'))
        outcome, code, retry, headers, success,category = 'ok', None, False, None, None,'ok'
        try:
            result = fn(remaining(timeout))
            remaining()  # 不交付超出预算才返回的结果。
            success = True
            store.settle_usage(ident,result)
            if cache_key:
                store.put(cache_key, result)
            return result
        except HTTPError as error:
            category=classify(error)
            progress('finalize',failure_reason(error),error.code)
            code, headers = error.code, error.headers
            if code==429:
                seconds=retry_after_seconds(headers)
                if seconds is not None:store.defer_provider(scope,seconds)
            retry = code in (429, 500, 502, 503, 504)
            outcome, success = 'http_error', False if retry else None
            if not retry or attempt == attempts - 1:
                raise
        except (TimeoutError, URLError, ConnectionError) as error:
            category=classify(error)
            progress('finalize',failure_reason(error))
            outcome = 'timeout' if isinstance(error, TimeoutError) else 'network_error'
            success = False
            if isinstance(error, BudgetExceeded) or attempt == attempts - 1:
                raise
            retry = True
        except BaseException as error:
            category=classify(error)
            progress('finalize','result_validation')
            # HTTPX 网络错误由 LangChain transport 在此分类，不对任意模型格式错误重试。
            if opener_errors and isinstance(error, opener_errors):
                category='timeout' if 'Timeout' in type(error).__name__ else 'connect_error'
                outcome, success, retry = 'network_error', False, True
                if attempt == attempts - 1:
                    raise
            else:
                outcome = 'invalid_response'
                raise
        finally:
            progress('finalize')
            store.release(ident, scope, success,category=category)
            store.event(phase+'_api', model, attempt + 1, time.monotonic() - admitted, outcome, code,reason=category)
            store.event(phase, model, attempt + 1, time.monotonic() - started, outcome, code,reason=category)
        if retry:
            delay = retry_delay(headers, attempt)
            if delay >= remaining():
                raise BudgetExceeded('重试等待超过剩余预算')
            pause(delay)


@lru_cache(maxsize=1)
def quota_tokenizer():
    import tiktoken
    return tiktoken.get_encoding('cl100k_base')


def estimate_request_tokens(payload,default_output=0):
    # A tokenizer proxy with 25% headroom, not UTF-8 bytes or exact provider billing.
    text=json.dumps(payload,ensure_ascii=False)
    input_tokens=len(quota_tokenizer().encode(text,disallowed_special=()))
    output=payload.get('max_completion_tokens',payload.get('max_tokens',default_output))
    if not isinstance(output,int) or isinstance(output,bool) or output<0:raise ValueError('invalid_output_budget')
    return math.ceil(input_tokens*1.25)+output


def request_json(request, timeout=45, phase='model', cache_namespace=None, attempts=3, admission_timeout=None):
    payload = json.loads(request.data) if request.data else {}
    model = payload.get('model', '')
    tokens = estimate_request_tokens(payload)
    scope = urlparse(request.full_url).netloc + ':' + (model or phase)
    key = None
    if cache_namespace:
        fingerprint = json.dumps([os.environ.get('RECIPE_CACHE_NAMESPACE',''),cache_namespace, request.full_url, payload], sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(fingerprint.encode()).hexdigest()
    def invoke(wait):
        from usage_ledger import record_usage
        result=None;failure=None
        try:
            with urlopen(request, timeout=wait) as response:
                result=json.load(response)
        except Exception as error:
            failure=error
            raise
        finally:
            record_usage(request.full_url,model,phase,result,failure)
        if request.full_url.endswith('/embeddings'):
            vectors=result.get('data',[])
            expected=len(payload['input']) if isinstance(payload.get('input'),list) else 1
            if not expected or not isinstance(vectors,list) or len(vectors)!=expected:raise ValueError('向量数量无效')
            if expected>1 and (any(type(r.get('index')) is not int for r in vectors) or {r.get('index') for r in vectors}!=set(range(expected))):raise ValueError('向量编号无效')
            for row in vectors:
                vector=row.get('embedding',[])
                if len(vector)!=int(payload['dimensions']) or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in vector) or not any(vector):
                    raise ValueError('向量无效，不写入缓存')
        if request.full_url.endswith('/rerank'):
            rows=result.get('results',[]);expected=min(payload.get('top_n',len(payload['documents'])),len(payload['documents']))
            if len(rows)!=expected or len(set(r['index'] for r in rows))!=expected or any(not isinstance(r['index'],int) or isinstance(r['index'],bool) or not 0<=r['index']<len(payload['documents']) or not isinstance(r['relevance_score'],(int,float)) or isinstance(r['relevance_score'],bool) or not math.isfinite(r['relevance_score']) for r in rows):
                raise ValueError('重排结果无效，不写入缓存')
        return result
    # Admission, retries and backoff share one API-call budget.
    with request_budget(seconds=timeout):
        return guarded_call(invoke, phase=phase, model=model, scope=scope, tokens=tokens,
                            timeout=timeout, attempts=attempts, cache_key=key, admission_timeout=admission_timeout)


def generation_with_fallback(request, timeout=45, phase='generation'):
    """仅聊天生成可配置备用模型；向量与重排不会偷偷切换模型空间。"""
    from urllib.request import Request
    fallback=os.environ.get('RECIPE_GENERATION_FALLBACK','')
    try:
        return request_json(request,timeout=timeout,phase=phase)
    except HTTPError as error:
        if error.code not in (429,500,502,503,504):raise
        if not fallback:raise
    except (URLError,TimeoutError,CircuitOpen) as error:
        if not fallback or isinstance(error,BudgetExceeded):raise
    remaining(timeout)
    payload=json.loads(request.data)
    if not request.full_url.endswith('/chat/completions'):
        raise ValueError('备用模型只适用于聊天生成')
    if payload['model']==fallback:raise ValueError('备用模型必须与主模型不同')
    alternate=Request(request.full_url,data=json.dumps({**payload,'model':fallback},ensure_ascii=False).encode(),
                      headers={k:v for k,v in request.header_items() if k.lower()!='content-length'},method='POST')
    result=request_json(alternate,timeout=timeout,phase=phase+'_fallback')
    # 事实格式与依据继续走调用方原有校验，不直出备用模型内容。
    return result


def langchain_http_client():
    """让 LangChain 的 HTTP 请求也经过同一跨进程准入与总预算。"""
    import httpx
    class GovernedTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport()
        def handle_request(self, request):
            body = json.loads(request.content) if request.content else {}
            def invoke(wait):
                from usage_ledger import record_usage
                request.extensions['timeout'] = {k: wait for k in ('connect','read','write','pool')}
                try:response = self.inner.handle_request(request)
                except Exception as error:
                    record_usage(str(request.url),body.get('model',''),'agent',error=error)
                    raise
                if response.status_code >= 400:
                    response.read()
                    response.close()
                    error=HTTPError(str(request.url), response.status_code, '模型请求失败', response.headers, None)
                    record_usage(str(request.url),body.get('model',''),'agent',error=error,error_body=response.content[:8193])
                    raise error
                response.read()  # 读取响应也包含在 lease 和预算内。
                record_usage(str(request.url),body.get('model',''),'agent',response.json())
                return response
            return guarded_call(invoke, phase='agent', model=body.get('model',''),
                scope=request.url.host + ':' + body.get('model',''),
                tokens=estimate_request_tokens(body,default_output=700),
                timeout=60, opener_errors=(httpx.TransportError,))
        def close(self):
            self.inner.close()
    return httpx.Client(transport=GovernedTransport())
