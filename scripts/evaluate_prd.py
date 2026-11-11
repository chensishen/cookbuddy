"""PRD 开发集评测：实时基线/消融，逐字溯源；语义准确率须独立复核。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import itertools
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from urllib.request import Request
from check_embedding import ROOT, config
from llm_config import require_llm, chat_parameters, llm_settings
from recipe_tools import load_chunks, query_recipes
from lexical_retrieval import BM25
from rag_dataset import FOLDER, COLLECTION
from acceptance_closeout import corpus_audit, exact_evidence, citation_metadata
from ask_test_recipes import run_stage
from recipe_agent import execute
from service_runtime import request_json, request_budget, RuntimeStore
from naive_baseline import load_index, dense_search, gold_windows


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_fingerprint(root=None):
    root=ROOT if root is None else root
    digest=hashlib.sha256()
    for path in sorted((root/'scripts').rglob('*.py')):
        digest.update(path.relative_to(root).as_posix().encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()


def metrics(ids, gold, k=5):
    if not gold:return None
    ids=list(dict.fromkeys(ids))[:k]; gold=set(gold)
    rel=[int(x in gold) for x in ids]
    dcg=sum(x/math.log2(i+2) for i,x in enumerate(rel))
    ideal=sum(1/math.log2(i+2) for i in range(min(k,len(gold))))
    return {'hit':float(any(rel)), 'recall':len(set(ids)&gold)/len(gold),
            'precision':sum(rel)/k, 'mrr':next((1/(i+1) for i,x in enumerate(rel) if x),0),
            'ndcg':dcg/ideal}


def interval(values, seed=20261014):
    if not values:return None
    rng=random.Random(seed)
    samples=sorted(statistics.mean(rng.choices(values,k=len(values))) for _ in range(2000))
    return {'mean':statistics.mean(values),'ci95':[samples[49],samples[1949]],'n':len(values)}


def paired_test(before, after):
    if len(before)!=len(after):raise ValueError('配对样本数量不一致')
    differences=[b-a for a,b in zip(before,after)]
    differences=[x for x in differences if abs(x)>1e-12]
    if not differences:return {'p_value':1.,'mean_delta':0.,'significant':False}
    observed=abs(sum(differences))
    exact=len(differences)<=16
    rng=random.Random(20261019)
    signs=itertools.product((-1,1),repeat=len(differences)) if exact else (
        rng.choices((-1,1),k=len(differences)) for _ in range(10000))
    count=sum(abs(sum(s*x for s,x in zip(sign,differences)))>=observed-1e-12 for sign in signs)
    p=count/(2**len(differences)) if exact else (count+1)/10001
    return {'p_value':p,'mean_delta':statistics.mean([b-a for a,b in zip(before,after)]),'significant':p<.05,
            'method':'exact_sign_flip' if exact else 'monte_carlo_sign_flip','seed':20261019}


def rerank_policy_effect(rows,cases):
    expected={c['id']:c for c in cases if c['gold']}
    variants={name:{r['case']:r for r in rows if r['variant']==name and r.get('metrics')}
        for name in ('hybrid_no_rerank','hybrid_rerank')}
    common=sorted(set(expected)&set(variants['hybrid_no_rerank'])&set(variants['hybrid_rerank']))
    groups={}
    for ident in common:
        sources=expected[ident].get('sources',[])
        key=tuple(sorted(sources)) if sources else ('query:'+ident,)
        groups.setdefault(key,[]).append(ident)
    before=[];after=[]
    for group in groups.values():
        # 按来源整体翻转符号，保留每题权重；同源多题不作为独立样本。
        scale=len(groups)/len(common)
        before.append(sum(variants['hybrid_no_rerank'][i]['metrics']['mrr'] for i in group)*scale)
        after.append(sum(variants['hybrid_rerank'][i]['metrics']['mrr'] for i in group)*scale)
    return {'test':paired_test(before,after) if common else None,
        'coverage':{'paired':len(common),'expected':len(expected),
            'model_applied':sum(bool(variants['hybrid_rerank'][i].get('rerank_applied')) for i in common),
            'source_clusters':len(groups)},
        'complete':bool(expected) and set(common)==set(expected),
        'scope':'完整查询上的启用策略对照，降级与失败零分保留；按来源簇置换。不同来源集合的重叠相关性仍需专门研究。'}


@contextmanager
def policy(**values):
    previous={key:os.environ.get(key) for key in values}
    os.environ.update(values)
    try:yield
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def baseline(case, contexts):
    settings=require_llm()
    prompt='回答菜谱问题。返回JSON：{"answer":"回答", "abstain":false, "evidence":[]}。'
    if contexts:
        prompt+='仅使用资料；每条依据包含chunk_id、source、quote，quote须逐字摘录资料content中的连续文本。资料不足则abstain=true。'
    payload=chat_parameters({'messages':[{'role':'system','content':prompt},
        {'role':'user','content':json.dumps({'question':case['question'],'contexts':contexts},ensure_ascii=False)}],
        'response_format':{'type':'json_object'},'max_tokens':2000,'temperature':0},settings)
    response=request_json(Request(settings['base_url']+'/chat/completions',data=json.dumps(payload,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+settings['api_key'],'Content-Type':'application/json'}),timeout=45,phase='eval_baseline')
    if response['choices'][0]['finish_reason']!='stop':raise ValueError('基线输出未完整结束')
    result=json.loads(response['choices'][0]['message']['content'])
    if not isinstance(result.get('answer'),str) or type(result.get('abstain')) is not bool or not isinstance(result.get('evidence'),list):
        raise ValueError('基线输出格式错误')
    if contexts:
        for evidence in result['evidence']:
            window=next((r for r in contexts if r['chunk_id']==evidence.get('chunk_id') and r['source']==evidence.get('source')),None)
            quote=evidence.get('quote')
            if window is None or not isinstance(quote,str) or not quote or window['content'].count(quote)!=1:
                raise ValueError('基线引用不属于提供的唯一原文片段')
            raw=(ROOT/window['source']).read_text()
            if 'start_char' in window:start=window['start_char']+window['content'].index(quote)
            else:
                lines=raw.splitlines(keepends=True)
                start=sum(len(x) for x in lines[:window['source_start_line']-1])+window['content'].index(quote)
            end=start+len(quote)
            if raw[start:end]!=quote:raise ValueError('基线引用偏移不一致')
            evidence.update(start_char=start,end_char=end,start_line=raw.count('\n',0,start)+1,
                end_line=raw.count('\n',0,end-1)+1)
    return {'short_answer':result['answer'],'evidence':result['evidence'],
        'status':'insufficient' if result['abstain'] else 'baseline_unreviewed','usage':[response.get('usage',{})]}


def naive_query(question,manifest,rows,settings):
    spec=manifest['spec']
    payload={'model':spec['model'],'input':question,'dimensions':spec['dimensions'],'encoding_format':'float'}
    request=Request(spec['base_url']+'/embeddings',data=json.dumps(payload,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
    response=request_json(request,timeout=45,phase='eval_naive_embedding',cache_namespace='naive512:'+manifest['spec_sha256'])
    return dense_search(response['data'][0]['embedding'],rows)


def summarize_retrieval(rows):
    result={}
    for variant in sorted({r['variant'] for r in rows}):
        chosen=[r for r in rows if r['variant']==variant and r.get('metrics')]
        result[variant]={'metrics':{key:interval([r['metrics'][key] for r in chosen])
            for key in ('hit','recall','precision','mrr','ndcg')},'categories':{}}
        for kind in sorted({r['type'] for r in chosen}):
            subset=[r for r in chosen if r['type']==kind]
            result[variant]['categories'][kind]={key:interval([r['metrics'][key] for r in subset]) for key in ('hit','recall','mrr')}
    return result


def collect_usage(record):
    usages=[r.get('usage',{}) for r in record.get('model_calls',[]) if r.get('usage')]
    for output in record.get('outputs',[]):
        path=output['result'].get('answer_result')
        if path:
            answer=json.loads((ROOT/path).read_text())
            usages.extend(c['response'].get('usage',{}) for c in answer.get('model_calls',[]))
    return usages


def answer_variants(case_id):
    pair=('full_agent','fixed_workflow')
    if int(hashlib.sha256(case_id.encode()).hexdigest(),16)%2:pair=pair[::-1]
    return ('closed_book','naive_rag')+pair


def register_evaluation(out,heldout=False):
    path=ROOT/'data/processed/prd-acceptance/latest.json'
    current=json.loads(path.read_text()) if path.exists() else {}
    report=str((out/'report.json').relative_to(ROOT));markdown=str((out/'report.md').relative_to(ROOT))
    current.update(latest_evaluation_report=report,latest_evaluation_markdown=markdown)
    if heldout or not current.get('report'):
        previous=current.get('report')
        if previous and previous!=report:
            history=current.setdefault('previous_formal_reports',[])
            if previous not in history:history.append(previous)
        current.update(report=report,markdown=markdown)
        if heldout:current['selection_policy']='本轮一次性封存测试在首条结果前登记为当前正式报告，无论结果好坏均保留；历史报告不覆盖，开发回归不替代封存成绩'
    path.write_text(json.dumps(current,ensure_ascii=False,indent=2)+'\n')


def run(live=False, fixture=ROOT/'data/evaluation/prd-dev-v1.json'):
    out=ROOT/'data/processed/prd-acceptance'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out.mkdir(parents=True)
    with policy(RECIPE_CACHE_NAMESPACE=out.name,RECIPE_USAGE_LEDGER=str(out/'usage.jsonl')):
        return run_evaluation(live,fixture,out)


def run_evaluation(live, fixture, out):
    started=time.monotonic();data=json.loads(fixture.read_text());chunks=load_chunks();lookup={r['chunk_id']:r for r in chunks}
    for source,digest in data['source_hashes'].items():
        if sha(ROOT/source)!=digest:raise ValueError('评测参考来源已变化，请重新标注')
    for case in data['cases']:
        if not set(case['gold'])<=set(lookup):raise ValueError('参考块不属于活动版本')
    versions={}
    for name in ('pymilvus','langchain','langchain-openai','tiktoken'):
        try:versions[name]=metadata.version(name)
        except metadata.PackageNotFoundError:versions[name]=None
    s=config();llm=llm_settings()
    code=code_fingerprint()
    heldout=None
    if data['split']=='test':
        if not live:raise ValueError('封存测试只允许实时执行')
        from eval_dataset import consume
        heldout=consume(fixture,out)
    naive_manifest=None;naive_rows=[]
    naive_folder=ROOT/'data/processed/naive-512-v1'
    if live and (naive_folder/'manifest.json').exists():
        naive_manifest,naive_rows=load_index(naive_folder)
        spec=naive_manifest['spec']
        if (spec['model']!=s['EMBEDDING_MODEL'] or spec['dimensions']!=int(s['EMBEDDING_DIMENSIONS'])
            or spec['base_url']!=s['EMBEDDING_BASE_URL'].rstrip('/')
            or set(spec['source_hashes'])!={r['source'] for r in chunks}):
            raise ValueError('512-token基线与活动全库/向量模型不一致')
    report={'schema_version':3,'mode':'live' if live else 'offline','fixture_sha256':sha(fixture),
        'report_path':str((out/'report.json').relative_to(ROOT)),
        'fixture_path':os.path.relpath(fixture.resolve(),ROOT.resolve()),
        'dataset_sha256':sha(FOLDER/'chunks.jsonl'),'code_sha256':code,'python':sys.version,'dependencies':versions,
        'source_commit':subprocess.check_output(['git','-C',str(ROOT/'data/HowToCook'),'rev-parse','HEAD'],text=True).strip(),
        'fixture_scope':data['scope'],'split':data['split'],
        'configuration':{'collection':COLLECTION,'llm_model':llm['model'],'llm_base_url':llm['base_url'],
            'embedding_model':s.get('EMBEDDING_MODEL'),'dimensions':s.get('EMBEDDING_DIMENSIONS'),
            'top_k':20,'top_n':5,'rerank_candidate_limit':int(os.environ.get('RAG_RERANK_CANDIDATES','10')),
            'bootstrap_seed':20261014,'answer_cache':False,'runtime_isolated':False,
            'cache_namespace':out.name,'model_limits_scope':'本地准入策略，不是已确认的供应商账号配额',
            'answer_order':'按case哈希交替Agent与固定工作流；共享真实准入额度，缓存按case/方案隔离',
            'code_fingerprint_scope':'scripts/**/*.py relative paths and contents',
            'rerank_admission_seconds':float(os.environ.get('RAG_RERANK_ADMISSION_SECONDS','0.5')),
            'review_thinking':os.environ.get('RECIPE_REVIEW_THINKING','0')=='1',
            'model_limits':{'concurrency':int(os.environ.get('RECIPE_MODEL_CONCURRENCY','2')),
                'rpm':int(os.environ.get('RECIPE_MODEL_RPM','60')),'tpm':int(os.environ.get('RECIPE_MODEL_TPM','60000'))},
            'baseline_scope':'同一全库、同一向量模型，独立固定512-token稠密索引，无前缀/重排/重叠' if naive_rows else 'H2稠密诊断；固定512索引不可用',
            'naive_512_manifest':naive_manifest,
            'baseline_latency_scope':'independent_embedding_dense_generation' if naive_rows else 'generation_only_h2_diagnostic',
            'naive_512_gold_policy':'同一原文gold行范围与窗口交集至少32字符；短gold取全文长度，跨粒度指标单独报告'},
        'held_out_manifest':heldout,
        'corpus':corpus_audit(),'retrieval':[],'answers':[], 'failures':[]}
    (out/'human-review.json').write_text(json.dumps({'rubric':'请独立核对事实正确、完整、每项引用支持以及库外拒答；不把关键词命中当语义正确。','reviews':[]},ensure_ascii=False,indent=2))
    bm25=BM25(chunks)
    def save():
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save();register_evaluation(out,heldout=bool(heldout))
    for case in data['cases']:
        if not case['gold'] and not live:continue
        lexical=bm25.search(case['question'],20)
        if case['gold']:
            report['retrieval'].append({'case':case['id'],'type':case['type'],'variant':'bm25',
                'metrics':metrics([r['chunk_id'] for r in lexical],case['gold'])})
        vector=[];naive=[]
        if live:
            try:
                with request_budget(120),policy(RAG_TOP_K='20',RAG_RETRIEVAL='hybrid',RAG_AUGMENT='1',RAG_RERANK='1',RAG_TOP_N='5'):
                    t=time.monotonic();search_path,search=run_stage('search_test_recipes.py',case['question'],'search-results')
                    search_seconds=time.monotonic()-t
                    vector=sorted([r for r in search['results'] if 'vector' in r['retrieval_channels']],key=lambda r:-r['distance'])
                    if naive_rows:
                        naive=dense_search(search['query_vector'],naive_rows)
                        mapped=gold_windows(case['gold'],chunks,naive_rows)
                        report['retrieval'].append({'case':case['id'],'type':case['type'],'variant':'naive_512',
                            'metrics':metrics([r['chunk_id'] for r in naive],mapped),
                            'gold':mapped,'results':naive})
                    no_bm25=[r for r in search['results'] if set(r['retrieval_channels'])-{'bm25'}]
                    # 重新融合剩余通道，不能只从含BM25分数的RRF列表删去条目。
                    from lexical_retrieval import rrf_merge
                    channels={name:sorted([r for r in no_bm25 if name in r['retrieval_channels']],key=lambda r:-r['distance']) for name in ('vector','title','ingredient')}
                    without_sparse=rrf_merge(channels)
                    t=time.monotonic();rerank_path,rerank=run_stage('rerank_test_recipes.py',str(search_path),'rerank-results')
                    rerank_seconds=time.monotonic()-t
                hybrid=search['results']
                if rerank.get('candidate_scope')=='ingredient_tables':
                    hybrid=[r for r in hybrid if '必备原料' in r['section']]
                    without_sparse=[r for r in without_sparse if '必备原料' in r['section']]
                for variant, hits in (('naive_vector',vector),('hybrid_no_rerank',hybrid),('without_bm25',without_sparse),('hybrid_rerank',rerank['results'])):
                    report['retrieval'].append({'case':case['id'],'type':case['type'],'variant':variant,
                        'metrics':metrics([r['chunk_id'] for r in hits],case['gold']),
                        'search_seconds':search_seconds,'rerank_seconds':rerank_seconds if variant=='hybrid_rerank' else 0,
                        'rerank_applied':bool(rerank.get('rerank_enabled')) if variant=='hybrid_rerank' else False,
                        'rerank_fallback':rerank.get('rerank_fallback') if variant=='hybrid_rerank' else None,
                        'search_record':str(search_path.relative_to(ROOT)),'rerank_record':str(rerank_path.relative_to(ROOT))})
            except Exception as error:
                report['failures'].append({'case':case['id'],'stage':'retrieval','type':type(error).__name__})
                if case['gold']:
                    for variant in ('naive_vector','hybrid_no_rerank','without_bm25','hybrid_rerank')+ (('naive_512',) if naive_rows else ()):
                        if any(r['case']==case['id'] and r['variant']==variant for r in report['retrieval']):continue
                        report['retrieval'].append({'case':case['id'],'type':case['type'],'variant':variant,
                            'metrics':metrics([],case['gold']),'failure_type':type(error).__name__})
            if case['answer_eval']:
                contexts=naive if naive_rows else vector[:5]
                for order,variant in enumerate(answer_variants(case['id'])):
                    store=RuntimeStore();events=store.events_since();event_start=events[-1]['id'] if events else 0
                    row={'case':case['id'],'type':case['type'],'variant':variant,'semantic_review':'pending','execution_order':order};t=time.monotonic()
                    try:
                        with request_budget(180),policy(RECIPE_ANSWER_CACHE='0',RAG_RETRIEVAL='hybrid',RAG_AUGMENT='1',RAG_RERANK='1',RAG_TOP_K='20',RAG_TOP_N='5',
                                RECIPE_CACHE_NAMESPACE=out.name+':'+case['id']+':'+variant):
                            if variant in ('full_agent','fixed_workflow'):
                                record=execute(case['question'],mode='agent' if variant=='full_agent' else 'rag')
                                results=[o['result'] for o in record.get('outputs',[])]
                                result={'status':results[0]['status'] if len(results)==1 else record['status'],
                                    'short_answer':'\n'.join(r['short_answer'] for r in results),
                                    'evidence':[e for r in results for e in r.get('evidence',[])], 'usage':collect_usage(record)}
                                row['agent_record']=record
                                row['answer_methods']=[r.get('answer_method','unspecified') for r in results]
                            else:
                                if variant=='naive_rag' and naive_rows:
                                    retrieval_started=time.monotonic()
                                    contexts=naive_query(case['question'],naive_manifest,naive_rows,s)
                                    row['naive_retrieval_seconds']=time.monotonic()-retrieval_started
                                if variant=='naive_rag' and not contexts:raise ValueError('检索失败，不能伪造Naive RAG')
                                result=baseline(case,contexts if variant=='naive_rag' else [])
                        row.update(result=result,evidence_check=exact_evidence(result),citation_metadata=citation_metadata(result,chunks),
                            required_terms_present=all(term in result['short_answer'] for term in case['required_terms']),
                            refusal_correct=(result['status']=='insufficient' and bool(result['short_answer'].strip())) if not case['gold'] else None)
                        if variant=='naive_rag':
                            row['retrieved_contexts']=[r['content'] for r in contexts]
                            row['baseline_strategy']='fixed512' if naive_rows else 'h2_dense_diagnostic'
                            allowed={r['source'] for r in contexts}
                            row['evidence_in_context']=all(e.get('source') in allowed and any(
                                e.get('source')==r['source'] and type(e.get('start_line')) is int and type(e.get('end_line')) is int
                                and r['source_start_line']<=e['start_line']<=e['end_line']<=r['source_end_line'] for r in contexts)
                                for e in result['evidence'])
                    except Exception as error:row.update(failure_type=type(error).__name__,refusal_correct=False if not case['gold'] else None)
                    row['seconds']=time.monotonic()-t;row['phase_events']=store.events_since(event_start)
                    report['answers'].append(row);save()
                    print('回答对照：'+case['id']+' / '+variant+' / '+row.get('result',{}).get('status','failed'),flush=True)
        save();print('检索评测：'+case['id'],flush=True)
    report['aggregates']=summarize_retrieval(report['retrieval'])
    pairs={variant:{r['case']:r['metrics']['mrr'] for r in report['retrieval'] if r['variant']==variant and r['metrics']
        and (variant!='hybrid_rerank' or r.get('rerank_applied'))} for variant in ('hybrid_no_rerank','hybrid_rerank')}
    common=sorted(set(pairs['hybrid_no_rerank'])&set(pairs['hybrid_rerank']))
    report['rerank_significance']=paired_test([pairs['hybrid_no_rerank'][i] for i in common],[pairs['hybrid_rerank'][i] for i in common]) if common else None
    report['rerank_comparison_coverage']={'paired':len(common),'expected':sum(bool(c['gold']) for c in data['cases']),
        'policy':'仅真实模型重排进入组件显著性检验，降级结果仍保留在完整系统指标中'}
    report['rerank_policy_effect']=rerank_policy_effect(report['retrieval'],data['cases'])
    report['answer_aggregates']={}
    for variant in sorted({r['variant'] for r in report['answers']}):
        rows=[r for r in report['answers'] if r['variant']==variant];latencies=sorted(r['seconds'] for r in rows)
        report['answer_aggregates'][variant]={'n':len(rows),'p50_seconds':statistics.median(latencies),
            'p95_seconds':latencies[math.ceil(.95*len(latencies))-1],'failures':sum('failure_type' in r for r in rows),
            'degraded':sum(r.get('result',{}).get('status')=='degraded' for r in rows),
            'refusal':interval([int(r['refusal_correct']) for r in rows if r.get('refusal_correct') is not None]),
            'semantic_accuracy':None,'total_tokens':sum(u.get('total_tokens',0) for r in rows for u in r.get('result',{}).get('usage',[])),
            'cost':None,'cost_note':'完整供应商价格快照和embedding/rerank计费用量尚不齐全，不用token代理冒充真实费用'}
    full=report['aggregates'].get('hybrid_rerank',{}).get('metrics',{}).get('hit')
    effect=report['rerank_policy_effect'];policy_test=effect['test']
    report['acceptance']={'source_restore':report['corpus']['passed'],
        'hit5_target':full['mean']>=.9 if full else None,
        'rerank_significant_improvement':bool(policy_test['significant'] and policy_test['mean_delta']>0 and effect['complete']) if policy_test else None,
        'rerank_significance_scope':'全查询启用策略（含降级/失败），来源簇配对；成功调用子集另外保留',
        'citation_semantic_accuracy_98_percent':None,'held_out_test':bool(heldout),'ragas': 'not_run',
        'failure_policy':'检索失败按零分纳入；答案失败保留在样本分母中',
        'cost_acceptability':None,'overall':'partial: 开发诊断验收；统计质量、独立语义复核与成本尚未全面达标'}
    citations=[r['citation_metadata'] for r in report['answers'] if r['variant']=='full_agent' and r.get('citation_metadata')]
    count=sum(r['count'] for r in citations);correct=sum(r['correct'] for r in citations)
    report['citation_metadata']={'count':count,'correct':correct,'rate':correct/count if count else None,
        'scope':'PRD9.3规则引用准确率；不等同语义引用支持率'}
    report['configuration']['embedding_rerank_cache']='每轮独立空缓存；回答按case/方案隔离，避免工作流复用Agent缓存；检索对照同轮可复用，延迟不是生产SLA'
    report['code_sha256_end']=code_fingerprint()
    from usage_ledger import summarize
    report['usage_ledger']=summarize(out/'usage.jsonl')
    report['code_changed_during_run']=report['code_sha256_end']!=report['code_sha256']
    report['elapsed_seconds']=time.monotonic()-started;save()
    markdown='# PRD 可复现评测\n\n'+report['acceptance']['overall']+'。\n\n'
    markdown+='模式：'+report['mode']+'；题集：'+data['scope']+'。bootstrap 区间仅反映此开发集；小样本 P95 不代表生产 SLA。\n\n'
    markdown+='| 检索方案 | N | Hit@5 | Recall@5 | MRR |\n|---|---:|---:|---:|---:|\n'
    for variant,a in report['aggregates'].items():
        m=a['metrics'];markdown+=f"| {variant} | {m['hit']['n']} | {m['hit']['mean']:.3f} | {m['recall']['mean']:.3f} | {m['mrr']['mean']:.3f} |\n"
    markdown+='\n重排配对检验：`'+json.dumps(report['rerank_significance'],ensure_ascii=False)+'`。\n\n'
    markdown+='逐题回答、所有失败、分类指标、版本和置信区间见同目录 report.json；待人工复核见 human-review.json。引用逐字一致只证明溯源，不代表结论语义获支持。\n'
    (out/'report.md').write_text(markdown)
    register_evaluation(out,heldout=bool(heldout))
    print('验收报告：'+str(out/'report.md'),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    parser.add_argument('--fixture',type=Path,default=ROOT/'data/evaluation/prd-dev-v1.json');args=parser.parse_args()
    result=run(args.live,args.fixture)
    sys.exit(1 if result['failures'] or any('failure_type' in r for r in result['answers']) or not result['corpus']['passed'] else 0)
