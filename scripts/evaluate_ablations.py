"""对已保存候选执行真实稠密重排消融；不把共享索引冒充无前缀索引。"""
import argparse
import json
from pathlib import Path
import time
from check_embedding import ROOT
from evaluate_prd import metrics,summarize_retrieval,policy,code_fingerprint
from ask_test_recipes import run_stage
from service_runtime import request_budget


def run(path):
    report=json.loads(path.read_text());fixture=json.loads((ROOT/report['fixture_path']).read_text())
    cases={r['id']:r for r in fixture['cases']};out=path.parent/'ablations';out.mkdir(exist_ok=True)
    rows=[]
    for row in report['retrieval']:
        if row['variant']!='hybrid_rerank' or not row.get('metrics'):continue
        case=cases[row['case']];item={'case':case['id'],'type':case['type'],'variant':'dense_rerank'}
        try:
            search=json.loads((ROOT/row['search_record']).read_text())
            search['results']=sorted([r for r in search['results'] if 'vector' in r['retrieval_channels']],key=lambda r:-r['distance'])
            search.update(retrieval_mode='vector',fusion=None,matched_titles=[],ingredient_sources=[])
            source=out/(case['id']+'-dense.json');source.write_text(json.dumps(search,ensure_ascii=False,indent=2)+'\n')
            with request_budget(30),policy(RAG_RERANK='1',RAG_TOP_N='5',
                RAG_RERANK_CANDIDATES=str(report['configuration']['rerank_candidate_limit']),
                RECIPE_RUNTIME_DB=str(out/'runtime.sqlite3'),RECIPE_CACHE_NAMESPACE=out.parent.name+':A-2:'+case['id'],
                RECIPE_USAGE_LEDGER=str(out/'usage.jsonl')):
                started=time.monotonic();record,rerank=run_stage('rerank_test_recipes.py',str(source),'rerank-results')
            item.update(metrics=metrics([r['chunk_id'] for r in rerank['results']],case['gold']),
                seconds=time.monotonic()-started,rerank_applied=rerank['rerank_enabled'],rerank_fallback=rerank.get('rerank_fallback'),
                rerank_record=str(record.relative_to(ROOT)))
        except Exception as error:item.update(metrics=metrics([],case['gold']),failure_type=type(error).__name__)
        rows.append(item);print('A-2：'+case['id'],flush=True)
    matrix={
        'E0':'answers.closed_book','E1':'retrieval.naive_512 + answers.naive_rag',
        'E2':None,'E3':None,'E4':None,'E5':None,'E6':None,
        'E7':'retrieval.hybrid_rerank','E8':'answers.full_agent',
        'A-1':'retrieval.hybrid_no_rerank','A-2':'ablation.rows.dense_rerank',
        'A-3':None,'A-4':None,'A-5':None,'A-6':'answers.fixed_workflow'}
    result={'schema_version':1,'code_sha256':code_fingerprint(),'complete':False,
        'scope':'A-2使用原始向量通道Top20并真实重排；A-1为检索级移除重排；A-6为端到端固定工作流。未支持项保持空值。',
        'matrix':matrix,'missing':[key for key,value in matrix.items() if value is None],
        'missing_reason':'E2/E3/A-3须重建隔离H2无前缀索引；E4/E5/E6/A-4/A-5须独立开关并补充有效约束/别名样本，不能从同一融合分数删行推断。',
        'rows':rows,'aggregates':summarize_retrieval(rows)}
    (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    report['ablation']=result;path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('report',type=Path);args=parser.parse_args()
    result=run(args.report)
    raise SystemExit(1 if any(r.get('failure_type') or not r.get('rerank_applied') for r in result['rows']) else 0)
