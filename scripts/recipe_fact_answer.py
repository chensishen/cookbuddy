"""Narrow literal fact lookup; interpretive/modified recipes still use full QA."""
import re
from preference_recommendation import plain


def source_fact(question,chunks):
    return serving_quantity_fact(question,chunks) or duration_relation_fact(question,chunks) or starter_fact(question,chunks) or baking_fact(question,chunks) or substitution_fact(question,chunks) or oil_options_fact(question,chunks) or ingredient_fact(question,chunks) or quantity_table_fact(question,chunks) or procedure_fact(question,chunks)


def serving_quantity_fact(question,chunks):
    from cookbuddy.ltm.guard import current,source_allowed
    from recipe_quantity_answer import select_quantity_plan

    plan=select_quantity_plan(question,chunks)
    if not plan or not source_allowed(plan['source'],current()):return None
    source=plan['source']
    rows=[row for row in chunks if row['source']==source]
    evidence_by_line={}
    claims=[]
    for calculation in plan['calculations']:
        refs=[]
        for item in calculation['evidence']:
            line=item['line']
            if line not in evidence_by_line:
                row=next(r for r in rows if r['source_start_line']<=line<=r['source_end_line'])
                evidence_by_line[line]={'reference_id':len(evidence_by_line)+1,'source':source,
                    'title':row['title'],'section':row['section'],'start_line':line,'end_line':line,
                    'quote':item['quote'],'evidence_type':'markdown_original'}
            refs.append(evidence_by_line[line]['reference_id'])
        claims.append(calculation['text']+''.join(f'[{ref}]' for ref in refs))
    answer='按原文核对 '+str(plan['servings'])+' 份的用量：\n\n'+'\n\n'.join(claims)
    evidence=list(evidence_by_line.values())
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'source_quantity_calculator',
            'sources':[{**{k:e[k] for k in ('reference_id','source','title','section')},'line':e['start_line']} for e in evidence],
            'evidence':evidence,'images':[]}


def duration_relation_fact(question,chunks):
    """Separate explicitly ordered rest/fermentation steps in the same section."""
    from recipe_tools import resolve_title,local_file
    from cookbuddy.ltm.guard import current,source_allowed
    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    if current() and not source_allowed(source,current()):return None
    clean=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',question).strip()
    clean=re.sub(r'\n当前菜谱：'+re.escape(title)+r'\s*$','',clean).strip()
    clean=re.sub(r'^(?:请问|请)?'+re.escape(title)+r'(?:的)?','',clean)
    clean=re.sub(r'[。.!！?？；;]\s*(?:请)?(?:只|仅)?(?:依据|根据)原文回答[。.!！?？]*$','',clean)
    match=re.fullmatch(r'(?:这里的|这一步的|这道菜的)?(?:酵头的)?发酵(?:时间|时长)(?:是否)?(?:包含|包括)(?:了)?(?:最开始|开始|前面|之前)?静置(?:的)?(?:(\d+)\s*分钟|时间)(?:吗|么)?[。.!！?？]*',clean)
    if not match:return None
    lines=local_file(source).read_text().splitlines();sections={}
    for row in chunks:
        if row['source']==source and row['section'].split(' > ')[0]=='操作':
            sections.setdefault(row['section'],set()).update(range(row['source_start_line']-1,row['source_end_line']))
    pairs=[]
    duration=r'(\d+(?:\s*(?:~|～|-|至|到)\s*\d+)?)\s*分钟'
    for section,indices in sections.items():
        steps=[i for i in sorted(indices) if re.match(r'^\s*\d+[.、]',lines[i])]
        for first in steps:
            rest=re.search(r'静置\s*(\d+)\s*分钟',plain(lines[first]))
            if not rest or '之后' not in lines[first] or '发酵' in lines[first] or match[1] and rest[1]!=match[1]:continue
            for last in steps:
                text=plain(lines[last])
                ferment=re.search(r'(?:时长为|发酵)\s*'+duration,text)
                if last>first and '发酵' in text and not re.search(r'静置|包含|包括|累计|总计|总时长',text) and ferment:
                    pairs.append((section,first,last,rest[1],ferment[1]))
    if len(pairs)!=1:return None
    section,first,last,rest,ferment=pairs[0]
    evidence=[{'reference_id':n,'source':source,'title':title,'section':section,
               'start_line':i+1,'end_line':i+1,'quote':lines[i],'evidence_type':'markdown_original'}
              for n,i in enumerate((first,last),1)]
    rest_text=plain(re.sub(r'^\s*\d+[.、]\s*','',lines[first]))
    ferment_text=plain(re.sub(r'^\s*\d+[.、]\s*','',lines[last]))
    answer='按原文的步骤顺序，不包含。\n\n静置步骤：'+rest_text+'[1]\n\n随后另一个编号步骤是发酵：'+ferment_text+'[2]\n\n前面的静置'+rest+'分钟不计入后一个发酵步骤的'+ferment+'分钟。'
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'ordered_duration_relation',
            'sources':[{'reference_id':e['reference_id'],'source':source,'title':title,'section':section,'line':e['start_line']} for e in evidence],
            'evidence':evidence,'images':[],'validation':'按同一章节中明确编号及先后顺序区分静置与发酵；两条原文依据完整保留。'}


def starter_fact(question,chunks):
    """Read unchanged starter parameters only when all requested facts exist."""
    from recipe_tools import resolve_title,local_file
    from cookbuddy.ltm.guard import current,source_allowed
    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    if current() and not source_allowed(source,current()):return None
    clean=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',question).strip()
    clean=re.sub(r'\n当前菜谱：'+re.escape(title)+r'\s*$','',clean).strip()
    clean=re.sub(r'^(?:请问|请)?'+re.escape(title)+r'(?:的)?','',clean)
    if not all(word in clean for word in ('酵头','酵母','水温','发酵')):return None
    vocabulary=r'酵头|干酵母|酵母|水温|发酵|静置|需要|多少|多久|多长|时间|用量|分别|区分|说明|原文|依据|给出|请|并|和|与|及|是|的'
    if re.sub(r'[\s，,。；;！!？?、：:]','',re.sub(vocabulary,'',clean)):return None
    lines=local_file(source).read_text().splitlines()
    blocks=[i for i,line in enumerate(lines) if re.fullmatch(r'\s*[-*+]\s+酵头\s*',plain(line))
            and any(r['source']==source and r['section'].split(' > ')[0]=='计算'
                    and r['source_start_line']<=i+1<=r['source_end_line'] for r in chunks)]
    if len(blocks)!=1:return None
    start=blocks[0];end=start+1
    while end<len(lines) and (not lines[end].strip() or lines[end].startswith((' ','\t'))):end+=1
    amounts=[plain(line).strip(' -*\t') for line in lines[start+1:end]]
    yeast=[line for line in amounts if re.search(r'酵母\s*\d',line)]
    water=[line for line in amounts if re.search(r'\d\s*(?:℃|°C)',line) and '水' in line]
    section_lines={i for r in chunks if r['source']==source and '操作' in r['section'] and '酵头' in r['section']
                   for i in range(r['source_start_line']-1,r['source_end_line'])}
    steps=[i for i in sorted(section_lines) if re.match(r'^\s*\d+[.、]',lines[i])]
    rest=[i for i in steps if re.search(r'静置\s*\d+\s*分钟',lines[i])]
    ferment=[i for i in steps if '发酵' in lines[i] and re.search(r'\d+\s*分钟',lines[i])]
    if len(yeast)!=1 or len(water)!=1 or len(rest)!=1 or len(ferment)!=1:return None
    evidence=[]
    def cite(first,last):
        section=next(r['section'] for r in chunks if r['source']==source and r['source_start_line']<=first+1<=r['source_end_line'])
        evidence.append({'reference_id':len(evidence)+1,'source':source,'title':title,'section':section,
            'start_line':first+1,'end_line':last,'quote':'\n'.join(lines[first:last]),'evidence_type':'markdown_original'})
        return '['+str(len(evidence))+']'
    answer='### 酵头原配方\n'+yeast[0]+'；'+water[0]+'。'+cite(start,end)
    for label,index in [('静置时间',rest[0]),('发酵时间',ferment[0])]:
        answer+='\n\n### '+label+'\n'+plain(re.sub(r'^\s*\d+[.、]\s*','',lines[index]))+cite(index,index+1)
    intro=[i for i in sorted(section_lines) if '酵头' in lines[i] and '发酵' in lines[i]
           and re.search(r'\d+\s*(?:分钟|天)',lines[i]) and not re.match(r'^\s*[>\d]',lines[i])]
    if intro:
        answer+='\n\n### 其他时间说明（与上述操作步骤分列）\n'+'\n'.join(plain(lines[i])+cite(i,i+1) for i in intro)
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'literal_starter_fact',
        'sources':[{'reference_id':e['reference_id'],'source':source,'title':title,'section':e['section'],'line':e['start_line']} for e in evidence],
        'evidence':evidence,'images':[],'validation':'逐字核对酵头原配方及静置、发酵步骤；不改配方或推算替代参数。'}


def quantity_table_fact(question,chunks):
    """只交付单菜原配方用量；不处理人数换算、替代或新增限制。"""
    from recipe_tools import resolve_title,checked_chunks
    from retrieval_candidates import normalize_recipe_aliases
    from cookbuddy.ltm.guard import current,source_allowed
    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    if current() and not source_allowed(source,current()):return None
    question=normalize_recipe_aliases(question)
    question=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',question).strip()
    head=r'(?:请问|请告诉我|告诉我|请)?'+re.escape(title)+r'(?:的)?'
    material=r'(?:原配方|原文配方|配方)?(?:材料|原料|食材|配料)'
    patterns=(head+material+r'(?:分别|各自|各)?(?:要|需要)?(?:准备|用)?多少[？?。]*',
              head+r'(?:原配方|原文)?(?:材料|原料|食材|配料)?用量(?:分别|各)?(?:是|为)?多少[？?。]*')
    if not any(re.fullmatch(pattern,question) for pattern in patterns):return None
    rows=checked_chunks(source)
    amounts=[row for row in rows if row['section'].split(' > ')[0]=='计算']
    if not amounts:return None
    selected=[row for row in rows if row['section'].split(' > ')[0] in ('必备原料和工具','计算')]
    evidence=[{'reference_id':i,'source':source,'title':title,'section':row['section'],
        'start_line':row['source_start_line'],'end_line':row['source_end_line'],'quote':row['content'],
        'evidence_type':'markdown_original'} for i,row in enumerate(selected,1)]
    answer='以下为原配方的材料和用量说明，保留原文份量、区间、公式及可选项，未按人数换算。\n\n'+\
        '\n\n'.join(item['quote']+'\n['+str(item['reference_id'])+']' for item in evidence)
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'source_quantity_table',
        'sources':[{'reference_id':item['reference_id'],'source':source,'title':title,'section':item['section'],
                    'line':item['start_line']} for item in evidence],
        'evidence':evidence,'images':[],
        'validation':'逐字核对当前原文原料表与计算章节，保留份量、公式、区间和可选项；不推断换算、替代或表外用量。'}


def ingredient_fact(question,chunks):
    """只摘录明确单菜名的原料表；数量、替代、限制和组合问题继续走 QA。"""
    from recipe_tools import resolve_title,checked_chunks
    from retrieval_candidates import normalize_recipe_aliases
    from cookbuddy.ltm.guard import current,source_allowed
    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    if current() and not source_allowed(source,current()):return None
    normalized=normalize_recipe_aliases(question)
    normalized=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',normalized).strip()
    pattern=r'(?:请问|请告诉我|告诉我|请)?'+re.escape(title)+r'(?:的)?(?:要|需要|需用|用)?(?:哪些|什么)(?:原料|食材|材料|配料)[？?。]*'
    preparation=r'(?:请问|请告诉我|告诉我|请)?(?:做|制作)?'+re.escape(title)+r'(?:之前|前)?[，,]?(?:需要|要|需)?(?:准备|备齐)?(?:哪些|什么)(?:原料|食材|材料|配料)(?:和(?:器具|工具))?[？?。]*'
    listing=r'(?:请问|请告诉我|告诉我|请)?'+re.escape(title)+r'(?:的)?(?:原料|食材|材料|配料)(?:清单)?(?:有|是)?(?:哪些|什么)[？?。]*'
    if not any(re.fullmatch(p,normalized) for p in (pattern,preparation,listing)):return None
    rows=[r for r in checked_chunks(source) if '必备原料' in r['section']]
    if not rows:return None
    evidence=[{'reference_id':i,'source':source,'title':title,'section':r['section'],
        'start_line':r['source_start_line'],'end_line':r['source_end_line'],'quote':r['content'],
        'evidence_type':'markdown_original'} for i,r in enumerate(rows,1)]
    answer='\n\n'.join(e['quote']+'\n['+str(e['reference_id'])+']' for e in evidence)
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'source_ingredient_table',
        'sources':[{'reference_id':e['reference_id'],'source':source,'title':title,'section':e['section'],
                    'line':e['start_line']} for e in evidence],
        'evidence':evidence,'images':[],
        'validation':'逐字摘录当前指定菜谱的必备原料表，保留可选项及工具，不推断替代、用量或表外原料。'}


def substitution_fact(question,chunks):
    """State only whether the named replacement appears in the current source line."""
    from recipe_tools import resolve_title,checked_chunks,local_file
    from cookbuddy.ltm.guard import current,source_allowed
    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    if current() and not source_allowed(source,current()):return None
    clean=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',question).strip()
    clean=re.sub(r'\n当前菜谱：'+re.escape(title)+r'\s*$','',clean).strip()
    clean=re.sub(r'^(?:请问|请)?'+re.escape(title)+r'(?:里面|里|中)?(?:的)?','',clean).strip()
    combined=re.match(r'(?P<target>[\u4e00-\u9fff]{1,12}?)(?:需要|要|用)多少[？?。；;，,]\s*(?:可以|能|能否)用(?P<replacement>[\u4e00-\u9fff]{1,12}?)(?:来)?(?:替换|替代|代替)(?:吗|么)?',clean)
    match=combined or re.search(r'(?P<target>[\u4e00-\u9fff]{1,12}?)(?:能|可以|能否|可不可以)(?:用|拿)?'
                    r'(?P<replacement>[\u4e00-\u9fff]{1,12}?)(?:来)?(?:替换|替代|代替)(?:吗|么|嘛)?',clean)
    if not match:return None
    target,replacement=match['target'],match['replacement']
    tail=clean[match.end():].strip()
    if tail and not re.fullmatch(r'[，,。；;！!？?\s]*原文(?:没|没有)(?:说明|提到)的?请?(?:明确)?(?:指出|说明)?[。！？!? ]*',tail):
        return None
    rows=[row for row in checked_chunks(source) if row['section'].split(' > ')[0] in ('必备原料和工具','计算')]
    hits=[]
    for row in rows:
        for offset,line in enumerate(row['content'].splitlines()):
            if target in plain(line):hits.append((row,row['source_start_line']+offset,line))
    if not hits:return None
    quantities=[hit for hit in hits if hit[0]['section'].split(' > ')[0]=='计算'
                and re.search(r'=\s*\d',hit[2])]
    if combined and len(quantities)!=1:return None
    row,line_no,line=next(((row,line_no,line) for row,line_no,line in hits if replacement in plain(line)),quantities[0] if combined else hits[0])
    quoted=re.sub(r'^\s*[-*+]\s*','',line).strip()
    if replacement in plain(line):
        answer='原文在同一条原料说明中同时提到“'+target+'”和“'+replacement+'”；这里只确认原文列出了该选项，不推断二者在所有工序中完全等价。\n\n原文：'+quoted
    else:
        answer='原文没有提到可以用“'+replacement+'”替换“'+target+'”。当前原料表原文为：“'+quoted+'”。仅按原文判断，不推测替换比例、效果或工序等价性。'
    if combined:
        quantified=re.sub(r'^\s*[-*+]\s*','',quantities[0][2]).strip()
        answer='原文给出的用量是：'+quantified+'。\n\n'+answer
    evidence={'reference_id':1,'source':source,'title':title,'section':row['section'],
              'start_line':line_no,'end_line':line_no,'quote':line,'evidence_type':'markdown_original'}
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'literal_substitution_boundary',
            'sources':[{'reference_id':1,'source':source,'title':title,'section':row['section'],'line':line_no}],
            'evidence':[evidence],'images':[],
            'validation':'仅核对当前菜谱原料说明是否出现被询问的替代食材；不生成比例、效果或未写入原文的替代关系。'}


def oil_options_fact(question,chunks):
    """Read explicitly listed oil options and their caveats from one source line."""
    from cookbuddy.ltm.guard import current,source_allowed
    from recipe_tools import checked_chunks,resolve_title
    from src.agent.turn_intents import original_oil_options_request

    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    if current() and not source_allowed(source,current()):return None
    clean=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',question).strip()
    clean=re.sub(r'\n当前菜谱：'+re.escape(title)+r'\s*$','',clean).strip()
    clean=re.sub(r'^(?:请问)?'+re.escape(title)+r'(?:里|中)?(?:的)?','',clean)
    if not original_oil_options_request(clean):return None

    matches={}
    for row in checked_chunks(source):
        if row['section'].split(' > ')[0]!='必备原料和工具':continue
        for offset,line in enumerate(row['content'].splitlines()):
            bullet=re.match(r'^\s*[-*+]\s+(.+)',line)
            if not bullet:continue
            item=re.match(r'^([\u4e00-\u9fff]+?)\s*(?:[（(]|$)',plain(bullet[1]))
            if not item:continue
            choices=re.split(r'或(?:者)?',item[1])
            if not all(re.fullmatch(r'[\u4e00-\u9fff]{1,10}油',v) for v in choices):continue
            line_no=row['source_start_line']+offset
            matches[line_no]=(row['section'],line,choices)
    if len(matches)!=1:return None
    line_no,(section,line,choices)=next(iter(matches.items()))
    answer='原文列出的选项是'+'或'.join(choices)+'。[1]\n\n原料说明：'+re.sub(r'^[-*+]\s+','',line)
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'literal_ingredient_options',
            'sources':[{'reference_id':1,'source':source,'title':title,'section':section,'line':line_no}],
            'evidence':[{'reference_id':1,'source':source,'title':title,'section':section,
                         'start_line':line_no,'end_line':line_no,'quote':line,'evidence_type':'markdown_original'}],
            'images':[],'validation':'仅引用原料表明确列出的选项及其说明。'}


def procedure_fact(question,chunks):
    """Answer only a fully recognized, unchanged recipe procedure request."""
    import json
    from recipe_tools import resolve_title,checked_chunks
    from retrieval_candidates import normalize_recipe_aliases
    from cookbuddy.ltm.guard import current,source_allowed
    from source_recipe_answer import prepare_full_recipe_answer
    from generate_test_answer import validate_answer
    from answer_blocks import build_source_display
    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    if current() and not source_allowed(source,current()):return None
    question=normalize_recipe_aliases(question)
    question=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',question).strip()
    head=r'(?:请问|请告诉我|请给我|告诉我|请)?'
    if re.fullmatch(r'我想做'+re.escape(title)+r'[，,]我是新手[。；;]请根据菜谱原文说明用料和操作步骤[。？?]?',question):
        question=title+'怎么做？'
    how=r'(?:'+re.escape(title)+r'(?:的)?(?:怎么制作|怎么做|做法|完整做法|用量和步骤|用料和步骤)|(?:如何制作|如何做)'+re.escape(title)+r')'
    sequence=re.escape(title)+r'(?:从开始(?:制作)?到完成[，,]?)?(?:应该|应当|需要|要)?按(?:照)?(?:什么|怎样的)?顺序(?:操作|制作)'
    flow=re.escape(title)+r'(?:的)?(?:制作|烹饪)?(?:流程|步骤|操作顺序)(?:是(?:什么|怎样)|如何安排)?'
    how='(?:'+how+'|'+sequence+'|'+flow+')'
    match=re.match(head+how,question)
    if not match:return None
    # Consume the entire remainder. Unknown requests (including substitutions,
    # calorie targets and combined image requests) must still go through QA.
    rest=question[match.end():]
    presentation=r'注意事项|按顺序|分步骤|简洁|简要|简短|详细|说明|介绍|保留|包含|给出|给我|请|并且|以及|关键|必要|重要|用量|温度|时间|用料|材料|配方|步骤|做法|操作|编号|完整|但|并|和|与'
    if re.sub(r'[\s，,。；;！!？?、：:]+','',re.sub(presentation,'',rest)):return None
    rows=checked_chunks(source)
    references=[{**row,'reference_id':i+1,'content_start_line':row['source_start_line']} for i,row in enumerate(rows)]
    payload=prepare_full_recipe_answer(title+'怎么做？',references,source,compact=True)
    if not payload:return None
    answer,evidence,cited=validate_answer(json.dumps(payload,ensure_ascii=False),references)
    by_id={r['reference_id']:r for r in references}
    evidence=[{**e,'source':source,'title':title,'section':by_id[e['reference_id']]['section'],
               'evidence_type':'markdown_original'} for e in evidence]
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'source_compact_recipe',
            'display_source':build_source_display(payload,references),'display_blocks':[],
            'sources':[{'reference_id':i,'source':source,'title':title,'section':by_id[i]['section'],
                        'line':by_id[i]['content_start_line']} for i in cited],
            'evidence':evidence,'images':[],
            'validation':'直接整理当前完整菜谱原文，省去可选工具介绍；用量、条件及操作保留，逐字核对引用与行号，未调用生成或语义复核模型。'}


def baking_fact(question,chunks):
    from recipe_tools import resolve_title,local_file
    named=resolve_title(question,chunks)
    if not named:return None
    title,source=named
    from cookbuddy.ltm.guard import current,source_allowed
    if current() and not source_allowed(source,current()):return None
    clean=re.sub(r'\n指定菜谱原文来源：'+re.escape(source)+r'\s*$','',question).strip()
    clean=re.sub(r'\n当前菜谱：'+re.escape(title)+r'\s*$','',clean).strip()
    label=r'(?:'+re.escape(title)+r'|面包)' if clean!=question and '面包' in title else re.escape(title)
    head=r'(?:请问|请|告诉我)?'+label+r'(?:的)?(?:应该|应当)?'
    time=r'(?:要|需要)?(?:烤|烘烤)(?:制)?(?:多久|多长时间)'
    temperature=r'(?:烘烤)?温度(?:是)?多少|多少度|用多少度'
    join=r'[，,、和及\s]*'
    classic=head+r'(?:'+time+join+r'(?:'+temperature+r')|(?:'+temperature+r')'+join+time+r')[？?。]*'
    combined=head+r'(?:烘烤|烤制)?温度和时间(?:是)?多少[？?。]*(?:原文(?:中)?(?:有没有|是否有)(?:根据|依据)烤箱(?:表现|情况)?调整(?:的)?(?:方法|建议)[？?。]*)?'
    if not any(re.fullmatch(pattern,clean) for pattern in (classic,combined)):return None
    lines=local_file(source).read_text().splitlines()
    hits=[i for i,line in enumerate(lines) if re.match(r'^\s*\d+[.、]\s*',line)
          and re.search(r'\d+\s*(?:℃|°C)',line) and re.search(r'烤.{0,8}\d+\s*分钟',line)
          and not re.search(r'预热|如果|若|假如',line)]
    if len(hits)!=1:return None
    start=hits[0];end=start+1
    # Retain adjacent source notes; do not invent a universal oven guarantee.
    while end<len(lines) and (not lines[end].strip() or lines[end].lstrip().startswith('>')):end+=1
    while end>start+1 and not lines[end-1].strip():end-=1
    section=next((r['section'] for r in chunks if r['source']==source and r['source_start_line']<=start+1<=r['source_end_line']),'操作')
    core=re.sub(r'^\s*\d+[.、]\s*','',plain(lines[start]))
    notes=[plain(re.sub(r'^\s*>\s*','',line)) for line in lines[start+1:end] if line.lstrip().startswith('>')]
    notes=[line for line in notes if line]
    if '调整' in clean and not notes:return None
    answer='### 烘烤温度与时间\n'+core
    if notes:answer+='\n\n### 调整提醒\n'+'\n\n'.join(notes)
    evidence={'reference_id':1,'source':source,'title':title,'section':section,'start_line':start+1,'end_line':end,
              'quote':'\n'.join(lines[start:end]),'evidence_type':'markdown_original'}
    return {'status':'verified','short_answer':answer,'answer':answer,'answer_method':'literal_baking_fact',
            'sources':[{'reference_id':1,'source':source,'title':title,'section':section,'line':start+1}],
            'evidence':[evidence],'images':[],'validation':'回答直接摘自当前菜谱步骤及相邻提醒，未生成替代或调整结论。'}
