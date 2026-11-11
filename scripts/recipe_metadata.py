"""在原文逐行一致的前提下补齐实体字段；质量分是解析检查表，不是事实置信度。"""
import hashlib
import json
from pathlib import Path
import subprocess

from check_embedding import ROOT
from ingredient_recommendation import ingredient_fields
from recipe_catalog import build_catalog
from search_filters import filter_chunks

SCHEMA_VERSION=1


def enrich(chunks,root=ROOT,commit=None,dirty_sources=()):
    root=Path(root)
    if commit is None:
        repo=root/'data/HowToCook'
        commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        dirty=subprocess.check_output(['git','-C',str(repo),'status','--porcelain','--','dishes'],text=True)
        if dirty.strip():raise ValueError('来源仓库有未提交变化，先明确版本再构建元数据')
    _,_,known=filter_chunks(chunks,max_difficulty=5)
    grouped={}
    for row in chunks:grouped.setdefault(row['source'],[]).append(row)
    metadata={}
    for item in build_catalog(chunks):
        source=item['source'];path=(root/source).resolve()
        if not path.is_relative_to((root/'data/HowToCook').resolve()):raise ValueError('来源路径越界')
        raw=path.read_bytes();lines=raw.decode('utf-8').splitlines();rows=grouped[source]
        for row in rows:
            if lines[row['source_start_line']-1:row['source_end_line']]!=row['content'].splitlines():
                raise ValueError('块与原文行不一致，不能补元数据')
        ingredients_rows=[row for row in rows if '必备原料' in row['section']]
        required,optional,tools,uncertain,_=ingredient_fields(ingredients_rows,lines)
        difficulty=known.get(source,{}).get('difficulty',0)
        flags=[]
        if not difficulty:flags.append('difficulty_unknown')
        if not ingredients_rows:flags.append('ingredient_table_missing')
        if not required:flags.append('ingredients_empty')
        if uncertain:flags.append('ingredients_uncertain')
        if source in dirty_sources:flags.append('source_dirty')
        metadata[source]={'category':item['category'],'difficulty':difficulty,
            'ingredients':sorted(required),'optional_ingredients':sorted(optional),'tools':sorted(tools),
            'ingredients_uncertain':uncertain,'source_commit':commit,'doc_hash':hashlib.sha256(raw).hexdigest(),
            'quality_score':(5-len(flags))/5,'flags':json.dumps(flags,ensure_ascii=False),
            'parser_version':'recipe-metadata-v1','metadata_schema_version':SCHEMA_VERSION}
    result=[]
    for row in chunks:
        section=row['section']
        kind=next((kind for word,kind in (('必备原料','ingredients'),('计算','calc'),('操作','steps'),('简介','overview_source')) if word in section),'extra')
        result.append({**row,**metadata[row['source']],'chunk_type':kind,'heading_path':row['title']+' > '+section})
    return result,metadata
