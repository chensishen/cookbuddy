"""Only user utterances enter extraction. Rules precede bounded JSON extraction."""
import re
from dataclasses import replace
from datetime import timedelta
from .models import Candidate,now,stamp,DIMS,SINGLE,SESSION_ONLY,settings
from .guard import normalize

from .prompts import EXTRACT_PROMPT
from src.agent.turn_intents import recipe_reference as reference_only

def scoped(c,text):
    if c.dimension in SESSION_ONLY and c.op=='set':return replace(c,scope='session',valid_to=None)
    if re.search(r'今天|今晚|这顿|这次|现在想|这回',text):return replace(c,scope='session',valid_to=None)
    if re.search(r'^(?:请)?(?:我)?不要|不要放|不要加|别放|别加|不放|少放',text) and not re.search(r'记住|以后|一直|从来',text):
        return replace(c,scope='session',valid_to=None)
    if re.search(r'最近|这周|这个月|这段时间',text):
        return replace(c,scope='temporary',valid_to=stamp(now()+timedelta(days=7 if '这周' in text else settings()['long_term']['temporary_default_days'])))
    if not re.search(r'我一直|我从来|我不吃|我对.+过敏|我家没有|记住|以后',text):c=replace(c,confidence=min(.7,c.confidence))
    return c


def coalesce(rows):
    # One atomic intent per single-valued dimension in a user turn. The final
    # explicit qualification (e.g. “中辣可以”) refines earlier vague wording.
    result={}
    for c in rows:
        key=(c.dimension,c.scope,c.op) if c.dimension in SINGLE else (c.dimension,c.value,c.polarity,c.scope,c.op)
        result[key]=c
    return list(result.values())


def anchor_expiry(candidate,created_at):
    """Delayed jobs must not refresh the shelf life of an old stock declaration."""
    if not candidate.valid_to:return candidate
    from datetime import datetime
    lifetime=datetime.fromisoformat(candidate.valid_to)-now()
    return replace(candidate,valid_to=stamp(created_at+lifetime))

def rule_extract(text):
    if not isinstance(text,str):raise ValueError('user_message_required')
    from .management import parse_command
    if parse_command(text):return []
    from src.agent.turn_intents import alternative_request
    alternative=alternative_request(text)
    if alternative and alternative.get('ambiguous_input'):return []
    if alternative and alternative['constraint']:
        rows=rule_extract(alternative['constraint'])
        return [replace(c,scope='session',valid_to=None,evidence_text=text) for c in rows]
    result=[]
    for sentence in re.split(r'[，,。；;！!\n]+',text):
        s=sentence.strip()
        if not s or re.search(r'假如|如果|例如|比如|[?？]|是否|吗$|助手说|文档说|菜谱写|引用|[“”"]',s):continue
        from src.agent.turn_intents import rejected_ordinal
        if rejected_ordinal(s) is not None:continue
        if re.search(r'朋友|同事|客人|他说|她说|他对|她对|他不吃|她不吃',s) and not s.startswith(('我和','我与')):continue
        def add(dim,value,polarity='like',strength='soft',op='set',source='explicit'):
            if dim=='avoid_ingredient' and reference_only(normalize(value)):return
            try:result.append(scoped(Candidate(dim,normalize(value),polarity,strength,source,'global',op,.95,s),s))
            except ValueError:pass
        no_allergy=re.search(r'(?:我)?对(.{1,20}?)(?:已经)?(?:不再|不)过敏',s)
        if no_allergy:
            add('allergy',no_allergy[1],'avoid','hard','revoke');continue
        can_oven=re.search(r'现在有烤箱|已经有烤箱|买了烤箱|不是没有烤箱',s)
        if can_oven:
            add('equipment','无烤箱','must','hard','revoke');continue
        reversal=re.search(r'不是不吃(.{1,15})',s)
        if reversal:
            add('avoid_ingredient',reversal[1],'avoid','hard','revoke');continue
        release=re.fullmatch(r'(?:我)?(?:现在)?(?:可以放|可以吃|能吃)(.{1,15}?)(?:了)?',s)
        if release and release[1] not in {'辣','中辣','微辣','麻辣'}:
            add('avoid_ingredient',release[1],'avoid','hard','revoke');continue
        allergy=re.search(r'(?:我)?对(.{1,30}?)过敏',s)
        if allergy:
            for food in re.split(r'[、和及与]',allergy[1]):add('allergy',food,'avoid','hard','revoke' if re.search(r'不用提醒|不用管|忘掉|不再过敏|取消',s) else 'set')
            continue
        revoke=re.search(r'^(.{1,20}?)(?:的事|过敏)?(?:不用管了|不用提醒了|忘掉|取消限制|可以吃了)$',s)
        prefix=re.fullmatch(r'(?:请)?(?:忘掉|忘记|取消)(.{1,20}?)(?:的偏好|的限制)?',s)
        if prefix and not revoke:
            add('avoid_ingredient',prefix[1],'avoid','hard','revoke');continue
        if revoke:
            add('allergy' if '过敏' in s else 'avoid_ingredient',revoke[1],'avoid','hard','revoke');continue
        avoidance=re.search(r'(?:我(?:一直|从来|绝对)?|以后)?(?:不吃|绝对不吃|不能吃|不要带|不要放|不要加|别放|别加|不放|不要)(.{1,30}?)(?:的|了)?$',s)
        if avoidance and avoidance[1]!='辣' and not re.search(r'油炸|太咸|太油|辣的|推荐|分钟|小时',avoidance[1]):
            for food in re.split(r'[、和及与]',avoidance[1]):add('avoid_ingredient',food,'avoid','hard')
            continue
        negative_flavor=re.search(r'(?:不再喜欢|不太喜欢|不喜欢|不再偏好)(清淡|重口|酸甜|少油|少盐)(?:口味|味)?(?:的菜)?(?:了)?$',s)
        if negative_flavor:
            add('flavor',negative_flavor[1],'dislike');continue
        if re.search(r'(?:不再喜欢|不太喜欢|不喜欢)(?:吃)?辣(?:的|口味)?(?:了)?$',s):
            add('spice_level','0');continue
        soft=re.search(r'(?:不太爱吃|不太喜欢|不喜欢|不爱吃|少放)(.{1,20})',s)
        if soft:add('avoid_ingredient',soft[1],'dislike');continue
        if re.search(r'能吃辣|想吃辣|喜欢吃辣|不太能吃辣|不能吃辣|不吃辣|不要辣|中辣|微辣|重辣|特辣|麻辣',s):
            value='0' if re.search(r'不能吃辣|不吃辣|不要辣',s) else '1' if re.search(r'不太能吃辣|微辣',s) else '2' if '中辣' in s else '3'
            add('spice_level',value,source='implicit' if re.search(r'推荐|来一|想要',s) and not re.search(r'我喜欢|能吃|一直',s) else 'explicit')
        matched_flavors=[flavor for flavor in ('清淡','重口','酸甜','少油','少盐','黑胡椒','胡椒','孜然','咖喱','香草') if flavor in s]
        matched_flavors=[flavor for flavor in matched_flavors if not any(flavor!=other and flavor in other for other in matched_flavors)]
        for flavor in matched_flavors:
            if re.search(r'喜欢|想|少吃|偏好|要|口味(?:改成|换成|改为|变成)',s):add('flavor',flavor)
        if '少吃油' in s:add('flavor','少油')
        for label,value in [('粤菜','粤'),('广东菜','粤'),('川菜','川'),('四川菜','川'),('湘菜','湘'),('湖南菜','湘'),('鲁菜','鲁'),('家常菜','家常')]:
            if label in s and re.search(r'喜欢|偏好|爱吃',s):add('cuisine',value)
        for label,value in [('纯素','vegan'),('素食','vegetarian'),('清真','halal')]:
            if label in s and not re.search(r'不是|不吃素|不想吃素|不需要|不用',s):add('diet_type',value,'must','hard')
        for label in ('无烤箱','没有烤箱','仅电磁炉','只有电磁炉'):
            if label in s:add('equipment','无烤箱' if '烤箱' in label else '仅电磁炉','must','hard')
        for tool in ('高压锅','电饭煲','空气炸锅','微波炉','厨师机','面包机','电压力锅','电炖锅','破壁机','榨汁机'):
            if re.search(r'(?:没有|不用|无)'+tool,s):add('equipment','无'+tool,'must','hard')
            if re.search(r'(?:只有|仅)'+tool,s):add('equipment','仅'+tool,'must','hard')
        for tool in ('高压锅','电饭煲','空气炸锅','微波炉','厨师机','面包机','电压力锅','电炖锅','破壁机','榨汁机','烤箱','电磁炉'):
            owned=re.search(r'(?:^|我家|家里|手边|手头|我)(?:也|还)?(?:有|买了)[^，。；;]{0,20}?'+tool,s)
            if owned and not re.search(r'(?:没有|不用|无|只有|仅)\s*'+tool,s):
                add('equipment','有'+tool,'like','soft')
        people=re.search(r'(?:我家|我们家|共|有)(\d{1,2})[口人]',s)
        if people:add('household_size',people[1])
        time=re.search(r'(\d{1,3})分钟(?:内|以内)',s)
        if time:add('time_budget',time[1])
        inv=re.search(r'^(?:(?:请)?记住[：:]?\s*)?(?:今天|这次)?(?:我(?:今天|现在|家里|手头|手边)?(?:只)?有|(?:我家)?(?:冰箱里|家里|手边|手头)(?:只)?有)(.{1,30})$',s)
        if inv and not re.search(r'什么|哪些',inv[1]):
            foods=[food for food in re.split(r'[、和及与]',inv[1])
                   if food.strip() and not any(tool in food for tool in ('高压锅','电饭煲','空气炸锅','微波炉','厨师机','面包机','电压力锅','电炖锅','破壁机','榨汁机','烤箱','电磁炉'))]
            for food in foods:add('inventory',food,'must')
    return coalesce(result)[:12]

def extract(user_msg,recent_context=(),model=None):
    if isinstance(user_msg,dict):
        if user_msg.get('role')!='user':return []
        user_msg=user_msg.get('content','')
    if not isinstance(user_msg,str):return []
    from .management import parse_command
    if parse_command(user_msg):return []
    rules=rule_extract(user_msg)
    if rules or model is None:return rules
    if re.search(r'助手说|文档说|菜谱写|引用|[“”"]',user_msg):return []
    if not re.search(r'我|记住|以后|忘掉|不用管',user_msg):return []
    users=[m['content'] for m in recent_context if isinstance(m,dict) and m.get('role')=='user'][-2:]
    for _ in range(2):
        try:
            data=model(EXTRACT_PROMPT,{'current_user_message':user_msg,'recent_user_messages':users},'memory_extract_v2',max_tokens=1800)
            rows=data['candidates']
            if not isinstance(rows,list) or len(rows)>12:raise ValueError('invalid_candidates')
            output=[]
            for row in rows:
                quote=row.get('evidence','')
                if not quote or quote not in user_msg:raise ValueError('ungrounded_candidate')
                dim=row['dimension'];value=normalize(str(row['value']))
                if dim=='avoid_ingredient' and reference_only(value):continue
                if dim=='spice_level':
                    if re.search(r'不太能吃辣|微辣',quote):value='1'
                    elif re.search(r'不能吃辣|不吃辣|不要辣',quote):value='0'
                    elif '中辣' in quote:value='2'
                    elif re.search(r'重辣|特辣|麻辣|喜欢吃辣',quote):value='3'
                if dim in {'allergy','avoid_ingredient','inventory'} and value not in normalize(quote):raise ValueError('ungrounded_value')
                c=Candidate(dim,value,row['polarity'],row['strength'],row['source'],row['scope'],row['op'],min(.7,row['confidence']),quote)
                if c.scope=='temporary':c=replace(c,valid_to=stamp(now()+timedelta(days=min(365,max(1,int(row.get('valid_days') or 30))))))
                output.append(scoped(c,quote))
            return coalesce(output)
        except (KeyError,TypeError,ValueError):continue
    raise ValueError('memory_extract_rejected')
