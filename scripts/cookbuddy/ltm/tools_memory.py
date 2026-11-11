"""Tool identities are injected by orchestration, never accepted from the LLM."""
from contextvars import ContextVar
from contextlib import contextmanager
import uuid
from .models import Candidate
from .guard import normalize
from .episodic_store import EpisodicStore

CONTEXT=ContextVar('memory_tool_identity',default=None)

@contextmanager
def memory_tools(prefs,uid,sid):
    token=CONTEXT.set((prefs,uid,sid))
    try:yield
    finally:CONTEXT.reset(token)

TOOLS=[{'type':'function','function':{'name':name,'description':description,'parameters':{
    'type':'object','properties':props,'required':required,'additionalProperties':False}}}
    for name,description,props,required in (
    ('get_user_profile','读取当前用户画像资料。',{},[]),
    ('remember_preference','用户明确要求记住偏好时使用；可能需要确认。',
     {k:{'type':'string'} for k in ('dimension','value','polarity','strength','scope')},['dimension','value','polarity']),
    ('forget_preference','撤销当前用户某条偏好；硬约束必须确认。',
     {k:{'type':'string'} for k in ('dimension','value')},['dimension','value']),
    ('recall_history','召回当前用户有关做菜的历史反馈。',{'query':{'type':'string'},'top_k':{'type':'integer'}},['query']))]

def call(name,arguments):
    context=CONTEXT.get()
    if context is None:raise ValueError('memory_identity_required')
    prefs,uid,sid=context
    if not isinstance(arguments,dict):raise ValueError('invalid_memory_arguments')
    # Even a malformed provider tool call cannot select another actor.
    args={k:v for k,v in arguments.items() if k not in {'user_id','session_id'}}
    schema=next((t['function']['parameters'] for t in TOOLS if t['function']['name']==name),None)
    if not schema or set(args)-set(schema['properties']) or any(k not in args for k in schema['required']):raise ValueError('invalid_memory_arguments')
    if name=='get_user_profile':return {'decision':'NOOP','profile':prefs.load_profile(uid),'needs_confirmation':False,'message':'画像资料'}
    if name=='recall_history':
        if not isinstance(args['query'],str) or type(args.get('top_k',3)) is not int:raise ValueError('invalid_history_query')
        return {'decision':'NOOP','events':EpisodicStore(prefs).recall(uid,args['query'],args.get('top_k',3)),'needs_confirmation':False,'message':'历史资料'}
    dimension=args['dimension'];value=normalize(args['value'])
    if name=='forget_preference':
        existing=next((p for p in prefs.prefs(uid) if p.dimension==dimension and p.value==value),None)
        c=Candidate(dimension,value,existing.polarity if existing else 'avoid',existing.strength if existing else 'soft',op='revoke')
    else:c=Candidate(dimension,value,args['polarity'],args.get('strength','soft'),scope=args.get('scope','global'))
    result=prefs.apply(uid,sid,sid+':tool:'+uuid.uuid4().hex,c)
    if c.strength=='hard' and c.op=='set' and result['decision'] in {'ADD','UPDATE'}:
        from .guard import HARD,current
        HARD.set(tuple(current()+prefs.load_profile(uid)['hard_constraints']))
    return result
