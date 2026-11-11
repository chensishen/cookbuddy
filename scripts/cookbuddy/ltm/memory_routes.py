"""Framework-neutral routes adapted to the existing authenticated HTTP server."""
import uuid
from dataclasses import asdict
from .models import Candidate
from .maintenance import clear_user
from .episodic_store import EpisodicStore
from .guard import normalize

def classify_session_confirmation(state,text):
    pending=state.pending_confirmation
    if not pending:return None
    from .models import stamp
    if pending['expires_at']<=stamp() or pending['turns_left']<=0:
        state.pending_confirmation=None;return None
    q=text.strip().strip('。！! ')
    if q in {'是','确认','好','可以'}:
        old=pending['constraint']
        state.slots['temp_constraints']['hard_constraints']=[c for c in state.slots['temp_constraints'].get('hard_constraints',[])
            if (c['dimension'],c['value'])!=(old['dimension'],old['value'])]
        state.pending_confirmation=None
        return {'decision':'UPDATE','message':'已移除这条会话临时限制。'}
    if q in {'不','不用','取消','保留'}:
        state.pending_confirmation=None;return {'decision':'NOOP','message':'已保留这条会话临时限制。'}
    pending['turns_left']-=1
    return None

def handle(prefs,uid,method,path,payload=None,query=None):
    payload={} if payload is None else payload;query=query or {}
    if not isinstance(payload,dict) or any(k in payload for k in ('user_id','session_id')) or any(k in query for k in ('user_id','session_id')):
        raise ValueError('identity_is_server_injected')
    if method=='PUT' and path=='/memory/settings':
        if set(payload)!={'enabled'}:raise ValueError('invalid_settings')
        prefs.set_enabled(uid,payload['enabled']);return 200,{'enabled':payload['enabled']}
    if method=='DELETE' and path=='/memory':return 200,clear_user(prefs,uid)
    if method=='GET' and path in ('/memory','/memory/export'):
        enabled=prefs.enabled(uid);rows=prefs.prefs(uid) if enabled else []
        if path.endswith('/export'):
            return 200,{'preferences':[asdict(p) for p in rows],'events':EpisodicStore(prefs).rows(uid) if enabled else []}
        rows=[p for p in rows if p.dimension!='inventory']
        items=[]
        for p in rows:
            data=asdict(p)
            if p.sensitive:data['value']='[敏感信息已隐藏]'
            items.append(data)
        return 200,{'enabled':enabled,'preferences':items,
                    'events':[{k:r[k] for k in ('mem_id','type','dish_refs','importance','created_at')} for r in EpisodicStore(prefs).rows(uid)] if enabled else []}
    if path.startswith('/memory/') and method in ('PATCH','DELETE'):
        mid=path.removeprefix('/memory/');old=next((p for p in prefs.prefs(uid) if p.id==mid),None)
        if old is None:raise KeyError('memory_not_found')
        allowed={'value','polarity','strength','scope','version','restore_version'} if method=='PATCH' else set()
        if set(payload)-allowed:raise ValueError('invalid_memory_patch')
        desired=old
        if 'restore_version' in payload:
            allrows={p.id:p for p in prefs.prefs(uid,('active','pending','superseded','archived','deleted'),include_expired=True)}
            while desired.version!=payload['restore_version'] and desired.supersedes:
                desired=allrows[desired.supersedes]
            if desired.version!=payload['restore_version']:raise ValueError('version_not_found')
        c=Candidate(old.dimension,normalize(payload.get('value',desired.value)),payload.get('polarity',desired.polarity),
            payload.get('strength',desired.strength),scope=payload.get('scope',desired.scope),op='revoke' if method=='DELETE' else 'set',valid_to=desired.valid_to)
        result=prefs.apply(uid,'management','management:'+uuid.uuid4().hex,c,expected_version=payload.get('version',old.version),target_id=mid)
        if result['needs_confirmation']:
            if query.get('confirm',['false'])==['true']:result=prefs.confirm(uid,'management','确认',target_id=mid)
            else:return 409,{**result,'message':prefs.pending_message(uid,'management')}
        return 200,result
    raise KeyError('memory_route_not_found')
