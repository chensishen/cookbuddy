"""本地真实页面接口负载、幂等、取消、deadline、重启持久性验收。"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request,urlopen

from check_embedding import ROOT
from evaluate_engineering import percentile


def call(base,path,payload=None,headers=None):
    request=Request(base+path,data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,
                    headers={'Content-Type':'application/json',**(headers or {})})
    with urlopen(request,timeout=5) as response:return response.status,json.load(response)


def start(env,port):
    process=subprocess.Popen([sys.executable,str(ROOT/'scripts/serve_recipe_app.py'),'--port',str(port)],cwd=ROOT,env=env,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    base=f'http://127.0.0.1:{port}'
    for _ in range(100):
        try:call(base,'/api/status');return process,base
        except (URLError,ConnectionError):time.sleep(.03)
    process.terminate();process.wait();raise RuntimeError('验收服务未启动')


def stop(process):
    process.terminate()
    try:process.wait(timeout=6)
    except subprocess.TimeoutExpired:process.kill();process.wait()


def wait_job(base,ident,limit=20):
    end=time.monotonic()+limit
    while time.monotonic()<end:
        _,snapshot=call(base,'/api/job?id='+ident)
        if snapshot['state'] in ('done','failed','cancelled','expired'):return snapshot
        time.sleep(.03)
    raise TimeoutError('任务验收等待超时')


def run():
    report={'scope':'本机原文工具负载；不代表远程模型高并发容量','workers':2,'capacity':8,'levels':[]}
    with TemporaryDirectory() as directory:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env=dict(os.environ,RECIPE_JOBS_DB=str(Path(directory)/'jobs.sqlite'),RECIPE_WORKERS='2',RECIPE_QUEUE_CAPACITY='8',RECIPE_USER_RPM='1000')
        process,base=start(env,port)
        payload={'question':'查看蒸水蛋操作原文','mode':'recipe','title':'蒸水蛋','section':'操作'}
        try:
            for level in (1,4,16):
                started=time.monotonic()
                def submit(i):
                    before=time.monotonic()
                    try:
                        status,data=call(base,'/api/ask',payload,{'Idempotency-Key':f'load-{level}-{i}'})
                        return {'status':status,'job_id':data['job_id'],'submitted':before}
                    except HTTPError as error:return {'status':error.code}
                    except (URLError,TimeoutError):return {'status':'network_error'}
                with ThreadPoolExecutor(max_workers=level) as pool:results=list(pool.map(submit,range(level)))
                snapshots=[];latencies=[]
                for result in results:
                    if result['status']==202:
                        snapshot=wait_job(base,result['job_id']);snapshots.append(snapshot)
                        # 以数据库完成时间衡量，避免串行读取完成记录造成延迟夸大。
                        latencies.append(snapshot['finished']-snapshot['created'])
                events=[]
                for s in snapshots:
                    if s['started']:events.extend([(s['started'],1),(s['finished'],-1)])
                running,peak=0,0
                for _,change in sorted(events):running+=change;peak=max(peak,running)
                row={'concurrency':level,'accepted':sum(r['status']==202 for r in results),'rejected_429':sum(r['status']==429 for r in results),
                     'network_errors':sum(r['status']=='network_error' for r in results),'finished':sum(s['state']=='done' for s in snapshots),'peak_running':peak,
                     'p50_seconds':percentile(latencies,.5),'p95_seconds':percentile(latencies,.95),'p99_seconds':percentile(latencies,.99),
                     'elapsed_seconds':time.monotonic()-started,'queue_p95_seconds':call(base,'/api/metrics')[1]['jobs']['queue_p95_seconds']}
                row['passed']=row['accepted']+row['rejected_429']==level and row['finished']==row['accepted'] and peak<=2
                report['levels'].append(row);print('页面并发验收：'+str(level)+' / '+str(row['passed']),flush=True)
            _,one=call(base,'/api/ask',payload,{'Idempotency-Key':'persistent-idempotency'})
            _,two=call(base,'/api/ask',payload,{'Idempotency-Key':'persistent-idempotency'})
            report['idempotency_passed']=one['job_id']==two['job_id'] and two['reused']
            completed=wait_job(base,one['job_id'])
            _,cancelled=call(base,'/api/ask',payload)
            _,cancel=call(base,'/api/cancel',{'job_id':cancelled['job_id']})
            report['cancel_passed']=cancel['cancelled'] and wait_job(base,cancelled['job_id'])['state']=='cancelled'
            stop(process);process,base=start(env,port)
            report['restart_persistence_passed']=call(base,'/api/job?id='+one['job_id'])[1]['result']==completed['result']
            stop(process)
            process,base=start(dict(env,RECIPE_REQUEST_SECONDS='.001'),port)
            _,expired=call(base,'/api/ask',payload)
            report['deadline_passed']=wait_job(base,expired['job_id'])['state']=='expired'
            stop(process)
            process,base=start(dict(env,RECIPE_WORKERS='0'),port)
            _,external=call(base,'/api/ask',payload)
            time.sleep(.15)
            queued=call(base,'/api/job?id='+external['job_id'])[1]['state']=='queued'
            worker=subprocess.Popen([sys.executable,str(ROOT/'scripts/job_queue.py')],cwd=ROOT,env=env,
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:report['external_worker_passed']=queued and wait_job(base,external['job_id'])['state']=='done'
            finally:stop(worker)
        finally:stop(process)
    report['passed']=all(r['passed'] for r in report['levels']) and all(report[k] for k in ('idempotency_passed','cancel_passed','restart_persistence_passed','deadline_passed','external_worker_passed'))
    out=ROOT/'data/processed/engineering-acceptance';out.mkdir(exist_ok=True)
    (out/'local-load-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


if __name__=='__main__':
    result=run();print('全部页面工程验收通过：'+str(result['passed']))
    sys.exit(0 if result['passed'] else 1)
