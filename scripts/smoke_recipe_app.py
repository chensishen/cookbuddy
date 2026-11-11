"""通过本地网页接口复查原文、图片、RAG 与 Agent 五个固定场景。"""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from check_embedding import ROOT

BASE = 'http://127.0.0.1:8080'
CASES = [
    ('原文', {'question':'查看蒸水蛋操作原文','mode':'recipe','title':'蒸水蛋','section':'操作'}, 'read_recipe'),
    ('本地图片', {'question':'查看基础牛奶面包酵头图片','mode':'images','title':'基础牛奶面包','section':'酵头'}, 'find_recipe_images'),
    ('原文外链', {'question':'查看完美水煮蛋图片','mode':'images','title':'完美水煮蛋','section':''}, 'find_recipe_images'),
    ('菜谱问答', {'question':'酸辣土豆丝原文需要哪些调料？','mode':'rag'}, 'query_recipes'),
    ('Agent选图', {'question':'请给我看基础牛奶面包酵头的图片','mode':'agent'}, 'find_recipe_images'),
]


def request_json(path, payload=None):
    request = Request(BASE + path,
                      data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                      headers={'Content-Type':'application/json'} if payload is not None else {})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def evidence_matches_source(evidence):
    for item in evidence:
        source = (ROOT / item['source']).resolve()
        source.relative_to((ROOT / 'data/HowToCook/dishes').resolve())
        lines = source.read_text(encoding='utf-8').splitlines()
        if item['quote'] != '\n'.join(lines[item['start_line']-1:item['end_line']]):
            return False
    return True


def run_case(name, payload, expected_tool):
    created = request_json('/api/ask', payload)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        job = request_json('/api/job?' + urlencode({'id':created['job_id']}))
        if job['state'] in ('done','failed','cancelled','expired'):
            break
        time.sleep(1)
    else:
        return {'case':name,'passed':False,'reason':'页面任务超过240秒仍未结束'}
    if job['state']!='done':
        return {'case':name,'passed':False,'reason':job['progress']}
    run = job['result']
    outputs = [item for item in run['outputs'] if item['tool']==expected_tool]
    if not outputs:
        return {'case':name,'passed':False,'status':run['status'],'record':job['record'],
                'reason':'未调用预期工具'}
    result = outputs[0]['result']
    source_ok = evidence_matches_source(result.get('evidence',[]))
    paths_ok = all((ROOT / image['image_path']).is_file() for image in result.get('images',[]))
    expected = {'原文':result['status']=='verified_source' and result.get('complete') is True,
                '本地图片':result['status']=='verified_source' and len(result.get('images',[]))==3,
                '原文外链':result['status']=='insufficient' and not result.get('images')
                        and len(result.get('remote_links',[]))==1
                        and result['remote_links'][0]['status']=='not_downloaded',
                '菜谱问答':result['status']=='verified' and bool(result.get('sources')),
                'Agent选图':result['status']=='verified_source' and len(result.get('images',[]))==3}[name]
    return {'case':name,'passed':bool(expected and source_ok and paths_ok),
            'status':run['status'],'tool_status':result['status'],'record':job['record'],
            'evidence_count':len(result.get('evidence',[])),
            'image_count':len(result.get('images',[])),
            'remote_link_count':len(result.get('remote_links',[])),
            'source_quotes_match':source_ok,'local_images_exist':paths_ok}


def main():
    status = request_json('/api/status')
    results = [run_case(*case) for case in CASES]
    record = {'created_at':datetime.now(timezone.utc).isoformat(),
              'app_status':status['app'],'recipe_count':status['recipe_count'],
              'image_count':status['image_count'],'cases':results,
              'passed':sum(item['passed'] for item in results),'total':len(results)}
    path = ROOT / 'data/evaluation/app-acceptance-v1/summary.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for item in results:
        print(f"{item['case']}：{'通过' if item['passed'] else '待处理'}；{item.get('status',item.get('reason',''))}")
    print('验收记录：'+str(path))
    return 0 if record['passed']==record['total'] else 2


if __name__=='__main__':
    sys.exit(main())
