"""Only checked sources are formatted; model prose is never a fallback."""


def literal_evidence(row):
    from .config import ROOT
    source=row.get('source','');start=row.get('source_start_line');end=row.get('source_end_line')
    path=(ROOT/source).resolve()
    if not path.is_relative_to(ROOT/'data/HowToCook/dishes') or not path.is_file():return None
    lines=path.read_text().splitlines()
    if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):return None
    quote='\n'.join(lines[start-1:end])
    if quote!=row.get('content'):return None
    return {'source':source,'title':row.get('title',''),'section':row.get('section',''),
            'start_line':start,'end_line':end,'quote':quote,'evidence_type':'markdown_original'}


def fallback(run, entries=None):
    run.degraded=True;run.quality_limited=True
    outputs=[]
    seen=set();groups={}
    for entry in run.evidence.values() if entries is None else entries:
        full=entry['full']
        if not full.get('artifact') and full.get('status') in ('verified','verified_source','insufficient'):
            outputs.append({'tool':entry['name'],'result':full})
            continue
        compact=entry['compact']
        if compact['status'] not in ('ok','degraded'):continue
        for row in compact.get('items',[]):
            key=(row['source_path'],row['section'],row['line_range'],row['snippet'])
            if key in seen:continue
            seen.add(key)
            source=row['source_path']
            if source not in groups:
                result={'status':'degraded','short_answer':f"本次未能核实完整结论，以下仅为候选原文：\n《{row['dish_name']}》",'sources':[],'evidence':[]}
                groups[source]=result;outputs.append({'tool':entry['name'],'result':result})
            result=groups[source]
            for original in full.get('rows',[]):
                if original.get('source')==source and original.get('section')==row['section']:
                    evidence=literal_evidence(original)
                    if evidence and evidence not in result['evidence']:result['evidence'].append(evidence)
            if len(result['sources'])<3:result['short_answer']+=f"\n{row['section']}：{row['snippet']}"
            result['sources'].append({'title':row['dish_name'],'section':row['section'],'source':source,'line_range':row['line_range']})
    return coalesce_originals(outputs) or [{'tool':'fallback','result':{'status':'tool_failed','short_answer':'服务繁忙，请稍后重试','sources':[],'evidence':[]}}]


def coalesce_originals(outputs):
    details={}
    for output in outputs:
        result=output['result'];sources={r.get('source') for r in result.get('sources',[])}
        if output['tool']=='get_recipe_detail' and result.get('status')=='verified_source' and len(sources)==1:
            details[next(iter(sources))]=output
    kept=[]
    for output in outputs:
        result=output['result'];sources={r.get('source') for r in result.get('sources',[])}
        detail=details.get(next(iter(sources))) if len(sources)==1 else None
        if result.get('status')=='degraded' and detail:
            full=detail['result']
            detail['result']={**full,'notice':'未能核实本次问题的完整结论，以下为已核对的原文。'}
        else:kept.append(output)
    return kept
