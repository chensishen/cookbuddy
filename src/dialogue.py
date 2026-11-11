"""One state transition per understood turn; executors never reinterpret language.

The durable session owns recipe references and inventory. Recommendation state
owns its filters and verified result batch. Chat history is bounded presentation
context, never the source of truth for either.
"""
import copy
from .nlu.router import GENERAL_TEXT


def public_reply(plan):
    mode=plan.get('answer_mode')
    if plan.get('unmatched_dish') and mode=='grounded_recipe':
        return '知识库里没有找到相近的「'+plan['unmatched_dish']+'」。可以补充菜名，或换一个名称试试。'
    if mode=='general_chat':
        if plan['intent']=='unclear' and plan.get('errors'):
            return '理解服务暂时不可用，暂时无法可靠理解这句话。你的原话和已有菜谱、食材状态已保留，请稍后重试。'
        if plan['intent']=='identity_capability':return GENERAL_TEXT
        if plan['intent']=='chitchat':
            normalized=plan.get('normalized','')
            if any(word in normalized for word in ('谢谢','谢啦','多谢','感谢')):return '不客气，随时可以继续问我。'
            return '你好，我在。你可以告诉我想做什么菜，或者手边有哪些食材。'
        return '没太理解你的意思，可以告诉我想做什么菜，或者手边有哪些食材吗？'
    if mode=='out_of_scope':return '我主要帮助你查菜谱和安排做饭，这个问题超出了我的范围。你可以告诉我想吃什么，或手边有哪些食材。'
    if mode=='clarify':return plan.get('clarify_question') or '你想先了解哪一项？'


def reduce_turn(plan,session,previous,rows):
    """Consume one validated understanding, without running another interpreter."""
    from recipe_tools import resolve_title
    from cookbuddy.task_context import (reduce_edits,task_question,normalized_state,
                                        continuation_request,reset_independent_recommendation)
    from cookbuddy.recommendation_schema import new_state
    from cookbuddy.recommendation_turn import Edit,interpret
    s=plan['slots'];q=plan.get('original',plan['normalized']);intent=plan['intent']
    prior=normalized_state(previous)
    literal=interpret(q)
    pending=session.slots.get('nlu_pending',{})
    pending_conditions=(pending.get('slot')=='conditions'
                        and q in pending.get('options',[]))
    independent=(intent!='meal_plan' and literal.recommend and not literal.resume
                 and not continuation_request(q) and not pending_conditions)
    if s.get('reset_context'):prior=None
    model_independent=(intent=='ingredient_recommend'
            and not s.get('reset_context') and not continuation_request(q)
            and not pending_conditions)
    if prior and (independent or model_independent):
        prior=reset_independent_recommendation(prior)
        session.slots['ingredients_on_hand']=[]
    if independent:
        # A new "推荐..." request is authoritative for its own filters. Models
        # sometimes copy the prior inventory into constraint_refine; use the
        # literal turn for task-local fields instead of inheriting that residue.
        s=copy.deepcopy(s)
        s.update(ingredients=[],mode=None,limit=None,constraints={},response_fields=[])
        for edit in literal.edits:
            if edit.slot in ('ingredients','mode','limit'):
                s[edit.slot]=list(edit.values) if edit.slot=='ingredients' else edit.values[0]
            elif edit.slot=='tastes':
                s['constraints']['taste']=edit.values[0]
            elif edit.slot in ('excluded_ingredients','excluded_methods'):
                key=edit.slot if edit.operation=='add' else edit.slot.replace('excluded','allowed')
                s['constraints'].setdefault(key,[]).extend(edit.values)
            elif edit.slot=='extra_constraints':
                s['constraints'].setdefault('unresolved',[]).extend(edit.values)
            elif edit.slot=='response_fields':
                s['response_fields']=list(dict.fromkeys(s['response_fields']+list(edit.values)))
        if literal.max_difficulty is not None:
            current=s['constraints'].get('max_difficulty')
            s['constraints']['max_difficulty']=literal.max_difficulty if current is None else min(current,literal.max_difficulty)
        if literal.unresolved:s['constraints']['unresolved']=list(dict.fromkeys(s['constraints'].get('unresolved',[])+list(literal.unresolved)))
        plan['slots']=s
    task={'state':prior,'action':'none','question':q,
          'boundary':s.get('reset_context',False) or independent,'parser_version':5}
    named=resolve_title(s['dish'],rows) if s['dish'] else None
    choices=session.slots['last_recommendations'] or [
        {'dish_name':r['title'],'source_path':r['source']} for r in (prior or {}).get('choices',[])]
    selected=session.slots['selected_dish'] or (choices[0] if len(choices)==1 else None)
    if s.get('reset_context'):choices=[];selected=None

    def clarify(message,options=()):
        plan.update(answer_mode='clarify',needs_clarification=True,clarify_question=message,options=list(options)[:10])
        return {**task,'action':'clarify','clarification':message,'clarification_options':list(options)[:10]},None

    if public_reply(plan) is not None:
        if plan['answer_mode']=='clarify':return clarify(public_reply(plan),plan.get('options',[]))
        return task,named
    if s['selected_ref']:
        n=s['selected_ref']
        if not 1<=n<=len(choices):return clarify('你想看哪道菜？', [r['dish_name'] for r in choices])
        selected=choices[n-1];named=(selected['dish_name'],selected['source_path'])
    if intent in ('recipe_section','follow_up') and not named and selected:
        named=(selected['dish_name'],selected['source_path'])
    if intent=='meal_plan':
        meal=s.get('meal')
        if not meal:return clarify('这餐有几个人、多少时间？')
        old_meal={} if s.get('reset_context') else session.slots.get('meal_request',{})
        request={**meal,**{k:old_meal[k] for k in ('people','minutes') if meal.get(k) is None and k in old_meal},'question':q,
            'previous_sources':list(dict.fromkeys(([] if s.get('reset_context') else session.slots.get('recent_meal_sources',[]))+[r['source_path'] for r in choices])),
            'selection_seed':session.session_id,
            'ingredients':s['ingredients'] or old_meal.get('ingredients',[]),
            'mode':s['mode'] or old_meal.get('mode','preference'),
            'constraints':{**old_meal.get('constraints',{}),**copy.deepcopy(s['constraints'])}}
        if request.get('meal_type','unspecified')=='unspecified' and old_meal.get('meal_type'):
            request['meal_type']=old_meal['meal_type']
        from cookbuddy.meal_planning import unresolved_conditions
        unresolved=list(dict.fromkeys(old_meal.get('constraints',{}).get('unresolved',[])+s['constraints'].get('unresolved',[])))
        request['constraints']['unresolved']=unresolved_conditions(unresolved,q)
        from cookbuddy.meal_planning import accepted_offer_minutes
        offer=None if s.get('reset_context') else session.slots.get('pending_meal_offer')
        if accepted_offer_minutes(q,offer) is not None:
            request['accepted_sources']=list(offer['sources'])
        session.slots['meal_request']=copy.deepcopy(request)
        return {**task,'action':'meal_plan','meal_request':request},None
    if s['request_kind']=='ingredients' and intent not in ('ingredient_recommend','constraint_refine'):
        if not named:return clarify('你想核对哪道菜的食材？',[r['dish_name'] for r in choices])
        if s['ingredients']:
            session.slots['ingredients_on_hand']=list(s['ingredients'])
            if prior and prior['mode']=='inventory':prior['ingredients']=list(s['ingredients'])
        return {**task,'action':'ingredients','selected_source':named[1]},named
    if intent not in ('ingredient_recommend','constraint_refine'):
        if intent=='recipe_section' and not named:return clarify('你想看哪道菜的这个章节？',[r['dish_name'] for r in choices])
        if named:
            literal_named=resolve_title(q,rows)
            task['question']=q if literal_named and literal_named[1]==named[1] else q+'\n当前菜谱：'+named[0]+'\n指定菜谱原文来源：'+named[1]
            if s['selected_ref'] and not s['section'] and s['request_kind'] in ('facts','source'):
                task.update(action='followup',selected_source=named[1])
        return task,named

    state=copy.deepcopy(prior or new_state());edits=[]
    ingredients=s['ingredients']
    if ingredients or s.get('ingredient_operation')=='clear':
        if s.get('ingredient_operation')=='clear':ingredients=[]
        existing=state['ingredients'] or session.slots['ingredients_on_hand']
        if s.get('ingredient_operation')=='add':ingredients=list(dict.fromkeys(existing+ingredients))
        elif s.get('ingredient_operation')=='remove':ingredients=[v for v in existing if v not in ingredients]
        edits.append(Edit('set','ingredients',tuple(ingredients),''))
    if s['mode']:edits.append(Edit('set','mode',(s['mode'],),''))
    if s['replace_ref']:edits.append(Edit('set','limit',(1,),''))
    elif s['limit'] is not None:edits.append(Edit('set','limit',(s['limit'],),''))
    elif s.get('replace_batch') or s['exclude_refs']:edits.append(Edit('set','limit',(1,),''))
    constraints=s['constraints']
    if constraints.get('taste') and state.get('memory_tastes')!=[constraints['taste']]:
        edits.append(Edit('set','tastes',(constraints['taste'],),''))
    for key in ('excluded_ingredients','excluded_methods'):
        if constraints.get(key):edits.append(Edit('add',key,tuple(constraints[key]),''))
    for key in ('ingredients','methods'):
        if constraints.get('allowed_'+key):edits.append(Edit('remove','excluded_'+key,tuple(constraints['allowed_'+key]),''))
    if literal.quick:edits.append(Edit('add','extra_constraints',('快手',),''))
    if constraints.get('unresolved'):edits.append(Edit('add','extra_constraints',tuple(constraints['unresolved']),''))
    if s.get('response_fields'):edits.append(Edit('add','response_fields',tuple(s['response_fields']),''))
    if pending.get('slot')=='conditions' and q==pending.get('options',[''])[-1]:state['extra_constraints']=[]
    if intent=='constraint_refine' and not prior and not choices and not edits:
        return clarify('想调整哪道菜或哪一组推荐？请先告诉我菜名或食材。')
    try:state=reduce_edits(state,edits)
    except ValueError:return clarify('这次条件调整还不能可靠处理，请说明食材、口味或数量。',['食材','口味','数量'])
    excluded=[choices[r['value']-1]['dish_name'] for r in s['exclude_refs'] if 1<=r['value']<=len(choices)]
    if s.get('replace_batch') and plan.get('ignored_tokens') and any(m['role']=='user' and m['content']==q for m in session.messages):
        from cookbuddy.recommendation_turn import interpret
        residue=set(interpret(q).unresolved)
        state['extra_constraints']=[v for v in state['extra_constraints'] if v not in residue]
    if s.get('replace_batch'):excluded.extend(r['dish_name'] for r in choices)
    from .agent.turn_intents import change_dish_request
    resumed_change=(pending_conditions and q==pending.get('options',[''])[-1] and pending.get('exclude_choices'))
    if change_dish_request(q) or resumed_change:excluded.extend(r['dish_name'] for r in choices)
    state['filters']={**state.get('filters',{}),**{k:constraints[k] for k in ('category','max_difficulty') if constraints.get(k) is not None}}
    result={**task,'state':state,'action':'recommend','question':task_question(state),
        'filters':state['filters'],
        'exclude_dishes':list(dict.fromkeys(excluded)),
        'edits':[{'operation':e.operation,'slot':e.slot,'values':list(e.values),'evidence':e.evidence} for e in edits]}
    if s['replace_ref']:
        n=s['replace_ref']
        if not 1<=n<=len(choices):return clarify('要替换刚才哪一道菜？',[r['dish_name'] for r in choices])
        result['replacement_index']=n
        state['choices']=[{'title':r['dish_name'],'source':r['source_path']} for r in choices]
        excluded.extend(r['dish_name'] for r in choices)
        result['exclude_dishes']=list(dict.fromkeys(excluded))
    return result,None
