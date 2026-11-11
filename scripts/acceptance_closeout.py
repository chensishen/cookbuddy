"""阶段验收：全库来源工具、独立依赖环境、小规模在线问答并发。

各项结果独立记录，不以来源检查代替生成正确率或生产容量。
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.request import urlopen
import uuid

from check_embedding import ROOT


def exact_evidence(result, root=ROOT):
    evidence = result.get('evidence', [])
    checks = []
    for item in evidence:
        try:
            path = (root / item['source']).resolve()
            if not path.is_relative_to((root / 'data/HowToCook/dishes').resolve()):
                checks.append(False)
                continue
            lines = path.read_text().splitlines()
            start, end = item['start_line'], item['end_line']
            if 'start_char' in item or 'end_char' in item:
                raw=path.read_text();left,right=item.get('start_char'),item.get('end_char')
                checks.append(type(start) is int and type(end) is int and type(left) is int and type(right) is int and 0<=left<right<=len(raw)
                    and raw[left:right]==item['quote'] and start==raw.count('\n',0,left)+1 and end==raw.count('\n',0,right-1)+1)
            else:
                checks.append(type(start) is int and type(end) is int and 1 <= start <= end <= len(lines)
                              and '\n'.join(lines[start-1:end]) == item['quote'])
        except (OSError, KeyError, TypeError):
            checks.append(False)
    return {'count': len(checks), 'all_exact': bool(checks) and all(checks)}


def citation_metadata(result,chunks,root=ROOT):
    checks=[]
    for item in result.get('evidence',[]):
        exact=exact_evidence({'evidence':[item]},root)['all_exact']
        matching=[r for r in chunks if r['source']==item.get('source') and r['title']==item.get('title')]
        valid=exact and any(r['section']==item.get('section') and
            r['source_start_line']<=item['start_line']<=item['end_line']<=r['source_end_line'] for r in matching)
        if exact and matching and item.get('section')=='难度':
            valid='预估烹饪难度' in item['quote']
        checks.append(bool(valid))
    return {'count':len(checks),'correct':sum(checks),'rate':sum(checks)/len(checks) if checks else None,
        'scope':'仅核对菜名、章节、路径、范围及逐字摘录；语义支持另由独立评审检查'}


def corpus_audit():
    from recipe_catalog import build_catalog
    from recipe_tools import load_chunks, read_recipe, find_recipe_images, resolve_title
    from lexical_retrieval import BM25
    from retrieval_candidates import match_titles
    from rag_dataset import COLLECTION, VERSION
    chunks = load_chunks()
    catalog = build_catalog(chunks)
    bm25 = BM25(chunks)
    titles = {row['title'] for row in chunks}
    records = []
    for item in catalog:
        row = {'source': item['source'], 'title': item['title'],
               'source_sha256': hashlib.sha256((ROOT/item['source']).read_bytes()).hexdigest()}
        try:
            original = read_recipe(item['source'])
            photos = find_recipe_images(item['source'])
            exact = exact_evidence(original)
            row.update(original_status=original['status'], evidence=exact,
                       original_complete=original.get('complete', False),
                       images=len(photos.get('images', [])), image_status=photos['status'],
                       image_sources_match=all(p['source']==item['source'] for p in photos.get('images', [])),
                       explicit_source_resolved=resolve_title(item['source'], chunks)==(item['title'], item['source']),
                       title_matched=item['title'] in match_titles(item['title'], titles),
                       bm25_source_hit_at_20=item['source'] in {r['source'] for r in bm25.search(item['title']+'的操作步骤', 20)})
            # 无图是合法结果；只对存在的图检查归属。长原文截断另计，不能称全文完整。
            row['passed'] = original['status']=='verified_source' and exact['all_exact'] and row['explicit_source_resolved'] and row['image_sources_match']
        except Exception as error:
            row.update(passed=False, failure_type=type(error).__name__)
        records.append(row)
    return {'scope':'全库来源工具与具名BM25覆盖；不是向量语义召回或生成正确率',
            'collection':COLLECTION,'version':VERSION,'sources':len(catalog),'chunks':len(chunks),
            'passed_sources':sum(r['passed'] for r in records),
            'complete_originals':sum(r.get('original_complete', False) for r in records),
            'bm25_source_hits_at_20':sum(r.get('bm25_source_hit_at_20', False) for r in records),
            'title_matches':sum(r.get('title_matched', False) for r in records),
            'image_references':sum(r.get('images', 0) for r in records),
            'remote_model_calls':0,'passed':all(r['passed'] for r in records),'records':records}


def deployment_probe():
    """同机独立venv与项目副本，不复制密钥、运行状态或Git历史。"""
    from rag_dataset import FOLDER, COLLECTION
    report = {'scope':'同机Python 3.12.11独立venv、安装依赖与项目副本启动；非全新机器部署',
              'secrets_copied':False, 'runtime_copied':False, 'remote_model_calls':0}
    with TemporaryDirectory(prefix='recipe-deploy-') as directory:
        base = Path(directory)
        staging = base/'project'
        staging.mkdir()
        for name in ('scripts', 'app'):
            shutil.copytree(ROOT/name, staging/name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT/'requirements-engineering.txt', staging/'requirements-engineering.txt')
        shutil.copytree(ROOT/'data/HowToCook', staging/'data/HowToCook', ignore=shutil.ignore_patterns('.git'))
        for relative in ('data/processed/media_pilot', 'data/processed/media_ocr_pilot',
                         str(FOLDER.relative_to(ROOT))):
            shutil.copytree(ROOT/relative, staging/relative)
        for relative in ('data/processed/active-dataset.json', f'data/processed/index-lab/{COLLECTION}.json'):
            (staging/relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT/relative, staging/relative)
        env = {k:v for k,v in os.environ.items() if not k.startswith(('RECIPE_', 'RAG_', 'EMBEDDING_', 'GITEE_'))}
        env['CONDA_DEFAULT_ENV'] = 'py3.12.11'
        # 此标签仅供项目检查器确认指定基础Python；报告明确实际运行的是独立venv。
        subprocess.run([sys.executable,'-m','venv',str(base/'venv')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        python = base/'venv/bin/python'
        installed = subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(staging/'requirements-engineering.txt')],
                                   env=env,capture_output=True,text=True,timeout=240)
        report['dependency_install_exit'] = installed.returncode
        if installed.returncode:
            # 不落盘pip原始输出，其中可能含私有索引地址／认证值。
            output=installed.stdout+'\n'+installed.stderr
            missing=re.findall(r'No matching distribution found for ([A-Za-z0-9_.=<>-]+)',output)
            return {**report,'passed':False,'failure':'dependency_install',
                    'unavailable_requirements':missing,
                    'tls_error':'SSLError' in output,
                    'network_error':any(word in output for word in ('ConnectionError','NewConnectionError','ReadTimeout'))}
        pipcheck = subprocess.run([str(python),'-m','pip','check'],env=env,capture_output=True,text=True,timeout=30)
        report['dependency_consistency'] = pipcheck.returncode==0
        diagnosis = subprocess.run([str(python),'scripts/project.py','check','--json'],cwd=staging,env=env,capture_output=True,text=True,timeout=30)
        check = json.loads(diagnosis.stdout)
        report['diagnosis'] = check
        report['isolated_python'] = subprocess.check_output([str(python),'-c','import sys; print(sys.version.split()[0])'],text=True).strip()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        env.update(RECIPE_LOCAL_ONLY='1',RECIPE_JOBS_DB=str(staging/'data/runtime/local-jobs.sqlite3'))
        process = subprocess.Popen([str(python),'scripts/serve_recipe_app.py','--port',str(port)],cwd=staging,env=env,
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            ready = False
            for _ in range(100):
                try:
                    with urlopen(f'http://127.0.0.1:{port}/api/identity',timeout=1) as response:
                        identity=json.load(response)
                    ready=identity['project_id']==check['project_id'] and identity['local_only']
                    break
                except OSError:
                    if process.poll() is not None:break
                    time.sleep(.05)
            report['local_startup'] = ready
            if ready:
                demo = subprocess.run([str(python),'scripts/project.py','demo','--port',str(port)],cwd=staging,env=env,
                                      stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
                report['demo_exit'] = demo.returncode
                reports=list((staging/'data/processed/run-delivery').glob('*/local-flows.json'))
                report['demo_receipts'] = [json.loads(path.read_text()) for path in reports]
            report['passed'] = check['local_ready'] and report['dependency_consistency'] and ready and report.get('demo_exit')==0
        finally:
            import signal
            os.killpg(process.pid,signal.SIGTERM) if process.poll() is None else None
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
    return report


def online_probe():
    from recipe_agent import execute
    from service_runtime import request_budget
    questions = ['糖醋汁里的清水、生抽、白糖、白醋和料酒分别用多少？不要把克和毫升混在一起。',
                 '美式炒蛋的番茄丁和培根丁是必备的吗？我不想加这两样。']
    rows=[]
    def one(index, question, level):
        started=time.monotonic()
        try:
            with request_budget(180):
                record=execute(question,mode='rag')
            evidence=[exact_evidence(output['result']) for output in record.get('outputs',[])]
            statuses=[o['result']['status'] for o in record.get('outputs',[])]
            return {'id':index,'concurrency':level,'question':question,'seconds':round(time.monotonic()-started,3),
                    'record':record,'tool_statuses':statuses,'evidence_checks':evidence,'semantic_review':'pending',
                    'completed_with_exact_evidence':record['status']=='completed' and bool(evidence) and all(e['all_exact'] for e in evidence)}
        except Exception as error:
            return {'id':index,'concurrency':level,'question':question,'seconds':round(time.monotonic()-started,3),
                    'failure_type':type(error).__name__,'completed_with_exact_evidence':False}
    # 先串行一次，再两题并发。三请求小样本，只作可用性与共享治理观察。
    rows.append(one('serial-amount',questions[0],1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows.extend(pool.map(lambda pair:one('parallel-'+str(pair[0]),pair[1],2),enumerate(questions)))
    return {'scope':'3次真实问答：1次串行、2次同时执行；非生产P95、吞吐或多用户SLA',
            'request_budget_seconds':180,'records':rows,
            'completed_with_exact_evidence':sum(r['completed_with_exact_evidence'] for r in rows),
            'semantic_review':'pending'}


def retrieval_probe():
    from recipe_catalog import build_catalog
    from recipe_tools import load_chunks
    from ask_test_recipes import run_stage
    from service_runtime import request_budget
    selected={}
    for item in build_catalog(load_chunks()):
        if item['same_title_sources']==1:
            selected.setdefault(item['category'], item)
    records=[]
    for category,item in sorted(selected.items()):
        question=item['title']+'的操作步骤是什么？'
        started=time.monotonic()
        row={'category':category,'question':question,'expected_source':item['source']}
        try:
            with request_budget(120):
                search_path,search=run_stage('search_test_recipes.py',question,'search-results')
                rerank_path,rerank=run_stage('rerank_test_recipes.py',str(search_path),'rerank-results')
            row.update(candidate_source_hit=any(r['source']==item['source'] for r in search['results']),
                       rerank_source_hit_at_5=any(r['source']==item['source'] for r in rerank['results'][:5]),
                       search_result=str(search_path.relative_to(ROOT)),
                       rerank_result=str(rerank_path.relative_to(ROOT)))
            row['passed']=row['candidate_source_hit'] and row['rerank_source_hit_at_5']
        except Exception as error:
            row.update(passed=False,failure_type=type(error).__name__)
        row['seconds']=round(time.monotonic()-started,3)
        records.append(row)
        print('分类检索验收：'+category+' / '+str(row['passed']),flush=True)
    return {'scope':'每分类一道具名菜谱、真实Embedding与Rerank；参考答案为来源路径，不评价生成或无菜名推荐',
            'categories':len(records),'passed_categories':sum(r['passed'] for r in records),
            'passed':bool(records) and all(r['passed'] for r in records),'records':records}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--part',choices=('corpus','deployment','online','retrieval'),required=True)
    args=parser.parse_args()
    folder=ROOT/'data/processed/stage-acceptance'/(
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    started=time.monotonic()
    try:
        result={'corpus':corpus_audit,'deployment':deployment_probe,'online':online_probe,'retrieval':retrieval_probe}[args.part]()
    except Exception as error:
        result={'passed':False,'failure_type':type(error).__name__}
    result['elapsed_seconds']=round(time.monotonic()-started,3)
    path=folder/(args.part+'.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('验收记录：'+str(path),flush=True)
    return 1 if result.get('passed') is False else 0


if __name__=='__main__':sys.exit(main())
