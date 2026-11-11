"""项目运行入口：检查、启动与不调用远程模型的本地演示。"""
import argparse
from datetime import datetime,timezone
import json
import os
import socket
import subprocess
import sys
import uuid

from project_doctor import ROOT, check_project, display, local_json, project_id


def validate_port(value):
    port=int(value)
    if not 1024<=port<=65535:raise argparse.ArgumentTypeError('端口须在1024到65535之间')
    return port


def start(port, local_only=True):
    report=check_project(live=not local_only,port=port);display(report)
    if not report['local_ready'] or not local_only and not report['full_prerequisites']:return 1
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.3):pass
    except OSError:
        env=dict(os.environ,RECIPE_LOCAL_ONLY='1' if local_only else '0')
        if local_only:env['RECIPE_JOBS_DB']=str(ROOT/'data/runtime/local-jobs.sqlite3')
        print('启动本地模式（禁止模型请求）' if local_only else '启动完整模式（模型请求需现有API配置）',flush=True)
        try:return subprocess.call([sys.executable,str(ROOT/'scripts/serve_recipe_app.py'),'--port',str(port)],cwd=ROOT,env=env)
        except KeyboardInterrupt:return 0
    try:
        identity=local_json(f'http://127.0.0.1:{port}/api/identity')
        if identity.get('project_id')!=project_id(ROOT):raise ValueError()
        status=local_json(f'http://127.0.0.1:{port}/api/status',token=os.environ.get('RECIPE_ACCESS_TOKEN',''))
        if status.get('project_id')==project_id(ROOT) and bool(status.get('local_only'))==local_only:
            print(f'本项目已按所选模式运行：http://127.0.0.1:{port}');return 0
    except Exception:pass
    print('端口已占用、口令不匹配或运行模式不同；不会接管已有服务。请选择其他端口。')
    return 1


def main():
    parser=argparse.ArgumentParser(description='菜谱项目运行与交付入口')
    commands=parser.add_subparsers(dest='command',required=True)
    doctor=commands.add_parser('check');doctor.add_argument('--live',action='store_true');doctor.add_argument('--json',action='store_true');doctor.add_argument('--port',type=validate_port,default=8080)
    launch=commands.add_parser('start');launch.add_argument('--full',action='store_true');launch.add_argument('--port',type=validate_port,default=8080)
    demo=commands.add_parser('demo');demo.add_argument('--port',type=validate_port,default=8080)
    args=parser.parse_args()
    if args.command=='check':
        report=check_project(live=args.live,port=args.port)
        print(json.dumps(report,ensure_ascii=False,indent=2)) if args.json else display(report)
        return 0 if report['local_ready'] else 1
    if args.command=='start':return start(args.port,local_only=not args.full)
    try:
        identity=local_json(f'http://127.0.0.1:{args.port}/api/identity')
        if identity.get('project_id')!=project_id(ROOT):raise ValueError()
        status=local_json(f'http://127.0.0.1:{args.port}/api/status',token=os.environ.get('RECIPE_ACCESS_TOKEN',''))
        if status.get('project_id')!=project_id(ROOT):raise ValueError()
    except Exception:
        print('本项目应用未就绪；请先运行start，端口不同则为demo指定同一端口。');return 1
    import smoke_recipe_catalog as demo_module
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    demo_module.OUT=ROOT/'data/processed/run-delivery'/stamp
    demo_module.BASE=f'http://127.0.0.1:{args.port}'
    try:demo_module.main()
    except Exception:
        print('本地演示未完成，请用check --live核对服务状态；已完成任务的记录仍保留。');return 1
    print('演示记录：'+str(demo_module.OUT/'local-flows.json'));return 0


if __name__=='__main__':raise SystemExit(main())
