"""Complete recipe requests: present verified source sections without lossy model summarization."""
import re

RECIPE_SECTIONS={'必备原料和工具','必备原料','原料','食材','计算','操作','制作','做法','制作步骤','注意事项','附加内容'}

def verbatim_recipe_requested(question):
    if re.search(r'不(?:用|要|必|需)(?:要)?逐字|无需逐字',question):return False
    return bool(re.search(r'逐字|完整原文|原文全文|不要删减|不删减',question))

def prepare_full_recipe_answer(question,references,named_source,compact=False):
    if not named_source:return None
    question=re.sub(r'\n指定菜谱原文来源：'+re.escape(named_source)+r'\s*$','',question)
    # Only complete procedure requests; narrow fact, change, comparison and quantity questions remain QA.
    explicit=bool(re.search(r'完整.{0,8}(?:做法|步骤|制作|配方)|(?:怎么做|怎么制作|如何制作|如何做).*(?:完整|配方)',question))
    titles={row.get('title','') for row in references if row['source']==named_source}
    generic=False
    for title in titles- {''}:
        named_how=re.search(re.escape(title)+r'(?:的)?(?:怎么做|怎么制作)|(?:如何制作|如何做)'+re.escape(title),question)
        plain=re.fullmatch(r'(?:请问|请告诉我)?'+re.escape(title)+r'(?:的)?(?:怎么做|怎么制作)[？?。！!]*',question.strip())
        # Presentation modifiers do not change the recipe. Consume only known
        # output wording; any residual ingredient/change/fact request stays QA.
        presentation=False
        if named_how:
            rest=question[:named_how.start()]+question[named_how.end():]
            terms=r'注意事项|按顺序|分步骤|简洁|简要|简短|详细|说明|介绍|保留|包含|给出|请问|请告诉我|请|并且|以及|关键|必要|重要|用量|温度|时间|用料|材料|配方|步骤|做法|操作|编号|但|并|和|与'
            presentation=not re.sub(r'[\s，,。；;！!？?、：:]+','',re.sub(terms,'',rest))
        if plain or presentation or (named_how and re.search(r'用料|配方',question) and re.search(r'步骤|做法',question)):generic=True
    if not explicit and not generic:return None
    if re.search(r'省略|只有|仅有|现有|手头|手边|库存|预算|减半|倍量|一半|份量|增减|调整|缩放|不加|不使用|不含|禁用|去掉|换用|不放|少放|替代|替换|改成|改用|不用|没有|忌口|过敏|不吃|避免|低糖|低盐|减脂|素食|无糖|无麸质|无乳|少油|低脂|儿童|宝宝|婴儿|清淡|微辣|加倍|半份|能否|是否|可不可以|能不能|至少|最多|区别|差异|比较|还是|为什么|只(?:看|要|问)|几人|几份|[一二两三四五六七八九十百]+\s*(?:人|份)|\d+\s*(?:人|份|克|g|ml|毫升)',question,re.I):return None
    rows=[r for r in references if r['source']==named_source]
    if not rows or any(r['source']!=named_source for r in references):return None
    roots={r['section'].split(' > ')[0] for r in rows}
    if not roots.intersection({'必备原料和工具','必备原料','原料','食材','计算'}) or not roots.intersection({'操作','制作','做法','制作步骤'}):return None
    claims=[];last_line=0;previous_section=None
    for row in sorted(rows,key=lambda r:r['content_start_line']):
        if row['section'].split(' > ')[0] not in RECIPE_SECTIONS:continue
        # Optional equipment encyclopedias are not recipe instructions. Keep the
        # required tools, ingredient restrictions and every operation unchanged.
        if compact and re.search(r'可选.*(?:工具|材料).*介绍$',row['section']):continue
        lines=row['content'].splitlines();first=row['content_start_line']
        start=max(first,last_line+1);end=first+len(lines)-1
        if start>end:continue
        text='\n'.join(lines[start-first:])
        # Media stays structured; repository contribution notices do not answer recipe questions.
        text=re.sub(r'!\[[^\]]*\]\([^\n]*?\)','',text)
        text='\n'.join(line for line in text.splitlines() if not re.search(r'Issue|Pull request',line,re.I)).strip()
        if not text:continue
        if row['section']!=previous_section and not re.match(r'^\s*#',text):
            text='### '+row['section'].split(' > ')[-1]+'\n'+text
        claims.append({'text':text,'evidence':[{'reference_id':row['reference_id'],'start_line':start,'end_line':end}]})
        last_line=end;previous_section=row['section']
    if not claims or sum(len(c['text']) for c in claims)>24000:return None
    return {'claims':claims,'missing_information':''}
