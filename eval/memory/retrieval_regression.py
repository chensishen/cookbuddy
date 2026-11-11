"""Compare reconstructed pre-guard search and the new empty-memory path, live."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from ask_test_recipes import run_stage
from cookbuddy.ltm.guard import policy,source_allowed
from service_runtime import request_budget
from evaluate_prd import metrics
from check_embedding import ROOT
from recipe_tools import load_chunks


def legacy_script():
    current=(ROOT/'scripts/search_test_recipes.py').read_text()
    start=current.index('    from cookbuddy.ltm.guard import current,build_milvus_filter,post_check\n')
    end=current.index('    settings = config()\n',start)
    original=current[:start]+current[end:]
    original=original.replace('guarded_milvus(', 'milvus_api(')
    original=original.replace('min(int(os.environ.get("RAG_TOP_K","20")), len(chunks))','min(int(os.environ.get("RAG_TOP_K","20")), metadata["chunk_count"])')
    original=original.replace('    matched_titles = match_titles(', '    chunks = [json.loads(line) for line in (folder / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]\n    matched_titles = match_titles(')
    original=re.sub(r'              "hard_filter_applied":bool\(guard_filter\),"allowed_source_count":len\(\{r\[\x27source\x27\] for r in chunks\}\),\n','',original)
    return original


def main():
    code_hash=lambda:hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'scripts').rglob('*.py')))).hexdigest()
    cases=[c for c in json.loads((ROOT/'data/evaluation/prd-dev-v1.json').read_text())['cases'] if c['gold']]
    report={'dataset':'data/evaluation/prd-dev-v1.json','dataset_sha256':hashlib.sha256((ROOT/'data/evaluation/prd-dev-v1.json').read_bytes()).hexdigest(),
            'code_sha256':code_hash(),'baseline_code_sha256':hashlib.sha256(legacy_script().encode()).hexdigest(),
            'scope':'8 道现有开发诊断题；原流程为移除本次 guard 插入点后的重建基线。真实 Embedding/Milvus/Rerank；不是历史部署快照或独立盲测。','cases':[],'blocked':[]}
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'legacy_search.py';path.write_text(legacy_script())
        env={**os.environ,'PYTHONPATH':str(ROOT/'scripts'),'COOKBUDDY_HARD_CONTEXT':'[]'}
        for case in cases:
            row={'id':case['id']}
            try:
                result=subprocess.run([sys.executable,str(path),case['question']],cwd=ROOT,env=env,capture_output=True,text=True,timeout=90)
                if result.returncode:raise RuntimeError('baseline_search_failed')
                preview=Path(next(line.removeprefix('完整结果：').strip() for line in result.stdout.splitlines() if line.startswith('完整结果：')))
                baseline=preview.with_suffix('.json')
                with request_budget(120),policy([]):
                    current,search=run_stage('search_test_recipes.py',case['question'],'search-results')
                    before,before_data=run_stage('rerank_test_recipes.py',str(baseline),'rerank-results')
                    after,after_data=run_stage('rerank_test_recipes.py',str(current),'rerank-results')
                row.update(before=metrics([r['chunk_id'] for r in before_data['results']],case['gold']),
                           after=metrics([r['chunk_id'] for r in after_data['results']],case['gold']),
                           baseline_search=str(baseline.relative_to(ROOT)),current_search=str(current.relative_to(ROOT)),
                           baseline_rerank=str(before.relative_to(ROOT)),current_rerank=str(after.relative_to(ROOT)))
            except Exception as error:report['blocked'].append({'case':case['id'],'error_type':type(error).__name__,'code':getattr(error,'code',None)})
            report['cases'].append(row)
            print('retrieval_regression_case',case['id'],'measured' if 'after' in row else 'blocked',flush=True)
    measured=[r for r in report['cases'] if 'after' in r]
    report['metrics']={}
    for key in ('recall','mrr'):
        before=sum(r['before'][key] for r in measured)/len(measured) if measured else None
        after=sum(r['after'][key] for r in measured)/len(measured) if measured else None
        report['metrics'][key]={'before':before,'after':after,'delta':after-before if after is not None else None}
    hard=[{'dimension':'allergy','value':'花生','polarity':'avoid','strength':'hard'}]
    try:
        with request_budget(120),policy(hard):
            path,data=run_stage('search_test_recipes.py','推荐鸡肉菜谱','search-results')
        report['live_hard_search']={'hard_filter_applied':data['hard_filter_applied'],'returned':len(data['results']),
            'violations':sum(not source_allowed(r['source'],hard) for r in data['results']),'record':str(path.relative_to(ROOT))}
    except Exception as error:report['live_hard_search']={'blocked':type(error).__name__}
    report['passed']=len(measured)==len(cases) and all(m['delta']>=-.01 for m in report['metrics'].values()) and report['live_hard_search'].get('violations')==0
    report['code_sha256_end']=code_hash();report['code_changed_during_run']=report['code_sha256_end']!=report['code_sha256']
    if report['code_changed_during_run']:report['passed']=False
    target=ROOT/'data/processed/cookbuddy-acceptance/memory-v2-retrieval-20261018.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('memory_retrieval_regression',report['passed'],str(target.relative_to(ROOT)))
    return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
