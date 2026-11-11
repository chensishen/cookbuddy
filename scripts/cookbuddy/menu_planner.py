"""Deterministic bounded menu composition and an independent source validator."""
import copy
import hashlib
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
from check_embedding import ROOT
from ingredient_recommendation import recipe_ingredient_fields,normalize_ingredient,matched_inventory,TOOLS
from preference_recommendation import recommend_preferences,SEASONING,FAMILIES,EXCLUDED_FAMILIES,exclusion_pattern,plain
from .lexicon import settings as lexicon


@lru_cache(maxsize=1)
def templates():
    from recipe_catalog import CATEGORIES
    data=yaml.safe_load((Path(__file__).resolve().parents[2]/'config/meal_templates.yaml').read_text())
    for kind in lexicon()['meal_types']:
        for slot in data[kind]['slots']:
            if not set(slot['categories'])<=CATEGORIES.keys() or slot['count']<1:raise ValueError('invalid_meal_template')
    return data


def resolve_template(request):
    cfg=templates();kind=request.get('meal_type','unspecified');notice=''
    if kind=='unspecified':
        # Resolve the clock at the boundary; the planner itself consumes explicit inputs.
        hour=request.get('local_hour')
        if hour is None:hour=datetime.now(ZoneInfo(cfg['inference']['timezone'])).hour
        kind=next(p['meal_type'] for p in cfg['inference']['periods'] if p['start']<=hour<p['end'])
        notice='你未指定餐次，按当前本地时间（'+cfg['inference']['timezone']+'）先安排'+lexicon()['meal_types'][kind]['label']+'；可以直接改成其他餐次。'
    template=copy.deepcopy(cfg[kind]);template['meal_type']=kind;slots=[]
    explicit=request.get('explicit_composition',False)
    if explicit:
        composition=cfg['explicit_composition']
        if request.get('meat') is not None and request.get('vegetables') is not None:
            counts=[('meat',request['meat']),('vegetables',request['vegetables'])]
        else:counts=[('dish',request['dishes'])]
        counts.append(('soup',request['soups']))
        template['slots']=[{**composition[key],'count':count} for key,count in counts if count]
        if request.get('minutes') is not None:template.pop('max_total_minutes',None)
    if not explicit and request.get('people') is not None:
        for rule in cfg['people_scaling']:
            if ('min_people' in rule and request['people']<rule['min_people'] or
                'max_people' in rule and request['people']>rule['max_people'] or
                'meal_types' in rule and kind not in rule['meal_types']):continue
            template['slots']=[s for s in template['slots'] if s['role'] not in rule.get('drop_roles',[])]
            if rule.get('add_slot'):template['slots'].append(copy.deepcopy(rule['add_slot']))
    for slot in template['slots']:
        if slot.get('on_request') and not request.get('include_staple'):continue
        slots.extend({**slot,'slot_id':slot['role']+'_'+str(i+1)} for i in range(slot['count']))
    template['slots']=slots
    limits=[n for n in (template.get('max_total_minutes'),request.get('minutes')) if n is not None]
    template['budget']=min(limits) if limits else None
    difficulty=(request.get('constraints') or {}).get('max_difficulty')
    template['max_difficulty']=min(template['max_difficulty'],difficulty) if difficulty is not None else template['max_difficulty']
    return kind,template,notice


def inventory(row,source_rows):
    original=(ROOT/row['source']).read_text().splitlines()
    required,optional,tools,uncertain,refs=recipe_ingredient_fields(source_rows,original)
    return required,optional,tools,uncertain,refs


def primary_ingredients(row):
    title=row['title'];main=row.get('main_ingredients',[])
    names={name for name,pattern in FAMILIES.items() if re.search(pattern,title)}
    # Identify staple/vegetable proteins too, without treating common seasonings as a main.
    for raw in main:
        name=normalize_ingredient(raw)
        if not name or re.search(SEASONING,name) or name.endswith('油') or TOOLS.search(name):continue
        if name not in title and re.fullmatch(r'(?:香|小)?葱|大?蒜(?:瓣)?|生?姜',name):continue
        family=next((name for name,pattern in FAMILIES.items() if re.search(pattern,raw)),None)
        family=family or next((key for key in ('牛奶','鸡蛋') if re.search(EXCLUDED_FAMILIES[key],raw)),None)
        if re.fullmatch(r'(?:内酯|嫩|老|北|南|卤水|石膏)?豆腐',name):family='豆腐'
        names.add(family or re.sub(r'^(?:新鲜|熟透的|冷|熟)','',name))
    if names:return names
    return set()


def validate_menu(rows,template,grouped,state):
    """Recheck category, original time/difficulty, exclusions, hard policy and literal citations."""
    from .meal_planning import duration_estimate
    from .ltm.guard import current,post_check
    from search_filters import filter_chunks
    _,rejected=post_check(rows,current())
    if rejected:raise ValueError('menu_hard_constraint_violation')
    slots={s['slot_id']:s for s in template['slots']};seen=set();main=set();total=0
    for row in rows:
        source=row['source'];slot=slots[row['slot_id']]
        if (slot['role']=='main_staple' and template.get('staple_pattern') and row['category']=='breakfast'
                and not re.search(template['staple_pattern'],row['title'])):raise ValueError('menu_staple_violation')
        if source in seen:raise ValueError('menu_duplicate_source')
        seen.add(source)
        category=next((part for part in source.split('/') if part in slot['categories']),None)
        if category is None:raise ValueError('menu_category_violation')
        _,_,metadata=filter_chunks(grouped[source],max_difficulty=template['max_difficulty'])
        if source not in metadata:raise ValueError('menu_difficulty_violation')
        estimate=duration_estimate(grouped[source])
        if not estimate or estimate!=row['duration']:raise ValueError('menu_time_not_grounded')
        total+=estimate['minutes']
        text=(ROOT/source).read_text()
        if template.get('exclude_pattern') and re.search(template['exclude_pattern'],text):raise ValueError('menu_template_exclusion')
        if any(re.search(exclusion_pattern(n),plain(text)) for n in state.get('excluded_ingredients',[])):
            raise ValueError('menu_exclusion_violation')
        if state.get('excluded_methods') and re.search('炸','\n'.join(r['content'] for r in grouped[source] if re.search('操作|做法|制作',r['section']))):
            raise ValueError('menu_method_violation')
        primary=primary_ingredients(row)
        if main & primary:raise ValueError('menu_duplicate_main_ingredient')
        main.update(primary)
        original=text.splitlines()
        if not row['evidence']:raise ValueError('menu_missing_citation')
        for ref in row['evidence']:
            if ref['source']!=source or '\n'.join(original[ref['start_line']-1:ref['end_line']])!=ref['quote']:
                raise ValueError('menu_invalid_citation')
    if template['budget'] is not None and total>template['budget']:raise ValueError('menu_time_violation')


class MenuPlanner:
    def plan(self,request,chunks,state,template,*,seed=0,fastest=False,taste_preferences=()):
        from .meal_planning import SIMPLE_PREFERENCE,duration_estimate
        from .ltm.retriever import taste_preference_match
        from search_filters import filter_chunks
        grouped={}
        for row in chunks:grouped.setdefault(row['source'],[]).append(row)
        selected,_,metadata=filter_chunks(chunks,max_difficulty=template['max_difficulty'])
        stock={normalize_ingredient(n) for n in request.get('ingredients',[]) or state.get('ingredients',[])}
        previous=set(request.get('previous_sources',[]));accepted=set(request.get('accepted_sources',[]));pools={}
        for slot in template['slots']:
            categories=frozenset(slot['categories'])
            if categories in pools:continue
            selector=copy.deepcopy(state);selector['ingredients']=[]
            if categories<={'drink','soup'} and selector['tastes'] in (['辣味'],['微辣']):selector['tastes']=[]
            data=recommend_preferences(selector,selected,categories=categories,candidate_limit=400)
            pool=[]
            for row in (data or {}).get('recommendations',[]):
                category=metadata[row['source']]['category']
                if template.get('exclude_pattern') and re.search(template['exclude_pattern'],(ROOT/row['source']).read_text()):continue
                if category!='soup' and re.search(r'汤|羹',row['title']) and not categories<={'drink','soup'}:continue
                if slot['role']=='soup' and '粥' in row['title']:continue
                estimate=duration_estimate(grouped[row['source']])
                if not estimate:continue
                required,optional,tools,uncertain,refs=inventory(row,grouped[row['source']])
                matched=matched_inventory(required,optional,uncertain,stock)
                coverage=len(required & stock)/len(required) if required else 0
                tie=hashlib.sha256((str(seed or 0)+template['meal_type']+row['source']).encode()).hexdigest()
                taste_matches=[match for pref in taste_preferences
                               if (match:=taste_preference_match(row.get('description',''),pref))]
                pool.append({**row,'category':category,'difficulty':metadata[row['source']]['difficulty'],
                    'taste_matches':taste_matches,
                    'duration':estimate,'coverage':coverage,'primary_ingredients':sorted(primary_ingredients(row)),
                    'matched':sorted(matched),'missing':sorted(required-stock),'optional':sorted(optional),
                    'tools':sorted(tools),'unresolved':uncertain,'evidence':row['evidence']+refs,'tie':tie})
            # Keep the bounded recall pool: trimming to the fastest 25 excluded
            # dishes that become useful when the user has more time.
            pools[categories]=sorted(pool,key=lambda r:(r['duration']['minutes'],r['tie']))
        prefer_simple=bool(re.search(SIMPLE_PREFERENCE,request.get('question','')))
        required_ids={s['slot_id'] for s in template['slots'] if not s.get('optional')}
        def rank(rows):
            methods=[next((m for m in ('炒','蒸','煮','烤','拌','煎','炖') if m in row['title']),'其他') for row in rows]
            elapsed=sum(r['duration']['minutes'] for r in rows)
            coverage=(-len({r['role'] for r in rows if r['slot_id'] in required_ids}),
                      -sum(r['slot_id'] in required_ids for r in rows),-len(rows))
            taste_matches=sum(len(r['taste_matches']) for r in rows)
            if fastest:return (*coverage,elapsed,-taste_matches,tuple(r['tie'] for r in rows))
            target=(template['budget']*templates()['selection']['target_budget_fraction']*len(rows)/len(template['slots'])
                    if template['budget'] is not None else None)
            suitability=sum(any(m in r['title'] for m in template.get('preferred_methods',[]))
                            for r in rows if r['category'] not in ('soup','drink'))
            return (*coverage,-sum(r['source'] in accepted for r in rows),-taste_matches,sum(r['source'] in previous for r in rows),-sum(r['coverage'] for r in rows),
                sum(r['difficulty'] for r in rows) if prefer_simple else 0,
                abs(elapsed-target) if target is not None else 0,-suitability,
                len(methods)-len(set(methods)),-len({r['category'] for r in rows}),
                -sum(r.get('rerank_score',0) for r in rows),tuple(r['tie'] for r in rows))
        beam=[[]]
        # Plan scarce required courses first, so early dish choices cannot
        # consume the soup's time before that course is considered.
        ordered=sorted(template['slots'],key=lambda s:(bool(s.get('optional')),len(pools[frozenset(s['categories'])])))
        for slot in ordered:
            choices=[]
            for rows in beam:
                choices.append(rows)  # A missing role is explicit in the result, never substituted.
                used={r['source'] for r in rows};primary={n for r in rows for n in r['primary_ingredients']}
                elapsed=sum(r['duration']['minutes'] for r in rows)
                for row in pools[frozenset(slot['categories'])]:
                    if (slot['role']=='main_staple' and template.get('staple_pattern') and row['category']=='breakfast'
                            and not re.search(template['staple_pattern'],row['title'])):continue
                    if row['source'] in used or primary & set(row['primary_ingredients']):continue
                    if template['budget'] is not None and elapsed+row['duration']['minutes']>template['budget']:continue
                    choices.append(rows+[{**row,'role':slot['role'],'role_label':slot['label'],'slot_id':slot['slot_id']}])
            beam=sorted(choices,key=rank)[:128]
        positions={s['slot_id']:i for i,s in enumerate(template['slots'])}
        best=sorted(beam[0],key=lambda r:positions[r['slot_id']])
        validate_menu(best,template,grouped,state)
        selected_ids={r['slot_id'] for r in best}
        missing=[s for s in template['slots'] if s['slot_id'] not in selected_ids]
        return best,missing
