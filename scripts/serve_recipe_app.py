"""仅绑定本机的菜谱应用：异步查询、来源与原图展示。"""
import argparse
import os
import signal
import re
import hmac
import hashlib
from http.cookies import SimpleCookie
from job_queue import JobQueue, WorkerPool, QueueFull, IdempotencyConflict
from service_runtime import RuntimeStore
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from threading import Lock,BoundedSemaphore
from urllib.parse import parse_qs,urlsplit
import uuid

from check_embedding import ROOT
from process_recipe_media import local_file
from recipe_agent import execute,save_run
from recipe_tools import load_chunks,refresh_history_media,read_recipe
from recipe_catalog import catalog_page, bind_source
from project_doctor import project_id
from llm_config import llm_settings
from conversation_memory import validate_memory
from cookbuddy.store import Store, valid_id
from cookbuddy.models import settings as cookbuddy_settings
from cookbuddy.worker import MemoryWorker

class RecipeServer(ThreadingHTTPServer):
    request_queue_size=128
    def __init__(self,address):
        super().__init__(address,Handler)
        self.http_slots=BoundedSemaphore(64)
        self.queue=JobQueue(capacity=int(os.environ.get("RECIPE_QUEUE_CAPACITY","8")))
        self.access_token=os.environ.get('RECIPE_ACCESS_TOKEN','')
        self.cookbuddy_enabled=cookbuddy_settings().get('COOKBUDDY_ENABLED','0')=='1'
        self.memory_worker=MemoryWorker() if self.cookbuddy_enabled and os.environ.get('RECIPE_LOCAL_ONLY','0')!='1' else None
        self.local_only=os.environ.get('RECIPE_LOCAL_ONLY','0')=='1'
        if self.access_token and not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',self.access_token):raise ValueError('访问口令需要16至128位字母、数字或下划线横线')
        self.recipe_sources={r['source'] for r in load_chunks()}
        index=json.loads((ROOT/'data/processed/media_pilot/asset-index.json').read_text())
        self.image_paths={a['path'] for a in index['assets'] if a['media_type']=='image'}
        count=int(os.environ.get('RECIPE_WORKERS','2'))
        if count<0:raise ValueError('worker数量不能为负')
        self.workers=WorkerPool(self.queue,count) if count else None
    def server_close(self):
        if self.workers:self.workers.close()
        if self.memory_worker:self.memory_worker.close()
        super().server_close()

    def process_request(self,request,address):
        if not self.http_slots.acquire(blocking=False):
            body=json.dumps({'error':'请求较多，请稍后再试'},ensure_ascii=False).encode()
            request.sendall(b'HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
            self.shutdown_request(request)
            return
        try:super().process_request(request,address)
        except Exception:
            self.http_slots.release();raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.http_slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(5)
    def log_message(self,*args):pass

    def respond(self,code,data,content_type='application/json; charset=utf-8'):
        if isinstance(data,(dict,list)):data=json.dumps(data,ensure_ascii=False).encode()
        elif isinstance(data,str):data=data.encode()
        self.send_response(code);self.send_header('Content-Type',content_type)
        if code==429:self.send_header('Retry-After','2')
        if getattr(self,'new_user_cookie',None):self.send_header('Set-Cookie',self.new_user_cookie)
        self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(data)

    def valid_host(self):
        port=self.server.server_port
        return self.headers.get('Host') in (f'127.0.0.1:{port}',f'localhost:{port}')

    def authorized(self):
        if not self.server.access_token:return True
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return False
        supplied=self.headers.get('X-Recipe-Token') or (cookie['recipe_access'].value if 'recipe_access' in cookie else '')
        return hmac.compare_digest(supplied.encode(),self.server.access_token.encode())

    def cookbuddy_user(self):
        if getattr(self,'actor',None):return self.actor
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:pass
        token=cookie['cookbuddy_session'].value if 'cookbuddy_session' in cookie else ''
        secret=cookbuddy_settings().get('COOKBUDDY_SESSION_SECRET','')
        if len(secret)<32:raise ValueError('cookbuddy_session_secret_required')
        uid,separator,signature=token.partition('.')
        expected=hmac.new(secret.encode(),uid.encode(),hashlib.sha256).hexdigest()
        if not valid_id(uid) or not separator or not hmac.compare_digest(signature,expected):
            uid=uuid.uuid4().hex;signature=hmac.new(secret.encode(),uid.encode(),hashlib.sha256).hexdigest()
            Store().ensure_user(uid)
            self.new_user_cookie='cookbuddy_session='+uid+'.'+signature+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000'
        self.actor=uid
        return uid

    def owns_job(self,ident):
        with self.server.queue.connect() as db:row=db.execute('SELECT payload FROM jobs WHERE id=?',(ident,)).fetchone()
        if not row:return False
        owner=json.loads(row[0]).get('user_id')
        return not owner or owner==self.cookbuddy_user()

    def memory_route(self):
        parts=urlsplit(self.path);path=parts.path.removeprefix('/api')
        if path!='/memory' and not path.startswith('/memory/'):return False
        if not self.valid_host() or (self.command!='GET' and self.headers.get('Origin') and self.headers['Origin']!='http://'+self.headers.get('Host','')):
            self.respond(403,{'error':'请求来源不匹配'});return True
        if not self.authorized():self.respond(401,{'error':'请先填写访问口令'});return True
        if not self.server.cookbuddy_enabled:self.respond(503,{'error':'记忆服务尚未启用'});return True
        try:
            from cookbuddy.ltm.profile_store import ProfileStore
            from cookbuddy.ltm.memory_routes import handle
            payload={};length=int(self.headers.get('Content-Length','0'))
            if not 0<=length<=4096:raise ValueError('invalid_body')
            if length:
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('json_required')
                payload=json.loads(self.rfile.read(length))
            code,data=handle(ProfileStore(Store()),self.cookbuddy_user(),self.command,path,payload,parse_qs(parts.query))
            self.respond(code,data)
        except KeyError:self.respond(404,{'error':'记忆不存在'})
        except (ValueError,TypeError) as error:
            self.respond(409 if str(error)=='memory_version_conflict' else 400,{'error':'记忆版本冲突' if str(error)=='memory_version_conflict' else '记忆参数无效'})
        except Exception:self.respond(503,{'error':'记忆服务暂不可用'})
        return True

    def do_PATCH(self):
        if not self.memory_route():self.respond(404,{'error':'接口不存在'})

    def do_PUT(self):
        if not self.memory_route():self.respond(404,{'error':'接口不存在'})

    def do_DELETE(self):
        if not self.memory_route():self.respond(404,{'error':'接口不存在'})

    def do_GET(self):
        if self.memory_route():return
        if not self.valid_host():self.respond(403,{'error':'仅限本机访问'});return
        parts=urlsplit(self.path);query=parse_qs(parts.query)
        if parts.path=='/':
            self.respond(200,(ROOT/'app/index.html').read_bytes(),'text/html; charset=utf-8');return
        if parts.path=='/recipe_markdown.js':
            self.respond(200,(ROOT/'app/recipe_markdown.js').read_bytes(),'text/javascript; charset=utf-8');return
        if parts.path=='/favicon.svg':
            self.respond(200,"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='16' fill='#327358'/><path d='M18 34h28v5a14 14 0 0 1-28 0v-5Z' fill='white'/><path d='M22 25c0-5 4-8 10-8s10 3 10 8' fill='none' stroke='white' stroke-width='4' stroke-linecap='round'/></svg>",'image/svg+xml');return
        if parts.path=='/api/identity':
            self.respond(200,{'project_id':project_id(ROOT),'local_only':self.server.local_only});return
        if not self.authorized():self.respond(401,{'error':'请先填写访问口令'});return
        if parts.path=='/health':
            self.respond(200,RuntimeStore().resilience_health());return
        if parts.path in ('/api/conversations','/api/conversation'):
            if not self.server.cookbuddy_enabled:self.respond(503,{'error':'cookbuddy 后端尚未启用'});return
            try:
                uid=self.cookbuddy_user();store=Store()
                data=store.conversations(uid) if parts.path=='/api/conversations' else store.conversation(uid,query.get('id',[''])[0])
                if parts.path=='/api/conversation':data=refresh_history_media(data)
                self.respond(200,data)
            except ValueError:self.respond(404,{'error':'会话不存在'})
            except Exception:self.respond(503,{'error':'会话服务暂不可用，请检查 PostgreSQL'})
            return
        if parts.path=='/api/status':
            self.respond(200,{'recipe_count':len(self.server.recipe_sources),'image_count':len(self.server.image_paths),
                              'project_id':project_id(ROOT),'local_only':self.server.local_only,
                              'title_count':len({r['title'] for r in load_chunks()}),
                              'agent_model':llm_settings()['model'],'app':'ready','service_note':'问答需Milvus和API；原文与图片工具可本地使用。'});return
        if parts.path=='/api/recipe':
            source=query.get('source',[''])[0]
            if set(query)!={'source'} or len(query['source'])!=1 or source not in self.server.recipe_sources:
                self.respond(404,{'error':'菜谱不在知识库清单中'});return
            self.respond(200,read_recipe(source));return
        if parts.path=='/api/catalog':
            try:
                if set(query)-{'q','category','offset','limit'} or any(len(values)!=1 for values in query.values()):
                    raise ValueError('目录参数无效')
                page=catalog_page(load_chunks(),query.get('q',[''])[0],query.get('category',[''])[0],
                                  int(query.get('offset',['0'])[0]),int(query.get('limit',['12'])[0]))
            except ValueError as error:
                self.respond(400,{'error':str(error)});return
            self.respond(200,page);return
        if parts.path=='/api/job':
            ident=query.get('id',[''])[0]
            if not self.owns_job(ident):self.respond(404,{'error':'记录不存在'});return
            snapshot=self.server.queue.get(ident)
            self.respond(200 if snapshot else 404,snapshot or {'error':'记录不存在'});return
        if parts.path=='/api/metrics':
            self.respond(200,{'jobs':self.server.queue.metrics(),'models':RuntimeStore().metrics()});return
        if parts.path in ('/image','/source'):
            relative=query.get('path',[''])[0]
            allowed=self.server.image_paths if parts.path=='/image' else self.server.recipe_sources
            if relative not in allowed:self.respond(404,{'error':'文件不在知识库清单中'});return
            try:path=local_file(relative)
            except ValueError:self.respond(404,{'error':'来源不可用'});return
            if parts.path=='/image':
                self.respond(200,path.read_bytes(),mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            else:
                lines=path.read_text().splitlines();text='\n'.join(f'{i}: {line}' for i,line in enumerate(lines,1))
                self.respond(200,text,'text/plain; charset=utf-8')
            return
        self.respond(404,{'error':'页面不存在'})

    def do_POST(self):
        host=self.headers.get('Host','');origin=self.headers.get('Origin')
        if not self.valid_host() or origin and origin!='http://'+host:
            self.respond(403,{'error':'请求来源不匹配'});return
        if self.path=='/api/login':
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<2048:raise ValueError()
                token=json.loads(self.rfile.read(length))['token']
                if not isinstance(token,str) or not hmac.compare_digest(token.encode(),self.server.access_token.encode()):raise ValueError()
            except (ValueError,KeyError,TypeError):
                self.respond(401,{'error':'访问口令不正确'});return
            self.send_response(200)
            self.send_header('Set-Cookie','recipe_access='+self.server.access_token+'; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length','11')
            self.end_headers();self.wfile.write(b'{"ok":true}');return
        if not self.authorized():self.respond(401,{'error':'请先填写访问口令'});return
        if self.path=='/api/conversation/end':
            if not self.server.cookbuddy_enabled:self.respond(503,{'error':'记忆服务尚未启用'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<1024:raise ValueError('invalid_body')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict) or set(data)!={'id'} or not valid_id(data['id']):raise ValueError('invalid_conversation')
                from cookbuddy.ltm.maintenance import end_session
                self.respond(200,end_session(Store(),self.cookbuddy_user(),data['id']))
            except KeyError:self.respond(404,{'error':'会话不存在'})
            except (ValueError,TypeError):self.respond(400,{'error':'会话参数无效'})
            except Exception:self.respond(503,{'error':'记忆队列暂不可用'})
            return
        if self.path=='/api/conversations':
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<2048:raise ValueError()
                data=json.loads(self.rfile.read(length));title=data.get('title','新对话')
                if not isinstance(title,str) or len(title)>80:raise ValueError()
                cid=Store().create_conversation(self.cookbuddy_user(),title or '新对话')
                self.respond(201,{'id':cid});return
            except (ValueError,TypeError):self.respond(400,{'error':'会话参数无效'});return
            except Exception:self.respond(503,{'error':'会话服务暂不可用'});return
        if self.path=='/api/cancel':
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<1024:raise ValueError()
                ident=json.loads(self.rfile.read(length))['job_id']
                if not isinstance(ident,str) or len(ident)!=32:raise ValueError()
            except (ValueError,KeyError,TypeError):
                self.respond(400,{'error':'任务标识无效'});return
            if not self.owns_job(ident):self.respond(404,{'error':'记录不存在'});return
            self.respond(200,{'cancelled':self.server.queue.cancel(ident)});return
        if self.path!='/api/ask':self.respond(404,{'error':'接口不存在'});return
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
            self.respond(415,{'error':'需要JSON请求'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=50000:raise ValueError()
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict) or set(payload)-{'question','mode','title','section','source','history','memories','conversation_id'}:raise ValueError()
            validate_memory(payload.get('history', []), payload.get('memories', []))
            question=payload.get('question');mode=payload.get('mode','agent')
            if not isinstance(question,str) or not question.strip() or len(question)>2000:raise ValueError()
            if mode not in ('auto','agent','multi_agent','rag','recipe','images'):raise ValueError()
            if any(not isinstance(payload.get(k,''),str) or len(payload.get(k,''))>2000 for k in ('title','section','source')):raise ValueError()
            source=payload.get('source','')
            if source and source not in self.server.recipe_sources:raise ValueError()
        except (ValueError,TypeError,UnicodeDecodeError):
            self.respond(400,{'error':'请输入有效的问题，最多2000字'});return
        if self.server.local_only and mode not in ('recipe','images'):
            self.respond(400,{'error':'本次以本地模式启动，请选择原文或图片查询；模型请求已禁用'});return
        if source:
            try:bind_source(question,payload.get('title',''),source,load_chunks())
            except ValueError as error:
                self.respond(400,{'error':str(error)});return
        if mode=='auto':
            if not self.server.cookbuddy_enabled or self.server.local_only:
                self.respond(503,{'error':'自动问答后端尚未启用'});return
            try:
                uid=self.cookbuddy_user();store=Store();cid=payload.get('conversation_id')
                if cid:store.conversation(uid,cid)
                else:cid=store.create_conversation(uid,question.strip())
                payload={'question':question.strip(),'mode':'auto','user_id':uid,'conversation_id':cid,'memory_epoch':store.memory_epoch(uid)}
            except ValueError:self.respond(404,{'error':'会话不存在'});return
            except Exception:self.respond(503,{'error':'会话服务暂不可用，请检查 PostgreSQL'});return
        else:payload.pop('conversation_id',None)
        idem=self.headers.get('Idempotency-Key')
        if idem and (len(idem)>128 or not idem.isascii()):
            self.respond(400,{'error':'提交标识无效'});return
        try:
            ident,reused=self.server.queue.submit(payload,(hashlib.sha256((payload.get('user_id','')+idem).encode()).hexdigest() if idem else None),
                seconds=float(os.environ.get('RECIPE_REQUEST_SECONDS','240')),
                client=hashlib.sha256(self.client_address[0].encode()).hexdigest(),
                rate=int(os.environ.get('RECIPE_USER_RPM','60')),ready=mode!='auto')
        except QueueFull as error:
            self.respond(429,{'error':str(error),'retry_after_seconds':2});return
        except IdempotencyConflict as error:
            self.respond(409,{'error':str(error)});return
        if mode=='auto':
            try:
                store.append(uid,cid,ident,'user',question.strip())
                self.server.queue.release(ident)
            except Exception:
                self.server.queue.cancel(ident);self.respond(503,{'error':'消息保存失败，请重试'});return
        self.respond(202,{'job_id':ident,'reused':reused,'conversation_id':payload.get('conversation_id')})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8080)
    args=parser.parse_args()
    def stop_signal(*args):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop_signal)
    import sys
    sys.path.insert(0,str(ROOT))
    from src.retrieval.http_pool import NetworkPool
    with NetworkPool():
        server=RecipeServer(('127.0.0.1',args.port));print(f'cookbuddy：http://127.0.0.1:{server.server_port}',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()


if __name__=='__main__':main()
