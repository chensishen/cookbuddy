"""Interpret complete clauses into typed edits; never mutate conversation state.

Every clause must be consumed or reported as unresolved. Subjects and discourse
prefixes are grammar, not residual constraints. Negation is parsed before taste.
"""
from dataclasses import dataclass,field
import re
from .recommendation_schema import NUMBERS,EXCLUSION_NAMES,EXCLUSION_ALIASES,STYLES,ingredient_names


@dataclass(frozen=True)
class Edit:
    operation:str
    slot:str
    values:tuple
    evidence:str


@dataclass
class Turn:
    recommend:bool=False
    profile:bool=False
    resume:bool=False
    reset:bool=False
    max_difficulty:int|None=None
    quick:bool=False
    ordinal:int|None=None
    edits:list[Edit]=field(default_factory=list)
    unresolved:list[str]=field(default_factory=list)
    boundary:bool=False


def number(text):
    return int(text) if text.isdigit() else NUMBERS.get(text,0)


def names(text,vocabulary,*,correct_stock=False):
    values=re.split(r'\s*(?:、|以及|和|及|与)\s*',text)
    if correct_stock:
        from src.nlu.matching import fuzzy_match
        for i,value in enumerate(values):
            if value in vocabulary:continue
            if not re.fullmatch(r'[\u4e00-\u9fff]{2,12}',value) or re.search(r'不|没|无|过敏|忌口',value):return None
            match=fuzzy_match(value,vocabulary,fillers=False)
            # Only a unique high-confidence stock name may be corrected.
            # Exclusions and unknown multi-clause requirements remain literal.
            if not match['value'] or match['score']<.88 or match['level'] not in ('L3','L4'):return None
            values[i]=match['value']
    return values if values and all(v in vocabulary for v in values) else None


def canonical(values):
    return tuple(dict.fromkeys(EXCLUSION_ALIASES.get(v,v) for v in values))


# Output requests have a separate grammar and slot. Match a complete suffix so
# "介绍特点和热量" cannot silently discard an unsupported nutrition request.
OUTPUT_TERMS={'特点':'description','特色':'description','介绍':'description','简介':'description',
              '推荐原因':'reason','推荐理由':'reason','为什么推荐':'reason','理由':'reason','原因':'reason',
              '主要食材':'main_ingredients','主要用料':'main_ingredients',
              '还缺什么':'missing_ingredients','缺少什么':'missing_ingredients','缺少哪些食材':'missing_ingredients',
              '缺啥':'missing_ingredients','缺料':'missing_ingredients'}
OUTPUT_TERM=r'(?:各自的?|每道菜的?|这几道菜的?|它们的?)?(?:'+'|'.join(sorted(OUTPUT_TERMS,key=len,reverse=True))+')'
OUTPUT_SUFFIX=re.compile(r'(?:并且|并|也|再)?(?:请)?(?:分别)?(?:介绍|说明|解释|列出|给出|说说|说)'
                         r'(?:一下)?(?:'+OUTPUT_TERM+r')(?:\s*(?:和|及|与|、|以及)\s*'+OUTPUT_TERM+r')*(?:即可)?$')
DESCRIPTION_SUFFIX=re.compile(r'(?:并且|并|也|再)?(?:请)?(?:简单|简要)?介绍(?:一下)?(?:每道菜|各道菜|这些菜|这几道菜|菜品)(?:即可)?$')
SIMPLE_PREFIX=re.compile(r'^(?:(?:非常|特别|比较|很)?(?:简单|容易|易做|好做)(?:做|制作)?|(?:适合)?新手(?:也)?(?:能|可以)?(?:做|制作)?)(?:的)?')
QUICK_PREFIX=re.compile(r'^(?:快手|快速|省时)(?:的)?')


def split_output_request(clause):
    match=OUTPUT_SUFFIX.search(clause)
    if not match:
        match=DESCRIPTION_SUFFIX.search(clause)
        return (clause[:match.start()].strip(),('description',)) if match else (clause,())
    text=match[0]
    # The verb "介绍" itself does not request a second field when its object is
    # solely "推荐理由"; extract fields from the grammatical object only.
    obj=re.sub(r'^(?:并且|并|也|再)?(?:请)?(?:分别)?(?:介绍|说明|解释|列出|给出|说说|说)(?:一下)?','',text)
    fields=tuple(dict.fromkeys(OUTPUT_TERMS[m[0]] for m in re.finditer('|'.join(sorted(OUTPUT_TERMS,key=len,reverse=True)),obj)))
    return clause[:match.start()].strip(),fields


def interpret(question):
    turn=Turn();q=question.strip()
    if re.search(r'换个话题|新任务|重新开始|不聊这个',q):
        turn.reset=True
        q=re.sub(r'换个话题|新任务|重新开始|不聊这个','',q).strip('，,。 ')
    ordinal=re.fullmatch(r'(?:那|那么|请问|请|我要|要|给我|看看|查看)?第([一二两三四五六七八九十]|\d{1,2})(?:道|个)(?:菜)?(?:怎么做|怎么制作|的做法|(?:的)?(?:完整)?步骤|呢)[？?。！!]*(?:\s*(?:请)?(?:给我|提供|列出)(?:用料|食材)(?:和|及)(?:完整)?(?:步骤|做法)[？?。！!]*)?',q)
    if ordinal:
        turn.ordinal=number(ordinal[1]);return turn
    if re.search(r'软件|网站|餐厅|电影|教程|应用|图片|照片|原文',q):
        turn.boundary=True;return turn
    clauses=re.split(r'[，,。；;！!？?\n]+|但是|不过|而且|并且|另外|同时|然后',q)
    for raw in clauses:
        raw=raw.strip()
        if not raw:continue
        if re.fullmatch(r'(?:还)?缺啥(?:也)?(?:说|说下|说一下|说明)(?:下)?',raw):
            turn.edits.append(Edit('add','response_fields',('missing_ingredients',),raw));continue
        clause,fields=split_output_request(raw)
        if fields:turn.edits.append(Edit('add','response_fields',fields,raw))
        # Count and recommendation markers may occur before or after conditions.
        rec=re.search(r'(?:(?:请|麻烦)?(?:给我|帮我|为我)?)?(?P<resume>再|继续|重新)?(?:推荐(?!原因|理由)|整(?=几|俩|[一二两三四五六七八九十\d]))(?:一下)?(?P<count>几|俩|[一二两三四五六七八九十\d]+)?(?:道|个)?(?:菜谱|菜)?',clause)
        if rec:
            turn.recommend=True;turn.resume=turn.resume or bool(rec['resume'])
            if rec['count'] and rec['count']!='几':turn.edits.append(Edit('set','limit',(2 if rec['count']=='俩' else number(rec['count']),),raw))
            clause=clause[:rec.start()]+clause[rec.end():]
        if re.fullmatch(r'(?:那|那么)?(?:我)?(?:能|可以|该|应该)?(?:吃|做|整)(?:点|些|什么|哪些|啥)(?:菜|菜谱)?(?:呢)?',clause):
            turn.recommend=True;clause=''
        if re.fullmatch(r'(?:再)?(?:换个|换一道|换一个|换菜|换一批|再来一道|再来一个|再来一批)(?:别的|其它|其他)?的?(?:菜)?',clause):
            # A bare change-of-dish request has no new condition of its own.
            turn.recommend=True;turn.resume=True;continue
        # Subjects and temporal modifiers may appear in either order ("这次我"
        # and "我现在"). Consume grammatical prefix tokens until none remain.
        while True:
            rest=re.sub(r'^(?:这次|今天|临时|现在|那么|那|请|麻烦|还是|再|就|我们家|我家|我们|我)(?!的口味|的偏好)\s*','',clause)
            if rest==clause:break
            clause=rest
        # This clause selects the stored profile; it is not a recipe constraint.
        if re.fullmatch(r'(?:根据|按照|按|结合)我(?:的)?(?:饮食)?(?:口味|偏好|喜好|喜欢)',clause):
            turn.profile=True;continue
        if re.fullmatch(r'(?:根据|按照|按|用|结合)(?:我(?:的)?)?(?:现有|已有|剩下|剩余|手头|手边|家里|库存)(?:的)?食材',clause):
            turn.edits.append(Edit('set','mode',('inventory',),raw));continue
        if not clause or re.fullmatch(r'(?:其他|其它)(?:条件|要求)?不变|就这些',clause):continue
        quick=QUICK_PREFIX.match(clause)
        if quick:
            turn.quick=True
            clause=clause[quick.end():]
            if not clause or re.fullmatch(r'(?:菜|菜谱)',clause):continue
        simple=SIMPLE_PREFIX.match(clause)
        if simple:
            turn.max_difficulty=2
            clause=clause[simple.end():]
            if not clause or re.fullmatch(r'(?:菜|菜谱)',clause):continue
        count=re.fullmatch(r'(?:只要|改成|换成)?([一二两三四五六七八九十]|\d{1,2})道(?:菜)?',clause)
        if count:
            turn.edits.append(Edit('set','limit',(number(count[1]),),raw));continue
        pick_count=re.fullmatch(r'(?:帮我|给我)?挑(几|俩|[一二两三四五六七八九十\d]+)(?:道|样|个)?(?:菜)?',clause)
        if pick_count and any(edit.slot in ('ingredients','mode') for edit in turn.edits):
            turn.recommend=True
            if pick_count[1]!='几':
                turn.edits.append(Edit('set','limit',(2 if pick_count[1]=='俩' else number(pick_count[1]),),raw))
            continue
        removed=re.fullmatch(r'(?:已经)?(?:没有|没剩|删除|删掉)(.+?)(?:了)?',clause)
        if not removed:removed=re.fullmatch(r'(.+?)(?:已经)?(?:用完|用光|没有)(?:了)?',clause)
        if removed:
            values=names(removed[1],ingredient_names())
            if values:
                from ingredient_recommendation import normalize_ingredient
                turn.edits.extend([Edit('remove','ingredients',tuple(dict.fromkeys(normalize_ingredient(v) for v in values)),raw),
                                   Edit('set','mode',('inventory',),raw)])
                continue
        assumed=re.fullmatch(r'(?:这轮|这次|现在)?(?:别|不要|不)按(?:有|还有)?(.+?)(?:算|来算)?',clause)
        if assumed:
            values=names(assumed[1],ingredient_names())
            if values:
                from ingredient_recommendation import normalize_ingredient
                turn.edits.extend([Edit('remove','ingredients',tuple(dict.fromkeys(normalize_ingredient(v) for v in values)),raw),
                                   Edit('set','mode',('inventory',),raw)])
                continue
        topical=re.fullmatch(r'(.+?)(?:就|都)?(?:我不吃|不吃|不要|别放|别加|不放|不加)(?:了)?',clause)
        if topical:
            values=names(topical[1],EXCLUSION_NAMES)
            if values:
                turn.edits.append(Edit('add','excluded_ingredients',canonical(values),raw));continue
        # Longest negation verbs first. Parse the complete target rather than
        # finding "辣" inside "辣椒" and inventing a taste preference.
        negative=re.fullmatch(r'(?:不喜欢|不想|不爱|不能|不要|别|不吃|不含|不放|不加|不用|去掉|避开)(?:再)?(?:吃|带|放|加|含|用|有)?(.+?)(?:的菜|的|了)?',clause)
        if negative:
            target=negative[1]
            if target in ('辣','辣的','辣味'):
                turn.edits.append(Edit('set','tastes',('不辣',),raw));continue
            if target in ('油炸','油炸的','炸的'):
                turn.edits.append(Edit('add','excluded_methods',('油炸',),raw));continue
            targets=re.split(r'、|以及|和|及|与',target)
            if ('油炸' in targets and all(v=='油炸' or v in EXCLUSION_NAMES for v in targets)):
                turn.edits.append(Edit('add','excluded_methods',('油炸',),raw))
                foods=[v for v in targets if v!='油炸']
                if foods:turn.edits.append(Edit('add','excluded_ingredients',canonical(foods),raw))
                continue
            values=names(target,EXCLUSION_NAMES)
            if values:
                turn.edits.append(Edit('add','excluded_ingredients',canonical(values),raw));continue
        allowed=re.fullmatch(r'(?:可以|允许|恢复)(?:吃|带|放|加|含|用)?(.+?)(?:了|的)?',clause)
        if allowed:
            if allowed[1] in ('油炸','油炸的','炸的'):
                turn.edits.append(Edit('remove','excluded_methods',('油炸',),raw));continue
            values=names(allowed[1],EXCLUSION_NAMES)
            if values:
                turn.edits.append(Edit('remove','excluded_ingredients',canonical(values),raw));continue
        taste=re.fullmatch(r'(?:改成|换成|喜欢|想|想要|爱)?(?:吃)?(微辣|不辣|麻辣|辣味|辣)(?:的|了|的?菜)?',clause)
        if taste:
            value=taste[1];value='辣味' if value in ('辣','麻辣') else value
            turn.edits.append(Edit('set','tastes',(value,),raw));continue
        stock=re.fullmatch(r'(?:我|我家|我们)?(?:家里|冰箱(?:里)?|手边|手头)?(?:还|也)?(?:只)?(?:有|剩|剩下)\s*(.+)',clause)
        if stock and re.match(r'^(?:盐|食盐|糖|白糖|生抽|老抽|酱油|醋|香醋|蚝油|料酒|食用油|植物油|菜籽油|淀粉|胡椒|鸡精|味精)(?:[、，,和及与].*)?$',stock[1]):
            # A pantry list is stock, not an unverified selection condition.
            values=names(stock[1],ingredient_names())
            if values:
                from ingredient_recommendation import normalize_ingredient
                turn.edits.append(Edit('add','ingredients',tuple(dict.fromkeys(normalize_ingredient(v) for v in values)),raw))
                continue
        stock=re.fullmatch(r'(有|只有|冰箱(?:里)?(?:有|剩|剩下)|手边有|现有食材[：:]?)\s*(.+)',clause)
        added=re.fullmatch(r'(?:这次|现在|再|另外)?(?:加上|加入|加进|补充|还有)(.+)',clause)
        style=re.fullmatch(r'(?:换成|改成|来一道|来个|想吃|想要|要|推荐)?(?:别的|其它|其他)?(?:一|1)?(?:道|个)?(?:的)?('+'|'.join(STYLES)+r')(?:的)?(?:菜|菜谱)?',clause)
        if style:
            turn.recommend=True
            if re.match(r'(?:换成|改成)',clause):turn.resume=True
            turn.edits.append(Edit('add','extra_constraints',(style[1],),raw))
            continue
        replacement=re.fullmatch(r'(?:改成|换成|改用|换用)(.+)',clause)
        target=stock[2] if stock else added[1] if added else replacement[1] if replacement else clause
        values=names(target,ingredient_names(),correct_stock=bool(stock or added))
        if replacement and not values and re.search(r'菜',target):
            # "换成别的菜/换成清淡一点的菜" changes the dish, not an ingredient.
            turn.recommend=True;turn.resume=True
            condition=re.sub(r'^(?:改成|换成|改用|换用)','',target)
            if not re.fullmatch(r'(?:别的|其它|其他)(?:的)?(?:菜)?|(?:一道|一个|一批)(?:菜)?',condition):
                condition=re.sub(r'(?:的)?菜$','',condition).strip('，,。.;；、 ')
                if condition:turn.unresolved.append(condition)
            continue
        if values:
            from ingredient_recommendation import normalize_ingredient
            values=list(dict.fromkeys(normalize_ingredient(v) for v in values))
            turn.edits.append(Edit('add' if added else 'set','ingredients',tuple(values),raw))
            turn.edits.append(Edit('set','mode',('inventory' if stock or added else 'preference',),raw))
            if stock and stock[1]=='只有':turn.edits.append(Edit('add','extra_constraints',(raw,),raw))
            continue
        made=re.fullmatch(r'(?:今天|这次|现在)?(?:我)?(?:想|想要|要)?(?:用|拿)?(.+?)(?:做|来做|炒|蒸|煮|烤)(?P<count>一|1|[二两三四五六七八九十\d]+)?(?:道|个)?(?:的)?(?:简单|容易|快手|快速|省时)?(?:的)?(?:菜|菜谱)?',clause)
        if made:
            values=names(made[1],ingredient_names())
            if values:
                from ingredient_recommendation import normalize_ingredient
                turn.recommend=True
                turn.edits.append(Edit('set','ingredients',tuple(dict.fromkeys(normalize_ingredient(v) for v in values)),raw))
                turn.edits.append(Edit('set','mode',('preference',),raw))
                if made.group('count'):
                    turn.edits.append(Edit('set','limit',(2 if made.group('count')=='俩' else number(made.group('count')),),raw))
                if re.search(r'简单|容易|新手',clause):turn.max_difficulty=2
                if re.search(r'快手|快速|省时',clause):turn.quick=True
                continue
        # Compact discovery phrasing still maps to separate ingredient/taste slots.
        ingredients='|'.join(re.escape(v) for v in sorted(ingredient_names(),key=lambda v:(-len(v),v)))
        compact=re.fullmatch(r'(?:(辣|不辣|微辣)(?:的)?)?('+ingredients+r')菜',clause)
        if compact:
            from ingredient_recommendation import normalize_ingredient
            turn.edits.append(Edit('set','ingredients',(normalize_ingredient(compact[2]),),raw))
            if compact[1]:turn.edits.append(Edit('set','tastes',('辣味' if compact[1]=='辣' else compact[1],),raw))
            continue
        compact=re.fullmatch(r'('+ingredients+r')的(辣|不辣|微辣)菜',clause)
        if compact:
            from ingredient_recommendation import normalize_ingredient
            turn.edits.extend([Edit('set','ingredients',(normalize_ingredient(compact[1]),),raw),Edit('set','tastes',('辣味' if compact[2]=='辣' else compact[2],),raw)])
            continue
        turn.unresolved.append(raw)
    if turn.recommend:
        # A conversational question about cancellation is not a recipe filter.
        turn.unresolved=[value for value in turn.unresolved
            if not re.fullmatch(r'(?:取消|停止|中断)(?:后|之后)?(?:还|仍)?(?:能|可以)?(?:继续|接着)(?:吗|么)?',value)]
    return turn
