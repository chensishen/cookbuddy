"""Authenticated same-origin SSE; drafts and private graph updates stay server-side."""
import asyncio
import hashlib
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
import sys
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import StreamingResponse,FileResponse
from pydantic import BaseModel,ConfigDict,Field
from ..agent.checkpoints import checkpoint_store
from ..agent.config import load_config,ROOT
from ..agent.context import AppContext
from ..agent.engine import ChatEngine,BusyError
from ..retrieval.pipeline import RecipeServices
from ..retrieval.http_pool import NetworkPool


class ChatInput(BaseModel):
    model_config=ConfigDict(extra='ignore')
    session_id: str=Field(pattern='^[a-f0-9]{32}$')
    message: str=Field(min_length=1,max_length=2000)


def create_app(cfg=None,*,engine=None,secret=None,checkpoint_path=None):
    cfg=cfg or load_config()
    if secret is None:
        sys.path.insert(0,str(ROOT/'scripts'))
        from cookbuddy.models import settings
        secret=settings().get('COOKBUDDY_SESSION_SECRET','')
    if len(secret)<32:raise ValueError('session_secret_required')

    @asynccontextmanager
    async def lifespan(app):
        if engine:
            app.state.engine=engine
            try:yield
            finally:await engine.memory_queue.close()
        else:
            pool=NetworkPool()
            await asyncio.to_thread(pool.__enter__)
            try:
                async with checkpoint_store(cfg,checkpoint_path) as saver:
                    app.state.engine=ChatEngine(cfg,RecipeServices(cfg),saver)
                    try:yield
                    finally:await app.state.engine.memory_queue.close()
            finally:await asyncio.to_thread(pool.__exit__,None,None,None)
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)

    def actor(request):
        import os
        access=os.environ.get('RECIPE_ACCESS_TOKEN','')
        if access and not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+access):
            raise HTTPException(401,'access_required')
        origin=request.headers.get('origin')
        if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'origin_rejected')
        uid,_,sig=request.cookies.get('cookbuddy_session','').partition('.')
        expected=hmac.new(secret.encode(),uid.encode(),hashlib.sha256).hexdigest()
        if len(uid)!=32 or any(c not in '0123456789abcdef' for c in uid) or not hmac.compare_digest(sig,expected):return None
        return uid

    @app.get('/')
    async def index():return FileResponse(ROOT/'app/langchain.html')

    @app.get('/recipe_markdown.js')
    async def markdown_script():return FileResponse(ROOT/'app/recipe_markdown.js',media_type='text/javascript')

    @app.get('/health')
    async def health():
        from scripts.service_runtime import RuntimeStore
        return {'status':'ok','engine':'langchain','config':'agent.yaml',
                'checkpoint':'sqlite' if checkpoint_path else cfg.checkpoint.backend,
                **await asyncio.to_thread(lambda:RuntimeStore().resilience_health())}

    @app.get('/media')
    async def media(path:str,request:Request):
        if not actor(request):raise HTTPException(401,'session_required')
        def checked_image():
            index=json.loads((ROOT/'data/processed/media_pilot/asset-index.json').read_text())
            asset=next((r for r in index['assets'] if r['path']==path and r['media_type']=='image'),None)
            local=(ROOT/path).resolve()
            if (not asset or not local.is_relative_to(ROOT/'data/HowToCook') or not local.is_file()
                    or hashlib.sha256(local.read_bytes()).hexdigest()!=asset['sha256']):return None
            return local
        local=await asyncio.to_thread(checked_image)
        if local is None:raise HTTPException(404,'image_not_found')
        return FileResponse(local,headers={'X-Content-Type-Options':'nosniff'})

    @app.post('/sessions')
    async def session(request:Request):
        uid=actor(request) or uuid.uuid4().hex
        context=AppContext(uid,uuid.uuid4().hex)
        data=await app.state.engine.services.call('auth',{'create':True},context,timeout=5)
        from fastapi.responses import JSONResponse
        response=JSONResponse(data)
        sig=hmac.new(secret.encode(),uid.encode(),hashlib.sha256).hexdigest()
        response.set_cookie('cookbuddy_session',uid+'.'+sig,httponly=True,samesite='strict',max_age=31536000)
        return response

    @app.post('/chat')
    async def chat(body:ChatInput,request:Request):
        uid=actor(request)
        if not uid:raise HTTPException(401,'session_required')
        context=AppContext(uid,body.session_id,deadline=time.time()+cfg.agent.request_timeout_s)
        try:await app.state.engine.services.call('auth',{},context,timeout=5)
        except Exception:raise HTTPException(404,'session_not_found') from None
        # Admission happens before headers, so queue overflow is a real HTTP429.
        current=app.state.engine
        try:await asyncio.wait_for(current.semaphore.acquire(),context.remaining(cfg.resilience.queue_timeout_s))
        except TimeoutError:raise HTTPException(429,'queue_timeout',headers={'Retry-After':'2'}) from None
        queue=asyncio.Queue(32)
        async def emit(event,data):await queue.put((event,data))
        async def execute():
            try:await current.answer_admitted(body.message,context,emit)
            except asyncio.CancelledError:raise
            except Exception:await emit('error',{'message':'本次请求未完成，请稍后重试'})
            finally:
                try:queue.put_nowait(None)
                except asyncio.QueueFull:pass
        task=asyncio.create_task(execute())
        async def stream():
            try:
                while True:
                    if await request.is_disconnected():break
                    try:item=await asyncio.wait_for(queue.get(),.25)
                    except TimeoutError:
                        if task.done() and queue.empty():break
                        yield ': keepalive\n\n';continue
                    if item is None:break
                    event,data=item
                    yield 'event: '+event+'\ndata: '+json.dumps(data,ensure_ascii=False)+'\n\n'
            finally:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
        return StreamingResponse(stream(),media_type='text/event-stream',headers={
            'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff'})
    return app
