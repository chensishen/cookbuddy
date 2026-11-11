"""固定来源题集：检索/重排消融、FLAT-HNSW 对照、直接-单-多 Agent 对照。"""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import time
from urllib.request import Request

from check_embedding import ROOT,config
from ask_test_recipes import run_stage
from knowledge_versions import fingerprint,publish_alias,rollback_alias
from rag_dataset import FOLDER
from recipe_agent import execute,save_run
from service_runtime import request_json,request_budget

OUT=ROOT/'data/processed/engineering-acceptance'
CASES=ROOT/'data/evaluation/engineering-cases-v1.json'


def ranking_metrics(ids, gold, k=5):
    ids=list(dict.fromkeys(ids))[:k];gold=set(gold)
    if not gold:raise ValueError('评测必须提供非空参考证据')
    relevant=[int(x in gold) for x in ids]
    dcg=sum(value/math.log2(rank+2) for rank,value in enumerate(relevant))
    ideal=sum(1/math.log2(i+2) for i in range(min(k,len(gold))))
    return {'recall':len(set(ids)&gold)/len(gold),
            'mrr':next((1/(i+1) for i,value in enumerate(relevant) if value),0),
            'ndcg':dcg/ideal if ideal else 0,'all_evidence':gold<=set(ids)}


def percentile(values,p):
    if not values:return None
    ordered=sorted(values);return ordered[min(len(ordered)-1,max(0,math.ceil(len(ordered)*p)-1))]


def rerank(question,hits,chunks,metadata):
    settings=config();payload={'model':'Qwen/Qwen3-Reranker-8B','query':question,
        'documents':[chunks[r['chunk_id']]['text'] for r in hits],'top_n':len(hits),'return_documents':False}
    request=Request(settings['EMBEDDING_BASE_URL'].rstrip('/')+'/rerank',
        data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
    started=time.monotonic()
    response=request_json(request,timeout=45,phase='evaluation_rerank',cache_namespace=metadata['source_sha256'])
    order=sorted(response['results'],key=lambda r:r['relevance_score'],reverse=True)
    return [hits[r['index']]['chunk_id'] for r in order],time.monotonic()-started


def retrieval_evaluation():
    from pymilvus import MilvusClient
    client=MilvusClient(uri='http://127.0.0.1:19530')
    cases=json.loads(CASES.read_text())['retrieval']
    chunks={r['chunk_id']:r for r in (json.loads(x) for x in (FOLDER/'chunks.jsonl').read_text().splitlines())}
    metadata=json.loads((FOLDER/'embedding-metadata.json').read_text())
    outcomes=[];index_results=[]
    for case in cases:
        if not set(case['gold'])<=set(chunks):raise ValueError('参考证据不属于当前版本')
        with request_budget(180):
            started=time.monotonic()
            _,search=run_stage('search_test_recipes.py',case['question'],'search-results')
            hits=search['results']
            vector=sorted([r for r in hits if 'vector' in r['retrieval_channels']],key=lambda r:r['distance'],reverse=True)
            old_augmented=sorted([r for r in hits if set(r['retrieval_channels'])&{'vector','title','ingredient'}],key=lambda r:r['distance'],reverse=True)
            variants={'vector':vector,'vector_rules':old_augmented,'hybrid_rrf':hits}
            for name,candidates in variants.items():
                ids=[r['chunk_id'] for r in candidates]
                outcomes.append({'case':case['id'],'variant':name,'candidate_count':len(ids),
                    'candidate_recall':ranking_metrics(ids,case['gold'],len(ids))['recall'],
                    'top5':ranking_metrics(ids,case['gold'],5),'ids':ids[:5]})
            for name,candidates in (('vector_rerank',vector),('hybrid_rerank',hits)):
                ids,elapsed=rerank(case['question'],candidates,chunks,metadata)
                outcomes.append({'case':case['id'],'variant':name,'candidate_count':len(candidates),
                    'candidate_recall':ranking_metrics([r['chunk_id'] for r in candidates],case['gold'],len(candidates))['recall'],
                    'top5':ranking_metrics(ids,case['gold'],5),'rerank_seconds':round(elapsed,3),'ids':ids[:5]})
            flat=client.search('recipe_all_v1',data=[search['query_vector']],anns_field='vector',limit=20,
                               search_params={'metric_type':'COSINE'},consistency_level='Strong')[0]
            flat_ids=[r['chunk_id'] for r in flat]
            for ef in (32,64,128):
                latency=[];ann=[]
                for _ in range(5):
                    before=time.monotonic()
                    ann=client.search('recipe_hnsw_lab_v1',data=[search['query_vector']],anns_field='vector',limit=20,
                                      search_params={'metric_type':'COSINE','params':{'ef':ef}},consistency_level='Strong')[0]
                    latency.append(time.monotonic()-before)
                index_results.append({'case':case['id'],'ef':ef,'flat_overlap_at20':len(set(flat_ids)&{r['chunk_id'] for r in ann})/20,
                                      'p95_seconds':percentile(latency,.95),'samples':len(latency)})
        print('检索与重排对照完成：'+case['id'],flush=True)
    aggregates={}
    for variant in sorted({r['variant'] for r in outcomes}):
        rows=[r for r in outcomes if r['variant']==variant]
        aggregates[variant]={key:round(statistics.mean(float(r['top5'][key]) for r in rows),4) for key in ('recall','mrr','ndcg','all_evidence')}
    report={'dataset_sha256':fingerprint(FOLDER),'fixture':str(CASES.relative_to(ROOT)),
            'scope':'6个已定位参考块的诊断题；不代表全库准确率或泛化效果',
            'rows':outcomes,'aggregates':aggregates,'hnsw':index_results}
    OUT.mkdir(exist_ok=True);(OUT/'retrieval-ablation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def agent_evaluation():
    rows=[]
    for case in json.loads(CASES.read_text())['agents']:
        for mode in ('direct','agent','multi_agent'):
            started=time.monotonic();records=[]
            if mode=='direct':
                for tool in case['tools']:
                    direct_mode={'read_recipe':'recipe','find_recipe_images':'images','query_recipes':'rag'}[tool]
                    records.append(execute(case['question'],direct_mode,title=case.get('title',''),section=case.get('section','')))
            else:records=[execute(case['question'],mode)]
            paths=[str(save_run(r).relative_to(ROOT)) for r in records]
            outputs=[o for r in records for o in r['outputs']]
            actual=[o['tool'] for o in outputs]
            successes=all(o['result']['status'] in ('verified','verified_source') for o in outputs)
            calls=sum(sum(m.get('count',1) for m in r['model_calls']) for r in records)
            row={'case':case['id'],'mode':mode,'expected_tools':case['tools'],'actual_tools':actual,
                 'tool_choice_passed':set(actual)==set(case['tools']), 'source_status_passed':successes and bool(outputs),
                 'statuses':[r['status'] for r in records],'planner_calls':calls,
                 'elapsed_seconds':round(time.monotonic()-started,3),'records':paths}
            rows.append(row)
            OUT.mkdir(exist_ok=True);(OUT/'agent-comparison.json').write_text(json.dumps({'scope':'3个真实场景、无重复统计；非平均质量与性能结论','rows':rows},ensure_ascii=False,indent=2)+'\n')
            print('工具模式对照完成：'+case['id']+' / '+mode+' / '+str(row['source_status_passed']),flush=True)
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--part',choices=('retrieval','agents'),required=True);args=parser.parse_args()
    retrieval_evaluation() if args.part=='retrieval' else agent_evaluation()
