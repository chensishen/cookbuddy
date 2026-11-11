"""Explicit memory commands bypass recipe routing and never become new facts."""
import re
from .guard import normalize
from .models import Candidate,SESSION_ONLY


def memory_only_write(question):
    """An explicit save without a separate cooking request."""
    match=re.match(r'\s*(?:请(?:你)?|帮我)?(?:记住|记下|记一下)[\s，,：:]*',question)
    if not match:return False
    return not re.search(
        r'推荐|安排|搭配|挑|选菜|找菜|怎么|如何|多久|多少|哪些菜|什么菜|'
        r'能否|能不能|可以做|做(?:[一两二三四五六七八九十\d]+道|饭|菜|一餐|一顿)|'
        r'做法|原文|全文|步骤|图片|照片|配方|菜谱|换(?:成|一道|个菜)',
        question[match.end():])


def parse_command(question):
    raw=question.strip()
    text=raw.rstrip('。！!？? ').strip()
    # A direct request to stop retaining preference data is a memory-management
    # command. It must be handled before recipe understanding so the same turn
    # cannot be used to write a profile entry.
    if re.fullmatch(r'(?:我说)?(?:以后|今后|从现在起)?(?:也|再)?不要(?:再)?(?:记忆|记住|保存|记录)(?:我的)?(?:这些|这样)?(?:饮食)?(?:偏好|口味|忌口|信息)?',text):
        return ('disable','')
    if re.fullmatch(r'(?:请)?(?:帮我)?(?:重新)?(?:恢复|开启|打开|启用)(?:我的)?长期(?:饮食偏好)?记忆',text):
        return ('enable','')
    taste=(r'(?:请告诉我)?我(?:之前|以前|先前)(?:告诉过你|跟你说过)(?:什么|哪些)口味偏好',
           r'我的口味偏好(?:有哪些|是什么)')
    if any(re.fullmatch(pattern,text) for pattern in taste):return ('read','taste')
    read=(r'(?:请)?(?:查看|列出|告诉我|说说)(?:一下)?(?:你)?(?:还)?(?:记得|记住|保存|记录)?的?我的(?:哪些)?(?:饮食)?(?:偏好|画像|记忆|忌口)',
          r'你(?:还)?(?:记得|记住|保存|记录)(?:了)?我的(?:哪些|什么)?(?:饮食)?(?:偏好|画像|记忆|忌口)(?:有哪些|是什么|吗)?',
          r'你(?:现在)?(?:还)?(?:记得|记住|保存|记录)(?:了)?我(?:的)?(?:哪些|什么)(?:饮食|口味)?(?:偏好|忌口)(?:吗)?',
          r'我的(?:饮食)?(?:偏好|画像|记忆|忌口)(?:有哪些|是什么)',
          r'我(?:有|还保留了)(?:什么|哪些)(?:口味|饮食)?(?:偏好|记忆|忌口)',
          r'(?:请告诉我)?我(?:不吃|不能吃|忌口)(?:什么|哪些食材)',
          r'(?:请告诉我)?我(?:喜欢吃|爱吃)(?:什么(?:菜)?|哪些菜)',
          r'(?:请告诉我)?我(?:之前|以前|先前)(?:告诉过你|跟你说过)(?:什么|哪些)(?:口味|饮食)?偏好',
          r'我的(?:口味|饮食)偏好(?:有哪些|是什么)')
    if any(re.fullmatch(pattern,text) for pattern in read):return ('read','')
    # "取消" is also common in ordinary cooking requests ("取消后还能继续吗").
    # Treat it as a memory command only when it names an explicit memory object.
    match=re.fullmatch(r'(?:请)?(?:帮我)?(?:忘记|忘掉|删除|移除)(.{1,80})',text)
    if not match:
        match=re.fullmatch(r'(?:请)?(?:帮我)?取消(.{1,80}?)(?:饮食偏好|偏好|限制|记忆|记录|条目|提醒|忌口)',text)
    if not match:return None
    if re.search(r'[?？]',raw):return None
    target=match[1]
    if re.search(r'推荐|挑|选|做菜|菜谱|快手|继续|还能|可以吗|怎么办',target):return None
    preserve=re.fullmatch(r'(.+?)[，,；;]\s*保留(.+)',target)
    if preserve:target=preserve[1]
    # Whole-memory clear remains the separate, epoch-aware clearing operation.
    if re.fullmatch(r'(?:我(?:的)?)?(?:所有)?(?:长期)?记忆',target):return None
    target=preference_target(target)
    if preserve and target==preference_target(preserve[2]):return ('conflict','')
    return ('forget',target)


def preference_target(target):
    target=re.sub(r'(?:这条|这个|这一条|的)?(?:饮食偏好|偏好|限制|记忆|记录|条目|提醒|忌口)$','',target)
    target=re.sub(r'(?:这条|这一条)$','',target)
    target=re.sub(r'^(?:我(?:的)?)(?:一直|从来)?','',target)
    target=re.sub(r'^(?:对)','',target)
    target=re.sub(r'^(?:不太喜欢|不喜欢|不太爱吃|不爱吃|不吃|喜欢吃|喜欢|爱吃)','',target)
    return normalize(re.sub(r'(?:过敏|的)$','',target))


def describe(pref):
    dim= pref.dimension
    value=pref.value
    if dim=='flavor' and getattr(pref,'polarity','like')=='dislike':return '不喜欢'+value+'口味'
    if dim=='allergy':return '对'+value+'过敏'
    if dim=='avoid_ingredient':return ('不吃' if pref.strength=='hard' else '不太喜欢')+value
    if dim=='spice_level':return '辣度：'+{'0':'不辣','1':'微辣','2':'中辣','3':'较辣'}.get(value,value)
    if dim=='equipment':return ('设备：' if value.startswith('有') else '设备限制：')+value
    labels={'flavor':'口味','cuisine':'菜系','equipment':'设备限制','inventory':'现有食材',
            'household_size':'用餐人数','time_budget':'时间预算（分钟）','max_difficulty':'难度上限','diet_type':'饮食类型'}
    return labels.get(dim,dim)+'：'+value


def handle_command(command,prefs,uid,sid,turn,enabled=True,*,allow_enable=True):
    if command==('enable','') and allow_enable:
        if not enabled:prefs.set_enabled(uid,True)
        return {'decision':'NOOP' if enabled else 'UPDATE',
                'message':'长期饮食偏好记忆已开启。你可以重新告诉我想保存的偏好。已清除的记录不会恢复。',
                'needs_confirmation':False}
    if not enabled:
        return {'decision':'NOOP','message':'长期记忆已关闭，当前不会读取或修改已保存的偏好。','needs_confirmation':False}
    rows=[p for p in prefs.prefs(uid) if p.status=='active' and p.dimension!='inventory']
    action,target=command
    if action=='disable':
        from .maintenance import clear_user
        receipt=clear_user(prefs,uid)
        prefs.set_enabled(uid,False)
        return {'decision':'UPDATE','message':'已清除并关闭长期饮食偏好记忆；以后不会保存、读取或用于推荐。','needs_confirmation':False,
                'cleared':receipt.get('cleared',True)}
    if action=='conflict':
        return {'decision':'NOOP','message':'要忘记和保留的是同一项偏好，请明确要保留还是忘记；本次未修改记忆。','needs_confirmation':False}
    if action=='read':
        from .profile_store import effective_taste_rows
        rows=effective_taste_rows([p for p in rows if p.dimension not in SESSION_ONLY])
        if target=='taste':rows=[p for p in rows if p.dimension in ('flavor','spice_level')]
        message='目前没有已保存的饮食偏好。' if not rows else '目前记得的偏好和限制：\n'+'\n'.join('- '+describe(p) for p in rows)
        return {'decision':'NOOP','message':message,'needs_confirmation':False}
    matches=[p for p in rows if normalize(p.value)==target or normalize(describe(p))==target]
    if len(matches)!=1:
        message=('找到多条相关记录，请说明要删除哪一项：'+ '；'.join(describe(p) for p in matches)) if matches else '没有找到对应的已保存偏好，本次未修改其他记忆。'
        return {'decision':'NOOP','message':message,'needs_confirmation':False}
    item=matches[0]
    candidate=Candidate(item.dimension,item.value,item.polarity,item.strength,op='revoke')
    result=prefs.apply(uid,sid,turn,candidate,expected_version=item.version,target_id=item.id,
                       direct_food_forget=item.dimension=='avoid_ingredient' and item.strength=='hard')
    if result['decision']=='DELETE':result['message']='已忘记「'+describe(item)+'」这条偏好，其他记忆保留。'
    elif result['decision']=='ASK':
        label=describe(item)
        result['message']='「'+label+'」是硬性限制。要移除，请回复「确认忘记'+label+'这条记录」。要继续保留，请回复「保留」。'
    return result
