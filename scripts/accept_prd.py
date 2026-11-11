"""把评测执行结果转换为 PRD 门槛判定；缺证据不视为通过。"""
import argparse
import json
import math
from pathlib import Path

from check_embedding import ROOT
from eval_judge import execution_completed


def acceptance_matrix(report):
    checks=[]
    def add(name, value, evidence):
        checks.append({'criterion':name,'status':'pending' if value is None else 'passed' if value else 'failed',
                       'evidence':evidence})
    def threshold(value, minimum):
        return value>=minimum if type(value) in (int,float) and math.isfinite(value) else None
    corpus=report.get('corpus',{})
    add('溯源可还原率100%',corpus.get('passed') if corpus.get('sources',0)>0 else None,
        {'passed_sources':corpus.get('passed_sources'),'sources':corpus.get('sources')})
    full=report.get('aggregates',{}).get('hybrid_rerank',{}).get('metrics',{}).get('hit') or {}
    add('完整系统Hit@5≥90%',threshold(full.get('mean'),.9) if full.get('n',0)>0 else None,full)
    significance=report.get('rerank_significance')
    coverage=report.get('rerank_comparison_coverage',{})
    complete=coverage.get('expected',0)>0 and coverage.get('paired')==coverage['expected']
    policy=report.get('rerank_policy_effect')
    if policy:
        test=policy.get('test')
        add('重排策略MRR显著提升且查询覆盖完整',bool(test.get('significant') and test.get('mean_delta',0)>0 and policy.get('complete'))
            if test else None,policy)
    else:
        add('重排MRR显著提升且覆盖完整',bool(significance.get('significant') and significance.get('mean_delta',0)>0 and complete)
            if significance else None,{'test':significance,'coverage':coverage})
    answers=[r for r in report.get('answers',[]) if r['variant']=='full_agent']
    usable={'verified','verified_source','insufficient'}
    add('完整系统执行无失败或降级',all(execution_completed(r) and r.get('result',{}).get('status') in usable
        and bool(r['result'].get('short_answer','').strip()) for r in answers) if answers else None,
        {'samples':len(answers),'retrieval_failures':len(report.get('failures',[]))})
    if report.get('failures'):checks[-1]['status']='failed'
    refusals=[r for r in answers if r.get('refusal_correct') is not None]
    rate=sum(r['refusal_correct'] is True for r in refusals)/len(refusals) if refusals else None
    add('库外拒答正确率≥90%',threshold(rate,.9),{'rate':rate,'n':len(refusals)})
    acceptance=report.get('acceptance',{})
    citation=report.get('citation_metadata',{})
    add('元数据引用准确率≥98%',threshold(citation.get('rate'),.98) if citation.get('count',0)>0 else None,citation)
    add('独立测试集',True if acceptance.get('held_out_test') is True and report.get('split')=='test' else None,
        {'split':report.get('split'),'scope':report.get('fixture_scope')})
    manifest=report.get('configuration',{}).get('naive_512_manifest') or {}
    naive=[r for r in report.get('answers',[]) if r['variant']=='naive_rag']
    baseline=manifest.get('state')=='ready' and bool(naive) and all(r.get('baseline_strategy')=='fixed512' for r in naive)
    add('同源固定512-token稠密基线',baseline if manifest else None,report.get('configuration',{}).get('baseline_scope'))
    add('完整增量及留一法消融',report.get('ablation',{}).get('complete'),report.get('ablation',{}))
    calibration=report.get('human_calibration',{})
    add('人工盲评与judge一致性校准',calibration.get('calibrated'),calibration or '待两位独立评审填写盲评包')
    ragas=report.get('ragas',{});metric_coverage=ragas.get('coverage',{})
    all_metrics=('faithfulness','answer_relevancy','context_precision','context_recall','factual_correctness')
    answer_coverage=ragas.get('answer_coverage',{})
    add('Ragas全部PRD指标',answer_coverage.get('scored',0)==answer_coverage.get('expected') and all(metric_coverage.get(name,{}).get('applicable',0)>0 and
        metric_coverage[name]['scored']==metric_coverage[name]['applicable'] for name in all_metrics)
        if metric_coverage else None,{'coverage':metric_coverage,'status':acceptance.get('ragas','not_run')})
    add('完整成本与收益可接受',acceptance.get('cost_acceptability'),report.get('price_status','未核算'))
    improvements=[r for r in report.get('improvements',[]) if r['from']=='naive_rag' and r['to']=='full_agent']
    delta=improvements[0].get('p95_delta_seconds') if improvements else None
    timing=report.get('configuration',{}).get('baseline_latency_scope','legacy_unspecified')
    comparable=not timing.startswith('generation_only')
    add('样本P95延迟增量≤1.5秒',delta<=1.5 if comparable and type(delta) in (int,float) and math.isfinite(delta) else None,
        {'p95_delta_seconds':delta,'baseline_latency_scope':timing,
            'scope':'小样本全部尝试；generation_only旧协议不用于端到端门槛；不是生产SLA或首token验收'})
    add('评测期间代码未变化',not report['code_changed_during_run'] if 'code_changed_during_run' in report else None,
        {'start':report.get('code_sha256'),'end':report.get('code_sha256_end')})
    return {'overall':'passed' if all(r['status']=='passed' for r in checks) else 'partial',
            'counts':{state:sum(r['status']==state for r in checks) for state in ('passed','failed','pending')},
            'checks':checks}


def write_acceptance(path):
    report=json.loads(path.read_text())
    result=acceptance_matrix(report)
    result['report']=str(path.resolve())
    path.with_name('acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    lines=['# PRD门槛验收','',f"结论：{result['overall']}；{result['counts']}。缺少证据的项目保持待验收。",'',
           '| 门槛 | 状态 |','|---|---|']
    labels={'passed':'通过','failed':'未达标','pending':'待验收'}
    lines.extend(f"| {r['criterion']} | {labels[r['status']]} |" for r in result['checks'])
    lines.extend(['','原始证据和样本数见同目录 acceptance.json 与 report.json。'])
    path.with_name('acceptance.md').write_text('\n'.join(lines)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('report',nargs='?',type=Path)
    parser.add_argument('--strict',action='store_true',help='任一未达标或待验收项使退出码为2')
    args=parser.parse_args()
    path=args.report or ROOT/json.loads((ROOT/'data/processed/prd-acceptance/latest.json').read_text())['report']
    result=write_acceptance(path)
    print(json.dumps({'overall':result['overall'],**result['counts']},ensure_ascii=False))
    raise SystemExit(2 if args.strict and result['overall']!='passed' else 0)
