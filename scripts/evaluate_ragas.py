"""固定 Ragas 0.4.2 的五项指标与适用范围；保留原始评分。"""
import argparse
import asyncio
from importlib import metadata
import json
import math
import os
import hashlib
from copy import deepcopy
from pathlib import Path
from check_embedding import ROOT,config
from llm_config import require_llm, chat_parameters
from eval_judge import judge_settings,has_evaluable_answer
from service_runtime import request_json
from urllib.request import Request


def scoped_answers(report):
    selected=report.get('ragas_eval_variants')
    return [row for row in report['answers'] if not selected or row['variant'] in selected]


def tool_evidence_context(evidence):
    # 工具实际提供的出处与原文一起评审；不从Gold补充生成器未见过的内容。
    return json.dumps({key:evidence[key] for key in ('title','section','source','quote') if key in evidence},ensure_ascii=False)


def resume_metric_item(previous_rows,item):
    matches=[r for r in previous_rows if r.get('audit_sha256')==item['audit_sha256']]
    prior=next((r for r in matches if (r['case'],r['variant'])==(item['case'],item['variant'])),None)
    if prior is None:prior=next(iter(matches),None)
    if prior is None:return item
    result=deepcopy(prior)
    result.update(case=item['case'],variant=item['variant'])
    for key in ('context_policy','not_applicable'):
        if key in item:result[key]=deepcopy(item[key])
    for name in result.get('not_applicable',{}):
        result.pop(name,None);result.get('metric_failures',{}).pop(name,None)
    for other in matches:
        for name in ('faithfulness','answer_relevancy','context_precision','context_recall','factual_correctness'):
            if name not in result and name in other and name not in result.get('not_applicable',{}):
                result[name]=other[name]
                result.setdefault('metric_failures',{}).pop(name,None)
                if name=='faithfulness':result['diagnostics']=deepcopy(other.get('diagnostics',{}))
                result.setdefault('metric_reuse',[]).append({'metric':name,'case':other['case'],'variant':other['variant'],
                    'audit_sha256':other['audit_sha256']})
    return result


async def score_metric_jobs(jobs,item,on_result=lambda name:None):
    limit=asyncio.Semaphore(2)
    async def score(name,scorer,parameters):
        if name in item:return
        async with limit:
            try:
                value=await asyncio.wait_for(scorer.ascore(**parameters),timeout=180)
                score=float(value.value)
                if not math.isfinite(score):raise ValueError('指标没有返回有限数值')
                item[name]=score;item['metric_failures'].pop(name,None)
            except Exception as error:item['metric_failures'][name]=type(error).__name__
            on_result(name)
    await asyncio.gather(*(score(name,scorer,parameters) for name,(scorer,parameters) in jobs.items()))


def audited_faithfulness(llm, diagnostics):
    """保留 Ragas 0.4.2 原始评分及逐断言理由，不修改提示词或判分规则。"""
    from ragas.metrics.collections import Faithfulness

    class AuditedFaithfulness(Faithfulness):
        async def _create_statements(self, question, response):
            statements=await super()._create_statements(question,response)
            diagnostics['statements']=statements
            return statements

        async def _create_verdicts(self, statements, context):
            verdicts=await super()._create_verdicts(statements,context)
            diagnostics['verdicts']=verdicts.model_dump()['statements']
            return verdicts

    return AuditedFaithfulness(llm=llm)


async def run(path):
    os.environ['RAGAS_DO_NOT_TRACK']='true'
    from openai import AsyncOpenAI
    import httpx
    from ragas.llms import llm_factory
    if metadata.version('ragas')!='0.4.2':raise ValueError('需要requirements-evaluation.txt中的Ragas版本')
    report=json.loads(path.read_text());settings=judge_settings();embedding_config=config()
    client=AsyncOpenAI(api_key=settings['api_key'],base_url=settings['base_url'],timeout=90,max_retries=0,
        http_client=httpx.AsyncClient(timeout=90,limits=httpx.Limits(max_connections=2,max_keepalive_connections=2)))
    extra=chat_parameters({},settings);extra.pop('model')
    llm=llm_factory(settings['model'],client=client,temperature=0,max_tokens=8192,max_retries=2,extra_body=extra)
    from ragas.embeddings import BaseRagasEmbedding
    from ragas.metrics.collections import AnswerRelevancy,ContextPrecision,ContextRecall,FactualCorrectness

    class EvaluationEmbeddings(BaseRagasEmbedding):
        def embed_text(self,text,**kwargs):
            payload={'model':embedding_config['EMBEDDING_MODEL'],'input':text,
                'dimensions':int(embedding_config['EMBEDDING_DIMENSIONS']),'encoding_format':'float'}
            request=Request(embedding_config['EMBEDDING_BASE_URL'].rstrip('/')+'/embeddings',
                data=json.dumps(payload,ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer '+embedding_config['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
            return request_json(request,timeout=60,phase='ragas_embedding')['data'][0]['embedding']
        async def aembed_text(self,text,**kwargs):return await asyncio.to_thread(self.embed_text,text,**kwargs)
        def embed_texts(self,texts,**kwargs):return [self.embed_text(text,**kwargs) for text in texts]
        async def aembed_texts(self,texts,**kwargs):
            return [await self.aembed_text(text,**kwargs) for text in texts]

    embeddings=EvaluationEmbeddings()
    rows=[];previous=report.get('ragas',{})
    compatible=all(previous.get(k)==v for k,v in {'model':settings['model'],'base_url':settings['base_url'],
        'embedding_model':embedding_config['EMBEDDING_MODEL'],
        'embedding_dimensions':int(embedding_config['EMBEDDING_DIMENSIONS'])}.items())
    previous_rows=previous.get('rows',[]) if compatible else []
    from evaluate_prd import sha
    fixture_path=ROOT/report.get('fixture_path','data/evaluation/prd-dev-v1.json')
    if sha(fixture_path)!=report['fixture_sha256']:raise ValueError('评测题集与报告版本不一致')
    fixture=json.loads(fixture_path.read_text())
    cases={r['id']:r for r in fixture['cases']}
    answers=scoped_answers(report)
    try:
        for row in answers:
            case=cases[row['case']]
            if not case['gold'] or not has_evaluable_answer(row):continue
            references=[];context_policy=''
            context_mode=row.get('ragas_context_mode',row['variant'])
            if context_mode=='closed_book':
                references=[(ROOT/s).read_text() for s in case['sources']];context_policy='gold_source_audit: 基线没有收到这些上下文'
            elif context_mode=='naive_rag':
                if 'retrieved_contexts' in row:
                    references=row['retrieved_contexts'];context_policy=row.get('baseline_strategy','recorded_contexts')
                else:
                    retrieval=next(r for r in report['retrieval'] if r['case']==row['case'] and r['variant']=='naive_vector' and r.get('search_record'))
                    search=json.loads((ROOT/retrieval['search_record']).read_text())
                    vector=sorted([r for r in search['results'] if 'vector' in r['retrieval_channels']],key=lambda r:-r['distance'])[:5]
                    references=[r['content'] for r in vector];context_policy='naive_vector_top5'
            elif 'retrieved_contexts' in row:
                references=row['retrieved_contexts'];context_policy='recorded_controlled_retrieval: 实际生成输入，未用答案摘录替换'
            else:
                for output in row.get('agent_record',{}).get('outputs',[]):
                    result=output['result']
                    if result.get('answer_result'):
                        answer=json.loads((ROOT/result['answer_result']).read_text())
                        references.extend(r['content'] for r in answer['references'])
                    else:
                        references.extend(tool_evidence_context(e) for e in result.get('evidence',[]))
                        if result.get('recommendations'):
                            references.append('用户明确库存问题：'+case['question'])
                            references.append('工具依据原料表计算的派生数据（不是原文）：'+json.dumps(
                                [{k:r[k] for k in ('title','coverage','matched','missing','required','optional','tools','unresolved')}
                                 for r in result['recommendations']],ensure_ascii=False))
                            if result.get('coverage_policy'):
                                references.append('工具计算方法与适用范围（不是菜谱事实）：'+json.dumps(result['coverage_policy'],ensure_ascii=False))
                context_policy='实际完整系统工具上下文；直接证据保留title/section/source/quote，不补Gold正文'
            item={'case':row['case'],'variant':row['variant'],'context_policy':context_policy,
                  'retrieved_contexts':references,'diagnostics':{},'not_applicable':{},'metric_failures':{}}
            response=row['result']['short_answer'];question=case['question'];reference=case.get('reference_answer')
            jobs={'faithfulness':(audited_faithfulness(llm,item['diagnostics']),
                dict(user_input=question,response=response,retrieved_contexts=references)),
                'answer_relevancy':(AnswerRelevancy(llm=llm,embeddings=embeddings,strictness=3),dict(user_input=question,response=response))}
            if reference:
                jobs['factual_correctness']=(FactualCorrectness(llm=llm),dict(response=response,reference=reference))
                if context_mode!='closed_book':
                    jobs['context_precision']=(ContextPrecision(llm=llm),dict(user_input=question,reference=reference,retrieved_contexts=references))
                    jobs['context_recall']=(ContextRecall(llm=llm),dict(user_input=question,reference=reference,retrieved_contexts=references))
                else:
                    for name in ('context_precision','context_recall'):item['not_applicable'][name]='Closed-book未检索，不把gold审计资料冒充实际上下文'
            else:
                for name in ('context_precision','context_recall','factual_correctness'):
                    item['not_applicable'][name]='该旧题/开放推荐无固定reference_answer；不以被评答案冒充参考答案'
            item['audit_sha256']=hashlib.sha256(json.dumps([question,response,references,reference,settings['model'],settings['base_url'],
                embedding_config['EMBEDDING_MODEL'],'ragas-0.4.2-five-metrics-v1'],ensure_ascii=False).encode()).hexdigest()
            item=resume_metric_item(previous_rows+rows,item)
            jobs['faithfulness']=(audited_faithfulness(llm,item['diagnostics']),jobs['faithfulness'][1])
            rows.append(item)
            seen={(r['case'],r['variant']) for r in rows}
            persisted=rows+[r for r in previous_rows if (r['case'],r['variant']) not in seen]
            report['ragas']={'version':'0.4.2','model':settings['model'],'base_url':settings['base_url'],
                'postprocessing_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'metric_cache_policy':'完全相同审计输入可复用成功指标；保留本条已有观测，只补缺失；不跨不同输入复用',
                'embedding_model':embedding_config['EMBEDDING_MODEL'],'embedding_dimensions':int(embedding_config['EMBEDDING_DIMENSIONS']),
                'temperature':0,'same_model_bias':settings['model']==report['configuration']['llm_model'],
                'api_timeout_seconds':90,'metric_timeout_seconds':180,
                'metric_parallelism':2,'max_concurrent_http_requests':2,
                'budget_scope':'仅本轮新增调用；已有成功分数继续复用，历史调用可能采用45/120秒串行预算',
                'state':'running',
                'scope':'五项指标逐题记录；库外拒答单独评审；无参考答案和Closed-book上下文指标明确N/A；不代替人工认证','rows':persisted}
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            def progress(name):
                print('Ragas指标：'+row['case']+' / '+row['variant']+' / '+name+' / '+str(item.get(name,item['metric_failures'].get(name))),flush=True)
                path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            await score_metric_jobs(jobs,item,progress)
            print('Ragas：'+row['case']+' / '+row['variant']+' / '+str(item.get('faithfulness',item.get('failure_type'))),flush=True)
    finally:await client.close()
    expected={(r['case'],r['variant']) for r in answers if cases[r['case']]['gold']}
    actual={(r['case'],r['variant']) for r in rows}
    passed=bool(rows) and len(rows)==len(expected) and actual==expected and all(not r['metric_failures'] for r in rows)
    names=('faithfulness','answer_relevancy','context_precision','context_recall','factual_correctness')
    report['ragas']['coverage']={name:{'scored':sum(name in r for r in rows),
        'applicable':sum(name not in r['not_applicable'] for r in rows)} for name in names}
    report['ragas']['state']='completed';report['ragas']['rows']=rows
    report['ragas']['answer_coverage']={'scored':len(actual),'expected':len(expected),'duplicate_rows':len(rows)-len(actual)}
    report['ragas']['execution_exclusions']=[{'case':r['case'],'variant':r['variant'],
        'reason':'执行失败、不可用状态或没有非空回答'} for r in answers
        if cases[r['case']]['gold'] and not has_evaluable_answer(r)]
    report['acceptance']['ragas']='all_applicable_metrics_completed' if passed else 'partial_metric_failure'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return passed


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('report',nargs='?');args=parser.parse_args()
    path=Path(args.report) if args.report else ROOT/json.loads((ROOT/'data/processed/prd-acceptance/latest.json').read_text())['report']
    raise SystemExit(0 if asyncio.run(run(path)) else 1)
