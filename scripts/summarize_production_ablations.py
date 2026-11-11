"""固定题目分母汇总扩展层，分开执行、语义质量和组件激活。"""
import argparse
import hashlib
import json
from pathlib import Path

from check_embedding import ROOT
from eval_judge import has_evaluable_answer
from production_ablation_runtime import PROFILES


def summarize(path):
    report=json.loads(path.read_text());fixture=json.loads((ROOT/report['fixture_path']).read_text())
    expected={(case['id'],variant) for case in fixture['cases'] for variant in PROFILES}
    actual=[(row['case'],row['variant']) for row in report['answers']]
    result={'scope':report['scope'],'generation_code_unchanged':report.get('code_unchanged'),
        'report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'attempt_coverage':{'expected':len(expected),'observed':len(actual),'missing':sorted(expected-set(actual)),
            'duplicates':len(actual)-len(set(actual))},'profiles':{},'production_sla':None,'human_certification':None}
    for variant in PROFILES:
        rows=[row for row in report['answers'] if row['variant']==variant]
        correct=lambda row:has_evaluable_answer(row) and row.get('llm_judge',{}).get('scores',{}).get('correct') is True and row.get('llm_judge',{}).get('scores',{}).get('complete') is True
        result['profiles'][variant]={**report['answer_aggregates'][variant],
            'judge_passed':sum(correct(row) for row in rows),'denominator':len(fixture['cases']),
            'judge_scored':sum(has_evaluable_answer(row) and bool(row.get('llm_judge')) for row in rows),
            'judge_missing':[row['case'] for row in rows if has_evaluable_answer(row) and not row.get('llm_judge')],
            'source_fact_cases':[row['case'] for row in rows if row.get('activation',{}).get('source_fact_matched')],
            'retrieval_cases':[row['case'] for row in rows if row.get('activation',{}).get('retrieval_traversed')],
            'failures':[{'case':row['case'],'type':row.get('failure_type'),'status':row.get('result',{}).get('status')}
                for row in rows if not has_evaluable_answer(row)],
            'inexact_evidence':[row['case'] for row in rows if row.get('evidence_check',{}).get('all_exact') is False]}
    base=result['profiles']['E9']
    for variant,stats in result['profiles'].items():
        stats['judge_pass_delta_points_vs_E9']=(stats['judge_passed']-base['judge_passed'])/len(fixture['cases'])*100
    core_path=ROOT/report['core_report'];core=json.loads(core_path.read_text())
    result['core']={'path':report['core_report'],'sha256':hashlib.sha256(core_path.read_bytes()).hexdigest(),
        'attempts':len(core['answers']),'matrix_complete':core.get('matrix_complete'),
        'execution_failures':sum('failure_type' in row for row in core['answers']),
        'production_equivalence':core.get('production_equivalence')}
    result['expanded_execution_complete']=set(actual)==expected and len(actual)==len(expected)
    result['expanded_all_evaluable_scored']=all(not row['judge_missing'] for row in result['profiles'].values())
    result['all_components_certified']=False
    result['reason']='受控核心仍有历史执行失败；生产层为6题开发诊断，原文直答绕过的组件不具可识别效果，不代表完整大样本认证。'
    out=path.parent/'expanded-analysis.json';out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    lines=['# 扩展生产消融结果','',result['scope'],'',
        '| 方案 | Judge正确且完整/固定分母 | 可评回答已评分 | 原文直答命中 | 检索实际执行 | 执行失败 | 成功P95秒 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for variant,row in result['profiles'].items():
        p95=row.get('successful_p95_seconds');p95=f'{p95:.3f}' if p95 is not None else 'N/A'
        lines.append(f"| {variant} | {row['judge_passed']}/{row['denominator']} | {row['judge_scored']} | {row['source_fact_matched']} | {row['retrieval_traversed']} | {len(row['failures'])} | {p95} |")
    lines.extend(['',result['reason'],'','E8→E9包含切块、生成/复核与快捷路径等生产层变化，不能单独归因。P-A7仅关闭source_fact；保留库存覆盖率及后续生成器的原文降级。',
        '','逐题原始输出、真实阶段、引用、评审及失败见同目录report.json。所有已消费题不作为新盲测；失败/未评分保留固定分母。'])
    out.with_suffix('.md').write_text('\n'.join(lines)+'\n');return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('report',type=Path);summarize(parser.parse_args().report)
