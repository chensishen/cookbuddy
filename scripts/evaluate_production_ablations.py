"""追加真实生产 E9/P-A1..7，不改写受控核心或封存报告。"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

from acceptance_closeout import exact_evidence,citation_metadata
from check_embedding import ROOT
from evaluate_prd import code_fingerprint
from llm_config import llm_settings
from production_ablation_runtime import PROFILES,profile_environment
from recipe_tools import load_chunks


def run(out,fixture):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    cases=json.loads(fixture.read_text());start=code_fingerprint();settings=llm_settings()
    protocol=ROOT/'docs/expanded-ablation-protocol-20261021.md'
    report={'schema_version':1,'protocol':'expanded-production-v1','created_at':datetime.now(timezone.utc).isoformat(),
        'fixture_path':str(fixture.relative_to(ROOT)),'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(),'code_sha256':start,
        'scope':'消费过的6题开发诊断；受控核心与生产层分别解释，保留失败、组件激活情况及固定分母。',
        'configuration':{'llm_model':settings['model'],'llm_base_url':settings['base_url'],'profiles':PROFILES,
            'shared_runtime':True,'answer_cache':False,'account_quota':'未定'},
        'core_report':'data/processed/ablation-matrix-v1/run-v2/report.json',
        'production_equivalence':True,'production_instrumentation':'隔离进程观测阶段；E9无组件干预；P方案仅显式干预对应组件',
        'answers':[],'answer_aggregates':{},'acceptance':{'cost_acceptability':None,'overall':'development_only'},'status':'running'}
    from rag_dataset import FOLDER
    report['index_audit']={}
    for name,folder in [('active',FOLDER)]+[(key,ROOT/'data/processed/recipe_versions'/('ablation_bounded_v4_'+key)) for key in ('plain','raw')]:
        meta=json.loads((folder/'embedding-metadata.json').read_text())
        plan=json.loads((folder/'version-plan.json').read_text())
        validation=json.loads((ROOT/'data/processed/index-lab'/(plan['collection']+'.json')).read_text())
        if validation.get('status')!='validated' or validation['source_sha256']!=meta['source_sha256']:
            raise ValueError('实验集合未核验')
        report['index_audit'][name]={'collection':plan['collection'],'metadata':meta,'readback':validation['checked'],
            'vectors_sha256':hashlib.sha256((folder/'chunks-with-vectors.jsonl').read_bytes()).hexdigest()}
    path=out/'report.json'
    def save():path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (out/'report.md').write_text('# 扩展生产消融\n\n6题开发诊断；失败保留，不作为新封存测试。\n')
    for source,digest in cases['source_hashes'].items():
        if hashlib.sha256((ROOT/source).read_bytes()).hexdigest()!=digest:raise ValueError('题集来源变化')
    save()
    for case in cases['cases']:
        for variant in PROFILES:
            started=time.monotonic();worker=out/(case['id']+'-'+variant+'.json')
            env=dict(os.environ,RECIPE_PRODUCTION_ABLATION=variant,RECIPE_ANSWER_CACHE='0',
                RECIPE_USAGE_LEDGER=str(out/'usage.jsonl'),RECIPE_CACHE_NAMESPACE=out.name+':'+variant,
                **profile_environment(variant))
            try:
                process=subprocess.run([sys.executable,str(ROOT/'scripts/production_ablation_worker.py'),case['question'],str(worker)],
                    env=env,cwd=ROOT,capture_output=True,text=True,timeout=205)
                (out/(case['id']+'-'+variant+'.log')).write_text(process.stdout+'\n'+process.stderr)
                if process.returncode or not worker.exists():
                    row={'failure_type':'WorkerFailed','worker_returncode':process.returncode,'seconds':time.monotonic()-started}
                else:row=json.loads(worker.read_text())
            except subprocess.TimeoutExpired:
                row={'failure_type':'WorkerTimeout','seconds':time.monotonic()-started}
            row.update(case=case['id'],type=case['type'],variant=variant,semantic_review='pending')
            if row.get('result'):
                row.update(evidence_check=exact_evidence(row['result']),citation_metadata=citation_metadata(row['result'],load_chunks()),
                    refusal_correct=row['result'].get('status')=='insufficient' if not case['gold'] else None)
            report['answers'].append(row);save()
            print(case['id']+' / '+variant+' / '+row.get('failure_type',row.get('result',{}).get('status','failed')),flush=True)
    for variant in PROFILES:
        rows=[r for r in report['answers'] if r['variant']==variant];times=sorted(r['seconds'] for r in rows)
        report['answer_aggregates'][variant]={'n':len(rows),'p50_seconds':statistics.median(times),
            'p95_seconds':times[math.ceil(.95*len(times))-1],'failures':sum('failure_type' in r for r in rows),
            'retrieval_traversed':sum(r.get('activation',{}).get('retrieval_traversed',False) for r in rows),
            'source_fact_matched':sum(r.get('activation',{}).get('source_fact_matched',False) for r in rows)}
    report.update(status='completed',code_sha256_after=code_fingerprint(),code_unchanged=start==code_fingerprint(),
        matrix_executed=len(report['answers'])==len(cases['cases'])*len(PROFILES),
        matrix_successful=all('failure_type' not in row and row.get('result',{}).get('status') in
            ('verified','verified_source','insufficient') for row in report['answers']))
    save();(out/'generation-report.json').write_bytes(path.read_bytes())
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--fixture',type=Path,default=ROOT/'data/evaluation/controlled-matrix-dev-v1.json')
    parser.add_argument('--prepare-indexes',action='store_true')
    parser.add_argument('--judge',action='store_true')
    args=parser.parse_args()
    args.out=args.out.resolve();args.fixture=args.fixture.resolve()
    if args.prepare_indexes:
        from production_ablation_indexes import build
        from evaluate_prd import policy
        with policy(RECIPE_USAGE_LEDGER=str(ROOT/'data/processed/ablation-matrix-v1/production-index-usage.jsonl')):
            build('plain');build('raw')
    generated=run(args.out,args.fixture)
    if args.judge:
        from evaluate_prd import policy
        from review_prd_eval import review
        from summarize_production_ablations import summarize
        path=args.out/'report.json'
        with policy(RECIPE_USAGE_LEDGER=str(args.out/'judge-usage.jsonl')):review(path,judge=True)
        summary=summarize(path)
        if not summary['expanded_all_evaluable_scored']:raise SystemExit(1)
    if not generated['matrix_executed'] or not generated['matrix_successful']:raise SystemExit(1)
