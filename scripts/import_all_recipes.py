"""全量菜谱分块、可续跑向量生成、独立集合写入与逐条核对。"""
import concurrent.futures
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import chunk_test_recipes as splitter
from check_embedding import ROOT, config
from service_runtime import request_json
from insert_test_recipes import milvus_api

FOLDER = ROOT / 'data/processed/all_recipes'
SOURCE = ROOT / 'data/HowToCook/dishes'
COLLECTION = 'recipe_all_v1'
LIMITS = {'chunk_id':1024,'title':256,'section':1024,'source':2048,
          'content':16000,'text':20000,'embedding_model':256}


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temp.replace(path)


def prepare():
    splitter.SOURCE = SOURCE
    paths = sorted(p for p in SOURCE.rglob('*.md') if 'template' not in p.relative_to(SOURCE).parts)
    chunks = []
    references = []
    for path in paths:
        rows = splitter.split_recipe(path)
        for index, row in enumerate(rows, 1):
            row['chunk_id'] = f'{path.relative_to(SOURCE).as_posix()}::{index:03d}'
        if not rows:
            raise ValueError(f'菜谱没有正文：{path.name}')
        chunks.extend(rows)
        text = path.read_text(encoding='utf-8')
        links = re.findall(r'!\[([^\]]*)\]\(((?:[^()\n]|\([^()]*\))*)\)', text)
        links += [('', target) for target in re.findall(r'<img\b[^>]*\bsrc=[\"\x27]([^\"\x27]+)', text, re.I)]
        definitions = dict(re.findall(r'^\s*\[([^\]]+)\]:\s*(\S+)', text, re.M))
        for alt, ident in re.findall(r'!\[([^\]]*)\]\[([^\]]*)\]', text):
            links.append((alt, definitions.get(ident or alt, ident or alt)))
        for alt, target in links:
            target = target.strip().strip('<>')
            # 去除 Markdown 图片链接的可选 title。
            target = re.split(r'\s+[\"\x27]', target, maxsplit=1)[0]
            remote = urlparse(target).scheme in ('https','http')
            local = (path.parent / unquote(urlparse(target).path)).resolve() if not remote else None
            references.append({'recipe':path.relative_to(ROOT).as_posix(), 'alt':alt, 'target':target,
                'kind':'remote' if remote else 'local',
                'local_path':str(local.relative_to(ROOT)) if local and local.is_relative_to(ROOT) else None,
                'exists':local.is_file() if local else None})
    FOLDER.mkdir(parents=True, exist_ok=True)
    content = ''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in chunks)
    path = FOLDER/'chunks.jsonl'
    if path.exists() and path.read_text(encoding='utf-8') != content:
        raise ValueError('源菜谱变动；请使用新的导入版本，避免混合旧向量。')
    path.write_text(content, encoding='utf-8')
    assets = [{'path':p.relative_to(ROOT).as_posix(),'extension':p.suffix.lower(),
               'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
              for p in sorted(SOURCE.rglob('*')) if p.is_file() and p.suffix.lower() != '.md' and p.name != '.DS_Store' and 'template' not in p.relative_to(SOURCE).parts]
    save(FOLDER/'asset-manifest.json', {'files':assets,'image_references':references,
          'policy':'原文件保留；不下载远程图片；Markdown 引用及替代文字保留；未执行 OCR、视觉描述或图片向量化。'})
    summary = {'recipe_count':len(paths),'chunk_count':len(chunks),'asset_count':len(assets),
               'image_reference_count':len(references),'remote_image_references':sum(r['kind']=='remote' for r in references),
               'missing_local_images':[r for r in references if r['exists'] is False],
               'excluded':['dishes/template（贡献模板）', 'test_dishes（重复测试副本）', 'README、tips、工程文件', '.DS_Store（系统文件）'],
               'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    save(FOLDER/'preparation-summary.json', summary)
    print(f'准备完成：{len(paths)} 篇 / {len(chunks)} 块 / {len(assets)} 个附件', flush=True)
    return chunks, summary


def embeddings(chunks, summary):
    settings = config()
    key = settings.get('EMBEDDING_API_KEY','')
    base = settings.get('EMBEDDING_BASE_URL','').rstrip('/')
    model = settings.get('EMBEDDING_MODEL','')
    dim = int(settings.get('EMBEDDING_DIMENSIONS','1024'))
    if not key or not model or urlparse(base).scheme != 'https':
        raise ValueError('API 配置不完整')
    metadata = {'api_base_url':base,'requested_model':model,'dimensions':dim,
                'chunk_count':len(chunks),'source_sha256':summary['source_sha256'],
                'input_field':'text','input_format':'菜名、章节、原文路径 + Markdown 正文；图片仅保留引用'}
    meta_path = FOLDER/'embedding-metadata.json'
    if meta_path.exists() and json.loads(meta_path.read_text()) != metadata:
        raise ValueError('向量缓存配置不同，停止执行。')
    save(meta_path, metadata)
    cache = FOLDER/'vector-cache'
    cache.mkdir(exist_ok=True)
    def one(row):
        digest = hashlib.sha256(row['text'].encode()).hexdigest()
        path = cache/(hashlib.sha256(row['chunk_id'].encode()).hexdigest()+'.json')
        def validate(record):
            vector = record['vector']
            if record.get('text_sha256') != digest or len(vector)!=dim or not all(
                isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in vector
            ) or sum(v*v for v in vector)==0:
                raise ValueError('缓存或向量无效')
            return {**row,'vector':vector,'embedding_model':model}
        if path.exists():
            return validate(json.loads(path.read_text()))
        for attempt in range(5):
            try:
                req = Request(base+'/embeddings', data=json.dumps({'model':model,'input':row['text'],
                    'dimensions':dim,'encoding_format':'float'},ensure_ascii=False).encode(),
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
                result=request_json(req,timeout=60,phase="import_embedding",cache_namespace=metadata["source_sha256"],attempts=1)
                if len(result['data']) != 1:
                    raise ValueError('单文本返回的向量数量异常')
                record={'text_sha256':digest,'vector':result['data'][0]['embedding']}
                validated=validate(record)
                save(path,record)
                return validated
            except HTTPError as error:
                if error.code not in (429,500,502,503,504) or attempt==4:
                    raise RuntimeError(f'Embedding HTTP {error.code}，已完成向量缓存保留。') from None
            except (URLError,TimeoutError):
                if attempt==4:
                    raise RuntimeError('Embedding 网络失败，已完成向量缓存保留。') from None
            time.sleep(min(2**(attempt+1),16))
    records=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(one,row) for row in chunks]
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())
            if len(records)%50==0 or len(records)==len(chunks):
                print(f'向量完成 {len(records)}/{len(chunks)}',flush=True)
    records.sort(key=lambda r:r['chunk_id'])
    path=FOLDER/'chunks-with-vectors.jsonl'
    temp=path.with_suffix('.tmp')
    temp.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
    temp.replace(path)
    return records,metadata


def insert(records, metadata):
    for row in records:
        if any(len(row[k].encode())>limit for k,limit in LIMITS.items()):
            raise ValueError('字段长度超过 Milvus 限制')
    state_path=FOLDER/'import-state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'collection':COLLECTION,'source_sha256':metadata['source_sha256']}
    if state['source_sha256']!=metadata['source_sha256']:
        raise ValueError('集合导入版本不同')
    collections=milvus_api('collections/list',{})
    if COLLECTION not in collections:
        save(state_path,state)
        fields=[{'fieldName':k,'dataType':'VarChar','elementTypeParams':{'max_length':v},
                 **({'isPrimary':True} if k=='chunk_id' else {})} for k,v in LIMITS.items()]
        fields += [{'fieldName':k,'dataType':'Int64'} for k in ('source_start_line','source_end_line')]
        fields.append({'fieldName':'vector','dataType':'FloatVector','elementTypeParams':{'dim':str(metadata['dimensions'])}})
        milvus_api('collections/create',{'collectionName':COLLECTION,'description':'HowToCook 全量菜谱；副标题分块；图片引用保留',
            'schema':{'autoId':False,'enabledDynamicField':False,'fields':fields},
            'indexParams':[{'fieldName':'vector','indexName':'vector_cosine','metricType':'COSINE','params':{'index_type':'FLAT'}}],
            'params':{'consistencyLevel':'Strong'}})
    elif not state_path.exists():
        raise ValueError('同名集合不属于本次导入，停止。')
    schema=milvus_api('collections/describe',{'collectionName':COLLECTION})
    field=next(f for f in schema['fields'] if f['name']=='vector')
    if int(dict((p['key'],p['value']) for p in field['params'])['dim'])!=metadata['dimensions']:
        raise ValueError('已有集合维度不同')
    for start in range(0,len(records),100):
        batch=records[start:start+100]
        # 主键 upsert 使断点重跑不会产生重复记录。
        result=milvus_api('entities/upsert',{'collectionName':COLLECTION,'data':batch})
        if result.get('upsertCount')!=len(batch):
            raise RuntimeError('批次写入数量不同')
        state['completed']=start+len(batch)
        save(state_path,state)
        print(f'写入 {state["completed"]}/{len(records)}',flush=True)
    milvus_api('collections/load',{'collectionName':COLLECTION})
    total=milvus_api('entities/query',{'collectionName':COLLECTION,'filter':'','outputFields':['count(*)'],'consistencyLevel':'Strong'})
    if total[0]['count(*)']!=len(records):
        raise RuntimeError('集合总数与本地不一致')
    for start in range(0,len(records),100):
        batch=records[start:start+100]
        rows=milvus_api('entities/query',{'collectionName':COLLECTION,
            'filter':'chunk_id in '+json.dumps([r['chunk_id'] for r in batch],ensure_ascii=False),
            'limit':len(batch),'outputFields':list(records[0]),'consistencyLevel':'Strong'})
        actual={r['chunk_id']:r for r in rows}
        if len(actual)!=len(batch):
            raise RuntimeError('查询记录缺失')
        for expected in batch:
            row=actual[expected['chunk_id']]
            if any(row.get(k)!=v for k,v in expected.items() if k!='vector') or len(row['vector'])!=metadata['dimensions'] or not all(
                math.isclose(a,b,rel_tol=1e-6,abs_tol=1e-7) for a,b in zip(row['vector'],expected['vector'])):
                raise RuntimeError('正文、来源或向量核对失败')
    report={**metadata,'collection':COLLECTION,'database':'default','verified_count':len(records),
            'all_fields_verified':True,'index':'FLAT','metric':'COSINE','finished_at':datetime.now(timezone.utc).isoformat()}
    save(FOLDER/'milvus-import-result.json',report)
    save(ROOT/'data/processed/active-dataset.json',{'dataset':'all_recipes','collection':COLLECTION})
    print('逐条核对通过，当前问答已切换到全量菜谱。',flush=True)


if __name__=='__main__':
    chunks,summary=prepare()
    records,metadata=embeddings(chunks,summary)
    insert(records,metadata)
