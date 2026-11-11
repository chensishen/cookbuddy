"""隔离的512-token稠密基线：同源全库，不写入活动Milvus。"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
from urllib.request import Request

from check_embedding import ROOT,config
from recipe_tools import load_chunks
from service_runtime import request_json,quota_tokenizer

FOLDER=ROOT/'data/processed/naive-512-v1'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fixed_windows(text,limit=512):
    """按token预算连续切原文，保留字符偏移；不利用标题与段落边界。"""
    tokenizer=quota_tokenizer();start=0
    while start<len(text):
        end=min(len(text),start+limit*4)
        while end<len(text) and len(tokenizer.encode(text[start:end],disallowed_special=()))<=limit:
            end=min(len(text),start+2*(end-start))
        if len(tokenizer.encode(text[start:end],disallowed_special=()))>limit:
            low,high=start+1,end
            while low<high:
                middle=(low+high+1)//2
                if len(tokenizer.encode(text[start:middle],disallowed_special=()))<=limit:low=middle
                else:high=middle-1
            end=low
        piece=text[start:end]
        count=len(tokenizer.encode(piece,disallowed_special=()))
        if count>limit:raise ValueError('基线窗口超出token上限')
        yield {'content':piece,'start_char':start,'end_char':end,'token_count':count,
            'source_start_line':text.count('\n',0,start)+1,
            'source_end_line':text.count('\n',0,end-1)+1}
        start=end


def corpus_windows(chunks):
    sources={r['source']:r['title'] for r in chunks};rows=[];hashes={}
    for source,title in sorted(sources.items()):
        raw=(ROOT/source).read_bytes();hashes[source]=digest(raw)
        for row in fixed_windows((ROOT/source).read_text(encoding='utf-8')):
            ident=digest((source+':'+str(row['start_char'])+':'+hashes[source]).encode())
            rows.append({**row,'source':source,'title':title,'section':'固定512-token窗口',
                'chunk_id':ident,'baseline_strategy':'fixed512'})
    return rows,hashes


def build(folder=FOLDER,batch_size=8):
    if not 1<=batch_size<=8:raise ValueError('batch_size须为1至8，供应商大批次编号存在重置')
    folder.mkdir(parents=True,exist_ok=True)
    rows,hashes=corpus_windows(load_chunks());settings=config()
    spec={'model':settings['EMBEDDING_MODEL'],'dimensions':int(settings['EMBEDDING_DIMENSIONS']),
          'base_url':settings['EMBEDDING_BASE_URL'].rstrip('/'),'source_hashes':hashes,
          'strategy':'fixed512-v1','tokenizer':'cl100k_base','prefix':False,'overlap':0}
    spec_hash=digest(json.dumps(spec,sort_keys=True,ensure_ascii=False).encode())
    metadata_path=folder/'manifest.json';vector_path=folder/'vectors.jsonl'
    if metadata_path.exists() and json.loads(metadata_path.read_text())['spec_sha256']!=spec_hash:
        raise ValueError('已有基线版本与当前来源/模型不同，请使用新的隔离目录')
    meta={'spec':spec,'spec_sha256':spec_hash,'state':'building','production_enabled':False,'chunks':len(rows)}
    metadata_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    saved={}
    if vector_path.exists():
        for line in vector_path.read_text().splitlines():
            row=json.loads(line)
            if row['spec_sha256']!=spec_hash:raise ValueError('基线向量版本不一致')
            saved[row['chunk_id']]=row
    if not set(saved)<=set(r['chunk_id'] for r in rows):raise ValueError('未知基线向量')
    missing=[r for r in rows if r['chunk_id'] not in saved]
    for offset in range(0,len(missing),batch_size):
        batch=missing[offset:offset+batch_size]
        payload={'model':spec['model'],'input':[r['content'] for r in batch],
                 'dimensions':spec['dimensions'],'encoding_format':'float'}
        request=Request(spec['base_url']+'/embeddings',data=json.dumps(payload,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
        response=request_json(request,timeout=120,attempts=2,phase='baseline_index',cache_namespace=spec_hash)
        vectors=sorted(response['data'],key=lambda r:r.get('index',0))
        with vector_path.open('a') as stream:
            for row,item in zip(batch,vectors):
                value={**row,'vector':item['embedding'],'spec_sha256':spec_hash}
                stream.write(json.dumps(value,ensure_ascii=False)+'\n');saved[row['chunk_id']]=value
        print(f'512-token基线向量：{len(saved)}/{len(rows)}',flush=True)
    meta.update(state='ready',vector_file_sha256=digest(vector_path.read_bytes()),
                created_at=datetime.now(timezone.utc).isoformat())
    metadata_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    return meta


def load_index(folder=FOLDER):
    manifest=json.loads((folder/'manifest.json').read_text())
    if manifest['state']!='ready' or digest((folder/'vectors.jsonl').read_bytes())!=manifest['vector_file_sha256']:
        raise ValueError('512-token基线未完成或文件已变化')
    for source,expected in manifest['spec']['source_hashes'].items():
        if digest((ROOT/source).read_bytes())!=expected:raise ValueError('基线来源已变化')
    rows=[json.loads(line) for line in (folder/'vectors.jsonl').read_text().splitlines()]
    return manifest,rows


def dense_search(vector,rows,k=5):
    norm=math.sqrt(sum(x*x for x in vector))
    if not norm:raise ValueError('查询向量为零')
    def score(row):
        other=row['vector']
        if len(other)!=len(vector):raise ValueError('基线向量维度不一致')
        return sum(a*b for a,b in zip(vector,other))/(norm*math.sqrt(sum(x*x for x in other)))
    scored=[(score(row),row) for row in rows]
    return [{k:v for k,v in row.items() if k!='vector'}|{'distance':value}
        for value,row in sorted(scored,key=lambda x:(-x[0],x[1]['chunk_id']))[:k]]


def gold_windows(gold,chunks,windows):
    spans=[]
    for chunk in chunks:
        if chunk['chunk_id'] not in gold:continue
        raw=(ROOT/chunk['source']).read_text();lines=raw.splitlines(keepends=True)
        start=sum(len(x) for x in lines[:chunk['source_start_line']-1])
        end=sum(len(x) for x in lines[:chunk['source_end_line']])
        spans.append((chunk['source'],start,end))
    return [row['chunk_id'] for row in windows if any(row['source']==source and
        max(0,min(row['end_char'],end)-max(row['start_char'],start))>=min(32,end-start)
        for source,start,end in spans)]


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--folder',type=Path,default=FOLDER)
    parser.add_argument('--batch-size',type=int,default=8);args=parser.parse_args()
    result=build(args.folder,args.batch_size)
    print(json.dumps({'state':result['state'],'chunks':result['chunks'],'production_enabled':False},ensure_ascii=False))
