"""Complete literal requests and model understanding share slot validation.

Unconsumed or ambiguous language reaches the model; literal matching never
rejects an utterance at the entry point.
"""
import copy
import hashlib
import json
import re
import time
from contextvars import ContextVar
from urllib.request import Request
from pydantic import BaseModel,ConfigDict,Field,StrictInt,field_validator
from .config import settings
from .normalize import normalize
from .matching import fuzzy_match,match_notice,canonical
from .prompts import rendering as understanding_prompt
from typing import Literal

INTENTS={'chitchat','identity_capability','recipe_lookup','recipe_section','ingredient_recommend','meal_plan','constraint_refine','preference_update','follow_up','out_of_scope','unclear'}
GENERAL_TEXT='我是 cookbuddy，可以按食材找菜谱、回答做法和用量问题、查看原文与图片，也可以按人数和时间安排一餐。'
USAGE=ContextVar('nlu_usage',default=None)

def polite_closure(text):
    return bool(re.fullmatch(
        r'(?:谢谢|谢啦|多谢|感谢)(?:你)?(?:\s*[，,]\s*)?(?:(?:刚才的)?(?:回答|内容)[，,\s]*)?(?:先)?(?:保留|留着)?(?:这两道|这些|这个|吧)?[。！! ]*',
        text))

class Meal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    meal_type:Literal['breakfast','lunch','dinner','snack_late','unspecified']='unspecified'
    explicit_composition:bool=False
    people:StrictInt|None=Field(None,ge=1,le=20)
    minutes:StrictInt|None=Field(None,ge=1,le=1440)
    dishes:StrictInt=Field(2,ge=1,le=3)
    soups:StrictInt=Field(1,ge=0,le=2)
    meat:StrictInt|None=Field(None,ge=0,le=3)
    vegetables:StrictInt|None=Field(None,ge=0,le=3)


class Slots(BaseModel):
    model_config=ConfigDict(extra='forbid')
    dish:str=Field('',max_length=100)
    section:str=Field('',max_length=100)
    ingredients:list[str]=Field(default_factory=list,max_length=20)
    exclude_refs:list[dict]=Field(default_factory=list,max_length=10)
    selected_ref:StrictInt|None=None
    replace_ref:StrictInt|None=None
    limit:StrictInt|None=None
    mode:str|None=None
    request_kind:str='facts'
    constraints:dict=Field(default_factory=dict)
    raw_noise:list[str]=Field(default_factory=list,max_length=20)
    response_fields:list[str]=Field(default_factory=list,max_length=4)
    ingredient_operation:str='set'
    replace_batch:bool=False
    reset_context:bool=False
    meal:Meal|None=None

    @field_validator('selected_ref','replace_ref',mode='before')
    @classmethod
    def ordinal_value(cls,value):
        if isinstance(value,dict) and set(value)=={'type','value'} and value['type']=='ordinal' and type(value['value']) is int:
            return value['value']
        return value

class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    intent:str
    confidence:float=Field(ge=0,le=1,allow_inf_nan=False)
    slots:Slots=Field(default_factory=Slots)
    needs_clarification:bool=False
    clarify_question:str|None=Field(None,max_length=200)
    options:list[str]=Field(default_factory=list,max_length=10)
    assumptions:list[str]=Field(default_factory=list,max_length=10)
    rewrite_query:str=Field('',max_length=2000)


def validate(data):
    # Older integrations supplied course counts before explicit_composition existed.
    data=copy.deepcopy(data)
    meal=data.get('slots',{}).get('meal')
    if isinstance(meal,dict):
        if 'explicit_composition' not in meal and any(meal.get(key) is not None for key in ('dishes','soups')):
            meal['explicit_composition']=True
        # Unspecified composition is owned by the meal template. Models may
        # represent those absent counts as null; explicit counts stay strict.
        if not meal.get('explicit_composition'):
            for key in ('dishes','soups'):
                if meal.get(key) is None:meal[key]=Meal.model_fields[key].default
    result=Decision.model_validate(data).model_dump()
    s=result['slots']
    if result['intent'] not in INTENTS or s['request_kind'] not in ('facts','source','images','bundle','ingredients') or s['mode'] not in (None,'inventory','preference'):
        raise ValueError('invalid_nlu_enum')
    from cookbuddy.recommendation_schema import RESPONSE_FIELDS
    if s['ingredient_operation'] not in ('set','add','remove','clear') or any(v not in RESPONSE_FIELDS for v in s['response_fields']):raise ValueError('invalid_nlu_edit')
    if any(set(r)!={'type','value'} or r['type']!='ordinal' or type(r['value']) is not int or not 1<=r['value']<=99 for r in s['exclude_refs']):
        raise ValueError('invalid_nlu_reference')
    if set(s['constraints'])-{'taste','excluded_ingredients','excluded_methods','allowed_ingredients','allowed_methods','category','max_difficulty','unresolved'}:
        raise ValueError('invalid_nlu_constraint')
    for k in ('excluded_ingredients','excluded_methods','allowed_ingredients','allowed_methods','unresolved'):
        if k in s['constraints'] and (not isinstance(s['constraints'][k],list) or any(not isinstance(v,str) or len(v)>100 for v in s['constraints'][k])):raise ValueError('invalid_nlu_constraint')
    if s['constraints'].get('taste') not in (None,'辣味','微辣','不辣'):raise ValueError('invalid_nlu_taste')
    if s['constraints'].get('max_difficulty') is not None and (type(s['constraints']['max_difficulty']) is not int or not 1<=s['constraints']['max_difficulty']<=5):raise ValueError('invalid_nlu_difficulty')
    if s['constraints'].get('category') is not None:
        from recipe_catalog import CATEGORIES
        if s['constraints']['category'] not in CATEGORIES:raise ValueError('invalid_nlu_category')
    if any(not 0<=s[k]<=99 for k in ('selected_ref','replace_ref','limit') if s[k] is not None):raise ValueError('invalid_nlu_count')
    if s['exclude_refs'] or s['replace_ref'] is not None:result['intent']='constraint_refine'
    return result


def _model(original,text,context):
    from llm_config import require_llm,chat_parameters
    from service_runtime import request_json,request_budget
    cfg=require_llm();cfg={**cfg,'model':settings()['router']['model'] or cfg['model']}
    # hy3 counts internal reasoning against this cap; 650 produced no JSON.
    output_budget=3072 if cfg['model'].split('/')[-1]=='hy3' else 650
    payload=chat_parameters({'temperature':0,'max_tokens':output_budget,'response_format':{'type':'json_object'},
        'messages':[{'role':'system','content':understanding_prompt()},{'role':'user','content':json.dumps({'original':original,'normalized':text,'context':context},ensure_ascii=False)}]},cfg)
    request=Request(cfg['base_url']+'/chat/completions',data=json.dumps(payload,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+cfg['api_key'],'Content-Type':'application/json'})
    from resilience.config import settings as resilience_settings
    timeout=min(settings()['router']['timeout_s'],resilience_settings()['timeouts_ms']['router_model']/1000)
    with request_budget(timeout):
        response=request_json(request,timeout=timeout,attempts=1,phase='nlu_understanding')
    meter=USAGE.get()
    if meter is not None:
        meter['model_calls']+=1
        for key in ('prompt_tokens','completion_tokens'):meter[key]+=int((response.get('usage') or {}).get(key,0))
    choice=response['choices'][0]
    if choice.get('finish_reason')!='stop':raise ValueError('nlu_incomplete')
    return validate(json.loads(choice['message']['content']))


def _pending_price_waiver(text,meal):
    if not meal:return False
    from cookbuddy.meal_planning import PRICE_WAIVER
    unresolved=meal.get('constraints',{}).get('unresolved',[])
    return bool(re.search(PRICE_WAIVER,text)
        and any(re.search(r'预算|价格|费用|花费|成本',value) for value in unresolved))


def fallback(text,context):
    """Best-effort hints after a failed model; the literal query is preserved."""
    from recipe_tools import resolve_title,load_chunks
    from cookbuddy.recommendation_turn import interpret
    from src.agent.turn_intents import alternative_request,chapter_request,image_section,rejected_ordinal,recipe_fact_request,explicit_ordinal
    from cookbuddy.recommendation_schema import NUMBERS
    q=text.rstrip('.!? ');compact=re.sub(r'\s','',q);turn=interpret(q)
    alt=alternative_request(q)
    if alt:turn=interpret('' if rejected_ordinal(alt['constraint']) else alt['constraint'])
    s=Slots().model_dump();intent='unclear';selected=context.get('slots',{}).get('selected_dish')
    choices=context.get('slots',{}).get('last_recommendations') or (context.get('previous_task') or {}).get('choices') or []
    named=resolve_title(q,load_chunks());chapter=chapter_request(q,named[0] if named else '')
    # These are fallback features, not pre-model gates or an exhaustive grammar.
    if re.search(r'忘|记住|记忆|偏好设置',q):intent='preference_update'
    elif re.search(r'天气|股票|电影|软件|网站|写代码|旅游',q):intent='out_of_scope'
    elif chapter is not None or selected and re.search(r'制作|那段|章节|部分|酵头',q) and not re.search(r'图片|照片|配图|原图|多少|多久|温度|水温|酵母|需要|用量|比例|有没有|是否',q):
        intent='recipe_section';s['section']=chapter or re.sub(r'^(?:请)?(?:看看|展示|查看)','',q);s['request_kind']='source'
    elif (alt:=alternative_request(q)) is not None:
        intent='constraint_refine';s['limit']=alt.get('count');s['replace_ref']=alt.get('index')
        n=rejected_ordinal(alt['constraint'])
        if n:s['exclude_refs']=[{'type':'ordinal','value':n}]
    elif re.search(r'不要|别要|排除',q) and (m:=re.search(r'第\s*([一二两三四五六七八九十]|\d{1,2})\s*(?:道|个)',q)):
        intent='constraint_refine';s['exclude_refs']=[{'type':'ordinal','value':int(m[1]) if m[1].isdigit() else NUMBERS[m[1]]}];s['limit']=1
    elif choices and (ordinal:=explicit_ordinal(q)) is not None:
        intent='follow_up';s['selected_ref']=ordinal;s['request_kind']='facts'
    elif turn.ordinal is not None:intent='follow_up';s['selected_ref']=turn.ordinal
    elif turn.recommend or turn.edits or re.search(r'换|清淡|简单',q) and context.get('slots',{}).get('last_recommendations'):
        intent='constraint_refine' if context.get('previous_task') else 'ingredient_recommend'
    elif named or selected and recipe_fact_request(q) or re.search(r'做法|怎么做|原料|食材|烹饪|菜谱|调料|发酵|烘烤|炒|蒸|煮|烤|酵头',q):intent='recipe_lookup'
    elif re.search(r'谁|身份|名字|叫|介绍|功能|能力|本领|使用|能.*做',q):intent='identity_capability'
    elif re.search(r'好|嗨|在吗|谢|辛苦|早安|晚安',q):intent='chitchat'
    if named and intent!='constraint_refine':s['dish']=named[0]
    if selected and not named and re.fullmatch(r'(?:那|那么|请|请问)?(?:这道菜?|那道菜?|它)?(?:具体|到底|要|该|应该|得|再)?(?:怎么做|怎么制作|如何做|做法|步骤)(?:呢|呀|啊)?',q):
        intent='follow_up';s['dish']=selected['dish_name'];s['request_kind']='source'
        s['section']=context.get('slots',{}).get('selected_section','')
    if not named and intent=='recipe_lookup':
        # Extract a name candidate only in the fallback, then validate against
        # corpus; arbitrary unrelated prose is never force-matched to a dish.
        if selected and recipe_fact_request(q):
            s['dish']=selected['dish_name']
        else:
            candidate=re.sub(r'^(?:请|麻烦|帮我|给我|看看|查看|查一下)\s*','',q)
            candidate=re.split(r'的?(?:完整)?(?:菜谱|原文|做法|步骤|来源)|怎么|如何|做法|原料|需要',candidate)[0]
            s['dish']=candidate.strip('请帮我做查一下')
    for e in turn.edits:
        if e.slot in ('ingredients','mode','limit'):
            s[e.slot]=list(e.values) if e.slot=='ingredients' else e.values[0]
            if e.slot=='ingredients':s['ingredient_operation']=e.operation
        if e.slot=='tastes':s['constraints']['taste']=e.values[0]
        if e.slot in ('excluded_ingredients','excluded_methods'):
            key=e.slot if e.operation=='add' else e.slot.replace('excluded','allowed')
            s['constraints'][key]=list(e.values)
        if e.slot=='extra_constraints':
            s['constraints']['unresolved']=list(dict.fromkeys(s['constraints'].get('unresolved',[])+list(e.values)))
        if e.slot=='response_fields':s['response_fields']=list(e.values)
    if turn.max_difficulty is not None:
        current=s['constraints'].get('max_difficulty')
        s['constraints']['max_difficulty']=turn.max_difficulty if current is None else min(current,turn.max_difficulty)
    if turn.unresolved:s['constraints']['unresolved']=list(turn.unresolved)
    if intent not in ('ingredient_recommend','constraint_refine'):
        # Unknown recipe prose is a question, not an unfulfilled menu filter.
        s['constraints'].pop('unresolved',None)
    s['reset_context']=bool(turn.reset)
    from cookbuddy.meal_planning import meal_request
    meal=meal_request(q)
    explicit_meal=meal is not None
    old_meal=context.get('slots',{}).get('meal_request')
    if not meal and old_meal and not named:
        from cookbuddy.meal_planning import NUMBER
        # Short answers to the active menu stay in that task during an outage.
        if re.fullmatch(r'(?:'+NUMBER+r'\s*(?:个)?人|'+NUMBER+r'\s*(?:分钟|(?:个)?小时))',q):
            update=meal_request('定个菜单，'+q)
            meal={**old_meal,**{k:update[k] for k in ('people','minutes') if update[k] is not None}}
        elif _pending_price_waiver(q,old_meal):
            meal=copy.deepcopy(old_meal)
        elif (alt and not alt['single'] and not alt.get('ambiguous_input')) or re.fullmatch(r'(?:请)?重新安排(?:吧)?[。！!]?',q):
            meal=copy.deepcopy(old_meal)
    if meal:
        from cookbuddy.meal_planning import meal_conditions,meal_difficulty
        state,residual=meal_conditions(alt['constraint'] if alt and not explicit_meal else q)
        intent='meal_plan';s['dish']='';s['meal']={k:meal[k] for k in Meal.model_fields}
        s['constraints']=copy.deepcopy(old_meal.get('constraints',{})) if old_meal and not explicit_meal else {}
        s['constraints'].update({k:state[k] for k in ('excluded_ingredients','excluded_methods') if state[k]})
        if state['tastes']:s['constraints']['taste']=state['tastes'][0]
        difficulty=meal_difficulty(q)
        if difficulty is not None:s['constraints']['max_difficulty']=difficulty
        if residual and (explicit_meal or alt):s['constraints']['unresolved']=[residual]
    s['replace_batch']=alternative_request(q) is not None
    if not meal:
        from cookbuddy.ltm.extractor import rule_extract
        spice=next((c for c in reversed(rule_extract(q)) if c.dimension=='spice_level' and c.op=='set'),None)
        if spice and context.get('previous_task'):
            s['constraints']['taste']={'0':'不辣','1':'微辣','2':'辣味','3':'辣味'}[spice.value]
            if intent in ('unclear','chitchat','preference_update'):intent='constraint_refine'
            if re.fullmatch(r'(?:我)?(?:现在)?(?:不太能吃辣|不能吃辣|不吃辣|不要辣|微辣|中辣|重辣|特辣)',q):
                s['constraints'].pop('unresolved',None)
    if re.search(r'图片|照片|配图|原图',q):s['request_kind']='bundle' if re.search(r'原文|全文',q) else 'images'
    elif re.search(r'原文|全文',q) and not recipe_fact_request(q):s['request_kind']='source'
    ref=explicit_ordinal(q)
    if (not named and ref is not None and context.get('slots',{}).get('last_recommendations')
            and intent in ('recipe_lookup','recipe_section','follow_up')):
        s['selected_ref']=ref
        s['dish']=''
        if re.search(r'做法|步骤|怎么做',q):s['request_kind']='source'
    if s['request_kind']=='images' and selected:
        image=image_section(q,'')
        if image is not None:s['section']=image
    result={'intent':intent,'confidence':.7 if intent!='unclear' else .3,'slots':s}
    if alt and alt.get('count') is not None and not 1<=alt['count']<=10:
        result.update(needs_clarification=True,clarify_question='一次可以推荐1–10道菜，你想换几道？')
    if alt and alt.get('clarification'):result.update(needs_clarification=True,clarify_question=alt['clarification'])
    return validate(result)


def named_section(text,title,rows):
    """A chapter the user explicitly named for a resolved dish."""
    from recipe_tools import match_sections
    sources={r['source'] for r in rows if r['title']==title}
    if len(sources)!=1:return ''
    own=[r for r in rows if r['source']==next(iter(sources))]
    synonyms={'用料':'原料','食材':'原料','配料':'原料','步骤':'操作','做法':'操作','用量':'计算'}
    candidates=[]
    match=re.search(r'的([^的，。；！？]{1,12}?)(?:原文|全文|步骤|做法|部分|章节)',text)
    if match:candidates.append(match[1])
    for word in ('操作','原料','用料','食材','配料','步骤','做法','计算','简介','附加内容','必备原料和工具'):
        if word in text:candidates.append(word)
    for candidate in candidates:
        matched,_=match_sections(synonyms.get(candidate,candidate),own)
        if matched.get('value'):return matched['value']
    return ''


def literal_decision(text,context,allow_context_facts=True):
    """Skip remote reasoning only when the whole supported request is consumed."""
    from cookbuddy.recommendation_turn import interpret
    selection=pending_selection(text,context)
    if selection is not None:return selection
    from cookbuddy.meal_planning import meal_request,meal_conditions,NUMBER,number,PRICE_LIMIT
    from cookbuddy.lexicon import meal_match
    from src.agent.turn_intents import alternative_request,rejected_ordinal,explicit_ordinal,ingredient_followup,recipe_fact_request
    from cookbuddy.meal_planning import accepted_offer_minutes
    slots=context.get('slots',{});old_meal=slots.get('meal_request')
    selected=_selected_recipe(context)
    if selected and re.search(r'图片|照片|配图|图像',text) and re.search(r'只|仅|单|根据|凭|看',text) and re.search(r'熟|中心温度|内部|配比|比例|用量|温度',text):
        return validate({'intent':'recipe_lookup','confidence':1.,'slots':{
            'dish':selected['dish_name'],'request_kind':'images'}})
    if allow_context_facts and selected and recipe_fact_request(text):
        return validate({'intent':'recipe_lookup','confidence':1.,'slots':{
            'dish':selected['dish_name'],'request_kind':'facts'}})
    choices=slots.get('last_recommendations') or (context.get('previous_task') or {}).get('choices') or []
    ordinal=explicit_ordinal(text)
    if choices and ordinal is not None and re.fullmatch(
            r'(?:请|我要|我想|只看|看|选|选择)?\s*第\s*(?:[一二两三四五六七八九十]|\d{1,2})\s*(?:道|项|个)(?:菜)?[。！!？? ]*',
            text):
        return validate({'intent':'follow_up','confidence':1.,'slots':{
            'selected_ref':ordinal,'request_kind':'facts'}})
    if choices and ordinal is not None and re.fullmatch(
            r'(?:现在|刚才|那么|请问)?\s*第\s*(?:[一二两三四五六七八九十]|\d{1,2})\s*(?:道|项|个)(?:菜)?的?(?:完整)?(?:做法|原文|步骤|操作)(?:是什么|给我|吧)?[。！!？? ]*',
            text):
        return validate({'intent':'recipe_section','confidence':1.,'slots':{
            'selected_ref':ordinal,'section':'','request_kind':'source'}})
    ingredient_check=re.fullmatch(
        r'(?:请|帮我)?(?:核对|检查|对照)?(?P<dish>[^，。；！？\n]{2,30}?)(?:的)?(?:用料|食材|原料|配料)(?:是否齐全|够不够|有哪些|是什么)?[。！!？? ]*',
        text)
    if ingredient_check:
        from recipe_tools import load_chunks,resolve_title
        named=resolve_title(ingredient_check['dish'],load_chunks())
        if named:
            return validate({'intent':'recipe_lookup','confidence':1.,'slots':{
                'dish':named[0],'request_kind':'ingredients'}})
    if (not re.search(r'换个话题|新任务|重新开始|不聊这个',text)
            and re.search(r'图片|照片|配图|原图|原文|全文|完整菜谱|来源',text)):
        from recipe_tools import load_chunks,resolve_title
        named=resolve_title(text,load_chunks())
        if named:
            # "原文" as a qualifier ("原文没说明的请指出") does not turn a
            # specific attribute question into a full-text request.
            explicit_source=re.search(
                r'(?:看|查看|给我|展示|显示|读|打开|发).{0,8}(?:原文|全文|完整菜谱)|(?:原文|全文|完整菜谱).{0,6}(?:给我|看看|看一下|展示|发我)',
                text)
            if recipe_fact_request(text) and not explicit_source:
                return validate({'intent':'recipe_lookup','confidence':1.,'slots':{
                    'dish':named[0],'request_kind':'facts'}})
            has_image=bool(re.search(r'图片|照片|配图|原图',text))
            has_source=bool(re.search(r'原文|全文|完整菜谱|来源',text))
            kind='bundle' if has_image and has_source else 'images' if has_image else 'source'
            return validate({'intent':'recipe_lookup','confidence':1.,'slots':{
                'dish':named[0],'section':named_section(text,named[0],load_chunks()),'request_kind':kind}})
    minutes=accepted_offer_minutes(text,slots.get('pending_meal_offer'))
    if not meal_request(text) and _pending_price_waiver(text,old_meal):
        result=fallback(text,context)
        if result['intent']=='meal_plan':
            result['confidence']=1.
            return result
    if old_meal and not meal_request(text):
        people=re.search(r'('+NUMBER+r')\s*(?:个)?人',text)
        minutes_match=re.search(r'('+NUMBER+r')\s*分钟',text)
        if (people or minutes_match) and re.search(r'改成|改为|换成|仍然|还是|保持|其他要求不变|时间',text):
            meal={k:v for k,v in old_meal.items() if k in Meal.model_fields}
            if people:meal['people']=number(people[1])
            if minutes_match:meal['minutes']=number(minutes_match[1])
            return validate({'intent':'meal_plan','confidence':1.,'slots':{
                'meal':meal,'constraints':copy.deepcopy(old_meal.get('constraints',{}))}})
        if re.search(PRICE_LIMIT,text):
            constraints=copy.deepcopy(old_meal.get('constraints',{}))
            constraints['unresolved']=list(dict.fromkeys(constraints.get('unresolved',[])+[text.rstrip('。')]))
            meal={k:v for k,v in old_meal.items() if k in Meal.model_fields}
            return validate({'intent':'meal_plan','confidence':1.,'slots':{
                'meal':meal,'constraints':constraints}})
    if old_meal and minutes is not None:
        meal={k:v for k,v in old_meal.items() if k in Meal.model_fields}
        meal['minutes']=minutes
        return validate({'intent':'meal_plan','confidence':1.,'slots':{'meal':meal,
            'constraints':copy.deepcopy(old_meal.get('constraints',{}))}})
    alternative=alternative_request(text)
    if alternative:
        choices=context.get('slots',{}).get('last_recommendations') or (context.get('previous_task') or {}).get('choices')
        if not choices or alternative.get('clarification') or alternative.get('ambiguous_input'):return None
        clause=alternative['constraint']
        turn=interpret('' if rejected_ordinal(clause) else clause)
        complete=not turn.unresolved and all(e.slot!='extra_constraints' for e in turn.edits)
    elif ingredient_followup(text):
        choices=context.get('slots',{}).get('last_recommendations') or (context.get('previous_task') or {}).get('choices') or []
        ref=explicit_ordinal(text)
        if ref is not None and not 1<=ref<=len(choices):return None
        selected=_selected_recipe(context)
        if ref is not None and 1<=ref<=len(choices):selected=choices[ref-1]
        if ref is None and len(choices)==1:selected=choices[0]
        if selected:
            return validate({'intent':'follow_up','confidence':1.,'slots':{
                'selected_ref':ref,'dish':selected['dish_name'],'request_kind':'ingredients'}})
        return None
    elif meal_request(text):
        _,residue=meal_conditions(text)
        complete=not residue and not meal_match(text)['ambiguous']
    else:
        turn=interpret(text)
        complete=(turn.recommend and not turn.boundary and not turn.unresolved
                  and (any(e.slot=='mode' and e.values==('inventory',) for e in turn.edits)
                       or turn.max_difficulty is not None or turn.quick)
                  and all(e.slot!='extra_constraints' for e in turn.edits))
    if not complete:
        from recipe_tools import resolve_title,load_chunks
        from recipe_fact_answer import duration_relation_fact
        rows=load_chunks();named=resolve_title(text,rows);selected=_selected_recipe(context)
        from src.agent.turn_intents import explicit_ordinal,referenced_chapter
        ref=explicit_ordinal(text);chapter=referenced_chapter(text)
        choices=context.get('slots',{}).get('last_recommendations',[])
        ordinal_name=ref is not None and re.fullmatch(
            r'(?:现在|刚才|那么|请问)?\s*第\s*(?:[一二两三四五六七八九十]|\d{1,2})\s*(?:道|个)?\s*(?:菜)?\s*(?:叫什么|叫啥|是什么|是哪道|是哪一道)(?:菜)?(?:\s*[。！!？?]?\s*(?:给我|请给我|要)?(?:完整)?(?:做法|原文|步骤|操作)(?:吧)?)?[。！!？? ]*',
            text)
        if ordinal_name:
            kind='source' if re.search(r'做法|原文|步骤|操作',text) else 'facts'
            return validate({'intent':'follow_up','confidence':1.,'slots':{
                'selected_ref':ref,'request_kind':kind}})
        if chapter is not None and ref is not None:
            if not 1<=ref<=len(choices):
                return validate({'intent':'recipe_section','confidence':1.,'slots':{
                    'selected_ref':ref,'section':chapter,'request_kind':'source'}})
            selected=choices[ref-1]
        if selected:
            sections={p for r in rows if r['source']==selected['source_path'] for p in r['section'].split(' > ')}
            section=next((p for p in sections if canonical(chapter if chapter is not None else text)==canonical(p)),None)
            if section is not None:
                return validate({'intent':'recipe_section','confidence':1.,'slots':{
                    'dish':selected['dish_name'],'selected_ref':ref,'section':section,'request_kind':'source'}})
        if not named and selected:named=(selected['dish_name'],selected['source_path'])
        if named:
            query=text+'\n当前菜谱：'+named[0]+'\n指定菜谱原文来源：'+named[1]
            if duration_relation_fact(query,rows):
                return validate({'intent':'recipe_lookup','confidence':1.,'slots':{'dish':named[0],'request_kind':'facts'}})
        return None
    result=fallback(text,context)
    if result['intent'] not in ('ingredient_recommend','constraint_refine','meal_plan'):return None
    result['confidence']=1.
    return result


def pending_selection(text,context):
    pending=context.get('slots',{}).get('nlu_pending',{})
    slot=pending.get('slot')
    if slot=='conditions':
        if text!=pending.get('options',[''])[-1]:return None
        from cookbuddy.recommendation_schema import valid_state
        state=pending.get('state')
        if not valid_state(state):return None
        slots=Slots().model_dump()
        slots.update(ingredients=list(state['ingredients']),mode=state['mode'],limit=state['limit'])
        if state['tastes']:slots['constraints']['taste']=state['tastes'][0]
        for key in ('excluded_ingredients','excluded_methods'):
            if state.get(key):slots['constraints'][key]=list(state[key])
        return validate({'intent':'constraint_refine','confidence':1.,'slots':slots})
    if slot not in ('dish','section') or text not in pending.get('options',[]) or not pending.get('slots'):return None
    slots=copy.deepcopy(pending['slots']);slots[slot]=text
    slots['selected_ref']=None
    intent='recipe_section' if slots.get('section') else 'recipe_lookup'
    return validate({'intent':intent,'confidence':1.,'slots':slots})


def _selected_recipe(context):
    slots=context.get('slots',{})
    choices=slots.get('last_recommendations',[])
    return slots.get('selected_dish') or (choices[0] if len(choices)==1 else None)


def _context(context):
    # Only summary, slots and the last two turns. Tenant identifiers affect cache
    # isolation but are not sent to the model.
    selected=_selected_recipe(context)
    from recipe_tools import load_chunks
    sections=sorted({p for r in load_chunks() if selected and r['source']==selected['source_path'] for p in r['section'].split(' > ')})
    slots=copy.deepcopy(context.get('slots',{}))
    if selected:slots['selected_dish']=copy.deepcopy(selected)
    return {'summary':str(context.get('summary',''))[:1000],'section_candidates':sections,
        'slots':slots,
        'messages':[{'role':m['role'],'content':str(m['content'])[:600]} for m in context.get('messages',[])[-4:]],
        'previous_task':{k:v for k,v in (context.get('previous_task') or {}).items() if k in ('ingredients','tastes','mode','excluded_ingredients','excluded_methods','limit')}}


def clarify(result,question,options):
    question=re.split(r'[？?]',question)[0]+'？'
    options=list(dict.fromkeys(options))[:10]
    if len(options)<2:options+=['我补充说明','按食材推荐'][:2-len(options)]
    result.update(answer_mode='clarify',needs_clarification=True,clarify_question=question,
                  options=options)


def ground(result,context,use_model):
    from recipe_tools import load_chunks,resolve_title
    from ingredient_recommendation import ingredient_vocabulary,normalize_ingredient
    s=result['slots'];intent=result['intent'];matches=[]
    from src.agent.turn_intents import equipment_statement
    if equipment_statement(result['original']) and intent in ('recipe_lookup','ingredient_recommend','unclear','chitchat'):
        intent='preference_update';s['dish']=''
        result.update(intent=intent,needs_clarification=False,options=[])
    result['answer_mode']='general_chat' if intent in ('chitchat','identity_capability','unclear') else 'out_of_scope' if intent=='out_of_scope' else 'memory' if intent=='preference_update' else 'grounded_recipe'
    rows=load_chunks();titles={r['title'] for r in rows}
    from cookbuddy.recommendation_turn import interpret
    from src.agent.turn_intents import alternative_request
    replacement=alternative_request(result['normalized'])
    literal=interpret(replacement['constraint'] if replacement else result['normalized'])
    old_meal=context.get('slots',{}).get('meal_request')
    if (old_meal and replacement and not replacement['single'] and not replacement['constraint']
            and not replacement.get('ambiguous_input') and not replacement.get('clarification') and not s['reset_context']):
        s['meal']=Meal.model_validate({k:v for k,v in old_meal.items() if k in Meal.model_fields}).model_dump()
        s['constraints']=copy.deepcopy(old_meal.get('constraints',{}))
        result.update(intent='meal_plan',needs_clarification=False,answer_mode='grounded_recipe',options=[])
        intent='meal_plan'
    if intent=='meal_plan':
        from cookbuddy.meal_planning import unresolved_conditions
        s['constraints']['unresolved']=unresolved_conditions(s['constraints'].get('unresolved',[]),result['original'])
    if intent=='meal_plan' and s['meal'] is None:
        old={} if s['reset_context'] else context.get('slots',{}).get('meal_request',{})
        s['meal']=Meal.model_validate({k:v for k,v in old.items() if k in Meal.model_fields}).model_dump()
    if intent=='meal_plan' and s['meal'] and not s['constraints'].get('unresolved'):
        result.update(needs_clarification=False,options=[])
    if intent=='meal_plan':
        from cookbuddy.meal_planning import meal_request
        literal_meal=meal_request(result['normalized'])
        if literal_meal:
            if literal_meal['meal_type']!='unspecified':s['meal']['meal_type']=literal_meal['meal_type']
            for key in ('people','minutes'):
                if literal_meal[key] is not None:s['meal'][key]=literal_meal[key]
            if literal_meal['explicit_composition']:
                s['meal'].update({k:literal_meal[k] for k in ('dishes','soups','meat','vegetables','explicit_composition')})
    for edit in literal.edits:
        if edit.slot in ('excluded_ingredients','excluded_methods'):
            key=edit.slot if edit.operation=='add' else edit.slot.replace('excluded','allowed')
            s['constraints'][key]=list(dict.fromkeys(s['constraints'].get(key,[])+list(edit.values)))
        elif edit.slot=='limit':
            s['limit']=edit.values[0]
        elif edit.slot=='extra_constraints':
            s['constraints']['unresolved']=list(dict.fromkeys(s['constraints'].get('unresolved',[])+list(edit.values)))
        elif edit.slot=='response_fields':
            s['response_fields']=list(dict.fromkeys(s['response_fields']+list(edit.values)))
    if literal.max_difficulty is not None:
        current=s['constraints'].get('max_difficulty')
        s['constraints']['max_difficulty']=literal.max_difficulty if current is None else min(current,literal.max_difficulty)
    # Reject questions about optional inputs when a complete supported request
    # already exists. This is post-model slot validation, never an entry gate.
    if literal.recommend and not literal.unresolved and intent not in ('meal_plan','recipe_section','recipe_lookup','follow_up'):
        s['constraints'].pop('unresolved',None)
        result.update(intent='constraint_refine' if literal.resume else 'ingredient_recommend',
                      needs_clarification=False,answer_mode='grounded_recipe',options=[])
        intent=result['intent']
    # A change-of-dish request is task state, never a long-term preference write.
    # The model occasionally routes it to preference_update and the memory tool
    # then fails; the literal turn is the bounded fallback for this exact form.
    change_cue=re.search(r'(?:换成|换个|换一道|换一个|换菜|换一批|改成|改个|改用|换用)',result['original'])
    memory_cue=re.search(r'记住|记一下|记下|忘记|忘掉|偏好设置|以后|从现在|长期|一直|我的偏好',result['original'])
    if change_cue and not memory_cue and (literal.recommend or intent=='preference_update') and intent not in ('meal_plan','recipe_section','recipe_images'):
        if literal.unresolved:
            s['constraints']['unresolved']=list(dict.fromkeys(s['constraints'].get('unresolved',[])+literal.unresolved))
        result.update(intent='constraint_refine',needs_clarification=False,answer_mode='grounded_recipe',options=[])
        intent='constraint_refine'
    if result['needs_clarification'] and (context.get('previous_task') or context.get('slots',{}).get('last_recommendations')):
        from src.agent.turn_intents import alternative_request,rejected_ordinal
        alternative=alternative_request(result['original'])
        if alternative and not alternative.get('clarification') and (alternative.get('count') is None or 1<=alternative['count']<=10) and (not alternative['constraint'] or rejected_ordinal(alternative['constraint'])):
            result.update(intent='constraint_refine',needs_clarification=False,answer_mode='grounded_recipe',options=[])
            s['limit']=alternative.get('count') or 1;intent='constraint_refine'
    choices=context.get('slots',{}).get('last_recommendations',[]) or [
        {'dish_name':r['title'],'source_path':r['source']} for r in (context.get('previous_task') or {}).get('choices',[])]
    from src.agent.turn_intents import alternative_request
    replacement=alternative_request(result['normalized'])
    if replacement and replacement.get('index') and choices:
        n,keep=replacement['index'],replacement.get('preserved_index',replacement['index'])
        if 1<=n<=len(choices) and 1<=keep<=len(choices):
            s.update(replace_ref=n,selected_ref=None,replace_batch=False,limit=1,exclude_refs=[])
            if literal.unresolved:s['constraints']['unresolved']=list(literal.unresolved)
            else:s['constraints'].pop('unresolved',None)
            result.update(intent='constraint_refine',needs_clarification=False,answer_mode='grounded_recipe',options=[])
            intent='constraint_refine'
        else:
            clarify(result,'当前列表只有'+str(len(choices))+'道菜，请明确要替换和保留哪一道。',[str(i)+'. '+r['dish_name'] for i,r in enumerate(choices,1)])
    from src.agent.turn_intents import ingredient_followup,named_ingredient_stock,image_section,explicit_ordinal,referenced_chapter
    literal_named=resolve_title(result['original'],rows)
    named_stock=named_ingredient_stock(result['original'],literal_named[0]) if literal_named else None
    ref=explicit_ordinal(result['normalized'])
    chapter=referenced_chapter(result['normalized'])
    if chapter is not None and ref is not None and not 1<=ref<=len(choices):
        s.update(dish='',selected_ref=None,section=chapter,request_kind='source')
        clarify(result,'当前列表只有'+str(len(choices))+'道菜，你想查看哪一道的章节？',[r['dish_name'] for r in choices])
        result['clarification_slot']='dish'
        return result
    if (ref is not None and choices and not literal_named
            and (intent in ('recipe_lookup','recipe_section','follow_up') or ingredient_followup(result['normalized']) or chapter is not None)):
        s.update(selected_ref=ref,dish='')
        if (1<=ref<=len(choices) and s['request_kind'] in ('source','images','bundle','ingredients')
                and not s['constraints'].get('unresolved')):
            result.update(needs_clarification=False,answer_mode='grounded_recipe',options=[])
    selected=_selected_recipe(context)
    if s['selected_ref'] and 1<=s['selected_ref']<=len(choices):selected=choices[s['selected_ref']-1]
    # An explicit stock comparison must not become a chapter-name question.
    # The existing prepare step resolves ambiguous recipe references separately.
    if named_stock is not None:
        s.update(dish=literal_named[0],section='',request_kind='ingredients',selected_ref=None)
        if named_stock:s['ingredients']=named_stock
        s['constraints'].pop('unresolved',None)
        result.update(intent='recipe_lookup',needs_clarification=False,answer_mode='grounded_recipe',options=[])
        intent='recipe_lookup'
    elif ingredient_followup(result['normalized']):
        s.update(dish=selected['dish_name'] if selected else '',section='',request_kind='ingredients')
        result.update(intent='follow_up',needs_clarification=False,answer_mode='grounded_recipe',options=[])
        intent='follow_up'
    elif selected and not literal.recommend and not literal.edits:
        source=selected['source_path']
        sections={p for r in rows if r['source']==source for p in r['section'].split(' > ')}
        section=next((p for p in sorted(sections) if canonical(result['normalized'])==canonical(p)),None)
        if chapter is not None:section=chapter
        if section is not None:s['request_kind']='source'
        image=image_section(result['normalized'],'') if not literal_named else None
        # A verified chapter belongs to the current recipe, even when the model
        # or fallback mistakenly extracted the whole image request as a dish.
        if image is not None and (not image or fuzzy_match(image,sections)['value']):
            section=image;s['request_kind']='images'
        if section is not None:
            s.update(dish=selected['dish_name'],section=section,selected_ref=None)
            intent='recipe_section' if section else 'recipe_lookup'
            result.update(intent=intent,needs_clarification=False,answer_mode='grounded_recipe',options=[])
    if literal_named and intent!='meal_plan':
        from recipe_fact_answer import source_fact
        if intent in ('recipe_lookup','recipe_section','follow_up'):s['dish']=literal_named[0]
        if source_fact(result['original'],rows):
            s.update(dish=literal_named[0],section='')
            result.update(intent='recipe_lookup',needs_clarification=False,answer_mode='grounded_recipe',options=[])
            intent='recipe_lookup'
        image=image_section(result['normalized'],literal_named[0])
        if image is not None and s['request_kind']=='images':
            s.update(dish=literal_named[0],section=image)
            intent='recipe_section' if image else 'recipe_lookup'
            result.update(intent=intent,needs_clarification=False,answer_mode='grounded_recipe',options=[])
    if s['dish']:
        resolved=resolve_title(s['dish'],rows)
        from retrieval_candidates import match_titles
        alias=bool(match_titles(s['dish'],titles))
        m=({'value':resolved[0],'score':1.,'level':'L1','ambiguous':False} if resolved and alias else fuzzy_match(s['dish'],titles,fillers=False,min_score=settings()['fuzzy']['t_dish']));matches.append(m)
        if m['value']:
            s['dish']=m['value'];notice=match_notice(m)
            if notice:result['assumptions'].append(notice)
        elif m['ambiguous']:
            clarify(result,'你想查看哪道菜？',[r['value'] for r in m['choices']]);result['clarification_slot']='dish'
        else:
            result['unmatched_dish']=s['dish'];result['options']=[r['value'] for r in m.get('choices',[])]
    if (intent=='recipe_section' and not result['needs_clarification']
            and (s['request_kind']=='facts' or not s['section'] and s['request_kind'] in ('source','bundle'))):
        intent='recipe_lookup';result['intent']=intent
        if not s['dish'] and selected:s['dish']=selected['dish_name']
    if intent=='recipe_section':
        if not s['dish'] and selected:s['dish']=selected['dish_name']
        if not s['dish']:
            candidates=sorted({r['title'] for r in rows if canonical(s['section']) and canonical(s['section']) in canonical(r['section'])})[:3]
            clarify(result,'你想看哪道菜的这个章节？',candidates+['我补充菜名'])
        else:
            resolved=resolve_title(s['dish'],rows)
            sections={p for r in rows if resolved and r['source']==resolved[1] for p in r['section'].split(' > ')}
            def semantic(query,candidates):
                from cookbuddy.models import embed,cosine
                from service_runtime import request_budget
                with request_budget(settings()['router']['timeout_s']):
                    vector=embed(query,'intent_nlu_match')
                    return {c:cosine(vector,embed(c,'intent_nlu_candidate')) for c in candidates}
            m=fuzzy_match(s['section'],sections,semantic=semantic if use_model else None);matches.append(m)
            if m['value']:
                s['section']=m['value'];notice=match_notice(m)
                if notice:result['assumptions'].append(notice)
            else:
                clarify(result,'你想查看哪个章节？',[r['value'] for r in m['choices']] or ['操作','原料','计算']);result['clarification_slot']='section'
    known=ingredient_vocabulary()
    for index,name in enumerate(s['ingredients']):
        ingredient=normalize_ingredient(name)
        m=fuzzy_match(ingredient,known,fillers=False);matches.append(m)
        if m['value']:
            s['ingredients'][index]=m['value']
            notice=match_notice(m)
            if notice:result['assumptions'].append(notice)
        else:clarify(result,'你说的是哪种食材？',[r['value'] for r in m['choices']])
    for n in [r['value'] for r in s['exclude_refs']]+[s[k] for k in ('selected_ref','replace_ref') if s[k] is not None]:
        if not 1<=n<=len(choices):
            clarify(result,'刚才只推荐了'+str(len(choices))+'道菜，你想选哪道？',[str(i)+'. '+r['dish_name'] for i,r in enumerate(choices[:10],1)] or ['按食材推荐','告诉你菜名'])
    if result['needs_clarification'] and result.get('clarify_question') and not result.get('options'):
        result['options']=['按最可能的意思继续','我补充说明']
    if result['needs_clarification']:
        result['answer_mode']='clarify'
        result['clarify_question']=re.split(r'[？?]',result.get('clarify_question') or '你想先了解哪一项')[0]+'？'
    result['matches']=[{k:m[k] for k in ('score','level','ambiguous') if k in m} for m in matches]
    return result


def understand(original,context=None,*,use_model=True,model_call=None):
    started=time.monotonic();context=context or {};norm=normalize(original);text=norm['text'];cfg=settings();path='model';errors=[]
    usage={'model_calls':0,'prompt_tokens':0,'completion_tokens':0};USAGE.set(usage)
    cache=None;key=None;result=None
    if cfg['fast_path']['enabled']:
        result=validate({'intent':'chitchat','confidence':1.}) if text in ('你好','您好','谢谢') or polite_closure(text) else literal_decision(text,context,model_call is None)
        if result is not None:path='fast'
    if result is None and use_model:
        from service_runtime import RuntimeStore
        digest=hashlib.sha256(json.dumps({'input':text,'context':context,'config':cfg,'prompt':understanding_prompt()},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        key='nlu-v1:'+digest
        try:cache=RuntimeStore();result=cache.get(key)
        except Exception:errors.append('cache_unavailable');cache=None
        if result is not None:
            try:result=validate(result);path='cache'
            except ValueError:result=None;errors.append('cache_invalid')
        if result is None:
            for _ in range(cfg['router']['retries']+1):
                try:
                    result=(model_call or _model)(original,text,_context(context))
                    result=validate(result)
                    if result['confidence']>=cfg['router']['confidence_threshold'] and result['intent']!='unclear':break
                    errors.append('low_confidence');result=None
                except Exception as error:
                    # Only type-level fixed codes are retained, never exception text.
                    from urllib.error import HTTPError
                    from resilience.failure import classify
                    category=classify(error)
                    if category in ('invalid_json','validation_error','internal_error') and cache is not None:
                        cache.count_failure('router_model',category)
                    limited=getattr(error,'reason',None)=='provider_retry_after' or isinstance(error,HTTPError) and error.code==429
                    code='provider_rate_limited' if limited else 'circuit_open' if type(error).__name__=='CircuitOpen' else 'timeout' if isinstance(error,TimeoutError) else 'model_unavailable'
                    errors.append(code);result=None
                    if limited or code=='circuit_open':break
            if result is not None and cache is not None:
                try:cache.put(key,result,ttl=cfg['router']['cache_ttl_s'])
                except Exception:errors.append('cache_unavailable')
    if result is None:
        try:result=fallback(text,context) if cfg['fallback']['rule_router'] else validate({'intent':'unclear','confidence':0.})
        except Exception:result=validate({'intent':'unclear','confidence':0.});errors.append('rule_unavailable')
        path='rule' if result['intent']!='unclear' else 'general_chat'
    result=copy.deepcopy(result)
    result['original']=original;result['normalized']=text
    from src.agent.turn_intents import alternative_request
    alternative=alternative_request(original)
    if alternative and alternative.get('clarification'):
        result.update(needs_clarification=True,clarify_question=alternative['clarification'])
    if alternative and alternative.get('input_notice'):
        result['input_notice']=alternative['input_notice']
    pending=context.get('slots',{}).get('nlu_pending')
    selection=pending_selection(text,context)
    if selection is not None:
        result.update(slots=selection['slots'],intent=selection['intent'],needs_clarification=False,options=[])
    elif pending and pending.get('slot') in ('dish','section') and text in pending.get('options',[]):
        result['slots'][pending['slot']]=text
        result.update(intent='recipe_section' if pending['slot']=='section' else 'recipe_lookup',needs_clarification=False)
    if pending and pending.get('slot')=='conditions' and text==pending.get('options',[''])[-1]:
        result.update(intent='constraint_refine',needs_clarification=False)
        result['slots']['constraints'].pop('unresolved',None)
        if type(pending.get('limit')) is int and 1<=pending['limit']<=10:
            result['slots']['limit']=pending['limit']
    inferred=bool(result['assumptions']) or result['confidence']<cfg['fuzzy']['t_high']
    # Model free text is never presented as checked facts, even in a notice.
    result['assumptions']=[]
    try:result=ground(result,context,use_model)
    except Exception:
        errors.append('matching_unavailable')
        result['answer_mode']='general_chat' if result['intent'] in ('identity_capability','chitchat','unclear') else 'grounded_recipe'
    if (pending and result['answer_mode']=='clarify' and result.get('clarification_slot') in ('dish','section')
            and pending.get('slot')==result['clarification_slot'] and pending.get('options')==result.get('options')
            and result.get('options') and result['confidence']>=cfg['fuzzy']['t_mid']):
        # Do not trap the user in the same low-risk name question. All factual
        # output still goes through the selected source and hard-constraint guard.
        slot=result['clarification_slot'];choice=result['options'][0]
        result['slots'][slot]=choice;result.update(needs_clarification=False,options=[],clarify_question=None)
        result=ground(result,context,False)
        result['assumptions'].append('我先按「'+choice+'」理解了，不对请告诉我。')
    ignored=list(norm['ignored_tokens'])
    # A model cannot label meaningful restrictions as disposable noise.
    fillers=cfg['fuzzy']['filler_words']
    ignored.extend(v for v in result['slots']['raw_noise'] if v and v in original and (v in fillers or re.fullmatch(r'[\d\W_]+',v)))
    result.update(original=original,normalized=text,ignored_tokens=list(dict.fromkeys(ignored)),path=path,usage=usage,
                  fallback=path if path in ('rule','general_chat') else None,errors=errors,elapsed_ms=round((time.monotonic()-started)*1000,3))
    if ignored:result['assumptions'].append('我按「'+text+'」理解了，末尾多余内容未作为新条件，不对请告诉我。')
    elif inferred and result['answer_mode']=='grounded_recipe':
        s=result['slots'];label=s['dish']+('的'+s['section'] if s['section'] else '')
        if s['exclude_refs']:label='不要第'+str(s['exclude_refs'][0]['value'])+'道'
        if label:result['assumptions'].append('我按「'+label+'」理解了，不对请告诉我。')
    return result
