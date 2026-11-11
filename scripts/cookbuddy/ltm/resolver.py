"""Pure decisions; persistence and evidence accumulation are separate."""
from dataclasses import replace
from .models import Decision,item,SINGLE,HARD,settings


def opposite(a,b):return (a in {'like','must'})!=(b in {'like','must'})
def conflicting_values(dimension,a,b):
    return a!=b and (dimension in SINGLE or dimension=='flavor' and {a,b}=={'清淡','重口'})
def find_conflict(c,items):
    return next((p for p in items if p.status=='active' and
        ((conflicting_values(c.dimension,c.value,p.value) and (c.dimension in SINGLE or p.polarity==c.polarity=='like'))
         or (p.value==c.value and opposite(p.polarity,c.polarity)))),None)

def ask(action,p,c=None):
    return Decision('ASK',p.id,ask={'action':action,'message':'需要确认后才能修改这项限制','payload':{'target_id':p.id,'version':p.version}},reason=action)

def resolve(c,items):
    cfg=settings()['long_term']
    rows=[p for p in items if p.status in {'active','pending'} and p.dimension==c.dimension]
    same=next((p for p in rows if p.value==c.value and p.polarity==c.polarity),None)
    target=next((p for p in rows if p.value==c.value and p.status=='active'),None)
    conflict=find_conflict(c,rows)
    if c.scope=='session':return Decision('SESSION_ONLY',reason='session_scope')
    if c.op=='revoke':
        if c.source=='implicit':return Decision('NOOP',reason='implicit_revoke_not_authorized')
        target=target or (next((p for p in rows if p.status=='active'),None) if c.dimension in SINGLE else None)
        if not target:return Decision('NOOP',reason='nothing_to_revoke')
        return ask('relax_hard',target) if target.strength=='hard' else Decision('DELETE',target.id,reason='explicit_revoke')
    # A downgrade, polarity reversal or replacement of a hard single-valued item
    # is a relaxation even if the new candidate happens to be hard too.
    protected=conflict or target
    if c.source!='implicit' and protected and protected.strength=='hard' and (conflict or c.strength!='hard'):
        return ask('relax_hard',protected,c)
    if c.source=='implicit':
        if same:
            if same.status=='pending' and same.evidence_count>=cfg['implicit_promote_evidence'] and same.session_count>=cfg['implicit_promote_sessions']:
                if conflict and conflict.source in {'explicit','onboarding'}:return ask('adopt_implicit',conflict,c)
                return Decision('UPDATE',same.id,replace(item(c,previous=same),confidence=min(cfg['promote_conf_cap'],c.confidence)),reason='promote')
            return Decision('NOOP',same.id,reason='bump_evidence')
        return Decision('ADD',new_item=replace(item(c,status='pending'),confidence=min(cfg['implicit_conf_cap'],c.confidence)),reason='implicit_pending')
    if same:
        if same.status=='pending' or (same.strength=='soft' and c.strength=='hard'):
            return Decision('UPDATE',same.id,item(c,previous=same),reason='explicit_upgrade')
        return Decision('NOOP',same.id,reason='bump_evidence')
    if conflict:return Decision('UPDATE',conflict.id,item(c,previous=conflict),reason='explicit_replacement')
    return Decision('ADD',new_item=item(c),reason='explicit_add')
