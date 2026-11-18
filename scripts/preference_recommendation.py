"""Recipe discovery with source-backed introductions and preference explanations."""
import re
from check_embedding import ROOT
from ingredient_recommendation import TOOLS

FAMILIES={'鸡肉':r'鸡肉|鸡腿|鸡翅|鸡胸|鸡块|整鸡|全鸡|童子鸡|手枪腿|鸡[（(]|鸡\s*[一二两\d]',
          '牛肉':r'牛肉|牛腩|牛里脊|牛腱|牛排', '猪肉':r'猪肉|五花肉|猪里脊|猪瘦肉',
          '鱼肉':r'鱼肉|巴沙鱼|鲈鱼|鲫鱼|草鱼|鳕鱼|鲤鱼'}
SPICE=r'干辣椒(?:面|粉|碎)?|小米椒|辣椒粉|辣椒面|辣椒油|剁椒|(?:郫县|红油)(?:红油)?豆瓣酱|香辣酱|尖椒'
FLAVOR=r'麻辣|香辣|鲜辣|酸辣|糊辣|甜辣|微辣|辛辣'
DISH_CATEGORIES={'aquatic','meat_dish','vegetable_dish','soup'}
TOOL=TOOLS.pattern
SEASONING=r'^(?:盐|食盐|白糖|糖|味精|鸡精|料酒|生抽|老抽|酱油|蚝油|醋|香醋|食用油|植物油|菜籽油|淀粉|水|开水|清水|白胡椒粉|黑胡椒粉|胡椒粉)(?:$|[（( =\d])'
EXCLUDED_FAMILIES={
    '辣椒':r'辣|青椒|红椒|彩椒|甜椒|尖椒|泡椒|剁椒|小米椒|朝天椒|二荆条|灯笼椒|线椒|杭椒|豆瓣酱|火锅底料|老干妈',
    '香菜':r'香菜|芫荽', '花生':r'花生|落花生', '番茄':r'番茄|西红柿',
    '鸡蛋':r'鸡蛋|蛋清|蛋白|蛋黄|蛋液|全蛋', '牛奶':r'牛奶|奶粉|炼乳|乳酪|奶酪|黄油|奶油',
    '葱':r'葱', '姜':r'姜', '蒜':r'蒜',
}
# A named commercial blend without its composition cannot establish an
# ingredient exclusion. Keep such recipes out instead of assuming it is safe.
OPAQUE_BLEND=r'调料|调味料|腌料|料包|底料|奥尔良|咖喱|沙茶酱|烧烤酱|照烧酱|海鲜酱|XO酱|盐焗鸡粉|五香粉|十三香'
QUICK_TERM=re.compile(r'快手|快速|省时')
QUICK_MAX_MINUTES=30


def exclusion_pattern(name):
    return EXCLUDED_FAMILIES.get(name,FAMILIES.get(name,re.escape(name)))


def plain(text):
    text=re.sub(r'!\[[^\]]*\]\([^\n]*?\)','',text)
    text=re.sub(r'\[([^\]]+)\]\([^)]*\)',r'\1',text)
    return text.replace('*','').replace('`','').strip()


def source_evidence(source,title,selected,available,sections):
    ranges=[]
    for n in sorted(selected):
        if ranges and n==ranges[-1][1]+1 and sections[n]==sections[ranges[-1][0]]:ranges[-1][1]=n
        else:ranges.append([n,n])
    return [{'source':source,'title':title,'section':sections[start],'start_line':start,'end_line':end,
             'quote':'\n'.join(available[n] for n in range(start,end+1)),'evidence_type':'markdown_original'} for start,end in ranges]


def recommend_preferences(state,chunks,exclude_titles=(),categories=None,candidate_limit=None,owned_equipment=()):
    from cookbuddy.task_context import valid_state
    from cookbuddy.ltm.guard import current,source_allowed
    hard=current()
    # An inventory declaration is also useful for discovery. Checking that every
    # ingredient is available is a separate request and must not gate discovery.
    if not valid_state(state):return None
    if state.get('memory_tastes')==state.get('tastes'):state={**state,'tastes':[]}
    quick=any(QUICK_TERM.search(value) for value in state['extra_constraints'])
    extra=[value for value in state['extra_constraints'] if not QUICK_TERM.search(value)]
    from cookbuddy.recommendation_schema import STYLES
    styles=[value for value in extra if value in STYLES]
    style=styles[0] if len(styles)==1 else ''
    if len(styles)==1:extra=[value for value in extra if value!=style]
    if extra:
        return {'status':'insufficient','answer_mode':'clarify',
            'short_answer':'还有一个选菜条件需要确认：「'+extra[0]+'」，你希望怎样处理？',
            'clarification_options':['保留条件并补充说明','先按已理解条件推荐'],
            'pending_constraints':list(extra),
            'sources':[],'evidence':[],'recommendations':[]}
    if any(t not in ('辣味','微辣','不辣') for t in state['tastes']):return None
    from recipe_catalog import CATEGORIES
    allowed=DISH_CATEGORIES if categories is None else set(categories) & CATEGORIES.keys()
    grouped={}
    for row in chunks:grouped.setdefault(row['source'],[]).append(row)
    candidates=[]
    for source,rows in sorted(grouped.items()):
        if hard and not source_allowed(source,hard):continue
        quick_estimate=None
        if quick:
            from cookbuddy.meal_planning import duration_estimate
            quick_estimate=duration_estimate(rows)
            if not quick_estimate or quick_estimate['minutes']>QUICK_MAX_MINUTES:continue
        category=next((part for part in source.split('/') if part in allowed),'')
        if not category or categories is not None and category not in categories:continue
        path=(ROOT/source).resolve()
        if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file():continue
        original=path.read_text().splitlines();available={};sections={};valid=True
        for row in rows:
            content=row['content'].splitlines()
            starts=[i for i in range(row['source_start_line']-1,min(row['source_end_line'],len(original))) if original[i:i+len(content)]==content]
            if len(starts)!=1:valid=False;break
            for offset,line in enumerate(content):
                n=starts[0]+offset+1;available[n]=line;sections[n]=row['section']
        if not valid:continue
        title=rows[0]['title'];selected=set()
        if title in exclude_titles:continue
        ingredients={n:line for n,line in available.items() if re.search(r'必备原料|食材|原料|计算',sections[n])}
        matches=[];matched_names=[]
        for ingredient in state['ingredients']:
            from ingredient_recommendation import normalize_ingredient
            pattern=FAMILIES.get(ingredient)
            # Compare ingredient names, rather than accepting a substring of a
            # different ingredient such as 番茄酱 for 番茄.
            found=[n for n,line in ingredients.items() if re.match(r'^\s*[-*+]\s+',line) and
                   (re.search(pattern,line) if pattern else
                    normalize_ingredient(plain(re.sub(r'^\s*[-*+]\s+','',line)))==normalize_ingredient(ingredient))]
            if found:
                matches.append(found[0]);selected.add(found[0]);matched_names.append(normalize_ingredient(ingredient))
        if state['ingredients'] and (not matches if state['mode']=='inventory' else len(matches)!=len(state['ingredients'])):continue
        operations={n:line for n,line in available.items() if re.search(r'操作|做法|制作',sections[n])}
        exclusions=state.get('excluded_ingredients',[])
        # Absence is checked against the entire current source, including compound
        # seasonings, optional ingredients and operations, not just retrieval hits.
        # Conservative rejection never promises an unsupported ingredient omission.
        if exclusions:
            if not ingredients or not operations:continue
            source_text=plain('\n'.join(original))
            if any(re.search(exclusion_pattern(name),source_text) for name in exclusions):continue
            if any(re.search(r'^\s*[-*+]\s+.*(?:'+OPAQUE_BLEND+r')',line) for line in original):continue
            selected.update(ingredients);selected.update(operations)
        if state['excluded_methods'] and (not operations or re.search(r'炸','\n'.join(operations.values()))):continue
        if state['excluded_methods']:selected.update(operations)
        # Use a literal introductory sentence, never invent texture, difficulty or duration.
        intro=[(n,plain(line)) for n,line in sorted(available.items()) if sections[n]=='简介' and plain(line) and not re.match(r'[#>]|预估',plain(line))]
        description='';intro_line=None
        for n,line in intro:
            sentence=re.split(r'(?<=[。！？])',line)[0].strip()
            if 8<=len(sentence)<=240:description=sentence;intro_line=n;selected.add(n);break
        # Main ingredients come only from the source ingredient list; tools stay out.
        main=[];main_entries=[]
        for n,line in sorted(ingredients.items()):
            if sections[n].startswith('计算'):continue
            if '可选' in sections[n] or re.search(r'可选|不必|如果|'+TOOL,line):continue
            found=re.match(r'^\s*[-*+]\s+(.+)',line)
            if not found:continue
            name=plain(found[1])
            if re.fullmatch(r'(?:原料|食材|材料|配料|工具)[：:]?',name):continue
            if len(name)>38:continue
            if name not in [entry[1] for entry in main_entries]:main_entries.append((n,name))
        for n,name in sorted(main_entries,key=lambda entry:bool(re.search(SEASONING,entry[1])))[:4]:
            main.append(name);selected.add(n)
        if not description:
            if not main:continue
            description='这道菜的主要用料有'+ '、'.join(main[:3])+'。'
        style_note=''
        if style:
            if style=='炸' and re.search(r'炸酱|炸锅',title):continue
            if style not in title and style not in description:continue
            style_note='原文菜名或简介包含「'+style+'」'
        equipment_match=[];equipment_note=''
        if owned_equipment:
            from ingredient_recommendation import recipe_ingredient_fields
            _,_,tools,_,_=recipe_ingredient_fields(rows,original)
            equipment_match=[tool for tool in owned_equipment if any(tool in value for value in tools)]
            if equipment_match:
                equipment_note='原文工具包含'+'、'.join(equipment_match)+'，符合你家里有的设备'
        flavor_lines=[n for n,line in intro if re.search(FLAVOR,line) and not re.search(r'不辣|不吃辣|不喜欢辣',line)]
        mandatory=[];optional=[]
        optional_names=set();required_names=set()
        for n,line in ingredients.items():
            found=re.search(SPICE,line)
            if not found or sections[n].startswith('计算'):continue
            target=optional_names if '可选' in sections[n] or re.search(r'可选|可加|也可|可以|喜欢|按.*口味',line) else required_names
            target.add(found[0])
        for n,line in ingredients.items():
            found=re.search(SPICE,line)
            if not found:continue
            if re.search(r'不放|不加|不能|不辣',line):continue
            inherited_optional=sections[n].startswith('计算') and found[0] in optional_names-required_names
            (optional if inherited_optional or '可选' in sections[n] or re.search(r'可选|可加|也可|可以|喜欢|按.*口味',line) else mandatory).append(n)
        optional.extend(n for n,line in available.items() if n not in ingredients and re.search(SPICE,line) and re.search(r'可加|也可|可以|喜欢|添加|增加',line) and not re.search(r'不加|不能',line))
        spice_score=0;reason=[];notice='';spice_kind='none'
        if state['ingredients']:reason.append('用料包含'+ '、'.join(matched_names)+'，可以用上你提供的食材')
        if style_note:reason.append(style_note)
        if equipment_note:reason.append(equipment_note);spice_score+=1
        if state['tastes']:
            taste=state['tastes'][0]
            if taste=='不辣':
                # Non-spicy recipes rarely label themselves "不辣". Check the
                # complete unchanged recipe instead of requiring that adjective.
                hot=SPICE+r'|辣椒|小米辣|泡椒|朝天椒|二荆条|火锅底料|老干妈|胡椒|芥末|山葵'
                if (not ingredients or not operations or flavor_lines
                        or re.search(hot+'|'+OPAQUE_BLEND,plain('\n'.join(original)))):continue
                selected.update(ingredients);selected.update(operations)
                spice_score=4;spice_kind='source_no_spice'
                reason.append('原文配方及步骤未列辣椒、胡椒、芥末等加辣用料，适合作为不辣口味的候选')
            elif taste=='微辣':
                from cookbuddy.ltm.retriever import flavor_feature
                found=[n for n,line in intro if flavor_feature(line,taste) and not re.search(r'不喜欢|不能|如果',line)]
                if not found:continue
                selected.add(found[0]);spice_score=4;spice_kind='explicit'
                reason.append('菜谱明确描述为'+taste+'，符合你的口味要求')
            elif flavor_lines or mandatory:
                selected.update(flavor_lines[:1]);selected.update(mandatory[:2]);spice_kind='direct'
                spice_score=4 if flavor_lines and mandatory else 3 if flavor_lines else 2
                names=list(dict.fromkeys(re.search(SPICE,ingredients[n])[0] for n in mandatory))[:2]
                if flavor_lines:
                    feature=re.search(r'(?:'+FLAVOR+r')(?:鲜香|干香|鲜嫩|浓郁|爽口|开胃|滑嫩|过瘾|劲爽|荔枝味)?',available[flavor_lines[0]])[0]
                    reason.append('它的“'+feature+'”风味符合你想吃辣的要求'+('，配方中用'+ '、'.join(names)+'调味' if names else ''))
                elif names:reason.append('配方用'+ '、'.join(names)+'调味，符合你想吃辣的要求')
                else:reason.append('菜谱明确描述其'+re.search(FLAVOR,available[flavor_lines[0]])[0]+'风味，符合你想吃辣的要求')
            elif optional:
                n=optional[0];selected.add(n);spice_score=1;spice_kind='optional'
                name=re.search(SPICE,available[n])[0]
                reason.append('可以按菜谱加入'+name+'做成辣味，作为可调整口味的备选')
                notice='这道菜并非固定辣味；需要按原文选择加辣。'
            else:continue
        if state['excluded_methods']:reason.append('制作步骤未列油炸操作，符合你不要油炸的要求')
        if exclusions:reason.append('已核对原文配方及步骤，未列出'+ '、'.join(exclusions)+'及已识别的相关制品，符合本次排除条件')
        if quick_estimate:
            if quick_estimate['line'] in available:selected.add(quick_estimate['line'])
            reason.append('原文预计整菜约'+f'{quick_estimate["minutes"]:g}'+'分钟完成，符合快手要求')
        if not reason:reason=['用料和制作过程均有完整菜谱，可作为这次选菜的候选']
        primary='水产' if category=='aquatic' else next((name for name,pattern in FAMILIES.items() if re.search(pattern,title)),'')
        if not primary:primary=next((name for name,pattern in FAMILIES.items() if re.search(pattern,'\n'.join(ingredients.values()))),category)
        difficulty=rows[0].get('difficulty')
        if difficulty is not None:
            selected.update(n for n,line in available.items() if '预估烹饪难度' in line)
        inventory={}
        if state['mode']=='inventory' or 'missing_ingredients' in state.get('response_fields',[]):
            from ingredient_recommendation import recipe_ingredient_fields,normalize_ingredient,matched_inventory
            tables=[r for r in rows if '必备原料' in r['section']]
            if tables:
                required,optional,tools,uncertain,_=recipe_ingredient_fields(rows,original)
                stock={normalize_ingredient(v) for v in state['ingredients']} if state['mode']=='inventory' else set()
                inventory={'matched':sorted(matched_inventory(required,optional,uncertain,stock)),'missing':sorted(required-stock),
                           'optional':sorted(optional),'tools':sorted(tools),'unresolved':uncertain}
                selected.update(n for n in available if '必备原料' in sections[n])
                selected.update(n for n in available if sections[n].split(' > ')[0]=='计算')
            else:inventory={'unresolved':['原文没有可核实的必备原料表，暂时无法完整列出需要补齐的食材。']}
        evidence=source_evidence(source,title,selected,available,sections)
        if quick_estimate and not any(item['start_line']<=quick_estimate['line']<=item['end_line'] for item in evidence):
            evidence.append({'source':source,'title':title,'section':'简介','start_line':quick_estimate['line'],
                             'end_line':quick_estimate['line'],'quote':quick_estimate['quote'],'evidence_type':'markdown_original'})
        candidates.append({'difficulty':difficulty,'title':title,'source':source,'description':description,'reason':'；'.join(reason)+'。',
                           'equipment_match':equipment_match,'inventory_matches':matched_names,
                           'main_ingredients':main,'notice':notice,'spice_match':spice_kind,
                           'evidence':evidence,'primary_group':primary,'score':spice_score,**inventory})
    candidates.sort(key=lambda row:(-row['score'],row['source']))
    # Match quality comes before variety: optional add-on spice never displaces a direct match.
    chosen=[]
    limit=state['limit'] if candidate_limit is None else min(400,max(state['limit'],candidate_limit))
    for score in sorted({r['score'] for r in candidates},reverse=True):
        tier=[r for r in candidates if r['score']==score];groups=set()
        if not state['ingredients']:
            for row in tier:
                if len(chosen)>=limit:break
                if row['primary_group'] not in groups:chosen.append(row);groups.add(row['primary_group'])
        for row in tier:
            if len(chosen)>=limit:break
            if row not in chosen:chosen.append(row)
    if state['mode']=='inventory' and state['ingredients']:
        from ingredient_recommendation import select_inventory_recipes
        chosen=select_inventory_recipes(candidates,state['ingredients'],limit)
    chosen=[{**{k:v for k,v in row.items() if k not in ('primary_group','score')},'rerank_score':row['score']} for row in chosen];evidence=[];sources=[]
    for row in chosen:
        for item in row['evidence']:
            item={**item,'reference_id':len(evidence)+1};evidence.append(item)
            sources.append({'reference_id':item['reference_id'],'source':item['source'],'title':item['title'],'section':item['section'],'line':item['start_line']})
    criteria=[]
    if style:criteria.append('做法'+style)
    if state['ingredients']:criteria.append('使用'+ '、'.join(state['ingredients']))
    if state['tastes']:criteria.append('口味'+ '、'.join(state['tastes']))
    if state.get('excluded_ingredients'):criteria.append('排除'+ '、'.join(state['excluded_ingredients'])+'及已识别的相关制品')
    if state['excluded_methods']:criteria.append('不要'+ '、'.join(state['excluded_methods']))
    if quick:criteria.append('原文预计不超过'+str(QUICK_MAX_MINUTES)+'分钟')
    if not chosen:return {'status':'insufficient','short_answer':'当前条件：'+('；'.join(criteria) or '按现有要求选菜')+'。暂未找到原文可确认同时满足条件的菜谱。你可以更换主食材或放宽一项条件；我没有直接删改原配方。','sources':[],'evidence':[],'images':[]}
    summary=('给你推荐这1道菜，下面介绍它的特点和推荐理由。' if len(chosen)==1
             else '给你推荐这'+str(len(chosen))+'道菜，下面介绍各自的特点和推荐理由。')
    if style:summary+='本轮按「'+style+'」筛选（依据菜名或简介）。'
    if owned_equipment and any(row.get('equipment_match') for row in chosen):
        summary+='已优先考虑你家里有的'+'、'.join(owned_equipment)+'。'
    if state.get('excluded_ingredients'):
        others=[value for value in criteria if value!='做法'+style]
        summary+='本轮按'+ '；'.join(others)+'筛选。'
    if state['mode']=='inventory':summary+='这里先按你提供的食材选菜，其他配料是否齐全还需确认。'
    if state['mode']=='inventory':
        from ingredient_recommendation import unused_inventory_note
        summary+=unused_inventory_note(state['ingredients'],chosen)
    return {'status':'verified','short_answer':summary,'recommendation_kind':'preference','recommendation_mode':state['mode'],'recommendations':chosen,
            'sources':sources,'evidence':evidence,'images':[],'answer_method':'source_preference_filter',
            'validation':'菜品介绍、主要食材和匹配理由绑定当前菜谱原文。优先明确辣味及配方加辣依据，可选加辣放在后面并注明。食材声明用于选菜，未证明库存齐全。排除油炸采用保守过滤。做法筛选按菜名或简介文本核对。设备匹配按原文工具表核对。'}
