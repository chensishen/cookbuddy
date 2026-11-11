"""Bounded corpus matching. Ambiguity returns choices, never an arbitrary source."""
import re
from rapidfuzz.fuzz import ratio
from pypinyin import lazy_pinyin
from .config import settings
from .normalize import normalize


def canonical(text,fillers=True):
    text=normalize(text)['text'].lower()
    if fillers:
        for word in sorted(settings()['fuzzy']['filler_words'],key=len,reverse=True):text=text.replace(word,'')
    return re.sub(r'[\W_\s]+','',text)


def phonetic(text):return ''.join(lazy_pinyin(text)).replace(' ','')


def fuzzy_match(query,candidates,*,semantic=None,disambiguate=None,fillers=True,min_score=None):
    candidates=sorted(set(candidates));cfg=settings()['fuzzy'];q=canonical(query,fillers)
    floor=cfg['t_mid'] if min_score is None else min_score
    if not q or not candidates:return {'value':None,'score':0.,'level':'none','choices':[],'ambiguous':False}
    ranked=[]
    for candidate in candidates:
        leaf=candidate.split(' > ')[-1];c=canonical(leaf,fillers)
        if query in (candidate,leaf):score,level=1.,'L0'
        elif q in (c,canonical(candidate,fillers)):score,level=.99,'L1'
        elif len(q)>=2 and (q in c or c in q):score,level=.89,'L2'
        else:
            score,level=ratio(q,c)/100,'L3'
            p=ratio(phonetic(q),phonetic(c))/100*.94
            if p>score:score,level=p,'L4'
        ranked.append({'value':candidate,'score':round(score,4),'level':level})
    ranked.sort(key=lambda r:(-r['score'],len(r['value']),r['value']))
    # Vector/model escalation is supplied by the understanding service and uses
    # the existing embedding provider, never a new vector store.
    if ranked[0]['score']<cfg['t_high'] and semantic:
        scores=semantic(query,[r['value'] for r in ranked[:3]])
        for r in ranked[:3]:
            if scores.get(r['value'],0)>r['score']:r.update(score=scores[r['value']],level='L5')
        ranked.sort(key=lambda r:-r['score'])
    top=ranked[0];second=ranked[1]['score'] if len(ranked)>1 else 0
    # Exact/canonical matches are definitive. Multiple hierarchy paths with the
    # same leaf remain separate source choices at the caller.
    ambiguous=top['level'] not in ('L0','L1') and top['score']>=floor and top['score']-second<cfg['margin']
    if ambiguous and disambiguate:
        chosen=disambiguate(query,ranked[:3])
        if chosen in [r['value'] for r in ranked[:3]]:
            top=next(r for r in ranked if r['value']==chosen);top={**top,'level':'L6'};ambiguous=False
    return {**top,'value':top['value'] if top['score']>=floor and not ambiguous else None,
            'ambiguous':ambiguous,'choices':ranked[:3],
            'assumption':top['level'] in ('L3','L4','L5','L6') or top['score']<cfg['t_high']}


def match_notice(result):
    return '我按「'+result['value']+'」理解了，不对请告诉我。' if result.get('value') and result.get('assumption') else ''
