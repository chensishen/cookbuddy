"""Tenant-scoped short-term context. Recipe evidence never becomes chat history."""
import copy
import json
import re
from dataclasses import dataclass, field, asdict

from check_embedding import ROOT
from .ltm.prompts import SUMMARY_PROMPT,REWRITE_PROMPT,CONTEXT_SYSTEM


def slots_default():
    return {'ingredients_on_hand': [], 'temp_constraints': {},
            'last_recommendations': [], 'recent_meal_sources': [], 'selected_dish': None, 'servings': None}


@dataclass
class SessionState:
    user_id: str
    session_id: str
    messages: list = field(default_factory=list)
    summary: str = ''
    slots: dict = field(default_factory=slots_default)
    pending_confirmation: dict | None = None

    def __post_init__(self):
        if not self.user_id or not self.session_id:
            raise ValueError('session_identity_required')

    def record(self):
        return asdict(self)


def options():
    # JSON is a YAML subset; no additional runtime dependency is needed.
    from .ltm.models import settings as memory_settings
    return memory_settings()['short_term']


def tokens(value):
    """UTF-8 bytes upper-bound byte-based model tokens, including JSON overhead."""
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,separators=(',', ':'))
    return len(text.encode('utf-8'))


def bounded(text, budget):
    return text.encode('utf-8')[:budget].decode('utf-8',errors='ignore')


def prompt_slots(slots,budget):
    """Keep valid JSON and whole facts; full slots are used by local resolution."""
    result={}
    for key in ('selected_dish','temp_constraints','ingredients_on_hand','servings','last_recommendations'):
        candidate={**result,key:slots[key]}
        if tokens(candidate)<=budget:result=candidate
    return result


def compact_assistant(result):
    rows=[]
    for output in result.get('outputs',[]):
        data=output.get('result',{})
        if data.get('status') not in ('verified','verified_source'):continue
        for dish in data.get('recommendations',[]):
            rows.append({'dish_name':dish['title'],'source_path':dish['source']})
        if not data.get('recommendations'):
            sources=list(dict.fromkeys(e['source'] for e in data.get('evidence',[]) if e.get('source')))
            rows.extend({'source_path':s} for s in sources[:3])
    # Do not copy answers, raw tool content, quotes, or retrieved documents.
    return json.dumps({'recipes':rows[:10], 'outcome':result.get('status','unknown')},ensure_ascii=False)


def update_slots(state, task, result=None):
    if task.get('boundary'):
        state.slots['last_recommendations']=[]
        state.slots['selected_dish']=None
        state.slots.pop('selected_section',None)
        state.slots.pop('pending_meal_offer',None)
        state.slots['temp_constraints']={k:v for k,v in state.slots['temp_constraints'].items()
                                         if k in ('profile_overrides','hard_constraints')}
    prior=task.get('state')
    if prior:
        if prior.get('mode')=='inventory':state.slots['ingredients_on_hand']=list(prior['ingredients'])
        state.slots['temp_constraints'].update({
            'tastes':list(prior['tastes']),
            'excluded_ingredients':list(prior.get('excluded_ingredients',[])),
            'excluded_methods':list(prior['excluded_methods'])})
        if not state.slots['last_recommendations'] and prior.get('choices'):
            state.slots['last_recommendations']=[{'idx':n,'dish_name':r['title'],'source_path':r['source']}
                for n,r in enumerate(prior['choices'],1)]
    if task.get('selected_source'):
        selected=next((r for r in state.slots['last_recommendations'] if r['source_path']==task['selected_source']),None)
        if selected:state.slots['selected_dish']=copy.deepcopy(selected)
    if result:
        for output in result.get('outputs',[]):
            data=output.get('result',{})
            if data.get('status')!='verified' or not data.get('recommendations'):continue
            if data.get('meal_plan') and state.slots.get('meal_request'):
                state.slots['meal_request']['meal_type']=data['meal_plan']['meal_type']
            if data.get('meal_plan'):
                alternative=data['meal_plan'].get('alternative')
                if alternative:
                    state.slots['pending_meal_offer']={'minutes':alternative['estimated_minutes'],
                        'sources':[r['source'] for r in alternative['recipes']]}
                else:state.slots.pop('pending_meal_offer',None)
                recent=state.slots.get('recent_meal_sources',[])
                sources=[r['source'] for r in data['recommendations']]
                state.slots['recent_meal_sources']=([s for s in recent if s not in sources]+sources)[-24:]
            state.slots['last_recommendations']=[{'idx':n,'dish_name':r['title'],'source_path':r['source']}
                for n,r in enumerate(data['recommendations'][:10],1)]
            state.slots['selected_dish']=None
            state.slots.pop('selected_section',None)
            break
        if task.get('replacement_index') and prior and prior.get('choices'):
            state.slots['last_recommendations']=[{'idx':n,'dish_name':r['title'],'source_path':r['source']}
                for n,r in enumerate(prior['choices'],1)]


def resolve_reference(state, question):
    """Return a source-bound recipe, or None. Ambiguous pronouns never guess."""
    q=question.strip().rstrip('？?。！!')
    match=re.fullmatch(r'(?:那|那么|请问)?第([一二两三四五六七八九十]|\d{1,2})(?:个|道)(?:菜)?(?:怎么做|怎么制作|的做法)',q)
    rows=state.slots['last_recommendations']
    if match:
        from .recommendation_schema import NUMBERS
        n=NUMBERS.get(match[1],int(match[1]) if match[1].isdigit() else 0)
        return copy.deepcopy(next((r for r in rows if r['idx']==n),None))
    if re.fullmatch(r'(?:它|那个|这道菜|上一道|上一个)(?:怎么做|怎么制作|的做法)',q):
        return copy.deepcopy(state.slots['selected_dish'] or (rows[0] if len(rows)==1 else None))
    return None


def needs_rewrite(question):
    return bool(re.search(r'它|那个|这个|这道|那道|上一个|上一道|第[一二两三四五六七八九十\d]+[个道]|^(?:换|再|还有)|上次|之前|刚才|我的|我家|家里|适合我|按我',question)
                or re.fullmatch(r'(?:要|需要|得|能|可以)?(?:多少|多久|怎么做|怎么制作|可以吗|行吗|还缺什么)[？?。 ]*',question))


def rewrite_query(state, question, model=None, memory_items=None):
    selected=resolve_reference(state,question)
    if selected:
        state.slots['selected_dish']=selected
        return selected['dish_name']+'怎么做？'
    if not needs_rewrite(question) or model is None:return question
    data=model(REWRITE_PROMPT,
               {'summary':state.summary,'slots':state.slots,'recent_messages':state.messages[-4:],
                'current_question':question,'memory_context':memory_items or []},'memory_prompt_injection')
    value=data.get('question')
    if not isinstance(value,str) or not value.strip() or len(value)>2000:raise ValueError('context_question_invalid')
    return value.strip()


def append_message(state, role, content, summarizer=None):
    if role not in ('user','assistant') or not isinstance(content,str):raise ValueError('invalid_session_message')
    state.messages.append({'role':role,'content':content})
    cfg=options();removed=[]
    while len(state.messages)>cfg['window_turns']*2 or tokens(state.messages)>cfg['window_max_tokens']:
        removed.append(state.messages.pop(0))
    if removed:
        from .ltm.models import settings as memory_settings
        config=memory_settings()
        if config['experiments']['level']=='M1':
            state.summary='';return
        data={'old_summary':state.summary,'turns':removed,'slots':state.slots}
        summary=None
        if summarizer:
            try:
                response=summarizer(SUMMARY_PROMPT,data,'session_summary',max_tokens=400)
                if isinstance(response.get('summary'),str):summary=response['summary']
            except Exception:
                pass  # Summary service failure must not fail a user's answer.
        # A visible bounded fallback retains recent user facts; authoritative slots
        # are preserved separately even if the summary budget cannot fit all history.
        if not summary:
            facts={'食材':state.slots['ingredients_on_hand'],
                   '约束':state.slots['temp_constraints'],
                   '已推荐':[r['dish_name'] for r in state.slots['last_recommendations']],
                   '已选':(state.slots['selected_dish'] or {}).get('dish_name'),
                   '待确认':state.pending_confirmation}
            summary=json.dumps(facts,ensure_ascii=False)+'\n'+state.summary+'\n'+'\n'.join(m['content'] for m in removed if m['role']=='user')
        state.summary=bounded(summary,cfg['summary_max_tokens'])


def build_context(state, user_msg, memory_items=(), hard_constraints=(),episodic=()):
    """Ordered data blocks; hard constraints are never budget-trimmed."""
    cfg=options()
    profile=[]
    for item in memory_items:
        if tokens(profile+[item])<=300:profile.append(item)
    blocks=[{'role':'system','content':CONTEXT_SYSTEM},
            {'role':'user','content':json.dumps({'must_observe':list(hard_constraints)},ensure_ascii=False)},
            {'role':'user','content':'用户画像（资料）：'+json.dumps(profile,ensure_ascii=False,separators=(',', ':'))},
            {'role':'user','content':'会话摘要（资料）：'+bounded(state.summary,cfg['summary_max_tokens'])}]
    recent=state.messages[:-1] if state.messages and state.messages[-1]=={'role':'user','content':user_msg} else state.messages
    blocks.extend(copy.deepcopy(recent))
    # Full slots persist in PostgreSQL; selected/list identities remain available
    # to deterministic resolution even when their prompt projection is smaller.
    blocks.append({'role':'user','content':'会话槽位（资料）：'+json.dumps(prompt_slots(state.slots,cfg['slots_max_tokens']),ensure_ascii=False,separators=(',', ':'))})
    events=[]
    for event in list(episodic)[:3]:
        item={k:event[k] for k in ('mem_id','text','type')}
        if tokens(events+[item])<=300:events.append(item)
    if events:blocks.append({'role':'user','content':'相关历史（仅供参考）：'+json.dumps(events,ensure_ascii=False)})
    blocks.append({'role':'user','content':user_msg})
    return blocks
