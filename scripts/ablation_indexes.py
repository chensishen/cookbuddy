"""受控消融索引：独立H2向量与overview投影，正文始终指向原文，生产集合不变。"""
import hashlib
import json
from pathlib import Path
from urllib.request import Request

from check_embedding import ROOT,config
from ingredient_recommendation import ingredient_fields
from naive_baseline import load_index
from rag_dataset import FOLDER
from recipe_tools import load_chunks
from service_runtime import request_json


def sha(value):return hashlib.sha256(value).hexdigest()


def overview_rows(h2,metadata,chunks,normalized=True,prefix=True):
    grouped={}
    for row in chunks:grouped.setdefault(row['source'],[]).append(row)
    rows=[]
    for source,info in sorted(metadata.items()):
        candidates=[row for row in h2 if row['source']==source]
        original=next((r for r in candidates if '必备原料' in r['section']),candidates[0])
        ingredients=info['ingredients']
        if not normalized:
            ingredients,*_=ingredient_fields([r for r in grouped[source] if '必备原料' in r['section']],
                (ROOT/source).read_text().splitlines(),normalize_aliases=False)
            ingredients=sorted(ingredients)
        body=f"{original['title']}；分类：{info['category']}；难度：{info['difficulty']}星；可识别必备食材：{'、'.join(ingredients)}"
        text=(f"菜名：{original['title']}\n章节：overview\n原文路径：{source}\n\n" if prefix else '')+body
        rows.append({**{k:v for k,v in original.items() if k!='vector'},'chunk_id':'overview:'+sha(text.encode()),
            'evidence_chunk_id':original['chunk_id'],'text':text,'lexical_content':body,
            'projection_policy':'overview只作召回；交付给生成器的是原始H2食材表/简介正文，不能引用合成元数据为原文'})
    return rows


def build(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    chunks=load_chunks();metadata=json.loads((FOLDER/'source-metadata.json').read_text())
    h2manifest,h2=load_index(ROOT/'data/processed/chunk-ablations-v1/h2_prefix')
    settings=config()
    spec={'model':settings['EMBEDDING_MODEL'],'dimensions':int(settings['EMBEDDING_DIMENSIONS']),
        'base_url':settings['EMBEDDING_BASE_URL'].rstrip('/'),'source_hashes':h2manifest['spec']['source_hashes'],
        'metadata_sha256':sha((FOLDER/'source-metadata.json').read_bytes()),'protocol':'overview-projection-v1'}
    if any(spec[key]!=h2manifest['spec'][key] for key in ('model','dimensions','base_url')):raise ValueError('H2向量模型不一致')
    spec_hash=sha(json.dumps(spec,sort_keys=True).encode())
    manifest_path=folder/'manifest.json';vector_path=folder/'vectors.jsonl'
    if manifest_path.exists() and json.loads(manifest_path.read_text())['spec_sha256']!=spec_hash:raise ValueError('隔离索引版本已变化')
    variants={name:overview_rows(h2,metadata,chunks,normalized,prefix) for name,normalized,prefix in
        (('prefix_normalized',True,True),('plain_normalized',True,False),('prefix_raw',False,True))}
    unique={sha(row['text'].encode()):row['text'] for rows in variants.values() for row in rows}
    saved={}
    if vector_path.exists():
        for line in vector_path.read_text().splitlines():
            item=json.loads(line)
            if item['spec_sha256']!=spec_hash or item['text_sha256'] in saved:raise ValueError('实验向量版本错误或重复')
            saved[item['text_sha256']]=item['vector']
    if not set(saved)<=set(unique):raise ValueError('未知实验向量')
    manifest={'state':'building','spec':spec,'spec_sha256':spec_hash,'production_enabled':False,
        'variants':{key:len(value) for key,value in variants.items()},'unique_vectors':len(unique)}
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    missing=[key for key in unique if key not in saved]
    for offset in range(0,len(missing),8):
        keys=missing[offset:offset+8]
        req=Request(spec['base_url']+'/embeddings',data=json.dumps({'model':spec['model'],
            'input':[unique[key] for key in keys],'dimensions':spec['dimensions'],'encoding_format':'float'},ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
        response=request_json(req,timeout=120,attempts=1,phase='ablation_overview_index',cache_namespace=spec_hash)
        with vector_path.open('a') as stream:
            for key,item in zip(keys,sorted(response['data'],key=lambda row:row['index'])):
                saved[key]=item['embedding'];stream.write(json.dumps({'text_sha256':key,'vector':saved[key],'spec_sha256':spec_hash})+'\n')
        print(f'overview索引 {len(saved)}/{len(unique)}',flush=True)
    for name,rows in variants.items():
        path=folder/(name+'.jsonl')
        path.write_text(''.join(json.dumps({**row,'vector':saved[sha(row['text'].encode())]},ensure_ascii=False)+'\n' for row in rows))
    manifest.update(state='ready',vector_file_sha256=sha(vector_path.read_bytes()),
        variant_hashes={name:sha((folder/(name+'.jsonl')).read_bytes()) for name in variants})
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


def load(folder,name):
    folder=Path(folder);manifest=json.loads((folder/'manifest.json').read_text());path=folder/(name+'.jsonl')
    if manifest['state']!='ready' or sha(path.read_bytes())!=manifest['variant_hashes'][name]:raise ValueError('overview实验索引未完成或变化')
    if sha((FOLDER/'source-metadata.json').read_bytes())!=manifest['spec']['metadata_sha256']:raise ValueError('overview元数据版本变化')
    for source,digest in manifest['spec']['source_hashes'].items():
        if sha((ROOT/source).read_bytes())!=digest:raise ValueError('来源版本变化')
    return [json.loads(line) for line in path.read_text().splitlines()]


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--folder',type=Path,required=True)
    report=build(parser.parse_args().folder)
    print(json.dumps({key:report[key] for key in ('state','variants','unique_vectors','production_enabled')},ensure_ascii=False))
