"""来源隔离、封存和一次性测试；不把程序标注当成人工盲评。"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import random
import re

from check_embedding import ROOT
from recipe_tools import load_chunks


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_sources(value):
    if isinstance(value,str):
        return {value} if value.startswith('data/HowToCook/dishes/') and value.endswith('.md') else set()
    if isinstance(value,dict):
        return set().union(*(fixture_sources(item) for pair in value.items() for item in pair))
    if isinstance(value,list):return set().union(*(fixture_sources(item) for item in value))
    return set()


def make_holdout(path,source_count=32):
    if type(source_count) is not int or source_count<1:raise ValueError('来源数量须为正整数')
    if path.exists():raise ValueError('不覆盖已有评测集')
    chunks=load_chunks();groups={};names={}
    for row in chunks:
        groups.setdefault(row['source'],[]).append(row);names.setdefault(row['title'],set()).add(row['source'])
    dev_sources=set();old_questions=set()
    for fixture in (ROOT/'data/evaluation').glob('*.json'):
        try:data=json.loads(fixture.read_text())
        except ValueError:continue
        dev_sources.update(fixture_sources(data))
        if isinstance(data,dict):old_questions.update(case['question'] for case in data.get('cases',[]) if isinstance(case,dict) and isinstance(case.get('question'),str))
    eligible=[]
    for source,rows in sorted(groups.items()):
        if source in dev_sources or len(names[rows[0]['title']])!=1:continue
        sections={kind:[r for r in rows if r['section'].split(' > ')[0]==section]
            for kind,section in [('ingredients','必备原料和工具'),('amounts','计算'),('procedure','操作')]}
        if all(sections.values()) and all(any(len(r['content'].strip())>10 for r in group) for group in sections.values()):
            eligible.append((source,rows[0]['title'],sections))
    rng=random.Random(20261020);rng.shuffle(eligible)
    categories={};selected=[]
    for row in eligible:categories.setdefault(row[0].split('/')[3],[]).append(row)
    for category in sorted(categories):selected.append(categories[category][0])
    selected.extend(row for row in eligible if row not in selected)
    selected=selected[:source_count]
    if len(selected)!=source_count:raise ValueError('独立来源不足')
    cases=[];sources={};templates={
        'ingredients':'做{title}之前，需要准备哪些食材和器具？',
        'amounts':'{title}的配方材料分别要准备多少？',
        'procedure':'{title}从开始制作到完成，应该按什么顺序操作？'}
    for number,(source,title,sections) in enumerate(selected):
        sources[source]=sha(ROOT/source)
        for kind,rows in sections.items():
            content='\n'.join(r['content'] for r in rows)
            reference=re.sub(r'^#{1,6}\s+.*\n?','',content,flags=re.M)
            reference=re.sub(r'!\[[^\]]*\]\([^)]*\)','',reference).strip()
            cases.append({'id':f'heldout_{number:02d}_{kind}','type':kind,'question':templates[kind].format(title=title),
                'gold':[r['chunk_id'] for r in rows],'sources':[source],'required_terms':[],
                'answer_eval':number<8 and kind==('ingredients','amounts','procedure')[number%3],
                'reference_answer':reference,'reference_key_points':[reference],
                'annotation_method':'原文对应章节规则标注；未经两位人工评审认证'})
    unknown_pool=('银河尘埃布丁怎么做？','木星气旋寿司要哪些原料？','请给出电磁波炖星云的做法。','知识库中有暗物质汤圆的配方吗？',
        '磁单极子焗银河薄饼怎么做？','反物质琉璃炒时空要哪些原料？','请给出量子真空黑洞羹的做法。','知识库中有引力波晶格蒸星尘的配方吗？')
    unknowns=[question for question in unknown_pool if question not in old_questions][:4]
    if len(unknowns)!=4:raise ValueError('未消费库外题不足，不能重复使用已消费题冒充新测试')
    from retrieval_candidates import match_titles
    if any(match_titles(question,names) for question in unknowns):raise ValueError('库外题匹配活动目录，需要重新标注')
    for number,question in enumerate(unknowns):
        cases.append({'id':f'heldout_unknown_{number}','question':question,'type':'out_of_library','gold':[],
            'sources':[],'required_terms':[],'answer_eval':True,'reference_answer':'知识库未收录该菜谱，无法依据来源给出配方。',
            'annotation_method':'虚构菜名；封存前核对活动目录无匹配'})
    data={'version':path.stem,'split':'test','scope':'来源隔离封存测试：规则构造并按原文章节标注，不是人工盲评认证',
        'source_hashes':sources,'dev_sources':sorted(dev_sources),'seed':20261020,'cases':cases}
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    return data


def seal(path):
    from evaluate_prd import code_fingerprint
    manifest=path.with_suffix('.seal.json')
    if manifest.exists():raise ValueError('测试集已封存，不能覆盖；已见过结果的来源不能重新标为盲测')
    data=json.loads(path.read_text());overlap=set(data['source_hashes'])&set(data['dev_sources'])
    if overlap:raise ValueError('dev/test来源泄漏')
    result={'fixture_sha256':sha(path),'code_sha256':code_fingerprint(),'sealed_at':datetime.now(timezone.utc).isoformat(),
            'source_overlap':0,'test_sources':len(data['source_hashes']),'state':'sealed'}
    manifest.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');return result


def consume(path,out):
    from evaluate_prd import code_fingerprint
    manifest=path.with_suffix('.seal.json');data=json.loads(path.read_text());record=json.loads(manifest.read_text())
    if record['state']!='sealed':raise ValueError('封存测试已执行；后续运行须作为开发诊断，不能重复挑选test结果')
    if record['fixture_sha256']!=sha(path) or record['code_sha256']!=code_fingerprint():raise ValueError('测试封存后数据或代码已变化')
    if set(data['source_hashes'])&set(data['dev_sources']):raise ValueError('测试来源泄漏')
    record.update(state='consumed',report=str(out/'report.json'),started_at=datetime.now(timezone.utc).isoformat())
    manifest.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');return record


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','seal'));parser.add_argument('path',type=Path)
    parser.add_argument('--sources',type=int,default=32);args=parser.parse_args()
    result=make_holdout(args.path,args.sources) if args.action=='prepare' else seal(args.path)
    print(json.dumps({'action':args.action,'path':str(args.path),'cases':len(result.get('cases',[]))},ensure_ascii=False))
