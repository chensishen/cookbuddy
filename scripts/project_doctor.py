"""仅用标准库完成运行前检查；不输出配置值或调用远程模型。"""
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_KEYS = ('EMBEDDING_API_KEY','GITEE_AI_API_KEY','EMBEDDING_BASE_URL',
               'EMBEDDING_MODEL','EMBEDDING_DIMENSIONS','LLM_API_KEY','LLM_BASE_URL','LLM_MODEL')


def project_id(root):
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def config_values(root, environ):
    values = {}
    path = root/'.env'
    if path.exists():
        for line in path.read_text().splitlines():
            key, separator, value = line.strip().partition('=')
            if separator and key in CONFIG_KEYS:
                values[key] = value.strip().strip("\"'")
    values.update({key:environ[key] for key in CONFIG_KEYS if key in environ})
    return values


def local_json(url, payload=None, token=''):
    headers={'Content-Type':'application/json'}
    if token:headers['X-Recipe-Token']=token
    request=Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with urlopen(request,timeout=2) as response:
        return json.loads(response.read(1024*1024))


def check_project(root=ROOT, live=False, port=8080, environ=None, probe=local_json, version_lookup=metadata.version):
    root=Path(root).resolve();environ=os.environ if environ is None else environ
    checks=[]
    def add(name,status,message,action=''):
        checks.append({'name':name,'status':status,'message':message,'action':action})
    interpreter_ok=sys.version_info[:3]==(3,12,11) and (
        Path(sys.prefix).name=='py3.12.11' or environ.get('CONDA_DEFAULT_ENV')=='py3.12.11')
    add('python','ok' if interpreter_ok else 'error',
        '使用指定的py3.12.11环境' if interpreter_ok else '当前解释器不是指定的conda py3.12.11环境',
        '' if interpreter_ok else 'conda activate py3.12.11，再执行本入口')
    required=['scripts/serve_recipe_app.py','app/index.html','data/processed/media_pilot/asset-index.json']
    files_ok=all((root/path).is_file() for path in required)
    if files_ok:
        try:
            index=json.loads((root/required[2]).read_text())
            images=[item['path'] for item in index['assets'] if item['media_type']=='image']
            media_root=(root/'data/HowToCook').resolve()
            files_ok=all((root/path).resolve().is_relative_to(media_root) and (root/path).is_file() for path in images)
        except (OSError,ValueError,KeyError,TypeError):files_ok=False
    add('files','ok' if files_ok else 'error','应用及图片索引文件齐全' if files_ok else '应用或图片索引文件缺失',
        '' if files_ok else '恢复项目文件及数据产物；检查不会自动下载或入库')
    dataset_ok=False;collection=None;embedding={};source_count=0;chunk_count=0
    try:
        active=json.loads((root/'data/processed/active-dataset.json').read_text())
        dataset=environ.get('RAG_DATASET',active['dataset']);version=environ.get('RAG_VERSION',active.get('version',''))
        if dataset not in ('all_recipes','test_dishes') or not re.fullmatch(r'[A-Za-z0-9_]*',version):raise ValueError()
        folder=root/'data/processed'/('recipe_versions/'+version if version else dataset)
        embedding=json.loads((folder/'embedding-metadata.json').read_text())
        raw=(folder/'chunks.jsonl').read_bytes();rows=[json.loads(line) for line in raw.splitlines()]
        if any(not isinstance(row,dict) or any(not isinstance(row.get(field),str) for field in ('chunk_id','title','source','content'))
               or any(type(row.get(field)) is not int for field in ('source_start_line','source_end_line')) for row in rows):raise ValueError()
        if len(rows)!=embedding['chunk_count'] or hashlib.sha256(raw).hexdigest()!=embedding['source_sha256']:raise ValueError()
        if len({row['chunk_id'] for row in rows})!=len(rows):raise ValueError()
        collection={'all_recipes':'recipe_all_v1','test_dishes':'recipe_test_v1'}[dataset]
        if version:
            plan=json.loads((folder/'version-plan.json').read_text());collection=plan['collection']
            if not re.fullmatch(r'recipe_[A-Za-z0-9_]+',collection):raise ValueError()
            verified=json.loads((root/'data/processed/index-lab'/f'{collection}.json').read_text())
            if verified['status']!='validated' or verified['source_sha256']!=embedding['source_sha256']:raise ValueError()
        allowed=(root/'data/HowToCook'/('dishes' if dataset=='all_recipes' else 'test_dishes')).resolve()
        sources={row['source'] for row in rows}
        if not rows or not all((root/path).resolve().is_relative_to(allowed) and (root/path).is_file() for path in sources):raise ValueError()
        originals={source:(root/source).read_text().splitlines() for source in sources}
        for row in rows:
            lines=originals[row['source']];content=row['content'].splitlines();start=row['source_start_line'];end=row['source_end_line']
            if not content or not 1<=start<=end<=len(lines):raise ValueError()
            if sum(lines[offset:offset+len(content)]==content for offset in range(start-1,end))!=1:raise ValueError()
        source_count=len(sources);chunk_count=len(rows);dataset_ok=True
    except (OSError,ValueError,KeyError,TypeError):
        add('dataset','error','知识库产物缺失、未验证或与当前文件不一致','恢复对应版本的数据产物；按版本准备与入库流程核对，不直接覆盖集合')
    if dataset_ok:add('dataset','ok',f'活动知识库：{source_count}份来源、{chunk_count}块')
    deps_ok=True
    try:
        pins=[line.strip().split('==') for line in (root/'requirements-engineering.txt').read_text().splitlines() if line.strip()]
        for name,expected in pins:
            try:installed=version_lookup(name.split('[',1)[0])
            except metadata.PackageNotFoundError:installed=None
            if installed!=expected:deps_ok=False
        add('dependencies','ok' if deps_ok else 'warning','工程依赖版本匹配' if deps_ok else '部分工程依赖缺失或版本不匹配',
            '' if deps_ok else '在py3.12.11中执行 python -m pip install -r requirements-engineering.txt')
    except (OSError,ValueError):
        deps_ok=False;add('dependencies','warning','依赖清单不可读取','恢复requirements-engineering.txt')
    tokenizer_ok=True
    if embedding.get('splitter_version')=='bounded-v2':
        try:tokenizer_ok=bool(version_lookup('tiktoken'))
        except metadata.PackageNotFoundError:tokenizer_ok=False
        add('local_tokenizer','ok' if tokenizer_ok else 'error',
            '本地长章节核对所需tiktoken已安装' if tokenizer_ok else '本地长章节核对缺少tiktoken',
            '' if tokenizer_ok else '安装requirements-engineering.txt中的tiktoken依赖')
    config_ok=False
    try:
        values=config_values(root,environ);key=values.get('EMBEDDING_API_KEY','')
        base=values.get('EMBEDDING_BASE_URL','').rstrip('/');url=urlparse(base)
        config_ok=bool(key and set(key)!={'X'} and url.scheme=='https' and not url.username and not url.password and not url.query
            and base==embedding.get('api_base_url') and values.get('EMBEDDING_MODEL')==embedding.get('requested_model')
            and int(values.get('EMBEDDING_DIMENSIONS','0'))==embedding.get('dimensions'))
    except (OSError,ValueError):pass
    add('model_config','configured' if config_ok else 'warning',
        '模型配置与向量空间一致；密钥权限、余额和响应未验证' if config_ok else '模型配置缺失或与活动向量空间不一致',
        '' if config_ok else '复制.env.example为.env并在本地填写；已存在的.env不要覆盖')
    from llm_config import require_llm
    try:
        require_llm(config_values(root,environ));chat_ok=True
    except ValueError:chat_ok=False
    add('llm_config','configured' if chat_ok else 'warning',
        '独立聊天模型已配置；接口权限与余额尚未验证' if chat_ok else '聊天模型配置缺失或无效',
        '' if chat_ok else '在本地.env填写LLM_API_KEY、LLM_BASE_URL与LLM_MODEL')
    milvus_ok=False;app_state='not_checked';app_local_only=None
    if live:
        if dataset_ok:
            try:
                base='http://127.0.0.1:19530/v2/vectordb/'
                describe=probe(base+'collections/describe',{'collectionName':collection})
                stats=probe(base+'collections/get_stats',{'collectionName':collection})
                data=describe['data'];vector=next(f for f in data['fields'] if f['name']=='vector')
                dim=int(dict((p['key'],p['value']) for p in vector['params'])['dim'])
                milvus_ok=describe['code']==stats['code']==0 and data['load']=='LoadStateLoaded' and dim==embedding['dimensions'] and int(stats['data']['rowCount'])==chunk_count
            except Exception:pass
        add('milvus','ok' if milvus_ok else 'warning','活动集合已加载、维度与数量一致' if milvus_ok else 'Milvus不可用或活动集合状态不匹配',
            '' if milvus_ok else '在Docker Desktop启动本项目Milvus，再核对活动集合；不要删除其他集合')
        try:
            identity=probe(f'http://127.0.0.1:{port}/api/identity')
            if identity.get('project_id')!=project_id(root):raise ValueError()
            status=probe(f'http://127.0.0.1:{port}/api/status',token=environ.get('RECIPE_ACCESS_TOKEN',''))
            app_state='ready' if status.get('project_id')==project_id(root) else 'unrecognized'
            if app_state=='ready' and type(status.get('local_only')) is bool:app_local_only=status['local_only']
        except Exception:app_state='unavailable'
        add('app','ok' if app_state=='ready' else 'warning',
            ('本项目应用已运行，本地模式禁止模型请求' if app_local_only else '本项目应用已运行') if app_state=='ready' else '应用未启动、口令不匹配或端口属于其他服务',
            '' if app_state=='ready' else '运行本地启动入口；端口占用时使用其他端口，检查不会结束已有进程')
        docker=shutil.which('docker')
        if docker:
            states=[]
            for container in ('cookbuddy-recipe-milvus-1','cookbuddy-memory-milvus-1','cookbuddy-postgres-1','cookbuddy-recipe-etcd-1','cookbuddy-memory-etcd-1'):
                try:
                    result=subprocess.run([docker,'inspect','--format','{{.State.Status}}',container],capture_output=True,text=True,timeout=2)
                    states.append(result.returncode==0 and result.stdout.strip()=='running')
                except (OSError,subprocess.TimeoutExpired):states.append(False)
            add('docker','ok' if all(states) else 'warning','项目五个容器正在运行' if all(states) else '菜谱/记忆Milvus、etcd或PostgreSQL未运行／Docker不可访问',
                '' if all(states) else '用Docker Desktop查看deployment/cookbuddy配置的五个容器')
        else:add('docker','warning','未找到Docker命令','本地工具仍可使用；完整问答另需启动Milvus')
    local_ready=interpreter_ok and files_ok and dataset_ok and tokenizer_ok
    return {'project_id':project_id(root),'live_checks':live,'checks':checks,
            'local_ready':local_ready,'full_prerequisites':local_ready and deps_ok and config_ok and chat_ok and live and milvus_ok,
            'remote_model_access':'not_checked','app_state':app_state,'app_local_only':app_local_only,
            'knowledge':{'collection':collection,'sources':source_count,'chunks':chunk_count}}


def display(report):
    labels={'ok':'通过','configured':'已配置，未验证接口','warning':'待检查','error':'阻塞'}
    for check in report['checks']:
        print(f"[{labels[check['status']]}] {check['message']}")
        if check['action']:print('  下一步：'+check['action'])
    print('本地工具：'+('可启动' if report['local_ready'] else '请先解决阻塞项'))
    if not report.get('live_checks'):
        print('完整模式：尚未探测服务；请运行check --live，模型权限与余额不会自动验证')
    else:
        print('完整模式启动条件：'+('齐备，模型接口仍未验证' if report['full_prerequisites'] else '仍需核对依赖、配置与Milvus'))
