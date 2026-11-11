"""Isolated, measured memory ablations. Never enables unsafe policies in the app."""
import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from cookbuddy.ltm.models import settings,Candidate,CONFIG_OVERRIDE
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.extractor import extract
from cookbuddy.ltm.guard import policy,source_allowed
from cookbuddy.task_context import advance_task
from cookbuddy.session import SessionState,append_message,resolve_reference,build_context,tokens
from preference_recommendation import recommend_preferences
from recipe_tools import load_chunks


def percentile(values,p):return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None

def evaluate(level,scope='on',confirmation='on',hard_mode='code_enforced'):
    cases=[json.loads(line) for line in (Path(__file__).parent/'memory_dialogs_v1.jsonl').read_text().splitlines()]
    n=int(level[1]);rows=[];tp=fp=fn=correct=decisions=miswrites=negative=0;latencies=[];extra=[]
    with tempfile.TemporaryDirectory() as tmp:
        store=ProfileStore(path=Path(tmp)/'eval.db')
        for index,case in enumerate(cases):
            uid='case'+str(index);state=SessionState(uid,'s');got=[];baseline={}
            for turnno,turn in enumerate(case['turns']):
                role=turn.get('role','user');user=turn['user'];sid=turn.get('session','s')
                started=time.perf_counter();current=[]
                candidates=extract({'role':role,'content':user})
                predicted={(c.dimension,c.value,c.scope,c.op) for c in candidates};gold={tuple(x) for x in turn['expected']}
                tp+=len(predicted&gold);fp+=len(predicted-gold);fn+=len(gold-predicted)
                no_long_write=not turn.get('confirmation') and all(x[2]=='session' for x in gold)
                if no_long_write:negative+=1
                before=[(p.id,p.status) for p in store.prefs(uid)] if n!=3 else list(baseline.values())
                if n>=1:append_message(state,role if role in ('user','assistant') else 'user',user)
                if n>=3:
                    confirmed=store.confirm(uid,sid,user) if n>=5 and confirmation=='on' else None
                    if confirmed:current.append(confirmed['decision'])
                    for c in candidates:
                        if scope=='off':c=replace(c,scope='global',valid_to=None)
                        if n==3:
                            # Deliberately naive baseline, isolated from live code:
                            # direct last-write profile, no conflict/version policy.
                            key=c.dimension if c.dimension in {'spice_level','time_budget','household_size','diet_type','max_difficulty'} else (c.dimension,c.value)
                            if c.op=='revoke':baseline.pop(key,None);current.append('DELETE')
                            elif c.scope=='session':current.append('SESSION_ONLY')
                            else:baseline[key]=(c.dimension,c.value,'active');current.append('ADD')
                        else:
                            if n==4 and c.source=='implicit':c=replace(c,source='explicit')
                            result=store.apply(uid,sid,sid+':'+str(turnno),c)
                            if result['decision']=='ASK' and (n<5 or confirmation=='off'):
                                result=store.confirm(uid,sid,'确认')
                            current.append(result['decision'])
                got.extend(current);decisions+=1;correct+=current==turn['decisions']
                after=[(p.id,p.status) for p in store.prefs(uid)] if n!=3 else list(baseline.values())
                if no_long_write and before!=after:miswrites+=1
                latencies.append((time.perf_counter()-started)*1000)
                extra.append(max(0,tokens(build_context(state,user))-tokens(build_context(SessionState(uid,'s'),user))) if n else 0)
            profile=sorted(baseline.values()) if n==3 else sorted((p.dimension,p.value,p.status) for p in store.prefs(uid))
            rows.append({'id':case['id'],'decisions':got,'final_profile_correct':profile==sorted(tuple(x) for x in case['profile'])})
        # These are actual corpus recommendations, independently rechecked.
        hard=[{'dimension':'allergy','value':'花生','polarity':'avoid','strength':'hard'}]
        task=advance_task('我有鸡肉，推荐3道辣菜',None)['state'];tested=violations=0
        with policy(hard if n>=3 and hard_mode=='code_enforced' else []):
            data=recommend_preferences(task,load_chunks())
        for r in data.get('recommendations',[]):tested+=1;violations+=not source_allowed(r['source'],hard)
        refs=SessionState('ref','s');refs.slots['last_recommendations']=[{'idx':i,'dish_name':name,'source_path':'source'+str(i)} for i,name in enumerate(['菜一','菜二','菜三'],1)]
        resolved=resolve_reference(refs,'第二个怎么做') if n>=2 else None
    return {'level':level,'scope_detection':scope,'confirm_relax':confirmation,'hard_filter_mode':hard_mode,
            'metrics':{'update_correctness':correct/decisions,'final_profile_correctness':sum(r['final_profile_correct'] for r in rows)/len(rows),
            'miswrite_rate':miswrites/negative if negative else 0,'hard_constraint_violation_rate':violations/tested if tested else None,
            'hard_dishes_checked':tested,'coreference_accuracy':float(resolved is not None and resolved['idx']==2),
            'extraction_precision':tp/(tp+fp) if tp+fp else None,'extraction_recall':tp/(tp+fn) if tp+fn else None,
            'context_token_upper_bound_mean':statistics.mean(extra),'local_memory_p95_ms':percentile(latencies,.95)},'cases':rows}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--all',action='store_true');parser.add_argument('--config',type=Path);args=parser.parse_args()
    cfg=json.loads(args.config.read_text()) if args.config else settings();exp=cfg['experiments']
    CONFIG_OVERRIDE.set(cfg)
    levels=['M'+str(i) for i in range(7)] if args.all else [exp['level']]
    results=[evaluate(level,exp['scope_detection'],exp['confirm_relax'],exp['hard_filter_mode']) for level in levels]
    baseline=next((r for r in results if r['level']=='M0'),evaluate('M0'))
    for row in results:row['metrics']['local_p95_latency_increment_ms']=row['metrics']['local_memory_p95_ms']-baseline['metrics']['local_memory_p95_ms']
    if args.all:
        results.extend(evaluate('M6',scope,confirm,hard) for scope,confirm,hard in [('off','on','code_enforced'),('on','off','code_enforced'),('on','on','prompt_only')])
    try:commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,stderr=subprocess.DEVNULL,text=True).strip()
    except subprocess.CalledProcessError:commit=None
    run=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');folder=Path(__file__).parent/'reports'/run;folder.mkdir(parents=True)
    report={'run_id':run,'commit':commit,'commit_note':'仓库尚无提交时 commit 为 null；另存代码哈希',
            'code_sha256':hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'scripts').rglob('*.py')))).hexdigest(),
            'dataset_sha256':hashlib.sha256((Path(__file__).parent/'memory_dialogs_v1.jsonl').read_bytes()).hexdigest(),
            'configuration':cfg,'scope':'15 个自建确定性诊断对话；SQLite 本地时间；不含模型/网络延迟。token 为保守上界，不是供应商计费。非独立盲测。',
            'results':results}
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    md='# 记忆诊断评测\n\n'+report['scope']+'\n\n| 配置 | 更新正确率 | 最终画像正确率 | 硬约束违反率 | 指代正确率 | 提取 P/R |\n|---|---:|---:|---:|---:|---|\n'
    for row in results:
        m=row['metrics'];label=row['level']+'/'+row['scope_detection']+'/'+row['confirm_relax']+'/'+row['hard_filter_mode']
        md+=f"| {label} | {m['update_correctness']:.3f} | {m['final_profile_correctness']:.3f} | {m['hard_constraint_violation_rate']} | {m['coreference_accuracy']:.3f} | {m['extraction_precision']:.3f}/{m['extraction_recall']:.3f} |\n"
    (folder/'report.md').write_text(md)
    (Path(__file__).parent/'reports/latest.json').write_text(json.dumps({'report':str(folder.relative_to(ROOT)/'report.json'),'markdown':str(folder.relative_to(ROOT)/'report.md')},indent=2)+'\n')
    print('memory_evaluation_report',str(folder.relative_to(ROOT)/'report.md'))
    full=next((r for r in results if r['level']=='M6' and r['scope_detection']=='on' and r['confirm_relax']=='on' and r['hard_filter_mode']=='code_enforced'),None)
    if full and (full['metrics']['update_correctness']<1 or full['metrics']['final_profile_correctness']<1 or full['metrics']['hard_constraint_violation_rate']!=0):return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
