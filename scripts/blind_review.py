"""匿名人工盲评包及严格导入；不会自动填写真人结论。"""
import argparse
import hashlib
import json
from pathlib import Path
import random
from check_embedding import ROOT

FIELDS=('correct','complete','citation_supported')


def answer_hash(row):
    return hashlib.sha256(json.dumps(row.get('result',{}),sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def kappa(left,right):
    if not left or len(left)!=len(right):raise ValueError('一致性样本必须非空且配对')
    observed=sum(a==b for a,b in zip(left,right))/len(left)
    p=sum(left)/len(left);q=sum(right)/len(right);expected=p*q+(1-p)*(1-q)
    return None if expected==1 else (observed-expected)/(1-expected)


def export_packet(paths,folder,limit=50):
    if folder.exists():raise ValueError('不覆盖已有盲评包')
    if type(limit) is not int or limit<1:raise ValueError('抽样量须为正整数')
    candidates={};rng=random.Random(20261020)
    for path in paths:
        report=json.loads(path.read_text());fixture=ROOT/report['fixture_path']
        if hashlib.sha256(fixture.read_bytes()).hexdigest()!=report['fixture_sha256']:raise ValueError('题集版本变化')
        cases={r['id']:r for r in json.loads(fixture.read_text())['cases']}
        for index,row in enumerate(report['answers']):
            candidates.setdefault((row['variant'],row['type']),[]).append((path,index,row,cases[row['case']]))
    for rows in candidates.values():rng.shuffle(rows)
    chosen=[]
    while len(chosen)<limit and any(candidates.values()):
        for key in sorted(candidates):
            if candidates[key] and len(chosen)<limit:chosen.append(candidates[key].pop())
    if len(chosen)<limit:raise ValueError('可盲评答案不足；不要重复答案凑样本')
    rng.shuffle(chosen);items=[];mapping=[]
    for number,(path,index,row,case) in enumerate(chosen):
        ident=f'B{number+1:03d}';result=row.get('result',{})
        sources=set(case['sources'])|{e['source'] for e in result.get('evidence',[]) if isinstance(e,dict) and isinstance(e.get('source'),str)}
        originals=[]
        for source in sorted(sources):
            original=(ROOT/source).resolve()
            if original.is_relative_to((ROOT/'data/HowToCook/dishes').resolve()) and original.is_file():
                originals.append({'source':source,'text':original.read_text()})
        items.append({'item_id':ident,'question':case['question'],'answer':result.get('short_answer',''),
            'evidence':result.get('evidence',[]),'originals':originals,
            'reference_key_points':case.get('reference_key_points',[]),'out_of_library':not bool(case['gold']),
            'execution_failure':bool(row.get('failure_type'))})
        mapping.append({'item_id':ident,'report':str(path.resolve()),'answer_index':index,'answer_sha256':answer_hash(row)})
    folder.mkdir(parents=True)
    packet={'rubric':'仅依据原文逐项核对事实、完整性、引用支持；库外必须拒答；失败/空答案判错。独立完成后再讨论。不得查看private-mapping.json。',
        'items':items}
    (folder/'packet.json').write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n')
    (folder/'private-mapping.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2)+'\n')
    for number in (1,2):
        template={'reviewer_id':'','attestation':'','reviews':[{'item_id':item['item_id'],
            'correct':None,'complete':None,'citation_supported':None,'usability_1_to_5':None,'citation_usability_1_to_5':None,'reason':''} for item in items]}
        (folder/f'reviewer-{number}.json').write_text(json.dumps(template,ensure_ascii=False,indent=2)+'\n')
    (folder/'README.md').write_text('# 人工盲评\n\n将 packet.json 分别交给两位真实评审；每人填写自己的 reviewer-N.json。'
        'reviewer_id 使用不同的真实评审标识，attestation 填写“本人独立完成评审”。'
        '三项布尔值、1–5 分可用性和理由均必填。不要查看 private-mapping.json。\n\n'
        '完成后运行 `python scripts/blind_review.py import <本目录>`。空白模板不能通过验收；κ 在所有评分恒定时记为不可定义。\n')
    return {'items':len(items),'folder':str(folder)}


def import_reviews(folder):
    packet=json.loads((folder/'packet.json').read_text());mapping=json.loads((folder/'private-mapping.json').read_text())
    ids={r['item_id'] for r in packet['items']}
    if len(ids)!=len(packet['items']) or ids!={r['item_id'] for r in mapping} or len(mapping)!=len(ids):raise ValueError('盲评包映射错误')
    reviewers=[]
    for number in (1,2):
        data=json.loads((folder/f'reviewer-{number}.json').read_text());ident=data.get('reviewer_id','').strip()
        if not ident or data.get('attestation')!='本人独立完成评审':raise ValueError('须由真实评审独立填写并声明')
        rows=data.get('reviews',[])
        if len(rows)!=len(ids) or {r.get('item_id') for r in rows}!=ids:raise ValueError('漏评、重复或未知item')
        for row in rows:
            if any(type(row.get(field)) is not bool for field in FIELDS):raise ValueError('三项评分须为布尔值')
            for field in ('usability_1_to_5','citation_usability_1_to_5'):
                if type(row.get(field)) is not int or not 1<=row[field]<=5:raise ValueError('回答与引用可用性须为1至5整数')
            if not isinstance(row.get('reason'),str) or not row['reason'].strip():raise ValueError('须填写理由')
        reviewers.append((ident,{r['item_id']:r for r in rows}))
    if reviewers[0][0]==reviewers[1][0]:raise ValueError('需要两位不同评审')
    ordered=sorted(ids);calibration={};reports={}
    for entry in mapping:
        path=Path(entry['report'])
        if path not in reports:reports[path]=json.loads(path.read_text())
        row=reports[path]['answers'][entry['answer_index']]
        if answer_hash(row)!=entry['answer_sha256']:raise ValueError('盲评后答案发生变化')
    for field in FIELDS:
        left=[reviewers[0][1][i][field] for i in ordered];right=[reviewers[1][1][i][field] for i in ordered]
        calibration[field]={'human_kappa':kappa(left,right),'n':len(ordered)}
        pairs=[]
        for entry in mapping:
            row=reports[Path(entry['report'])]['answers'][entry['answer_index']]
            human=[r[1][entry['item_id']][field] for r in reviewers]
            judge=row.get('llm_judge',{}).get('scores',{}).get(field)
            if type(judge) is bool and human[0]==human[1]:pairs.append((human[0],judge))
        calibration[field]['judge_consensus_kappa']=kappa(*zip(*pairs)) if pairs else None
        calibration[field]['judge_consensus_n']=len(pairs)
    result={'reviewer_ids':[r[0] for r in reviewers],'n':len(ordered),'calibration':calibration,
        'policy':'分歧保持未决；κ不可定义不能认证judge。κ≥0.6与至少40条共识为本项目保守暂定线，非PRD指定阈值。',
        'calibrated':len(ordered)>=50 and all(c['human_kappa'] is not None and c['human_kappa']>=.6
            and c['judge_consensus_kappa'] is not None and c['judge_consensus_kappa']>=.6
            and c['judge_consensus_n']>=40 for c in calibration.values())}
    for entry in mapping:
        row=reports[Path(entry['report'])]['answers'][entry['answer_index']]
        row['human_reviews']=[{'reviewer_id':ident,**reviews[entry['item_id']]} for ident,reviews in reviewers]
    for path,report in reports.items():
        report['human_calibration']=result
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (folder/'calibration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('export','import'))
    parser.add_argument('folder',type=Path);parser.add_argument('reports',nargs='*',type=Path);parser.add_argument('--limit',type=int,default=50)
    args=parser.parse_args();result=export_packet(args.reports,args.folder,args.limit) if args.action=='export' else import_reviews(args.folder)
    print(json.dumps(result,ensure_ascii=False))
