"""Recommendation reducer: interpretation, state transitions and evidence are separate."""
import copy
import re
from .recommendation_schema import (NUMBERS,EXCLUSION_NAMES,
                                    EXCLUSION_ALIASES,RESPONSE_FIELDS,valid_state,new_state,ingredient_names)
from .recommendation_turn import interpret,Edit

_PRICE_TERM=re.compile(r'预算|价格|费用|花费|成本')
_PRICE_WAIVER=re.compile(r'(?:不|不用|无需|不需要)(?:核实|考虑)(?:采购)?(?:价格|预算)'
                         r'|(?:采购)?(?:价格|预算)(?:先|暂时)?(?:不|不用|无需|不需要)(?:核实|考虑)')


def reset_independent_recommendation(state):
    """Drop inventory residue while keeping explicit food preferences."""
    prices=[value for value in state.get('extra_constraints',[])
        if _PRICE_TERM.search(value) and not _PRICE_WAIVER.search(value)]
    fresh=new_state()
    fresh['tastes']=list(state.get('tastes',[]))
    fresh['excluded_methods']=list(state.get('excluded_methods',[]))
    fresh['excluded_ingredients']=list(state.get('excluded_ingredients',[]))
    fresh['extra_constraints']=prices
    return fresh


def task_question(state):
    parts=['请推荐'+str(state['limit'])+'道菜']
    if state['ingredients']:parts.append(('我有' if state['mode']=='inventory' else '主食材选用')+'、'.join(state['ingredients']))
    if state['tastes']:parts.append('口味：'+'、'.join(state['tastes']))
    if state['excluded_methods']:parts.append('不要'+'、'.join(state['excluded_methods']))
    if state.get('excluded_ingredients'):parts.append('排除食材：'+'、'.join(state['excluded_ingredients']))
    parts.extend(state['extra_constraints'])
    difficulty=state.get('filters',{}).get('max_difficulty')
    if difficulty is not None:parts.append('难度不超过'+str(difficulty)+'星')
    if state.get('response_fields'):
        labels={'description':'特点','reason':'推荐理由','main_ingredients':'主要食材','missing_ingredients':'还缺什么'}
        parts.append('回答中介绍各自的'+'、'.join(labels[v] for v in state['response_fields']))
    return '。'.join(parts)+'。'


def continuation_request(question):
    """Whether a recommendation turn explicitly continues the current task."""
    return bool(re.match(r'^\s*(?:继续|再|接着|还是|这次|那么|那|改成|换成|加上|去掉|不要|保留|换|重新|按刚才|刚才|之前|上次)',question))


def reduce_edits(previous,edits):
    """Atomic, ordered edits. Invalid edits never partially mutate prior state."""
    state=copy.deepcopy(previous);state['version']=2
    state.setdefault('excluded_ingredients',[])
    state.setdefault('response_fields',[])
    for edit in edits:
        slot,operation,values=edit.slot,edit.operation,list(edit.values)
        vocabulary={'ingredients':ingredient_names(),'tastes':{'辣味','微辣','不辣'},
                    'excluded_ingredients':EXCLUSION_NAMES,'excluded_methods':{'油炸'},
                    'mode':{'inventory','preference'},'response_fields':RESPONSE_FIELDS}
        legal={'ingredients':{'set','add','remove'},'tastes':{'set'},'excluded_ingredients':{'add','remove'},
               'excluded_methods':{'add','remove'},'mode':{'set'},'limit':{'set'},'extra_constraints':{'add'},'response_fields':{'add'}}
        if slot not in legal or operation not in legal[slot] or len(values)>20:raise ValueError('invalid_edit')
        if slot in vocabulary and any(not isinstance(v,str) or v not in vocabulary[slot] for v in values):raise ValueError('invalid_edit')
        if slot=='mode' and len(values)!=1:raise ValueError('invalid_edit')
        if slot=='limit':
            if len(values)!=1 or type(values[0]) is not int or not 1<=values[0]<=10:raise ValueError('invalid_limit')
            state[slot]=values[0]
        elif slot=='mode':state[slot]=values[0]
        elif operation=='set':
            state[slot]=values
            if slot=='ingredients':
                state['excluded_ingredients']=[v for v in state['excluded_ingredients'] if v not in [EXCLUSION_ALIASES.get(n,n) for n in values]]
        elif operation=='add':
            for value in values:
                if value not in state[slot]:state[slot].append(value)
            if slot=='excluded_ingredients':
                state['ingredients']=[v for v in state['ingredients'] if EXCLUSION_ALIASES.get(v,v) not in values]
                if '辣椒' in values:state['tastes']=[v for v in state['tastes'] if v not in ('辣味','微辣')]
        elif operation=='remove':state[slot]=[v for v in state[slot] if v not in values]
        else:raise ValueError('invalid_edit')
    if not valid_state(state):raise ValueError('invalid_task_state')
    state['choices']=[]
    return state


def normalized_state(previous):
    if not valid_state(previous):return None
    state=copy.deepcopy(previous)
    # Upgrade only completely understood legacy clauses, never model expansions
    # or unknown dietary constraints. Output requests must not block discovery.
    for constraint in list(state['extra_constraints']):
        from src.agent.turn_intents import alternative_request
        command=alternative_request(constraint)
        if command and not command['constraint'] and not command.get('clarification') and not command.get('ambiguous_input'):
            state['extra_constraints'].remove(constraint);continue
        parsed=interpret(constraint)
        if (parsed.edits or parsed.profile) and not parsed.unresolved and all(e.slot in ('excluded_ingredients','excluded_methods','response_fields','ingredients','mode','limit') for e in parsed.edits):
            state=reduce_edits(state,parsed.edits)
            state['extra_constraints'].remove(constraint)
    return state


def advance_task(question,previous,named_recipe=False):
    from .meal_planning import meal_request
    meal=meal_request(question)
    if meal and not named_recipe:
        return {'state':None,'action':'meal_plan','question':question,'meal_request':meal,'boundary':True,'parser_version':3}
    q=question.strip();prior=normalized_state(previous);turn=interpret(q)
    if turn.reset:prior=None
    if prior and turn.recommend and not turn.resume and not continuation_request(q):
        prior=reset_independent_recommendation(prior)
    if turn.ordinal is not None:
        choices=prior['choices'] if prior else [];n=turn.ordinal
        if not 1<=n<=len(choices):
            return {'state':prior,'action':'clarify','question':q,'clarification':'当前没有可对应的第'+str(n)+'道菜，请告诉我菜名。','parser_version':2}
        return {'state':prior,'action':'followup','question':choices[n-1]['title']+'怎么做？','selected_source':choices[n-1]['source'],'parser_version':2}
    if turn.boundary or (named_recipe and not turn.recommend):
        return {'state':None,'action':'none','question':q,'boundary':True,'parser_version':2}
    independent=bool(re.search(r'怎么|如何|多久|为什么|保存|过敏怎么办|你是谁|你好',q))
    output_only=bool(turn.edits) and not turn.unresolved and all(e.slot=='response_fields' for e in turn.edits)
    if not turn.recommend and (not prior or (independent and not output_only) or turn.reset):
        return {'state':None,'action':'none','question':q,'boundary':True,'parser_version':2}
    if prior and turn.unresolved and not turn.recommend:
        return {'state':prior,'action':'clarify','question':q,'parser_version':2,
                'clarification':'我保留了上一轮的选菜条件，但还不能可靠理解“'+'；'.join(turn.unresolved)+'”。请说明要调整的食材、口味、做法或数量。'}
    edits=list(turn.edits)
    if turn.recommend:
        edits.extend(Edit('add','extra_constraints',(clause,),clause) for clause in turn.unresolved)
    if not edits and not turn.recommend:
        return {'state':prior,'action':'clarify','question':q,'clarification':'想继续调整哪些选菜条件？可以告诉我食材、口味或数量。','parser_version':2}
    try:state=reduce_edits(prior or new_state(),edits)
    except ValueError as error:
        text='一次可以推荐 1–10 道菜，你想看几道？' if str(error)=='invalid_limit' else '这次条件调整超出可处理范围，请简化后再试。'
        return {'state':prior,'action':'clarify','question':q,'clarification':text,'parser_version':2}
    if turn.max_difficulty is not None:
        state['filters']={**state.get('filters',{}),'max_difficulty':turn.max_difficulty}
    if turn.quick and '快手' not in state['extra_constraints']:
        state['extra_constraints'].append('快手')
    if turn.recommend and state['mode']=='inventory' and not state['response_fields']:
        state['response_fields']=['missing_ingredients']
    return {'state':state,'action':'recommend','question':task_question(state),'filters':state.get('filters',{}),'parser_version':2,
            'edits':[{'operation':e.operation,'slot':e.slot,'values':list(e.values),'evidence':e.evidence} for e in edits]}


def remember_choices(state,result,replacement_index=None):
    if not valid_state(state):return state
    state=copy.deepcopy(state)
    if replacement_index is not None and state['choices']:state['limit']=len(state['choices'])
    for output in result.get('outputs',[]):
        data=output.get('result',{})
        if data.get('status')!='verified':continue
        rows=data.get('recommendations',[])
        if rows:
            if replacement_index is not None and 1<=replacement_index<=len(state['choices']):
                state['choices'][replacement_index-1]={'title':rows[0]['title'],'source':rows[0]['source']}
                state['limit']=len(state['choices'])
            else:state['choices']=[{'title':r['title'],'source':r['source']} for r in rows[:10]]
            break
    return state


def replay_task(messages):
    """Rebuild from this chat's user utterances, never model rewrites.

    Replay topic boundaries too. Assistant records supply only verified choices;
    a failed assistant cannot erase the conditions stated by the user.
    """
    from recipe_tools import resolve_title,load_chunks
    state=None
    for message in messages:
        if message['role']=='user':
            state=advance_task(message['content'],state,bool(resolve_title(message['content'],load_chunks())))['state']
        elif message['role']=='assistant' and state:
            state=remember_choices(state,message.get('record') or {})
    return state
