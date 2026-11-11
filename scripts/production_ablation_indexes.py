"""生产留一法的隔离无前缀/原始食材集合；不激活，不覆盖已有集合。"""
import hashlib
import json
from pathlib import Path
from urllib.request import Request

from check_embedding import ROOT, config
from ingredient_recommendation import ingredient_fields
from knowledge_versions import clone_index
from naive_baseline import load_index
from rag_dataset import FOLDER
from service_runtime import request_json


def sha(value):return hashlib.sha256(value).hexdigest()


def build(kind):
    if kind not in ('plain','raw'):raise ValueError('未知消融索引')
    version='ablation_bounded_v4_'+kind
    target=ROOT/'data/processed/recipe_versions'/version
    target.mkdir(parents=True,exist_ok=True)
    meta=json.loads((FOLDER/'embedding-metadata.json').read_text())
    original=[json.loads(line) for line in (FOLDER/'chunks.jsonl').read_text().splitlines()]
    if sha((FOLDER/'chunks.jsonl').read_bytes())!=meta['source_sha256']:raise ValueError('活动文本哈希变化')
    source_meta=json.loads((FOLDER/'source-metadata.json').read_text())
    spec={'kind':kind,'base_sha256':meta['source_sha256'],'model':meta['requested_model'],
          'dimensions':meta['dimensions'],'base_url':meta['api_base_url']}
    spec_path=target/'ablation-spec.json'
    if spec_path.exists() and json.loads(spec_path.read_text())!=spec:raise ValueError('实验索引输入变化')
    spec_path.write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n')
    vectors={r['chunk_id']:r['vector'] for r in (json.loads(line) for line in (FOLDER/'chunks-with-vectors.jsonl').read_text().splitlines())}
    rows=[dict(row) for row in original];reused=0;generated=0
    if kind=='raw':
        for source,info in source_meta.items():
            required,optional,tools,uncertain,_=ingredient_fields(
                [r for r in rows if r['source']==source and '必备原料' in r['section']],
                (ROOT/source).read_text().splitlines(),normalize_aliases=False)
            info.update(ingredients=sorted(required),optional_ingredients=sorted(optional),tools=sorted(tools),ingredients_uncertain=uncertain)
            for row in rows:
                if row['source']==source:
                    for key in ('ingredients','optional_ingredients','tools','ingredients_uncertain'):row[key]=info[key]
        reused=len(rows)
    else:
        manifest,prior=load_index(ROOT/'data/processed/chunk-ablations-v1/h2_plain')
        if any(manifest['spec'][key]!=spec[key] for key in ('model','dimensions','base_url')):raise ValueError('无前缀向量模型不同')
        lookup={r['content']:r['vector'] for r in prior}
        vector_path=target/'new-vectors.jsonl'
        if vector_path.exists():
            for line in vector_path.read_text().splitlines():
                item=json.loads(line)
                if item['spec']!=spec:raise ValueError('已保存向量版本不符')
                lookup[item['content']]=item['vector']
        settings=config()
        reused=sum(row['content'] in lookup for row in rows)
        missing=list(dict.fromkeys(r['content'] for r in rows if r['content'] not in lookup))
        for offset in range(0,len(missing),8):
            batch=missing[offset:offset+8]
            request=Request(spec['base_url']+'/embeddings',data=json.dumps({'model':spec['model'],'dimensions':spec['dimensions'],
                'input':batch,'encoding_format':'float'},ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer '+settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
            response=request_json(request,timeout=120,attempts=1,phase='production_ablation_index',cache_namespace=sha(json.dumps(spec,sort_keys=True).encode()))
            items=sorted(response['data'],key=lambda item:item['index'])
            if len(items)!=len(batch) or [item['index'] for item in items]!=list(range(len(batch))):raise ValueError('向量响应数量或编号错误')
            with vector_path.open('a') as stream:
                for content,item in zip(batch,items):
                    lookup[content]=item['embedding'];stream.write(json.dumps({'content':content,'vector':item['embedding'],'spec':spec},ensure_ascii=False)+'\n')
            generated+=len(batch);print(f'无前缀新增向量 {offset+len(batch)}/{len(missing)}',flush=True)
        for row in rows:row['text']=row['content'];vectors[row['chunk_id']]=lookup[row['content']]
        meta.update(input_format='原始Markdown正文，不含菜名/章节/路径前缀',path_in_embedding=False)
    import math
    for row in rows:
        vector=vectors[row['chunk_id']]
        if len(vector)!=spec['dimensions'] or not all(type(x) in (int,float) and math.isfinite(x) for x in vector) or not any(vector):raise ValueError('无效实验向量')
        if sha((ROOT/row['source']).read_bytes())!=row['doc_hash']:raise ValueError('原文变化')
    text=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows)
    (target/'chunks.jsonl').write_text(text)
    (target/'chunks-with-vectors.jsonl').write_text(''.join(json.dumps({**row,'vector':vectors[row['chunk_id']]},ensure_ascii=False)+'\n' for row in rows))
    meta['source_sha256']=sha(text.encode())
    (target/'embedding-metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    (target/'source-metadata.json').write_text(json.dumps(source_meta,ensure_ascii=False,indent=2)+'\n')
    plan={'version':version,'collection':'recipe_'+version,'chunks':len(rows),'source_version':FOLDER.name,
          'production_enabled':False,'reused_vectors':reused,'unique_embedding_inputs':len({row['text'] for row in rows}),
          'new_vectors_this_invocation':generated,'status':'embedded'}
    (target/'version-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
    saved=ROOT/'data/processed/index-lab'/(plan['collection']+'.json')
    if saved.exists():
        report=json.loads(saved.read_text())
        if report.get('status')!='validated' or report.get('source_sha256')!=meta['source_sha256']:raise ValueError('已有集合不匹配')
    else:report=clone_index(plan['collection'],'HNSW',target)
    print(json.dumps({'kind':kind,'checked':report['checked'],**plan},ensure_ascii=False),flush=True)
    return target


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('kind',choices=('plain','raw'))
    build(parser.parse_args().kind)
