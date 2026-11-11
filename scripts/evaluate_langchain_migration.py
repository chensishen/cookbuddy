"""Paired development retrieval check; never consumes/replaces a sealed holdout."""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))


async def run(out,fixture):
    from src.agent.config import load_config
    from src.agent.context import AppContext
    from src.retrieval.pipeline import RecipeServices
    from ask_test_recipes import run_stage
    from evaluate_prd import metrics
    from service_runtime import request_budget
    from langchain_service_worker import configure
    cfg=load_config();configure(cfg.model_dump());services=RecipeServices(cfg)
    data=json.loads(fixture.read_text())
    if data.get('split')!='dev':raise ValueError('development_fixture_required')
    out.mkdir(parents=True,exist_ok=False)
    report={'scope':'paired development retrieval; sequential warm-cache comparison, not cold latency or a new holdout',
        'fixture':str(fixture.relative_to(ROOT)),'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'created_at':datetime.now(timezone.utc).isoformat(),'config':cfg.model_dump(),'records':[]}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for case in data['cases']:
        if not case.get('gold'):continue
        record={'id':case['id']}
        try:
            with request_budget(30):
                path,baseline=run_stage('search_test_recipes.py',case['question'],'search-results')
                _,ranked=run_stage('rerank_test_recipes.py',str(path),'rerank-results')
            record['baseline_ids']=[r['chunk_id'] for r in ranked['results']]
            record['baseline']=metrics(record['baseline_ids'],case['gold'])
            context=AppContext('migration-retrieval','development',deadline=time.time()+30)
            actual=await services.call('search',{'query':case['question'],'top_k':5},context,timeout=8)
            record['adapted_ids']=[r['chunk_id'] for r in actual['rows']]
            record['adapted']=metrics(record['adapted_ids'],case['gold'])
            record['degraded']=actual['degraded']
            record['identical_order']=record['baseline_ids']==record['adapted_ids']
        except Exception as error:record['error']=type(error).__name__
        report['records'].append(record);save()
        print(case['id']+': '+('error' if record.get('error') else 'compared'),flush=True)
    records=report['records'];complete=all('adapted' in r for r in records) and bool(records)
    report['complete']=complete
    report['aggregates']={variant:{metric:sum(r.get(variant,{}).get(metric,0) for r in records)/len(records)
        for metric in ('recall','mrr','hit')} for variant in ('baseline','adapted')}
    report['drops']={key:report['aggregates']['baseline'][key]-report['aggregates']['adapted'][key] for key in ('recall','mrr')}
    report['within_one_percentage_point']=complete and all(v<=.01 for v in report['drops'].values())
    save()
    return 0 if report['within_one_percentage_point'] else 2


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--fixture',type=Path,default=ROOT/'data/evaluation/prd-dev-v2.json')
    a=p.parse_args();sys.exit(asyncio.run(run(a.out.resolve(),a.fixture.resolve())))
