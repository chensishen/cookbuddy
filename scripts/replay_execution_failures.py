"""重放已消费正式失败题；单独记录修复回归，不替换封存成绩。"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

from acceptance_closeout import exact_evidence,citation_metadata
from check_embedding import ROOT
from evaluate_prd import code_fingerprint,collect_usage,policy
from eval_judge import execution_completed
from llm_config import llm_settings
from recipe_agent import execute
from recipe_tools import load_chunks
from service_runtime import RuntimeStore
from usage_ledger import summarize


def replay(out,original):
    out=out.resolve();out.mkdir(parents=True,exist_ok=False)
    old=json.loads(original.read_text());fixture=ROOT/old['fixture_path'];data=json.loads(fixture.read_text())
    cases={c['id']:c for c in data['cases']};chunks=load_chunks()
    for source,digest in data['source_hashes'].items():
        if hashlib.sha256((ROOT/source).read_bytes()).hexdigest()!=digest:raise ValueError('历史参考来源已变化')
    selected=[row for row in old['answers'] if row['variant']=='full_agent' and not execution_completed(row)]
    settings=llm_settings();code=code_fingerprint()
    report={'schema_version':1,'scope':'已消费正式失败题的当前生产修复回归，不是新封存测试，不替换原成绩；每题只运行一次，失败不删除。',
        'original_report':str(original.relative_to(ROOT)),'fixture_path':str(fixture.relative_to(ROOT)),
        'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'code_sha256':code,
        'started_at':datetime.now(timezone.utc).isoformat(),'configuration':{
            'llm_model':settings['model'],'llm_base_url':settings['base_url'],'answer_cache':False,
            'runtime_isolated':False,'quota_policy':'沿用生产共享准入；未修改RPM/TPM、熔断或冷却以通过回归'},
        'answers':[],'historical_failures':[],'answer_aggregates':{},'status':'running',
        'acceptance':{'overall':'development_replay_only','cost_acceptability':None},'formal_acceptance_replaced':False}
    def save():
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (out/'report.md').write_text('# 正式失败题修复回归\n\n'+report['scope']+'\n')
    save();store=RuntimeStore()
    with policy(RECIPE_USAGE_LEDGER=str(out/'usage.jsonl'),RECIPE_ANSWER_CACHE='0'):
        for previous in selected:
            case=cases[previous['case']];old_record=previous.get('agent_record',{})
            report['historical_failures'].append({'case':case['id'],'status':old_record.get('status'),
                'stop_reason':old_record.get('stop_reason'),'error':old_record.get('error'),
                'failure_events':[{k:event.get(k) for k in ('phase','outcome','code','reason')}
                    for event in previous.get('phase_events',[]) if event.get('outcome') not in ('ok','cache_hit')]})
            row={'case':case['id'],'type':case['type'],'variant':'full_agent','semantic_review':'pending'}
            events=store.events_since();cursor=events[-1]['id'] if events else 0;started=time.monotonic()
            try:
                record=execute(case['question']);row['agent_record']=record
                outputs=[o['result'] for o in record.get('outputs',[])]
                result={'status':outputs[0]['status'] if len(outputs)==1 else record['status'],
                    'short_answer':'\n'.join(o.get('short_answer','') for o in outputs),
                    'evidence':[e for o in outputs for e in o.get('evidence',[])],'usage':collect_usage(record)}
                row.update(result=result,evidence_check=exact_evidence(result),citation_metadata=citation_metadata(result,chunks),
                    refusal_correct=(execution_completed(row) and record['status']=='insufficient' and bool(result['short_answer'].strip())) if not case['gold'] else None)
            except Exception as error:row['failure_type']=type(error).__name__
            row['seconds']=time.monotonic()-started;row['phase_events']=store.events_since(cursor)
            report['answers'].append(row);save()
            print(case['id']+' / '+row.get('agent_record',{}).get('status',row.get('failure_type','failed')),flush=True)
            time.sleep(1)
    rows=report['answers'];times=sorted(row['seconds'] for row in rows)
    report['answer_aggregates']['full_agent']={'n':len(rows),'p50_seconds':statistics.median(times),
        'p95_seconds':times[math.ceil(.95*len(times))-1]}
    report['code_sha256_end']=code_fingerprint();report['code_changed_during_run']=code!=report['code_sha256_end']
    report['usage_ledger']=summarize(out/'usage.jsonl');report['status']='completed';save()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--original',type=Path,default=ROOT/'data/processed/prd-acceptance/20261020T084856210072Z/report.json')
    args=parser.parse_args();replay(args.out,args.original.resolve())
