"""本机目录与来源选择验收，仅执行原文／图片工具，不请求远程模型。"""
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from check_embedding import ROOT
from rag_dataset import COLLECTION
from evaluate_quality_batch import evidence_checks

BASE = 'http://127.0.0.1:8080'
OUT = ROOT / 'data/processed/catalog-acceptance'


def request(path, payload=None):
    headers = {}
    if os.environ.get('RECIPE_ACCESS_TOKEN'):
        headers['X-Recipe-Token'] = os.environ['RECIPE_ACCESS_TOKEN']
    data = None
    if payload is not None:
        headers['Content-Type']='application/json'
        data=json.dumps(payload,ensure_ascii=False).encode()
    try:
        with urlopen(Request(BASE+path,data=data,headers=headers),timeout=5) as response:
            return response.status,json.load(response)
    except HTTPError as error:
        with error:
            return error.code,json.load(error)


def run(payload):
    status, created=request('/api/ask',payload)
    if status!=202:raise RuntimeError('本地工具提交失败')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        status, state=request('/api/job?'+urlencode({'id':created['job_id']}))
        if status!=200:raise RuntimeError('任务不可读取')
        if state['state']=='done':
            record=state['result']
            if record['model_calls']:raise ValueError('本批本地工具不应调用规划模型')
            return {'job_id':created['job_id'],'record':state['record'],'result':record,
                    'evidence_checks':evidence_checks(record)}
        if state['state'] in ('failed','cancelled','expired'):raise RuntimeError('本地工具未完成')
        time.sleep(.05)
    raise TimeoutError('本地工具验收等待超时')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    status,first=request('/api/catalog?limit=50')
    if status!=200 or first['collection']!=COLLECTION:raise ValueError('目录服务与当前项目版本不匹配')
    pages=[first]
    for offset in range(50,first['total'],50):
        status,page=request('/api/catalog?'+urlencode({'offset':offset,'limit':50}))
        if status!=200 or page['collection']!=first['collection']:raise ValueError('分页版本不一致')
        pages.append(page)
    items=[item for page in pages for item in page['items']]
    if len(items)!=first['source_count'] or len({item['source'] for item in items})!=len(items):
        raise ValueError('全量目录分页遗漏或重复来源')
    choices=[item for item in items if item['title']=='陈皮排骨汤']
    if len(choices)!=2:raise ValueError('同名验收场景与当前库不同')
    flows=[]
    question='查看陈皮排骨汤原文'
    ambiguous=run({'question':question,'mode':'recipe','title':'陈皮排骨汤'})
    if len(ambiguous['result']['outputs'][0]['result']['source_choices'])!=2:
        raise ValueError('同名未澄清')
    flows.append({'case':'duplicate_clarification',**ambiguous})
    for item in choices:
        observed=run({'question':question,'mode':'recipe','title':item['title'],'source':item['source']})
        result=observed['result']
        if result['question']!=question or result['selected_source']!=item['source'] or not observed['evidence_checks']['all_exact']:
            raise ValueError('所选来源或原始问题未保留')
        if any(row['source']!=item['source'] for row in result['outputs'][0]['result']['sources']):
            raise ValueError('混入另一来源')
        flows.append({'case':'selected_original','selected_source':item['source'],**observed})
    image_source=next(item['source'] for item in choices if item['source'].endswith('/陈皮排骨汤/陈皮排骨汤.md'))
    image=run({'question':'查看陈皮排骨汤图片','mode':'images','source':image_source})
    photos=image['result']['outputs'][0]['result']['images']
    if len(photos)!=1 or not image['evidence_checks']['all_exact'] or not all((ROOT/p['image_path']).is_file() for p in photos):
        raise ValueError('所选来源图片或引用不匹配')
    flows.append({'case':'selected_images',**image})
    for query,expected in (({'q':'蒸水蛋 操作','category':'breakfast'},1),({'q':'不存在的菜谱XYZ'},0)):
        status,page=request('/api/catalog?'+urlencode(query))
        if status!=200 or page['total']!=expected:raise ValueError('目录搜索不符合预期')
    status,_=request('/api/ask',{'question':'蒸水蛋怎么做','source':image_source})
    if status!=400:raise ValueError('来源与问题冲突未拒绝')
    receipt={'collection':first['collection'],'version':first['version'],
             'source_count':first['source_count'],'title_count':first['title_count'],
             'pages_checked':len(pages),'remote_model_calls':0,'flows':flows,
             'checks':['complete_catalog_pagination','duplicate_clarification','both_selected_sources',
                       'exact_quotes','selected_source_image','search_and_category','empty_search','source_conflict_rejected']}
    (OUT/'local-flows.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print('目录、同名来源、原文与图片验收通过；未调用远程模型。')


if __name__=='__main__':main()
