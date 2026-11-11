"""Async process boundary for existing pure-Python services. No framework imports."""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from contextlib import asynccontextmanager

ROOT=Path(__file__).resolve().parents[2]


class RecipeServices:
    def __init__(self, config):
        self.config=config

    @asynccontextmanager
    async def session_lock(self,context,timeout):
        from .session import checkpoint_thread
        token=checkpoint_thread.set(context.thread_id)
        try:
            async with self._session_lock(context,timeout):yield
        finally:checkpoint_thread.reset(token)

    @asynccontextmanager
    async def _session_lock(self,context,timeout):
        if self.config.checkpoint.backend=='postgres':
            from psycopg import AsyncConnection
            sys.path.insert(0,str(ROOT/'scripts'))
            from cookbuddy.models import settings
            import time
            key=int(context.thread_id[:16],16)
            if key>=2**63:key-=2**64
            budget=context.remaining(timeout);end=time.monotonic()+budget
            connection=await asyncio.wait_for(AsyncConnection.connect(settings()['COOKBUDDY_DATABASE_URL'],
                    autocommit=True,connect_timeout=3,options='-c statement_timeout=2000'),budget)
            async with connection as conn:
                while True:
                    row=await (await conn.execute('SELECT pg_try_advisory_lock(%s)',(key,))).fetchone()
                    if row[0]:break
                    if time.monotonic()>=end:raise TimeoutError('session_busy')
                    await asyncio.sleep(.02)
                # Closing this dedicated connection also releases the session
                # lock on cancellation; no unlock round trip can delay cleanup.
                yield
            return
        import fcntl
        import time
        folder=ROOT/'data/runtime/langchain-session-locks';folder.mkdir(parents=True,exist_ok=True)
        with (folder/context.thread_id).open('a') as handle:
            end=time.monotonic()+context.remaining(timeout)
            while True:
                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=end:raise TimeoutError('session_busy')
                    await asyncio.sleep(.02)
            try:yield
            finally:fcntl.flock(handle,fcntl.LOCK_UN)

    async def call(self, operation, arguments, context, *, hard=(), timeout=8):
        import uuid
        lease_job=context.request_id+'-'+uuid.uuid4().hex
        import hashlib
        meter_folder=ROOT/'data/runtime/langchain-usage'/hashlib.sha256(context.request_id.encode()).hexdigest()
        meter_folder.mkdir(parents=True,exist_ok=True)
        ledger=meter_folder/(uuid.uuid4().hex+'.jsonl')
        env=dict(os.environ, RECIPE_DEADLINE_EPOCH=str(min(context.deadline,time.time()+timeout)),RECIPE_JOB_ID=lease_job,
                 RECIPE_USAGE_LEDGER=str(ledger))
        payload={'operation':operation,'arguments':arguments,'user_id':context.user_id,
                 'session_id':context.session_id,'request_id':context.request_id,
                 'hard':list(hard),'config':self.config.model_dump()}
        process=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'scripts/langchain_service_worker.py'),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            cwd=ROOT,env=env,start_new_session=True)
        try:
            output,_=await asyncio.wait_for(process.communicate(json.dumps(payload,ensure_ascii=False).encode()),
                                             context.remaining(timeout))
            if process.returncode or len(output)>4_000_000: raise RuntimeError('service_failed')
            result=json.loads(output)
            if not isinstance(result,dict): raise RuntimeError('invalid_service_result')
            if result.get('_error') and operation!='model':
                from scripts.stage_diagnostics import normalize_reason
                error=RuntimeError('service_failed')
                reason={'TimeoutExpired':'stage_deadline','ValueError':'result_validation','KeyError':'result_validation'}.get(result['_error'],result['_error'])
                error.reason=normalize_reason(reason)
                raise error
            metering=[json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
            chats=[row for row in metering if row['endpoint'].endswith('/chat/completions')]
            def counts(value):
                return {k:v for k,v in (value or {}).items() if k in
                    ('prompt_tokens','completion_tokens','total_tokens','input_tokens','output_tokens','cached_tokens')
                    and type(v) is int and v>=0}
            result['_service_usage']={'api_calls':len(metering),'model_calls':len(chats),
                'input_tokens':sum((r.get('usage') or {}).get('prompt_tokens',0) for r in chats),
                'output_tokens':sum((r.get('usage') or {}).get('completion_tokens',0) for r in chats),
                'unknown_usage_calls':sum(r.get('usage_status')!='reported' for r in chats),
                'phases':[{'phase':r['phase'],'model':r['model'],'usage':counts(r.get('usage')),
                           'billed_units':counts(r.get('billed_units')),'outcome':r['outcome']} for r in metering]}
            return result
        finally:
            # Kill the process group even if a child outlived its parent. A timed-out
            # asyncio thread would not provide this cancellation guarantee.
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            if process.returncode is None: await process.wait()
            # SIGKILL cannot run a child's HTTP finally block. Reclaim only this
            # operation's leases, never sibling parallel calls in the request.
            from scripts.service_runtime import RuntimeStore
            await asyncio.to_thread(lambda:RuntimeStore().release_job(lease_job))
