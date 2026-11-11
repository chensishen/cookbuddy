"""Private Unix-socket gateway; reuse model and Milvus HTTP connections across jobs."""
import asyncio
import base64
from contextlib import asynccontextmanager
import os
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI,Request,HTTPException


def origin(url):
    parsed=urlsplit(url)
    return parsed.scheme,parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80)


def configured_origins():
    from check_embedding import config
    cfg=config()
    urls=[cfg.get('LLM_BASE_URL','https://api.deepseek.com/v1'),
          cfg.get('EMBEDDING_BASE_URL','https://ai.gitee.com/v1'),
          os.environ.get('COOKBUDDY_MILVUS_URL',cfg.get('COOKBUDDY_MILVUS_URL','http://127.0.0.1:19532')),
          'http://127.0.0.1:19530']
    return {origin(url) for url in urls}


def create_app(allowed_origins=None):
    allowed=allowed_origins if allowed_origins is not None else configured_origins()
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,
                limits=httpx.Limits(max_connections=16,max_keepalive_connections=16,keepalive_expiry=60)) as client:
            app.state.client=client;app.state.slots=asyncio.Semaphore(16)
            yield
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)

    @app.get('/health')
    async def health():return {'status':'ok'}

    @app.post('/request')
    async def forward(request:Request):
        raw=await request.body()
        if len(raw)>16_000_000:raise HTTPException(413)
        import json
        data=json.loads(raw)
        parsed=urlsplit(data['url'])
        if origin(data['url']) not in allowed or parsed.username or parsed.password:raise HTTPException(403)
        timeout=min(max(float(data['timeout']),.01),60)
        async def fetch():
            async with asyncio.timeout(timeout),app.state.slots:
                # No cookies or redirects cross users. Each request forwards only
                # its explicit headers; the client cookie jar is never used.
                outgoing=httpx.Request(data['method'],data['url'],headers=data['headers'],
                                       content=base64.b64decode(data['body']),
                                       extensions={'timeout':{'connect':timeout,'read':timeout,'write':timeout,'pool':timeout}})
                response=await app.state.client.send(outgoing,stream=True)
                try:
                    parts=[];size=0
                    async for part in response.aiter_bytes():
                        size+=len(part)
                        if size>16_000_000:raise ValueError('upstream_response_too_large')
                        parts.append(part)
                    return {'status':response.status_code,'headers':dict(response.headers),
                            'body':base64.b64encode(b''.join(parts)).decode()}
                finally:await response.aclose()
        task=asyncio.create_task(fetch())
        try:
            while not task.done():
                if await request.is_disconnected():raise asyncio.CancelledError()
                await asyncio.wait({task},timeout=.025)
            try:return task.result()
            except (TimeoutError,httpx.TimeoutException):return {'error':'timeout'}
            except (httpx.HTTPError,ValueError):return {'error':'network'}
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    return app


if __name__=='__main__':
    import sys
    import socket
    import uvicorn
    os.umask(0o077)
    # Uvicorn's uds= startup chmods the socket to 0666. Bind it ourselves
    # and hand over the descriptor so credentials stay owner-only.
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
        listener.bind(sys.argv[1]);os.chmod(sys.argv[1],0o600);listener.listen(128)
        server=uvicorn.Server(uvicorn.Config(create_app(),access_log=False,log_level='critical'))
        asyncio.run(server.serve(sockets=[listener]))
