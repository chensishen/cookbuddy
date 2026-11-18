"""Whole-utterance continuation intents shared by routing and tools."""
import re


def recipe_reference(value):
    """A prior choice is not an ingredient name."""
    return re.fullmatch(r'(?:重复)?(?:刚才|刚刚|之前|上次|上一轮|前面|上面|这|那|这些|那些|这个|那个)(?:推荐)?(?:的)?(?:那|这)?(?:一)?(?:道|个)?(?:菜|菜品|菜单)?',value) is not None


def explicit_ordinal(question):
    from scripts.cookbuddy.recommendation_schema import NUMBERS
    values={int(v) if v.isdigit() else NUMBERS[v] for v in re.findall(r'第\s*([一二两三四五六七八九十]|\d{1,2})\s*(?:道|项|个)',question)}
    return next(iter(values)) if len(values)==1 else None


def ordinal_title_question(question):
    return bool(re.fullmatch(
        r'(?:现在|刚才|这次|那么|请问)?\s*第\s*(?:[一二两三四五六七八九十]|\d{1,2})'
        r'\s*(?:道|项|个)(?:菜)?\s*(?:叫什么|叫啥|是什么|是哪一?道)(?:菜)?[。！!？? ]*',
        question.strip()))


def rejected_ordinal(text):
    match=re.fullmatch(r'(?:不要|别推荐|排除)(?:刚才|刚刚|之前|上次)?(?:的)?第([一二两三四五六七八九十]|\d{1,2})(?:道|个)(?:菜)?',text.strip())
    if not match:return None
    from scripts.cookbuddy.recommendation_schema import NUMBERS
    return int(match[1]) if match[1].isdigit() else NUMBERS.get(match[1],0)


def ingredient_followup(question):
    question=re.sub(r'[？?，,；;]\s*(?:别|不要|不用)(?:再)?沿用.{0,20}?(?:食材|材料|用料).*[。！!？? ]*$','',question.strip())
    reference=r'(?:这道菜?|那道菜?|它|第(?:[一二两三四五六七八九十]|\d{1,2})(?:道|项|个)(?:菜)?)?'
    missing=r'(?:还)?(?:缺|差)(?:少)?(?:哪些|什么)(?:食材|配料|材料)?'
    preparation=r'(?:还)?(?:需要|要)?(?:准备|买|补)?(?:哪些食材|什么食材|哪些配料|什么配料|还缺什么)'
    return re.fullmatch(r'(?:请问|那|那么)?'+reference+'(?:'+missing+'|'+preparation+r')[。！!？? ]*',question) is not None


def named_ingredient_stock(question,title):
    """Recognize a named ingredient check plus fully understood stock clauses."""
    from cookbuddy.recommendation_turn import interpret
    clauses=[s.strip() for s in re.split(r'[，,。.;；！!？?\n]+',question) if s.strip()]
    checks=[s for s in clauses if (s.startswith(title) and ingredient_followup(s[len(title):].lstrip('的')))
            or re.fullmatch(r'(?:请|帮我)?(?:核对|检查|对照)'+re.escape(title)+r'的?(?:用料|配料|原料|食材)',s)]
    if len(checks)!=1:return None
    stock=[]
    for clause in clauses:
        if clause==checks[0] or re.fullmatch(r'(?:请)?(?:把)?(?:已有|缺少|可选|必备|用料|食材|和|与|分开|分别|列出|、)+',clause):continue
        turn=interpret(clause)
        if turn.unresolved or not turn.edits or any(e.slot not in ('mode','ingredients') or e.operation!='set' for e in turn.edits):return None
        for edit in turn.edits:
            if edit.slot=='ingredients':stock.extend(edit.values)
    return list(dict.fromkeys(stock))


def original_oil_options_request(question):
    """Recognize a request for the source's oil choices, with no recipe edits."""
    return bool(re.fullmatch(
        r'(?:如果原文(?:没有|没)说明[，,]\s*)?'
        r'(?:(?:请)?(?:不要|别)(?:推测|猜测)替换比例[，,]\s*)?'
        r'(?:(?:只|仅)?(?:请)?告诉我)?(?:原文|原配方)(?:中|里)?'
        r'(?:允许|可以|能)(?:使用|选用|用)(?:什么|哪些|哪种)油[。！!？? ]*',
        question.strip()))


def recipe_fact_request(question):
    return (bool(re.search(r'多少|多久|多长|几分钟|几小时|能否|能不能|可不可以|可以|有没有|是否|包含|包括|替换|替代',question))
            and bool(re.search(r'酵头|酵母|水温|温度|发酵|烘烤|烤箱|用量|用料|原料|食材|静置|面团|火候|保存|替换|替代',question))
            and not re.search(r'换个话题|新任务|天气|软件|电影',question))


def equipment_statement(question):
    """A kitchen-equipment fact, not a recipe lookup."""
    tools=('高压锅','电饭煲','空气炸锅','微波炉','厨师机','面包机','电压力锅','电炖锅','破壁机','榨汁机','烤箱','电磁炉')
    return bool(re.search(r'(?:^|我家|家里|手边|手头|我)(?:也|还)?(?:有|买了|没有|不用|无|只有|仅)[^，。；;]{0,10}(?:'+'|'.join(tools)+r')',question)
                and not re.search(r'推荐|怎么|如何|做法|菜谱|原文|全文|图片|多少|多久|替换|替代',question))


def change_dish_request(question):
    """A request to swap the current dish for another, not an ingredient edit."""
    return bool(re.search(r'(?:换成|换个|换一道|换一个|换菜|换一批|改成|改个|改用|换用|再来一道|再来一个|再来一批|别的菜|其它菜|其他菜)(?!\s*(?:[一二两三四五六七八九十]|\d{1,2})\s*道)',question)
                and re.search(r'菜',question)
                and not re.search(r'记住|记一下|记下|忘记|忘掉|偏好设置|以后|从现在|长期|一直|我的偏好',question))


def open_recommendation(question):
    return bool(re.fullmatch(
        r'(?:请)?(?:给我|帮我|随机)?(?:随机)?推荐(?:一下)?(?:几个|几道|几种)(?:菜|菜谱|菜品)[。.!！?？ ]*',
        question.strip()))


def alternative_request(question):
    q=question.strip()
    q=re.sub(r'[，,；;]\s*(?:其他|其它|其余)(?:的菜|菜品|条件|要求)?(?:保持)?不变[。！!？? ]*$','',q)
    tail=re.fullmatch(r'([\s\S]+[。.!！?？])\s*([0-9０-９]{1,2})[。.!！?？\s]*',q)
    if tail:
        clean=tail[1].rstrip('。.!！?？ \n\t')
        parsed=_alternative_request(clean)
        if parsed:
            expected=parsed.get('count',1 if parsed['single'] else None)
            if int(tail[2])==expected:
                return {**parsed,'input_notice':'已按“'+clean+'”处理，末尾单独的“'+tail[2]+'”未作为新条件。',
                        'normalized_question':clean,'ignored_suffix':tail[2]}
            return {**parsed,'ambiguous_input':True,
                    'clarification':'末尾单独的“'+tail[2]+'”是想修改换菜数量，还是误输入？请删除多余内容或明确写成“换几道菜”。原来的选菜条件已保留。'}
    return _alternative_request(q)


def _alternative_request(question):
    more=re.fullmatch(r'(?:请)?(?:再|继续|接着)(?:帮我|给我)?推荐(几|俩|[一二两三四五六七八九十]|\d{1,2})?(?:道|个|种)?(?:菜|菜谱)?[。.!！?？ ]*',question.strip())
    if more:
        from scripts.cookbuddy.recommendation_schema import NUMBERS
        raw=more[1]
        count=None if raw in (None,'几') else int(raw) if raw.isdigit() else 2 if raw=='俩' else NUMBERS[raw]
        return {'single':False,'constraint':'','more':True,'count':count}
    replace_keep=re.fullmatch(r'第([一二两三四五六七八九十]|\d{1,2})(?:项|道|个)(?:菜)?(?:换一道|换一个|换掉|更换)(.*?)[，,；;]\s*第([一二两三四五六七八九十]|\d{1,2})(?:项|道|个)(?:菜)?(?:保留|不变|保持不变)(?:[，,；;]\s*(.+?))?[。！!？? ]*',question.strip())
    if replace_keep:
        from scripts.cookbuddy.recommendation_schema import NUMBERS
        n,keep=[int(v) if v.isdigit() else NUMBERS[v] for v in (replace_keep[1],replace_keep[3])]
        if n==keep:return {'single':True,'constraint':'','clarification':'同一道菜同时要求替换和保留，请明确要换哪一道。'}
        constraint='，'.join(part.strip('，,；; 。！!？?') for part in (replace_keep[2],replace_keep[4] or '') if part.strip('，,；; 。！!？?'))
        return {'single':True,'constraint':constraint,'index':n,'preserved_index':keep,'count':1}
    counted=re.fullmatch(r'(?:请)?(?:帮我|给我|我想要|我想|我要)?(?:再)?(?:换成|更换|换|再来)([一二两俩三四五六七八九十零]|\d{1,2})(?:道|个)(?:菜)?(?:[，,；;]\s*(.+?))?[。！!？? ]*',question.strip())
    if counted:
        from scripts.cookbuddy.recommendation_schema import NUMBERS
        count=int(counted[1]) if counted[1].isdigit() else {'俩':2,**NUMBERS}.get(counted[1],0)
        constraint=(counted[2] or '').rstrip('。！!？? ')
        index=rejected_ordinal(constraint) if count==1 else None
        return {'single':count==1,'constraint':'' if index else constraint,'count':count,
                **({'index':index} if index else {})}
    targeted=re.fullmatch(r'(?:请)?(?:帮我)?(?:更换|换掉|换)(?:刚才|刚刚|之前|上次)?第([一二两三四五六七八九十]|\d{1,2})(?:道|个)(?:菜)?(?:[，,；;]\s*(.+?))?[。！!？? ]*',question.strip())
    if not targeted:
        targeted=re.fullmatch(r'(?:请)?(?:帮我)?(?:把)?(?:刚才|刚刚|之前|上次)?第([一二两三四五六七八九十]|\d{1,2})(?:道|个|项)(?:菜)?(?:换一道|换一个|换掉|更换|换一下|换一换)(?:[，,；;]\s*(.+?))?[。！!？? ]*',question.strip())
    if targeted:
        from scripts.cookbuddy.recommendation_schema import NUMBERS
        index=int(targeted[1]) if targeted[1].isdigit() else NUMBERS.get(targeted[1],0)
        constraint=(targeted[2] or '').rstrip('。！!？? ')
        if re.fullmatch(r'(?:不要|别|不)(?:再)?重复(?:了|菜|这两道|刚才的)?','' if constraint is None else constraint):constraint=''
        return {'single':True,'constraint':constraint,'index':index}
    match=re.fullmatch(r'(?:请)?(?:再)?(换一个|换一道|再来一个|再来一道|换一批|换几道|换一套|换个菜单|换一份菜单|换一组)(?:吧)?(?:[，,；;]\s*(.+?))?[。！!？? ]*',question.strip())
    if not match:
        suffix=re.fullmatch(r'(?:请)?(?:我)?(?:这次|这顿|今天)?(?:不要|不想要|不想吃|别放)(.{1,30}?)[，, ]*(?:请)?(?:换一下|换一个|换一道|换道菜|换一批)[。！!？? ]*',question.strip())
        if not suffix:return None
        return {'single':'换一批' not in question,'constraint':'不要'+suffix[1].rstrip('，, ')}
    constraint=(match[2] or '').rstrip('。！!？? ')
    reference=re.fullmatch(r'(?:不要|别推荐|排除)(.+)',constraint)
    if reference and recipe_reference(reference[1]):constraint=''
    return {'single':match[1] in ('换一个','换一道','再来一个','再来一道'),'constraint':constraint}


def image_section(question,title):
    match=re.fullmatch(r'(?:请)?(?:只看|只查看|查看|看看|展示|显示|提供|给我|找到)?'+re.escape(title)+
        r'(?:的)?(?:完整|全部|所有)?(.*?)(?:的)?(?:图片|照片|原图|配图)[。！!？? ]*',question.strip())
    if not match:return None
    section=match[1]
    if re.search(r'原文|全文|不要|别|[，,。；;！!？?和与及、]',section):return None
    return section


def chapter_request(question,title=''):
    match=re.fullmatch(r'(?:请)?(?:只看|只查看|只要|仅看|仅查看)'+re.escape(title)+r'(?:的)?(.{1,40}?)(?:的原文|原文|的步骤|步骤)?[。！!？? ]*',question.strip())
    if not match:return referenced_chapter(question) if not title else None
    section=match[1]
    if re.search(r'图片|照片|配图|原图|不要|换|[，,。；;！!？?和与及、]',section):return None
    return section


def referenced_chapter(question):
    match=re.fullmatch(r'(?:请)?(?:只看|只查看|查看|看看)?(?:第(?:[一二两三四五六七八九十]|\d{1,2})(?:道|个|项)(?:菜)?|这道菜?|那道菜?|它)(?:的)?(.{1,40}?)(?:的原文|原文|的步骤|步骤)[。！!？? ]*',question.strip())
    if not match:return None
    section=match[1]
    if section=='的':return None
    if re.search(r'图片|照片|配图|原图|不要|换|[，,。；;！!？?和与及、]',section):return None
    return section
