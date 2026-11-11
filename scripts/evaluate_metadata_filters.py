"""真实查询向量与Milvus过滤开关配对；仅衡量约束合规，不冒充语义或端到端消融。"""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from urllib.request import Request

from check_embedding import ROOT,config
from insert_test_recipes import milvus_api
from rag_dataset import FOLDER,COLLECTION
from search_filters import filter_chunks,scalar_filter
from service_runtime import request_json
from evaluate_prd import code_fingerprint

CASES=[
    ('soup_easy','推荐难度不超过2星的汤羹',{'category':'soup','max_difficulty':2}),
    ('breakfast_easy','推荐难度不超过1星的早餐',{'category':'breakfast','max_difficulty':1}),
    ('vegetable_easy','推荐难度不超过2星的素菜',{'category':'vegetable_dish','max_difficulty':2}),
    ('meat_easy','推荐难度不超过2星的荤菜',{'category':'meat_dish','max_difficulty':2}),
    ('dessert_easy','推荐难度不超过2星的甜品',{'category':'dessert','max_difficulty':2}),
    ('aquatic_easy','推荐难度不超过2星的水产菜谱',{'category':'aquatic','max_difficulty':2}),
    ('egg_tomato_soup','必备原料同时有鸡蛋和西红柿的汤羹',{'category':'soup','ingredients_all':['鸡蛋','西红柿']}),
    ('egg_tomato_vegetable','必备原料同时有鸡蛋和番茄的素菜',{'category':'vegetable_dish','ingredients_all':['鸡蛋','番茄']}),
    ('potato_meat','必备原料里含土豆的荤菜',{'category':'meat_dish','ingredients_all':['土豆']}),
    ('milk_breakfast','必备原料里含牛奶的早餐',{'category':'breakfast','ingredients_all':['牛奶']}),
    ('any_vegetable','必备原料至少含菠菜或土豆之一的素菜',{'category':'vegetable_dish','ingredients_any':['菠菜','土豆']}),
    ('empty_combination','必备原料必须同时有鸡翅和可乐的汤羹',{'category':'soup','ingredients_all':['鸡翅','可乐']})
]


def compliance(hits,allowed,k=5):
    if not allowed:return float(not hits)
    expected=min(k,len(allowed))
    return sum(row['chunk_id'] in allowed for row in hits)/expected


def query_vectors(questions,metadata,settings):
    vectors=[];usages=[]
    # 当前供应商在12项批量中每8项重置index；分批后逐批严格校验，不猜测对应关系。
    for offset in range(0,len(questions),8):
        request=Request(metadata['api_base_url']+'/embeddings',data=json.dumps({
            'model':metadata['requested_model'],'input':questions[offset:offset+8],
            'dimensions':metadata['dimensions'],'encoding_format':'float'},ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
        response=request_json(request,timeout=60,phase='metadata_filter_ablation',attempts=1)
        vectors.extend(item['embedding'] for item in sorted(response['data'],key=lambda item:item['index']))
        usages.append(response.get('usage'))
    return vectors,usages


def run(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    rows=[json.loads(line) for line in (FOLDER/'chunks.jsonl').read_text().splitlines()]
    metadata=json.loads((FOLDER/'embedding-metadata.json').read_text())
    if metadata.get('metadata_schema_version')!=1:raise ValueError('需要已核对的原生元数据集合')
    settings=config()
    fixture={'scope':'开发集：只测原生过滤开关的约束合规，不测语义相关性或答案质量','cases':CASES}
    (out/'cases.json').write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+'\n')
    fingerprint=code_fingerprint()
    vectors,usages=query_vectors([case[1] for case in CASES],metadata,settings)
    report={'created_at':datetime.now(timezone.utc).isoformat(),'collection':COLLECTION,'code_sha256':fingerprint,
        'dataset_sha256':hashlib.sha256((FOLDER/'chunks.jsonl').read_bytes()).hexdigest(),
        'scope':fixture['scope'],'query_vector_origin':'真实问题Embedding，同一问题两侧复用同一向量',
        'embedding_usage':usages,'embedding_batch_size':8,'rows':[]}
    for (ident,question,filters),vector in zip(CASES,vectors):
        eligible,_,_=filter_chunks(rows,**filters);allowed={row['chunk_id'] for row in eligible}
        result={'id':ident,'question':question,'filters':filters,'eligible_chunks':len(allowed)}
        for variant,expression in (('without_filter',''),('with_filter',scalar_filter(filters))):
            hits=milvus_api('entities/search',{'collectionName':COLLECTION,'data':[vector],'annsField':'vector',
                'filter':expression,'limit':5,'outputFields':['chunk_id','source','category','difficulty','ingredients'],
                'searchParams':{'metricType':'COSINE','params':{}},'consistencyLevel':'Strong'})
            result[variant]={'hits':hits,'compliance':compliance(hits,allowed),
                'violations':sum(row['chunk_id'] not in allowed for row in hits)}
        report['rows'].append(result)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    report['aggregates']={variant:{'mean_constraint_compliance':sum(row[variant]['compliance'] for row in report['rows'])/len(CASES),
        'violations':sum(row[variant]['violations'] for row in report['rows'])} for variant in ('without_filter','with_filter')}
    report['code_sha256_end']=code_fingerprint()
    report['code_unchanged']=report['code_sha256_end']==fingerprint
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True)
    report=run(parser.parse_args().out);print(json.dumps(report['aggregates'],ensure_ascii=False))
