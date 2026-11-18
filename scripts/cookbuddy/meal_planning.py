"""Source-backed menu composition; elapsed time is a whole-meal constraint."""
import copy
import re
from check_embedding import ROOT
from preference_recommendation import plain
from .recommendation_schema import NUMBERS,new_state
from .recommendation_turn import interpret

NUMBER=r'(?:\d{1,3}|[一二两三四五六七八九十]{1,3})'
DISH_COUNT=r'(?:推荐|安排|想做|做|来|只要)\s*('+NUMBER+r')\s*道\s*菜(?!谱)'
HALF_HOUR=r'半\s*(?:个\s*)?小时'
PRICE_WAIVER=r'(?:不需要|不用|无需|不)(?:核实|考虑)(?:采购)?(?:价格|预算)|(?:采购)?(?:价格|预算)(?:先|暂时)?(?:不需要|不用|无需|不)(?:核实|考虑)'
PANTRY_PERMISSION=r'(?<!不)(?:可以|允许|可)(?:使用|用)常备调料'
PRICE_LIMIT=r'(?:采购)?(?:总)?(?:预算|价格)(?:总共|为|是|最多|上限|不超过|不超|控制在)?\s*\d+(?:\.\d+)?\s*(?:元|块)(?:以内)?(?:[，,、 ]*(?:必须)?(?:保证)?(?:不超过|不超))?'
SIMPLE_PREFERENCE=r'(?:尽量|尽可能)简单(?:一些|一点|点)?|简单(?:一些|一点|点)(?:就好|就行)?'


def accepted_offer_minutes(question,offer):
    """Consume an explicit acceptance and optional time; never discard new filters."""
    if not offer:return None
    clean=re.sub(r'[，,。！!？?\s]','',question)
    pattern=r'(?:请)?(?:按|按照)(?:刚才的?|上面的?|这个|这套)?(?:完整)?方案(?:来)?(?:安排|做)(?:吧)?'
    if not re.search(pattern,clean):return None
    clean=re.sub(pattern,'',clean)
    if not clean:return offer['minutes']
    match=re.fullmatch(r'(?:那就|就|改成|改为|增加到)?('+NUMBER+r')分钟',clean)
    return number(match[1]) if match else None


def unresolved_conditions(values,question):
    """Keep unresolved limits after removing supported wording and explicit waivers."""
    permissions=[p for p in (PRICE_WAIVER,PANTRY_PERMISSION) if re.search(p,question)]
    if PRICE_WAIVER in permissions:permissions.append(PRICE_LIMIT)
    unresolved=[]
    for value in values:
        from src.agent.turn_intents import alternative_request
        command=alternative_request(value)
        if command and not command['constraint'] and not command.get('clarification') and not command.get('ambiguous_input'):continue
        remainder=value
        for pattern in permissions:remainder=re.sub(pattern,'',remainder)
        _,residual=meal_conditions(remainder,consume_constraints=False)
        if residual:unresolved.append(remainder.strip('，,。.;；、 '))
    return unresolved


def number(value):
    if value.isdigit():return int(value)
    if value in NUMBERS:return NUMBERS[value]
    if '十' in value:
        head,tail=value.split('十',1)
        return (NUMBERS.get(head,1)*10)+NUMBERS.get(tail,0)
    return None


def meal_request(question):
    # Meal planning takes precedence over the generic “recommend N dishes” reducer.
    composition=re.search(r'('+NUMBER+r')\s*(?:道)?菜\s*[、，,和加配+及]*\s*('+NUMBER+r')\s*(?:道|份)?汤',question)
    mixed=re.search(r'('+NUMBER+r')荤('+NUMBER+r')素('+NUMBER+r')汤',question)
    from .lexicon import planning_request,meal_match
    match=planning_request(question)
    people=re.search(r'('+NUMBER+r')\s*(?:个)?人',question)
    minutes=re.search(r'('+NUMBER+r')\s*分钟',question)
    hours=re.search(r'('+NUMBER+r')\s*(?:个)?小时',question)
    half_hour=re.search(HALF_HOUR,question)
    timed=minutes or hours or half_hour
    counted=re.search(DISH_COUNT,question) if people or timed else None
    if not (composition or mixed or match or (counted and people and timed)):
        return None
    request={'meal_type':(match or meal_match(question))['meal_type'],
             'people':number(people[1]) if people else None,
             'minutes':number(minutes[1]) if minutes else number(hours[1])*60 if hours else 30 if half_hour else None,
             'dishes':number(composition[1]) if composition else number(mixed[1])+number(mixed[2]) if mixed else number(counted[1]) if counted else 2,
             'soups':number(composition[2]) if composition else number(mixed[3]) if mixed else 0 if counted else 1,
             'meat':number(mixed[1]) if mixed else None,'vegetables':number(mixed[2]) if mixed else None,
             'explicit_composition':bool(composition or mixed or counted),'question':question}
    return request


def duration_estimate(rows):
    """Only whole-recipe introductory estimates, never an isolated cooking step."""
    lines={}
    for row in rows:
        if row['section']=='简介':
            for offset,line in enumerate(row['content'].splitlines()):lines[row['source_start_line']+offset]=line
    for line_no,line in reversed(sorted(lines.items())):
        # Last total-time sentence wins over an earlier marinating/boiling time.
        for sentence in reversed(re.split(r'[。；]',plain(line))):
            if not re.search(r'全程|从.+到|预计|制作时长|总耗时|总计|仅需|只需|大约',sentence):continue
            if re.search(r'至少|以上|过夜|隔夜|[一二两三四五六七八九十半]+\s*(?:天|小时|分钟)',sentence):continue
            normalized=re.sub(r'(\d+(?:\.\d+)?)\s*(?:~|～|—|–|-|至|到)\s*(\d+(?:\.\d+)?)',r'\2',sentence)
            times=re.findall(r'(\d+(?:\.\d+)?)\s*(天|小时|分钟|秒)',normalized)
            if times:
                total=sum(float(value)*{'天':1440,'小时':60,'分钟':1,'秒':1/60}[unit] for value,unit in times)
                if total>0:return {'minutes':total,'line':line_no,'quote':line}
    return None


def clarification(message):
    return {'status':'insufficient','short_answer':message,'sources':[],'evidence':[],'images':[],
            'answer_method':'meal_planning','meal_plan':{'complete':False}}


def multi_day_request(question):
    """One-meal planner cannot promise a multi-day menu."""
    return bool(re.search(r'一周|一个星期|一星期|七天|7\s*天|连续\s*\d+\s*天|每天.{0,8}(?:餐|菜)',question))


def meal_difficulty(question):
    from search_filters import DIFFICULTY_LIMIT_PATTERN
    matches=list(re.finditer(DIFFICULTY_LIMIT_PATTERN,question))
    return int(matches[0][1]) if len(matches)==1 else None


def meal_conditions(question,*,consume_constraints=True):
    # Do not silently drop conditions that the bounded selector cannot establish.
    from .task_context import reduce_edits
    turn=interpret(question)
    state=reduce_edits(new_state(),[e for e in turn.edits if e.slot not in ('limit','response_fields')])
    residual=question
    if consume_constraints:
        for permission in (PRICE_WAIVER,PANTRY_PERMISSION):residual=re.sub(permission,'',residual)
    from .lexicon import meal_aliases
    meal_words='|'.join(re.escape(word) for word in meal_aliases())
    residual=re.sub(r'(?:继续|重新|照旧)(?:按(?:照)?)?(?='+NUMBER+r'|'+meal_words+r'|[，,。.;；、\s]*$)','',residual)
    residual=re.sub(r'(?:按照|按|改成|改为|换成)\s*(?=(?:'+meal_words+r'))','',residual)
    residual=re.sub(r'(?:整餐|用餐|制作|可用)?时间(?:预算)?(?:改成|改为|改到|调整到|调整为|增加到|缩短到|设为)?\s*(?='+NUMBER+r'\s*(?:分钟|(?:个)?小时))','',residual)
    residual=re.sub(r'(?:还是|仍然|仍旧|保持|照旧)\s*(?='+NUMBER+r'\s*(?:(?:个)?人|分钟|(?:个)?小时|(?:道)?菜))','',residual)
    for pattern in (r''+NUMBER+r'\s*(?:个)?人(?:的)?',r'(?:有)?'+NUMBER+r'\s*分钟(?:以内|内)?',r''+NUMBER+r'\s*(?:个)?小时(?:以内|内)?',
                    r''+NUMBER+r'\s*(?:道)?菜\s*[、，,和加配+及]*\s*'+NUMBER+r'\s*(?:道|份)?汤',
                    r''+NUMBER+r'荤'+NUMBER+r'素'+NUMBER+r'汤',DISH_COUNT,HALF_HOUR+r'(?:以内|内)?'):
        residual=re.sub(pattern,'',residual)
    if consume_constraints:
        for edit in turn.edits:residual=residual.replace(edit.evidence,'')
    from .lexicon import strip_meal_words
    residual=strip_meal_words(residual)
    residual=re.sub(SIMPLE_PREFERENCE,'',residual)
    if consume_constraints and meal_difficulty(question) is not None:
        from search_filters import DIFFICULTY_LIMIT_PATTERN
        residual=re.sub(DIFFICULTY_LIMIT_PATTERN,'',residual)
    residual=re.sub(r'^(?:按照|按|照|搭配)+','',residual)
    residual=re.sub(r'我们|我|吃|今天|一餐|一顿|菜单|怎么|如何|做好|完成|做完|想做|做|需要|想要|内|[，,、。.;；！!？?\s]','',residual)
    if residual in {'按照','按','照','搭配'}:residual=''
    return state,residual


def single_dish_revision(question):
    return bool(re.search(r'(?:只|仅)(?:想|要)?(?:换|替换|换掉)一(?:道|个)菜',question)
                and re.search(r'汤(?:照旧|保留|不变|别换|不要换)|(?:保留|不换|别换|不要换)(?:原来的|原来那道|原本的)?汤',question))


def plan_meal(request,chunks,*,seed=None,taste_preferences=()):
    people,budget,dishes,soups=(request[k] for k in ('people','minutes','dishes','soups'))
    if (people is not None and not 1<=people<=20) or (budget is not None and budget<1) or not 1<=dishes or not 0<=soups or dishes+soups>6:
        return clarification('请说明1–20人的用餐人数，以及合计不超过6道的菜、汤数量。')
    question=request['question']
    if multi_day_request(question):
        return clarification('我一次只能安排一餐，不能生成一周菜单。可以先帮你安排今天的一餐。')
    permissions=[p for p in (PRICE_WAIVER,PANTRY_PERMISSION) if re.search(p,question)]
    for pattern in permissions:question=re.sub(pattern,'',question)
    if re.search(r'预算|\d+\s*元|[块¥￥]|只[有用]|仅[有用]|全部|每道|每一道',question):
        return clarification('已识别菜单结构；当前还不能核实采购价格、仅有食材是否齐全或每道菜的特殊要求。请先说明可否按常备调料选菜，并把需要逐道满足的条件写清楚。')
    # Online requests already carry the single validated understanding. Legacy
    # import/evaluation calls still parse their original text locally.
    if 'constraints' in request:
        from .recommendation_turn import Edit
        from .task_context import reduce_edits
        constraints=request['constraints'];edits=[]
        unresolved=unresolved_conditions(constraints.get('unresolved',[]),request['question'])
        if unresolved:
            prices=[value for value in unresolved if re.search(r'预算|价格|费用|花费|成本',value)]
            others=[value for value in unresolved if value not in prices]
            if prices and not others:
                return clarification('这餐保留了价格或预算条件（'+'、'.join(prices)+'），但当前无法核实采购价格。'
                    '如果可以先不核实价格，请回复“价格不用核实”；我会继续按人数、时间和菜汤数量安排。')
            parts=[]
            if others:parts.append('还需要确认：'+'、'.join(others))
            if prices:parts.append('价格或预算（'+'、'.join(prices)+'）当前无法核实')
            message='；'.join(parts)+'。请补充可操作的条件。'
            if prices:message+='价格条件可以回复“价格不用核实”。'
            return clarification(message)
        if constraints.get('taste'):edits.append(Edit('set','tastes',(constraints['taste'],),''))
        if request.get('ingredients'):edits.append(Edit('set','ingredients',tuple(request['ingredients']),''))
        if request.get('mode'):edits.append(Edit('set','mode',(request['mode'],),''))
        for key in ('excluded_ingredients','excluded_methods'):
            if constraints.get(key):edits.append(Edit('add',key,tuple(constraints[key]),''))
        state=reduce_edits(new_state(),edits)
        from search_filters import filter_chunks
        chunks,_,_=filter_chunks(chunks,category=constraints.get('category'),max_difficulty=constraints.get('max_difficulty'))
    else:
        state,residual=meal_conditions(question)
        if residual:
            return clarification('已识别菜单结构，但还不能可靠核实“'+residual[:60]+'”这个条件。请说明食材、口味或做法限制。')
    current_taste=next(iter(state['tastes']),None)
    if current_taste:
        taste_preferences=()
    state['extra_constraints']=[];state['limit']=10
    difficulty=meal_difficulty(request['question'])
    if difficulty is not None:
        constraints={**request.get('constraints',{})}
        constraints['max_difficulty']=min(difficulty,constraints.get('max_difficulty') or difficulty)
        request={**request,'constraints':constraints}
    from .menu_planner import MenuPlanner,resolve_template
    kind,template,meal_notice=resolve_template(request)
    seed=request.get('selection_seed',0) if seed is None else seed
    planner=MenuPlanner()
    best,missing=planner.plan(request,chunks,state,template,seed=seed,taste_preferences=taste_preferences)
    if 'revise_menu' in request and not best:
        return clarification('当前条件下没找到能保留原汤和其余菜、只换一道菜的方案；原菜单保留。')
    alternative=None;completion_notice='只换了一道菜，原汤和其余菜保留。' if 'revise_menu' in request else ''
    if request.get('accepted_sources') and set(request['accepted_sources'])!={r['source'] for r in best}:
        completion_notice='原完整方案在当前限制下无法原样安排，以下为重新核对后的搭配。'
    if 'revise_menu' not in request and template['budget'] is not None and any(not s.get('optional') for s in missing):
        expanded={**template,'budget':template.get('max_total_minutes')}
        rows,gaps=planner.plan(request,chunks,state,expanded,seed=seed,fastest=True,taste_preferences=taste_preferences)
        if rows and not any(not s.get('optional') for s in gaps):
            minutes=sum(r['duration']['minutes'] for r in rows)
            if minutes<=template['budget']:
                # Completeness takes precedence over avoiding recent recipes.
                # The source validator has checked the same hard constraints.
                best,missing=rows,gaps
                if any(r['source'] in request.get('previous_sources',[]) for r in rows):
                    completion_notice='为在时间限制内补齐指定菜汤数量，这次复用了部分近期推荐的菜品。'
            else:
                alternative={'estimated_minutes':minutes,'recipes':[
                    {'title':r['title'],'source':r['source'],'role':r['role'],
                     'minutes':r['duration']['minutes'],'time_quote':r['duration']['quote'],
                     'time_line':r['duration']['line']} for r in rows]}
    missing_labels=[s['label'] for s in missing]
    if not best:
        result=clarification('当前符合餐次、难度、忌口和整餐耗时限制的菜谱不足；缺少：'+'、'.join(missing_labels)+'。可以增加时间或调整限制。')
        result['meal_plan'].update(meal_type=kind,missing_roles=[s['role'] for s in missing])
        return result
    evidence=[];sources=[];chosen=[];timeline=[];elapsed=0
    for row in (alternative or {}).get('recipes',[]):
        ref={'source':row['source'],'title':row['title'],'section':'简介',
             'start_line':row['time_line'],'end_line':row['time_line'],'quote':row['time_quote'],
             'evidence_type':'markdown_original','reference_id':len(evidence)+1}
        evidence.append(ref)
        sources.append({**{k:ref[k] for k in ('reference_id','source','title','section')},'line':ref['start_line']})
    for index,row in enumerate(best):
        estimate=row['duration'];start=elapsed;elapsed+=estimate['minutes']
        duration=f'{estimate["minutes"]:g}';role=row['role_label']
        chosen.append({**{k:v for k,v in row.items() if k not in ('duration','tie','preference_score')},
                       'reason':('作为这餐的'+role)+'，原文预计从准备到完成约'+duration+'分钟。'
                       +(row['reason'] if current_taste and row.get('spice_match')!='none' else '')
                       +''.join('原文简介写明“'+feature+'”，与你的'+label+'口味偏好相符。'
                                for label,feature in row.get('taste_matches',[]))
                       +''.join('原文简介提到'+cuisine+'，与你的菜系偏好相符。'
                                for cuisine in row.get('cuisine_matches',[]))
                       +('原文描述为“'+row['spicy_alternative']+'”，作为有辣味的备选，具体辣度未标明。'
                         if row.get('spicy_alternative') else '')})
        refs=list(row['evidence'])+[{'source':row['source'],'title':row['title'],'section':'简介',
              'start_line':estimate['line'],'end_line':estimate['line'],'quote':estimate['quote'],'evidence_type':'markdown_original'}]
        for ref in refs:
            ref={**ref,'reference_id':len(evidence)+1};evidence.append(ref)
            sources.append({**{k:ref[k] for k in ('reference_id','source','title','section')},'line':ref['start_line']})
        timeline.append(str(index+1)+'. '+f'{start:g}–{elapsed:g}'+'分钟：按原文准备并完成'+row['title']+'。')
    dishes=sum(r['category'] not in ('soup','drink') for r in best)
    soups=sum(r['category']=='soup' for r in best)
    summary='### '+(str(people)+'人菜单' if people is not None else '参考菜单')+'：'+str(dishes)+'道菜＋'+str(soups)+'道汤'
    from .lexicon import settings as lexicon
    summary+='（'+lexicon()['meal_types'][kind]['label']+'）\n'+ '、'.join(r['title'] for r in best)
    if meal_notice:summary+='\n\n'+meal_notice
    if completion_notice:summary+='\n\n'+completion_notice
    if current_taste:
        matched_names=[r['title'] for r in best if r.get('spice_match') in ('explicit','direct','source_no_spice')]
        if matched_names:
            summary+='\n\n已按本次的'+current_taste+'要求选菜。原文核实匹配：'+'、'.join(matched_names)+'。'
        else:
            summary+='\n\n已按本次的'+current_taste+'要求筛选；当前搭配没有核实到直接匹配的菜。'
        adjustable=[r['title'] for r in best if r.get('spice_match')=='optional']
        if adjustable:summary+='可按原文调整辣味：'+'、'.join(adjustable)+'。'
    if taste_preferences:
        matched_names=[r['title'] for r in best if r.get('taste_matches') or r.get('cuisine_matches')]
        if matched_names:summary+='\n\n已按长期饮食偏好优先选菜。原文能核实匹配的菜：'+'、'.join(matched_names)+'。'
        else:
            from .ltm.retriever import SPICE_LABELS
            preferred=list(dict.fromkeys(SPICE_LABELS.get(str(p['value']),str(p['value']))
                if p['dimension']=='spice_level' else str(p['value']) for p in taste_preferences
                if p['polarity']=='like' and p['dimension'] in ('flavor','spice_level','cuisine')))
            if preferred:
                summary+='\n\n已优先满足本餐的时间、难度和菜汤数量。当前搭配未核实到符合你偏好的'+ '、'.join(preferred)+'菜谱。'
        spicy_names=[r['title'] for r in best if r.get('spicy_alternative')]
        if spicy_names:
            summary+='\n'+ '、'.join(spicy_names)+'的菜谱明确描述有辣味，具体辣度未标明。'
    from .ltm.guard import current,forbidden
    avoided,_,_=forbidden(current())
    if avoided:summary+='\n\n已按饮食限制筛选，避开'+ '、'.join(avoided)+'。'
    if missing_labels:
        requested=str(request.get('dishes',dishes))+'道菜＋'+str(request.get('soups',soups))+'道汤'
        actual=str(sum(r['category'] not in ('soup','drink') for r in best))+'道菜＋'+str(sum(r['category']=='soup' for r in best))+'道汤'
        missing_counts={}
        for label in missing_labels: missing_counts[label]=missing_counts.get(label,0)+1
        missing_text='、'.join((str(count)+'道'+label if label in ('菜','汤') else (label+'×'+str(count) if count>1 else label)) for label,count in missing_counts.items())
        summary+='\n\n未能在'+str(budget)+'分钟内完成请求的'+requested+'；当前先安排'+actual+'。缺少或未安排：'+missing_text+'。'
    if alternative:
        summary+='\n\n找到的完整搭配为：'+'、'.join(r['title'] for r in alternative['recipes'])+'，按原文顺序制作预计约'+f'{alternative["estimated_minutes"]:g}'+'分钟。若希望补齐，可将时间增加到这一参考值；以上菜单仍按你的原时间限制安排。'
    summary+='\n\n### 时间安排\n'+ '\n'.join(timeline)
    summary+='\n\n按各菜原文总耗时依次相加，预计约'+f'{elapsed:g}'+'分钟'+('，预算'+str(budget)+'分钟。' if budget is not None else '；你未限定时间，先按此作为参考。')+'如有独立汤锅，也可在做汤时切配下一道菜。原文时间为估计，实际受备料、熟练程度和份量影响。'
    summary+='\n\n### 份量与准备\n'+('按'+str(people)+'人共食搭配，' if people is not None else '人数未指定，份量可按实际用餐人数调整。')+'具体用量和调料请查看各菜原文，按实际食量准备；增加份量后耗时可能延长。'
    if not any(r['role'] in ('main_staple','staple') for r in best):summary+='主食需另行准备，未计入上述时间。'
    return {'status':'verified','short_answer':summary,'recommendation_kind':'preference','recommendations':chosen,
            'sources':sources,'evidence':evidence,'images':[],'answer_method':'meal_planning',
            'meal_plan':{'complete':not any(not s.get('optional') for s in missing),'meal_type':kind,
                         'missing_roles':[s['role'] for s in missing],
                         'alternative':alternative,
                         'max_total_minutes':template['budget'],'max_difficulty':template['max_difficulty'],'people':people,'dishes':dishes,'soups':soups,'minutes':budget,
                         'estimated_minutes':elapsed,'timing_basis':'sum_of_source_total_estimates'}}
