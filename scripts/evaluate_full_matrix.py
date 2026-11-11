"""固定生成协议的E0-E8/A1-A6端到端受控矩阵；生产系统另外对照，失败不删除。"""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import math
import os
import statistics
from pathlib import Path
import time
import traceback
from unittest.mock import patch
from urllib.request import Request

import numpy as np

from ablation_indexes import load as load_overviews
from acceptance_closeout import exact_evidence,citation_metadata
from check_embedding import ROOT,config
from evaluate_prd import baseline,code_fingerprint,metrics,policy,interval
from ingredient_recommendation import ALIASES,normalize_ingredient,ingredient_fields
from lexical_retrieval import BM25,rrf_merge
from llm_config import llm_settings
from naive_baseline import load_index,gold_windows
from recipe_agent import execute,call_model,normalize_tool_request
from recipe_tools import load_chunks
from rag_dataset import FOLDER,COLLECTION
from search_filters import validate_filters,explicit_question_filters
from service_runtime import request_json,request_budget
from usage_ledger import summarize

from experiment_profiles import load_core_profiles
PROFILES=load_core_profiles(ROOT/'config/experiments/core')


def normalize_query(question):
    for old,new in sorted(ALIASES.items(),key=lambda item:-len(item[0])):
        if len(old)>1:question=question.replace(old,new)
    return question


def deduplicate(rows):
    result=[];seen=set()
    for row in rows:
        ident=row.get('evidence_chunk_id',row['chunk_id'])
        if ident not in seen:result.append(row);seen.add(ident)
    return result


def controlled_scope_complete(question,outputs):
    # 完成执行不等于事实已核验；保留baseline_unreviewed，避免实验标签诱发额外查询。
    return bool(outputs) and all(row['tool']=='query_recipes' and
        row['result'].get('status') in ('baseline_unreviewed','insufficient') and
        row['result'].get('short_answer','').strip() for row in outputs)


def failure_diagnostics(error):
    # 仅记录调用位置及已知本地校验文案，不保存任意供应商消息或请求密钥。
    frames=traceback.extract_tb(error.__traceback__)
    detail={'failure_type':type(error).__name__,'failure_trace':[
        {'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name} for frame in frames]}
    known={'基线输出未完整结束','基线输出格式错误','基线引用不属于提供的唯一原文片段','基线引用偏移不一致',
           '本题约束提取失败，不能假设没有限制','受控Agent未调用查询工具','生产Agent没有交付结果'}
    if str(error) in known:detail['failure_reason']=str(error)
    return detail


class Engine:
    def __init__(self,overview_folder):
        self.settings=config();self.chunks=load_chunks();self.active=json.loads((FOLDER/'embedding-metadata.json').read_text())
        self.metadata=json.loads((FOLDER/'source-metadata.json').read_text())
        self.raw_ingredients={}
        for source in self.metadata:
            self.raw_ingredients[source]=ingredient_fields([r for r in self.chunks if r['source']==source and '必备原料' in r['section']],
                (ROOT/source).read_text().splitlines(),normalize_aliases=False)[0]
        fixed_manifest,self.fixed=load_index(ROOT/'data/processed/naive-512-v1')
        if fixed_manifest['spec']['model']!=self.active['requested_model'] or fixed_manifest['spec']['dimensions']!=self.active['dimensions']:raise ValueError('固定窗口向量模型不一致')
        self.h2={};self.overviews={};self.cache={};self.query_vectors={};self.query_usage=[]
        for prefix,name in ((False,'h2_plain'),(True,'h2_prefix')):
            manifest,rows=load_index(ROOT/'data/processed/chunk-ablations-v1'/name)
            if manifest['spec']['model']!=self.active['requested_model'] or manifest['spec']['dimensions']!=self.active['dimensions']:raise ValueError('实验模型/维度不一致')
            self.h2[prefix]=rows
        for name in ('prefix_normalized','plain_normalized','prefix_raw'):self.overviews[name]=load_overviews(overview_folder,name)
        for profile in PROFILES.values():
            if profile['corpus']!='closed':self.corpus(profile)

    def corpus(self,profile):
        key=(profile['corpus'],profile['prefix'],profile['overview'],profile['normalize'])
        if key not in self.cache:
            rows=self.fixed if profile['corpus']=='fixed512' else self.h2[profile['prefix']]
            if profile['overview']:
                mode='plain_normalized' if not profile['prefix'] else 'prefix_normalized' if profile['normalize'] else 'prefix_raw'
                rows=rows+self.overviews[mode]
            matrix=np.asarray([row['vector'] for row in rows],dtype=np.float64)
            norms=np.linalg.norm(matrix,axis=1)
            if not np.isfinite(matrix).all() or (norms==0).any():raise ValueError('实验索引向量无效')
            bm25=BM25([{**r,'content':r.get('lexical_content',r['content'])} for r in rows])
            self.cache[key]=(rows,matrix/norms[:,None],bm25)
        return self.cache[key]

    def embed_queries(self,cases):
        questions=list(dict.fromkeys(text for case in cases for text in (case['question'],normalize_query(case['question']))))
        for offset in range(0,len(questions),8):
            batch=questions[offset:offset+8]
            req=Request(self.active['api_base_url']+'/embeddings',data=json.dumps({'model':self.active['requested_model'],
                'dimensions':self.active['dimensions'],'input':batch,'encoding_format':'float'},ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer '+self.settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
            response=request_json(req,timeout=90,attempts=1,phase='matrix_query_embedding')
            self.query_usage.append(response.get('usage'))
            for text,row in zip(batch,sorted(response['data'],key=lambda row:row['index'])):self.query_vectors[text]=row['embedding']

    def allowed(self,filters,normalized):
        validate_filters(**filters);allowed=set()
        for source,info in self.metadata.items():
            if filters.get('category') is not None and info['category']!=filters['category']:continue
            if filters.get('max_difficulty') is not None and not 1<=info['difficulty']<=filters['max_difficulty']:continue
            ingredients=set(info['ingredients']) if normalized else self.raw_ingredients[source]
            terms=lambda key:{normalize_ingredient(value,aliases=normalized) for value in filters.get(key,[])}
            any_terms=terms('ingredients_any');all_terms=terms('ingredients_all')
            if any_terms and not any_terms&ingredients or not all_terms<=ingredients:continue
            allowed.add(source)
        return allowed

    def retrieve(self,case,profile,understanding):
        rows,matrix,bm25=self.corpus(profile)
        filters=understanding if profile['filter'] else {}
        allowed=self.allowed(filters,profile['normalize'])
        question=normalize_query(case['question']) if profile['normalize'] else case['question']
        vector=np.asarray(self.query_vectors[question]);scores=matrix@(vector/np.linalg.norm(vector))
        eligible=[i for i,row in enumerate(rows) if row['source'] in allowed]
        ranked=sorted(eligible,key=lambda i:(-scores[i],rows[i]['chunk_id']))[:30]
        dense=[{**rows[i],'distance':float(scores[i])} for i in ranked]
        if profile['hybrid']:
            lookup={row['chunk_id']:row for row in rows}
            lexical=[{**lookup[row['chunk_id']],'bm25_score':row['bm25_score']} for row in bm25.search(question,len(rows)) if row['source'] in allowed][:30]
            hits=rrf_merge({'vector':dense,'bm25':lexical})
        else:hits=dense
        candidates=deduplicate(hits)[:30];rerank_applied=False
        if profile['rerank'] and candidates:
            req=Request(self.active['api_base_url']+'/rerank',data=json.dumps({'model':'Qwen/Qwen3-Reranker-8B',
                'query':case['question'],'documents':[r.get('text',r['content']) for r in candidates],
                'top_n':len(candidates),'return_documents':False},ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer '+self.settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
            response=request_json(req,timeout=30,attempts=1,phase='matrix_rerank')
            candidates=[candidates[item['index']] for item in sorted(response['results'],key=lambda row:-row['relevance_score'])]
            rerank_applied=True
        contexts=[{key:value for key,value in row.items() if key not in ('vector','lexical_content')} for row in candidates[:5]]
        canonical=[]
        for row in rows:
            if row.get('evidence_chunk_id') is None:canonical.append(row)
        gold=gold_windows(case['gold'],self.chunks,canonical)
        retrieved=[row.get('evidence_chunk_id',row['chunk_id']) for row in contexts]
        return contexts,{'metrics':metrics(retrieved,gold) if case['gold'] else None,'rerank_applied':rerank_applied,
            'filters':filters,'allowed_sources':len(allowed),'candidate_count':len(candidates),'retrieved_canonical_ids':retrieved}


def extract_filters(case,chunks):
    start=time.monotonic()
    from recipe_agent import SYSTEM
    message,metadata=call_model([{'role':'system','content':SYSTEM},{'role':'user','content':case['question']}])
    calls=message.get('tool_calls',[])
    if len(calls)!=1:raise ValueError('约束提取未返回唯一工具请求')
    function=calls[0]['function'];args=json.loads(function['arguments'])
    name,args,_=normalize_tool_request(function['name'],args,case['question'])
    if name!='query_recipes':raise ValueError('约束提取未调用查询工具')
    filters={key:value for key,value in args.items() if key!='question'};validate_filters(**filters)
    explicit=explicit_question_filters(case['question'],chunks)
    if any(key in filters and filters[key]!=value for key,value in explicit.items()):raise ValueError('约束提取与明确问题冲突')
    return {**filters,**explicit},{'raw_message':message,'metadata':metadata,'seconds':time.monotonic()-start}


def run(out,fixture,overview_folder):
    out=Path(out);fixture=Path(fixture);out.mkdir(parents=True,exist_ok=False)
    cases=json.loads(fixture.read_text());start_hash=code_fingerprint();settings=llm_settings()
    report={'schema_version':1,'protocol':'controlled-matrix-v1','scope':cases['scope'],
        'fixture_path':str(fixture.relative_to(ROOT)),'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'code_sha256':start_hash,'configuration':{'llm_model':settings['model'],'llm_base_url':settings['base_url'],
            'collection':COLLECTION,'rerank_top_k':30,'final_k':5,'profiles':PROFILES,
            'timing_scope':'共享预计算真实查询向量和每题约束提取；不用于生产端到端P95门槛'},
        'ragas_eval_variants':['E0','E1','E8','full_agent'],'answers':[],'understanding':{},'retrieval':[],
        'answer_aggregates':{},'status':'running','matrix_complete':False,
        'acceptance':{'cost_acceptability':None,'ragas':'not_run','overall':'controlled_development_matrix; formal_acceptance_not_replaced'},
        'production_equivalence':False,'production_deviation':'受控矩阵固定H2与同一原文引用生成协议，排除生产原文直答等捷径；另测未改动生产Agent作参考'}
    def save():
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (out/'report.md').write_text('# 受控端到端消融矩阵\n\n'+cases['scope']+'\n\n受控矩阵与生产Agent单列，失败不删除；不代表完整PRD通过。\n')
    save();engine=Engine(overview_folder)
    with policy(RECIPE_USAGE_LEDGER=str(out/'usage.jsonl'),RECIPE_ANSWER_CACHE='0'):
        engine.embed_queries(cases['cases'])
        (out/'query-vectors.json').write_text(json.dumps(engine.query_vectors,ensure_ascii=False)+'\n')
        for case in cases['cases']:
            try:
                with request_budget(90):understanding,audit=extract_filters(case,engine.chunks)
                report['understanding'][case['id']]={'filters':understanding,**audit}
            except Exception as error:
                understanding=None;report['understanding'][case['id']]=failure_diagnostics(error)
            for variant,profile in list(PROFILES.items())+[('full_agent',None)]:
                row={'case':case['id'],'type':case['type'],'variant':variant,'semantic_review':'pending'};started=time.monotonic();contexts=[];retrieval={}
                try:
                    with request_budget(180):
                        if profile is None:
                            record=execute(case['question']);outputs=[o['result'] for o in record.get('outputs',[])]
                            if not outputs:raise ValueError('生产Agent没有交付结果')
                            result={'status':outputs[0]['status'] if len(outputs)==1 else record['status'],
                                'short_answer':'\n'.join(r['short_answer'] for r in outputs),'evidence':[e for r in outputs for e in r.get('evidence',[])]}
                            row['agent_record']=record
                            if record['status'] not in ('completed','insufficient'):row['failure_type']='ProductionAgentIncomplete'
                        elif profile['corpus']=='closed':
                            result=baseline(case,[]);row['ragas_context_mode']='closed_book'
                        else:
                            if profile['filter'] and understanding is None:raise ValueError('本题约束提取失败，不能假设没有限制')
                            def pipeline():
                                nonlocal contexts,retrieval
                                contexts,retrieval=engine.retrieve(case,profile,understanding or {})
                                if not contexts:return {'status':'insufficient','short_answer':'当前知识库未找到满足所要求条件的原文。','evidence':[]}
                                return baseline(case,contexts)
                            if profile['agent']:
                                def dispatch(name,args,progress,original_question=None):
                                    if name!='query_recipes':raise ValueError('受控Agent仅提供查询操作')
                                    return pipeline()
                                def model(history):
                                    history=deepcopy(history)
                                    history[0]['content']+='\n这是隔离实验。查询工具的baseline_unreviewed表示已返回待独立评审的回答；获取一次查询结果后停止，不再次判断或改写答案。'
                                    return call_model(history)
                                with patch('recipe_agent.dispatch',side_effect=dispatch),patch('recipe_agent.query_scope_complete',side_effect=controlled_scope_complete):
                                    record=execute(case['question'],model_call=model)
                                row['agent_record']=record
                                if not record.get('outputs'):raise ValueError('受控Agent未调用查询工具')
                                result=record['outputs'][0]['result']
                                if record['status'] not in ('completed','insufficient'):row['failure_type']='ControlledAgentIncomplete'
                            else:result=pipeline()
                        context_lookup={r['chunk_id']:r for r in contexts}
                        for evidence in result.get('evidence',[]):
                            reference=context_lookup.get(evidence.get('chunk_id'))
                            if reference:evidence.update(title=reference['title'],section=reference['section'])
                        row.update(result=result,evidence_check=exact_evidence(result),citation_metadata=citation_metadata(result,engine.chunks if profile is None else contexts),
                            refusal_correct=(result['status']=='insufficient') if not case['gold'] else None)
                except Exception as error:row.update(failure_diagnostics(error))
                if profile is not None:row['retrieved_contexts']=[r['content'] for r in contexts]
                if retrieval:
                    row['retrieval']=retrieval
                    report['retrieval'].append({'case':case['id'],'variant':variant,'type':case['type'],**retrieval})
                row['seconds']=time.monotonic()-started
                report['answers'].append(row);save()
                print(case['id']+' / '+variant+' / '+row.get('failure_type',row.get('result',{}).get('status','failed')),flush=True)
                time.sleep(1)
    for variant in list(PROFILES)+['full_agent']:
        rows=[row for row in report['answers'] if row['variant']==variant]
        values=sorted(row['seconds'] for row in rows)
        report['answer_aggregates'][variant]={'n':len(rows),'failures':sum('failure_type' in row for row in rows),
            'p95_seconds':values[math.ceil(.95*len(values))-1],'p50_seconds':statistics.median(values),
            'refusal':interval([int(row.get('refusal_correct',False)) for row in rows if not next(c for c in cases['cases'] if c['id']==row['case'])['gold']])}
    expected={(case['id'],variant) for case in cases['cases'] for variant in list(PROFILES)+['full_agent']}
    actual={(row['case'],row['variant']) for row in report['answers']}
    report['coverage_complete']=actual==expected
    report['execution_failures']=sum('failure_type' in row for row in report['answers'])
    report['matrix_complete']=actual==expected and not any('failure_type' in row for row in report['answers'] if row['variant']!='full_agent')
    report['code_sha256_end']=code_fingerprint();report['code_changed_during_run']=start_hash!=report['code_sha256_end']
    report['usage_ledger']=summarize(out/'usage.jsonl');report['status']='completed';save()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--fixture',type=Path,required=True);parser.add_argument('--overview-folder',type=Path,required=True)
    parser.add_argument('--profiles',type=Path,default=ROOT/'config/experiments/core')
    args=parser.parse_args();PROFILES=load_core_profiles(args.profiles)
    run(args.out.resolve(),args.fixture.resolve(),args.overview_folder.resolve())
