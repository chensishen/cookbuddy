"""Preference weights affect ordering, never hard-constraint eligibility."""
import re
from .models import settings

FLAVOR_FEATURES={
    '清淡':r'清淡|清爽',
    '重口':r'重口(?:味)?|口味浓郁|浓郁|浓香|酱香|麻辣|香辣|鲜辣',
    '酸甜':r'酸甜|糖醋',
    '少油':r'少油|低油',
    '少盐':r'少盐|低盐',
}

def flavor_feature(text,value):
    for match in re.finditer(FLAVOR_FEATURES.get(value,re.escape(value)),text):
        if not re.search(r'不|无|非|避免|没有|并非',text[max(0,match.start()-4):match.start()]):return match[0]
    return None


def personalize(rows,profile,temp=None,recent=(),k=3,strict_flavor=False,inventory=()):
    if not rows:return []
    cfg=settings()['retrieval'];weights=cfg['personalization'];temp=temp or {}
    prefs=[p for p in profile['soft_prefs'] if p['dimension'] not in temp]
    prefs.extend({'dimension':d,'value':v} for d,v in temp.items() if d in {'flavor','cuisine','max_difficulty','spice_level'})
    scored=[]
    for row in rows:
        row=dict(row);reasons=[]
        score=row.get('rerank_score',row.get('score',0));text=row.get('description','')
        matched_flavor=False
        for pref in prefs:
            dim,value=pref['dimension'],str(pref['value'])
            feature=flavor_feature(text,value) if dim=='flavor' else None
            if feature and pref.get('polarity','like')=='like':
                matched_flavor=True
                score+=weights['flavor'];reasons.append('原文简介写明“'+feature+'”，与你的'+value+'口味偏好相符')
            elif feature and pref.get('polarity')=='dislike':score-=weights['flavor']
            if dim=='cuisine' and value in text:score+=weights['cuisine']
            if dim=='max_difficulty' and row.get('difficulty') is not None:
                score+=weights['difficulty']*(1 if row['difficulty']<=int(value) else -.5)
                if row['difficulty']<=int(value):reasons.append('原文预估难度'+str(row['difficulty'])+'星，在你偏好的'+value+'星以内')
            if dim=='spice_level':
                spicy=row.get('spice_match') not in (None,'none','optional')
                if spicy==(int(value)>0):score+=weights['flavor']
        if reasons:row['reason']='；'.join(reasons)+'。'+row.get('reason','')
        elif prefs:row['reason']='尚未核实到与现有偏好的直接匹配，作为备选供你比较。'+row.get('reason','')
        if row.get('title') in recent:score-=weights['repeat_penalty']
        scored.append((score,row,matched_flavor))
    flavor_prefs=any(p['dimension']=='flavor' and p.get('polarity','like')=='like' for p in prefs)
    if strict_flavor and flavor_prefs:
        scored=[item for item in scored if item[2]]
    ranked=[r for _,r,_ in sorted(scored,key=lambda item:item[0],reverse=True)]
    from ingredient_recommendation import select_inventory_recipes
    selected=select_inventory_recipes(ranked,inventory,k) if inventory else ranked[:k]
    if k and scored and cfg['explore_slots'] and not inventory and not (strict_flavor and flavor_prefs):
        explore=max((r for _,r,_ in scored),key=lambda r:r.get('rerank_score',r.get('score',0)))
        if explore not in selected:selected=selected[:max(0,k-1)]+[explore]
    return selected


def personalize_result(data,state,profile,session):
    if not data or not data.get('recommendations'):return data
    import re
    recent=[r['dish_name'] for r in session.slots['last_recommendations']]
    temp=session.slots['temp_constraints']
    constraints={**temp,**temp.get('profile_overrides',{})}
    rows=personalize(data['recommendations'],profile,constraints,recent,state['limit'],
                     strict_flavor=bool(state.get('tastes')) and state.get('memory_tastes')!=state.get('tastes'),
                     inventory=state['ingredients'] if state['mode']=='inventory' else ())
    if not rows:
        return {'status':'insufficient','short_answer':'暂未找到原文简介能明确匹配当前口味、并同时满足本次限制的菜谱。可以换一类食材或具体说明想要的风味。','sources':[],'evidence':[],'recommendations':[],'images':[]}
    sources={r['source'] for r in rows}
    summary=re.sub(r'^给你推荐这\d+道菜','给你推荐这'+str(len(rows))+'道菜',data['short_answer'])
    if state['mode']=='inventory':
        from ingredient_recommendation import inventory_coverage_note
        old_note=inventory_coverage_note(state['ingredients'],data['recommendations'])
        if old_note:summary=summary.replace(old_note,'',1)
        summary+=inventory_coverage_note(state['ingredients'],rows)
    return {**data,'recommendations':rows,'short_answer':summary,
            'sources':[r for r in data['sources'] if r['source'] in sources],
            'evidence':[r for r in data['evidence'] if r['source'] in sources]}
