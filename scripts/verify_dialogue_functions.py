"""Isolated real-application dialogue checks; never a complete semantic judge."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]


def run(base,out,groups):
    suite=json.loads((ROOT/'eval/dialogue/full-function-prompts.json').read_text())
    out.mkdir(parents=True,exist_ok=False)
    report={'scope':suite['scope'],'records':[],'ui_and_fault_injection':'not_run'}
    def save():
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for group in suite['groups']:
        if groups and group['id'] not in groups:continue
        # A fresh cookie jar creates a disposable identity, never reuses Chrome.
        client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),urllib.request.ProxyHandler({}))
        def request(path,payload=None):
            req=urllib.request.Request(base+path,data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,
                headers={'Content-Type':'application/json'} if payload is not None else {})
            with client.open(req,timeout=15) as response:return json.load(response)
        cid=None;recommendations=[]
        for index,case in enumerate(group['turns'],1):
            ident=group['id']+str(index);start=time.monotonic()
            record={'id':ident,'question':case['q'],'semantic_review':'pending','checks':{}}
            try:
                if not cid or case.get('new_conversation'):
                    cid=request('/api/conversations',{'title':'隔离功能测试 '+ident})['id']
                job_id=request('/api/ask',{'mode':'auto','conversation_id':cid,'question':case['q']})['job_id']
                while True:
                    job=request('/api/job?'+urllib.parse.urlencode({'id':job_id}))
                    if job['state'] in ('done','failed','cancelled','expired'):break
                    if time.monotonic()-start>120:raise TimeoutError('job_not_completed_within_120s')
                    time.sleep(.5)
                result=job.get('result') or {};outputs=result.get('outputs',[])
                text=result.get('text') or '\n'.join(o['result'].get('short_answer') or o['result'].get('answer','') for o in outputs)
                sources=[s for o in outputs for s in o['result'].get('sources',[])]
                images=[s for o in outputs for s in o['result'].get('images',[])]
                rows=[s for o in outputs for s in o['result'].get('recommendations',[])]
                checks={'completed':job['state']=='done','not_degraded':not result.get('degraded',True),'nonempty':bool(text.strip()),
                        'elapsed_recorded':isinstance(result.get('elapsed_seconds'),(int,float))}
                for word in case.get('contains',[]):checks['contains:'+word]=word in text
                for word in case.get('absent',[]):checks['absent:'+word]=word not in text
                if 'any' in case:checks['expected_response']=bool(re.search(case['any'],text))
                if 'tools' in case:checks['tools']=set(case['tools'])<={o['tool'] for o in outputs}
                if 'count' in case:checks['recommendation_count']=len(rows)==case['count']
                if 'images' in case:checks['image_count']=len({i['image_path'] for i in images})==case['images']
                if case.get('no_sources'):checks['no_sources']=not sources
                if case.get('source_title'):checks['source_title']=bool(sources) and all(s.get('title')==case['source_title'] for s in sources)
                if 'same_as_recommendation' in case:
                    n=case['same_as_recommendation']-1
                    checks['selected_source']=len(recommendations)>n and bool(sources) and all(s['source']==recommendations[n] for s in sources)
                if rows:
                    new=[r.get('source',r.get('source_path','')) for r in rows]
                    if 'replace_index' in case:
                        n=case['replace_index']-1
                        checks['new_replacement']=len(new)==1 and new[0] not in recommendations
                        if len(recommendations)>n and new:recommendations[n]=new[0]
                    else:recommendations=new
                evidence=[e for o in outputs for e in o['result'].get('evidence',[])]
                valid=True
                for e in evidence:
                    path=(ROOT/e['source']).resolve();path.relative_to(ROOT/'data/HowToCook/dishes')
                    lines=path.read_text().splitlines()
                    valid=valid and e['quote']=='\n'.join(lines[e['start_line']-1:e['end_line']])
                checks['quotes_match_source']=valid
                record.update(job_id=job_id,conversation_id=cid,text=text,sources=sources,checks=checks,
                    trace=result.get('trace'),passed=all(checks.values()),result=result)
            except Exception as error:
                # No request headers, cookies or exception body enter the report.
                record.update(passed=False,error=type(error).__name__)
            record['seconds']=round(time.monotonic()-start,2)
            report['records'].append(record);save()
            failed=[k for k,v in record['checks'].items() if not v]
            print(ident+': '+('passed' if record['passed'] else 'FAILED '+','.join(failed or [record.get('error','unknown')])),flush=True)
    report['passed']=all(r['passed'] for r in report['records'])
    report['total']=len(report['records']);save()
    return 0 if report['passed'] else 2


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8080')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--groups',default='',help='Comma-separated group IDs; default all')
    args=parser.parse_args()
    raise SystemExit(run(args.url.rstrip('/'),args.out.resolve(),set(filter(None,args.groups.split(',')))))
