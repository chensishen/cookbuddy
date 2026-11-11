"""复制活动块并补元数据，复用全部原向量；校验后可原子激活，旧版本保留。"""
import argparse
import hashlib
import json
from pathlib import Path

from check_embedding import ROOT
from knowledge_versions import clone_index,activate_version,safe_collection
from recipe_metadata import enrich,SCHEMA_VERSION


def prepare(version,base=None):
    if base is None:
        from rag_dataset import FOLDER
        base=FOLDER
    base=Path(base);safe_collection('recipe_'+version)
    chunks=[json.loads(line) for line in (base/'chunks.jsonl').read_text().splitlines()]
    vectors={row['chunk_id']:row for row in (json.loads(line) for line in (base/'chunks-with-vectors.jsonl').read_text().splitlines())}
    metadata=json.loads((base/'embedding-metadata.json').read_text())
    if hashlib.sha256((base/'chunks.jsonl').read_bytes()).hexdigest()!=metadata['source_sha256']:raise ValueError('文本版本哈希不一致')
    if len(vectors)!=len(chunks):raise ValueError('向量与文本数量不同')
    for row in chunks:
        if any(vectors[row['chunk_id']].get(key)!=value for key,value in row.items()):raise ValueError('向量版本与文本不同')
    rows,sources=enrich(chunks)
    folder=ROOT/'data/processed/recipe_versions'/version
    folder.mkdir(parents=True,exist_ok=False)
    text=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows)
    (folder/'chunks.jsonl').write_text(text)
    (folder/'chunks-with-vectors.jsonl').write_text(''.join(json.dumps({**vectors[row['chunk_id']],**row},ensure_ascii=False)+'\n' for row in rows))
    metadata.update(source_sha256=hashlib.sha256(text.encode()).hexdigest(),metadata_schema_version=SCHEMA_VERSION,
        metadata_policy='quality_score=(5-解析标记数)/5；不是语义置信度；ingredients仅为可识别必备原料表条目')
    (folder/'embedding-metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    plan={'version':version,'collection':'recipe_'+version,'chunks':len(rows),'sources':len(sources),
        'reusable_vectors':len(rows),'new_vectors':0,'source_version':base.name,'status':'embedded'}
    (folder/'version-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
    (folder/'source-metadata.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2)+'\n')
    if (base/'source-manifest.json').exists():(folder/'source-manifest.json').write_bytes((base/'source-manifest.json').read_bytes())
    return plan,folder


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--version',required=True)
    parser.add_argument('--import-index',action='store_true');parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    plan,folder=prepare(args.version)
    if args.import_index:clone_index(plan['collection'],'HNSW',folder)
    if args.activate:
        change=activate_version(args.version)
        (folder/'activation.json').write_text(json.dumps(change,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(plan,ensure_ascii=False))
