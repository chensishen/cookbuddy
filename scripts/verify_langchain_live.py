"""Live local integration checks. Rules are engineering checks, not human judging."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import re
import time
import urllib.request


def run(base,out):
    out.mkdir(parents=True,exist_ok=False)
    client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),urllib.request.ProxyHandler({}))
    def post(path,data):
        return client.open(urllib.request.Request(base+path,data=json.dumps(data,ensure_ascii=False).encode(),
            headers={'Content-Type':'application/json'}),timeout=45).read().decode()
    session=json.loads(post('/sessions',{}))['session_id']
    report={'scope':'real local API/tool/provider integration; rule checks only, not blinded quality or production SLA',
            'url':base,'session_id':session,'records':[]}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for ident,question in [('inventory','我有番茄和鸡蛋，推荐两道菜'),('ordinal','第二个怎么做'),
        ('alternative','再来一个'),('bundle','查看基础牛奶面包的原文和图片'),
        ('unknown','星际量子炖龙肉怎么做？'),('remember','记住我对花生过敏'),('guard','查看宫保鸡丁原文')]:
        start=time.monotonic()
        raw=post('/chat',{'session_id':session,'message':question});events=[]
        for frame in raw.split('\n\n'):
            lines=frame.splitlines()
            event=next((r[7:] for r in lines if r.startswith('event: ')),None)
            data=next((json.loads(r[6:]) for r in lines if r.startswith('data: ')),None)
            if event:events.append({'event':event,'data':data})
        text=''.join(r['data'].get('text','') for r in events if r['event']=='token')
        refs=next((r['data'].get('items',[]) for r in events if r['event']=='citations'),[])
        images=next((r['data'].get('images',[]) for r in events if r['event']=='citations'),[])
        done=next((r['data'] for r in events if r['event']=='done'),None)
        checks={'done_last':bool(events) and events[-1]['event']=='done',
                'no_error':not any(r['event']=='error' for r in events),
                'not_degraded':done is not None and not done['degraded']}
        if ident=='inventory':checks['coverage_and_missing']='覆盖率' in text and '盐' in text and '食用油' in text
        if ident=='ordinal':
            original=report['records'][0]['sources']
            checks['second_source_selected']=len(original)>=2 and bool(refs) and all(r['source']==original[1]['source'] for r in refs)
        if ident=='alternative':checks['previous_sources_excluded']=bool(refs) and not ({r['source'] for r in refs}&{r['source'] for r in report['records'][0]['sources']})
        if ident=='bundle':
            tools={r['data']['name'] for r in events if r['event']=='tool'}
            checks['two_tools']={'get_recipe_detail','find_recipe_images'}<=tools
            checks['local_image_available']=bool(images) and client.open(base+'/media?path='+urllib.parse.quote(images[0]['image_path']),timeout=5).status==200
        if ident=='unknown':checks['explicit_insufficiency']=bool(re.search('知识库|未收录|没有找到|资料不足|未找到',text))
        if ident=='remember':checks['memory_acknowledged']=bool(re.search('记忆|偏好|记住',text))
        if ident=='guard':checks['no_forbidden_body']=not refs and '限制' in text and '原料' not in text
        record={'id':ident,'question':question,'events':events,'text':text,'sources':refs,
                'seconds':time.monotonic()-start,'checks':checks,'passed':all(checks.values())}
        report['records'].append(record);save()
        print(ident+': '+('passed' if record['passed'] else 'failed'),flush=True)
    report['passed']=all(r['passed'] for r in report['records']);save()
    return 0 if report['passed'] else 2


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='http://127.0.0.1:8082')
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();raise SystemExit(run(a.url.rstrip('/'),a.out.resolve()))
