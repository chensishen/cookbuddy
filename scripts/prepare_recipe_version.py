"""完整菜谱新版本：只对子块文本变化重新向量化，保留旧版本可回滚。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
from urllib.request import Request

from check_embedding import ROOT,config
import chunk_test_recipes as splitter
from document_ingestion import bounded_chunks
from knowledge_versions import source_manifest, change_plan, clone_index, safe_collection
from service_runtime import request_json


def prepare(version='bounded_v2', max_tokens=768, path_in_embedding=True, embed=False):
    safe_collection('recipe_'+version)
    base=ROOT/'data/processed/all_recipes'
    old_meta=json.loads((base/'embedding-metadata.json').read_text())
    old=[json.loads(x) for x in (base/'chunks-with-vectors.jsonl').read_text().splitlines()]
    reuse={hashlib.sha256(r['text'].encode()).hexdigest():r['vector'] for r in old}
    for candidate in (ROOT/'data/processed/recipe_versions').glob('*/chunks-with-vectors.jsonl'):
        other_meta=json.loads((candidate.parent/'embedding-metadata.json').read_text())
        if any(other_meta.get(k)!=old_meta.get(k) for k in ('requested_model','dimensions','api_base_url')):continue
        for line in candidate.read_text().splitlines():
            record=json.loads(line);reuse.setdefault(hashlib.sha256(record['text'].encode()).hexdigest(),record['vector'])
    folder=ROOT/'data/processed/recipe_versions'/version;folder.mkdir(parents=True,exist_ok=True)
    snapshot=base/'source-manifest.json'
    if not snapshot.exists():
        for record in old:
            lines=(ROOT/record['source']).read_text().splitlines()
            if record['content'] not in '\n'.join(lines[record['source_start_line']-1:record['source_end_line']]):
                raise ValueError('首次来源快照需要旧入库来源仍一致')
        snapshot.write_text(json.dumps(source_manifest(ROOT/'data/HowToCook/dishes'),ensure_ascii=False,indent=2)+'\n')
    previous=json.loads(snapshot.read_text())
    splitter.SOURCE=ROOT/'data/HowToCook/dishes'
    parents=[]
    for path in sorted(splitter.SOURCE.rglob('*.md')):
        if 'template' in path.relative_to(splitter.SOURCE).parts:continue
        pieces=splitter.split_recipe(path)
        for index,row in enumerate(pieces,1):
            row['chunk_id']=path.relative_to(splitter.SOURCE).as_posix()+f'::{index:03d}'
        parents.extend(pieces)
    rows=[]
    for parent in parents:
        plain={k:v for k,v in parent.items() if k!='vector'}
        for block in bounded_chunks({**plain,'locator_type':'line'},max_tokens):
            # 保留原本准确的文本行号；splitter 的 strip 可能使简介头部有空行。
            source=(ROOT/block['source']).read_text().splitlines()
            content_lines=block['content'].splitlines()
            starts=[i for i in range(parent['source_start_line']-1,parent['source_end_line']) if source[i:i+len(content_lines)]==content_lines]
            if len(starts)!=1:raise ValueError('子块无法唯一定位原文')
            block['source_start_line']=starts[0]+1;block['source_end_line']=starts[0]+len(content_lines)
            block['parent_chunk_id']=parent['chunk_id']
            ident=json.dumps([block['source'],block['section'],block['part'],block['content']],ensure_ascii=False)
            block['chunk_id']=hashlib.sha256(ident.encode()).hexdigest()
            text=f"菜名：{block['title']}\n章节：{block['section']}\n"
            if path_in_embedding:text+=f"原文路径：{block['source']}\n"
            block['text']=text+'\n'+block['content']
            rows.append(block)
    content=''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows)
    destination=folder/'chunks.jsonl'
    if destination.exists() and destination.read_text()!=content:raise ValueError('版本已存在且内容不同，请使用新版本名')
    destination.write_text(content)
    meta={**old_meta,'chunk_count':len(rows),'source_sha256':hashlib.sha256(content.encode()).hexdigest(),
          'splitter_version':'bounded-v2','max_tokens_proxy':max_tokens,'tokenizer_proxy':'cl100k_base',
          'path_in_embedding':path_in_embedding}
    (folder/'embedding-metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    manifest=source_manifest(ROOT/'data/HowToCook/dishes')
    (folder/'source-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    plan={'version':version,'collection':'recipe_'+version,'chunks':len(rows),
          'reusable_vectors':sum(hashlib.sha256(r['text'].encode()).hexdigest() in reuse for r in rows),
          'path_in_embedding':path_in_embedding,'source_diff':change_plan(previous,manifest,old_meta['requested_model'],meta['requested_model'],old_meta['dimensions'],meta['dimensions']),
          'status':'prepared'}
    plan['new_vectors']=len(rows)-plan['reusable_vectors']
    if embed:
        settings=config();key=settings['EMBEDDING_API_KEY'];api=meta['api_base_url']
        cache=folder/'vector-cache';cache.mkdir(exist_ok=True)
        def one(row):
            digest=hashlib.sha256(row['text'].encode()).hexdigest();path=cache/(digest+'.json')
            vector=reuse.get(digest)
            if vector is None and path.exists():vector=json.loads(path.read_text())
            if vector is None:
                request=Request(api+'/embeddings',data=json.dumps({'model':meta['requested_model'],'input':row['text'],
                    'dimensions':meta['dimensions'],'encoding_format':'float'},ensure_ascii=False).encode(),
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
                response=request_json(request,timeout=60,phase='version_embedding',cache_namespace=meta['source_sha256'])
                if len(response.get('data',[]))!=1:raise ValueError('向量数量错误')
                vector=response['data'][0]['embedding']
                path.write_text(json.dumps(vector))
            if len(vector)!=meta['dimensions'] or not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in vector) or not any(vector):
                raise ValueError('新版本向量无效')
            return {**row,'vector':vector}
        with ThreadPoolExecutor(max_workers=2) as pool:records=list(pool.map(one,rows))
        (folder/'chunks-with-vectors.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records))
        plan['status']='embedded'
    (folder/'version-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
    return plan,folder


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--version',default='bounded_v2');parser.add_argument('--embed',action='store_true')
    parser.add_argument('--import-index',choices=('FLAT','HNSW'));parser.add_argument('--without-path',action='store_true');args=parser.parse_args()
    plan,folder=prepare(args.version,path_in_embedding=not args.without_path,embed=args.embed)
    if args.import_index:
        report=clone_index(plan['collection'],args.import_index,folder)
        print(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(plan,ensure_ascii=False,indent=2))
