"""来源增量计划、隔离索引验收、别名发布与回滚；不修改其他项目集合。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time

from check_embedding import ROOT


def source_manifest(folder):
    folder=Path(folder)
    return {p.relative_to(folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob('*')) if p.is_file() and p.suffix.lower() in ('.md','.pdf','.docx') and 'template' not in p.parts}


def change_plan(previous, current, old_model, new_model, old_dim, new_dim):
    rebuild = old_model != new_model or old_dim != new_dim
    return {'added':sorted(current.keys()-previous.keys()),'deleted':sorted(previous.keys()-current.keys()),
            'changed':sorted(k for k in previous.keys() & current.keys() if previous[k]!=current[k]),
            'unchanged':sorted(k for k in previous.keys() & current.keys() if previous[k]==current[k]),
            'embedding_rebuild_required':rebuild,
            'reuse_policy':'相同来源和文本哈希、模型、维度、输入约定才可复用；删除块不进入新集合'}


def safe_collection(name):
    if not re.fullmatch(r'recipe_[a-zA-Z0-9_]+',name):
        raise ValueError('只允许管理本项目 recipe_ 集合与别名')
    return name


def fingerprint(folder):
    return hashlib.sha256((Path(folder)/'chunks.jsonl').read_bytes()).hexdigest()


def clone_index(target, index_type='HNSW', folder=ROOT/'data/processed/all_recipes'):
    from pymilvus import MilvusClient, DataType
    safe_collection(target)
    if index_type not in ('FLAT','HNSW'):raise ValueError('索引类型无效')
    folder=Path(folder);metadata=json.loads((folder/'embedding-metadata.json').read_text())
    if metadata['source_sha256']!=fingerprint(folder):raise ValueError('来源版本不一致')
    client=MilvusClient(uri='http://127.0.0.1:19530')
    if client.has_collection(target):raise ValueError('目标已存在；使用新的版本名，禁止覆盖')
    schema=client.create_schema(auto_id=False,enable_dynamic_field=True)
    schema.add_field('chunk_id',DataType.VARCHAR,is_primary=True,max_length=1024)
    schema.add_field('vector',DataType.FLOAT_VECTOR,dim=metadata['dimensions'])
    if metadata.get('metadata_schema_version')==1:
        for name,length in {'category':64,'chunk_type':64,'heading_path':2048,'source_commit':64,
                'doc_hash':64,'parser_version':128,'flags':2048}.items():
            schema.add_field(name,DataType.VARCHAR,max_length=length)
        schema.add_field('difficulty',DataType.INT64)
        schema.add_field('quality_score',DataType.FLOAT)
        schema.add_field('ingredients',DataType.ARRAY,element_type=DataType.VARCHAR,max_capacity=256,max_length=128)
    indexes=client.prepare_index_params()
    indexes.add_index('vector',index_type=index_type,metric_type='COSINE',
                      params={'M':16,'efConstruction':128} if index_type=='HNSW' else {})
    started=time.monotonic()
    client.create_collection(target,schema=schema,index_params=indexes)
    records=[]
    for line in (folder/'chunks-with-vectors.jsonl').read_text().splitlines():
        row=json.loads(line)
        records.append({**row,'embedding_model':metadata['requested_model']})
    for i in range(0,len(records),100):client.insert(target,records[i:i+100])
    client.flush(target)
    client.load_collection(target)
    count=client.query(target,filter='',output_fields=['count(*)'],consistency_level='Strong')[0]['count(*)']
    if count!=len(records):raise RuntimeError('新集合计数核对失败')
    # 分批逐条读回正文、元信息及向量，不能只验 count。
    checked=0
    for i in range(0,len(records),100):
        batch=records[i:i+100]
        actual=client.query(target,filter='chunk_id in '+json.dumps([r['chunk_id'] for r in batch],ensure_ascii=False),
                            output_fields=['*'],consistency_level='Strong',limit=len(batch))
        lookup={r['chunk_id']:r for r in actual}
        for expected in batch:
            found=lookup[expected['chunk_id']]
            if any(found.get(k)!=v for k,v in expected.items() if k not in ('vector','quality_score')):
                raise RuntimeError('新集合文本核对失败')
            if 'quality_score' in expected and abs(found['quality_score']-expected['quality_score'])>1e-6:
                raise RuntimeError('新集合质量字段核对失败')
            if any(abs(a-b)>1e-6 for a,b in zip(found['vector'],expected['vector'])) or len(found['vector'])!=metadata['dimensions']:
                raise RuntimeError('新集合向量核对失败')
            checked+=1
    report={**metadata,'collection':target,'index_type':index_type,'checked':checked,'status':'validated',
            'elapsed_seconds':round(time.monotonic()-started,3),'created_epoch':time.time()}
    out=ROOT/'data/processed/index-lab';out.mkdir(exist_ok=True)
    (out/(target+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def publish_alias(alias, target, report, client=None):
    from pymilvus import MilvusClient
    safe_collection(alias);safe_collection(target)
    if report.get('status')!='validated' or report.get('collection')!=target:
        raise ValueError('只能发布已验收的目标版本')
    client=client or MilvusClient(uri='http://127.0.0.1:19530')
    aliases=client.list_aliases() if hasattr(client,'list_aliases') else [alias]
    aliases=aliases.get('aliases',[]) if isinstance(aliases,dict) else aliases
    if alias in aliases:
        old=client.describe_alias(alias)['collection_name']
    else:
        old=None
    if old:client.alter_alias(target,alias)
    else:client.create_alias(target,alias)
    return {'alias':alias,'previous':old,'current':target,'timestamp':time.time()}


def rollback_alias(change, client=None):
    from pymilvus import MilvusClient
    client=client or MilvusClient(uri='http://127.0.0.1:19530')
    safe_collection(change['alias'])
    if not change.get('previous'):raise ValueError('没有旧版本可回滚')
    safe_collection(change['previous'])
    current=client.describe_alias(change['alias'])['collection_name']
    if current!=change['current']:raise ValueError('别名已被其他发布修改，拒绝覆盖')
    client.alter_alias(change['previous'],change['alias'])


def activate_version(version):
    """配置文件原子切换；新任务进程读新版本，运行中的请求保持原版本。"""
    safe_collection('recipe_'+version)
    folder=ROOT/'data/processed/recipe_versions'/version
    plan=json.loads((folder/'version-plan.json').read_text())
    report=json.loads((ROOT/'data/processed/index-lab'/(plan['collection']+'.json')).read_text())
    if report.get('status')!='validated' or report['source_sha256']!=fingerprint(folder):
        raise ValueError('只允许激活已逐条核对的版本')
    active=ROOT/'data/processed/active-dataset.json'
    old=json.loads(active.read_text())
    new={'dataset':'all_recipes','version':version,'collection':plan['collection']}
    temp=active.with_suffix('.json.tmp');temp.write_text(json.dumps(new,ensure_ascii=False,indent=2)+'\n');temp.replace(active)
    return {'previous':old,'current':new,'timestamp':time.time()}


def rollback_active_version(change):
    active=ROOT/'data/processed/active-dataset.json'
    if json.loads(active.read_text())!=change['current']:
        raise ValueError('当前版本已被其他发布修改，拒绝覆盖')
    previous=change['previous']
    if previous.get('dataset') not in ('all_recipes','test_dishes'):
        raise ValueError('回滚目标无效')
    temp=active.with_suffix('.json.tmp');temp.write_text(json.dumps(previous,ensure_ascii=False,indent=2)+'\n');temp.replace(active)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--clone',required=True)
    parser.add_argument('--index',choices=('FLAT','HNSW'),default='HNSW');args=parser.parse_args()
    print(json.dumps(clone_index(args.clone,args.index),ensure_ascii=False,indent=2))
