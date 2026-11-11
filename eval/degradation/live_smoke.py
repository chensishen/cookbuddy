"""Isolated live API smoke; records observed results without assuming model health."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[2]


def run(out):
    client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),urllib.request.ProxyHandler({}))
    def request(path,payload=None):
        req=urllib.request.Request('http://127.0.0.1:8080'+path,data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,
            headers={'Content-Type':'application/json'} if payload is not None else {})
        with client.open(req,timeout=15) as response:return json.load(response)
    records=[]
    groups=[ [('帮我安排早餐。','breakfast'),('换一套，不要刚才的。','breakfast')],
        [('帮我安排午餐，两个人，60分钟。','lunch'),('换一套。','lunch')],
        [('帮我安排晚餐。','dinner')], [('帮我安排宵夜。','snack_late')],
        [('安排早参','breakfast')],
        [('查看基础牛奶面包的完整原文和图片。','source'),('只看酵头制作的图片。','images'),
         ('酵头需要多少酵母、水温多少、发酵多久？请区分静置时间和发酵时间，并给出依据。','facts'),
         ('面包的烘烤温度和时间是多少？原文有没有根据烤箱表现调整的方法？','facts')]]
    for group in groups:
        cid=request('/api/conversations',{'title':'降级菜单隔离验收'})['id'];previous=set()
        for question,expected in group:
            start=time.monotonic();row={'question':question,'expected':expected}
            try:
                job=request('/api/ask',{'mode':'auto','conversation_id':cid,'question':question})['job_id']
                while True:
                    state=request('/api/job?id='+job)
                    if state['state'] in ('done','failed','cancelled','expired'):break
                    if time.monotonic()-start>120:raise TimeoutError()
                    time.sleep(.5)
                result=state.get('result') or {};outputs=result.get('outputs',[])
                data=[o['result'] for o in outputs];menus=[d for d in data if d.get('meal_plan')]
                rows=[r for d in data for r in d.get('recommendations',[])];sources={r['source'] for r in rows}
                evidence=[r for d in data for r in d.get('evidence',[])]
                checks={'done':state['state']=='done','elapsed':result.get('elapsed_seconds',0)>0,
                    'request_id':result.get('request_id')==job,'notice_accurate':not result.get('service_notice') or result.get('quality_limited')}
                if expected in ('breakfast','lunch','dinner','snack_late'):
                    checks['meal_type']=bool(menus) and menus[0]['meal_plan']['meal_type']==expected
                    checks['source_backed']=bool(evidence) and bool(rows)
                    if previous:checks['changed']=bool(sources-previous)
                    previous=sources
                elif expected=='images':
                    images=[i for d in data for i in d.get('images',[])]
                    checks['chapter_images']=bool(images) and all('酵头' in i.get('section','') for i in images)
                else:checks['source_backed']=bool(evidence)
                checks['quotes_exact']=all('\n'.join((ROOT/r['source']).read_text().splitlines()[r['start_line']-1:r['end_line']])==r['quote'] for r in evidence)
                row.update(checks=checks,passed=all(checks.values()),result=result,state=state['state'])
            except Exception as error:row.update(passed=False,error=type(error).__name__)
            row['wall_seconds']=round(time.monotonic()-start,3);records.append(row)
            out.write_text(json.dumps({'scope':'live configured APIs, isolated identity; assertions are structural, factual prose needs review','records':records},ensure_ascii=False,indent=2))
            print(expected, row['passed'],row.get('checks',{}),flush=True)
    return all(r['passed'] for r in records)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);args=parser.parse_args()
    if args.out.exists():raise SystemExit('refuse_to_overwrite_report')
    raise SystemExit(0 if run(args.out) else 1)
