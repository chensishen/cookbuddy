"""冻结小样本后验证真实首轮规划；不执行菜谱工具，不改写正式验收。"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import time

from check_embedding import ROOT
from recipe_agent import call_model,SYSTEM,MODEL
from search_filters import validate_filters,ingredient_terms
from service_runtime import request_budget
from usage_ledger import summarize

CASES=ROOT/'data/evaluation/agent-filter-planning-v1.json'


def score(case,message):
    result={'schema_valid':False,'filters_exact':False,'question_exact':False,'passed':False}
    try:
        calls=message.get('tool_calls',[])
        if len(calls)!=1 or calls[0]['function']['name']!='query_recipes':return result
        args=json.loads(calls[0]['function']['arguments'])
        if not isinstance(args,dict) or set(args)-{'question','category','max_difficulty','ingredients_any','ingredients_all'}:return result
        if not isinstance(args.get('question'),str) or not args['question'].strip() or len(args['question'])>2000:return result
        filters={key:value for key,value in args.items() if key!='question'}
        if any(value is None for value in filters.values()):return result
        validate_filters(**filters)
        filters={key:ingredient_terms(value) if key.startswith('ingredients_') else value for key,value in filters.items()}
        expected={key:ingredient_terms(value) if key.startswith('ingredients_') else value for key,value in case['expected_filters'].items()}
        result.update(schema_valid=True,filters_exact=filters==expected,question_exact=args['question']==case['question'])
        result['passed']=all(result[key] for key in ('schema_valid','filters_exact','question_exact'))
    except (ValueError,TypeError,KeyError,IndexError,AttributeError):pass
    return result


def code_hash():
    digest=hashlib.sha256()
    for path in sorted((ROOT/'scripts').rglob('*.py')):
        digest.update(path.relative_to(ROOT).as_posix().encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()


def evaluate(out,fixture=None,model_call=None,interval=5):
    if interval<0:raise ValueError('调用间隔不能为负')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    fixture=json.loads(CASES.read_text()) if fixture is None else fixture
    raw=json.dumps(fixture,ensure_ascii=False,sort_keys=True).encode()
    (out/'cases.json').write_bytes(raw)
    initial=code_hash()
    report={'fixture':fixture['id'],'model':MODEL,'scope':fixture['scope'],'code_sha256':initial,
        'cases_sha256':hashlib.sha256(raw).hexdigest(),'started_at':datetime.now(timezone.utc).isoformat(),
        'interval_seconds':interval,'interval_is_account_quota':False,'tool_execution':False,
        'rows':[],'status':'running'}
    def save():
        report['metrics']={key:{'passed':sum(bool(row.get(key)) for row in report['rows']),
            'total':len(fixture['cases'])} for key in ('schema_valid','filters_exact','question_exact','passed')}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save();stopped=False
    ledger=out/'usage.jsonl';previous=os.environ.get('RECIPE_USAGE_LEDGER')
    os.environ['RECIPE_USAGE_LEDGER']=str(ledger.resolve())
    try:
        for index,case in enumerate(fixture['cases']):
            if stopped:
                report['rows'].append({'id':case['id'],'status':'not_run_after_provider_failure','passed':False});save();continue
            if index and interval:time.sleep(interval)
            started=time.monotonic()
            try:
                with request_budget(60):
                    message,metadata=(model_call or call_model)([{'role':'system','content':SYSTEM},{'role':'user','content':case['question']}])
                row={'id':case['id'],'status':'scored','message':message,'metadata':metadata,**score(case,message)}
            except (ValueError,RuntimeError,OSError,TimeoutError) as error:
                row={'id':case['id'],'status':'provider_or_planning_failure','error_type':type(error).__name__,'passed':False}
                stopped=True
            row['seconds']=round(time.monotonic()-started,3)
            report['rows'].append(row);save()
            print(case['id']+': '+('passed' if row['passed'] else row['status']),flush=True)
    finally:
        if previous is None:os.environ.pop('RECIPE_USAGE_LEDGER',None)
        else:os.environ['RECIPE_USAGE_LEDGER']=previous
        report['code_sha256_end']=code_hash()
        report['usage']=summarize(ledger)
        report['status']='partial' if stopped else 'completed'
        report['code_unchanged']=initial==report['code_sha256_end']
        report['completed_at']=datetime.now(timezone.utc).isoformat();save()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path)
    parser.add_argument('--cases',type=Path,default=CASES)
    args=parser.parse_args()
    out=args.out or ROOT/'data/processed/prd-acceptance'/('agent-filter-planning-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    report=evaluate(out,fixture=json.loads(args.cases.read_text()))
    print('报告：'+str(out/'report.json'))
    return 0 if report['code_unchanged'] and report['metrics']['passed']['passed']==report['metrics']['passed']['total'] else 2


if __name__=='__main__':raise SystemExit(main())
