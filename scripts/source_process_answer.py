"""为明确的“如何处理 X、何时加入 Y”问题摘录同一菜谱的连续原文步骤。"""
import re


def prepare_section_answer(question, references, named_source):
    """Conservative fallback for a named recipe's exact, unmodified stage."""
    if not named_source:return None
    question=re.sub(r'\n指定菜谱原文来源：'+re.escape(named_source)+r'\s*$','',question)
    rows=[r for r in references if r['source']==named_source]
    titles={r.get('title','') for r in rows}- {''}
    sections={r['section'] for r in rows if r['section'].startswith(('操作 > ','制作 > ','做法 > '))}
    selected=[]
    for section in sections:
        leaf=section.split(' > ')[-1]
        subject=re.sub(r'(?:的)?(?:制作|做法|步骤)$','',leaf)
        if not subject:continue
        pattern=r'(?:请问|请告诉我)?(?:'+ '|'.join(re.escape(t) for t in titles)+r')的'+re.escape(subject)+r'(?:怎么制作|怎么做|如何制作|的做法)[？?。！!]*'
        if titles and re.fullmatch(pattern,question.strip()):selected.append((section,subject))
    if len(selected)!=1:return None
    section,subject=selected[0];claims=[]
    def add(row,start,end):
        lines=row['content'].splitlines();first=row['content_start_line']
        text='\n'.join(lines[start-first:end-first+1])
        text=re.sub(r'!\[[^\]]*\]\([^\n]*?\)','',text).strip()
        if not text:return
        if not re.match(r'^\s*#',text):text='### '+row['section'].split(' > ')[-1]+'\n'+text
        claims.append({'text':text,'evidence':[{'reference_id':row['reference_id'],'start_line':start,'end_line':end}]})
    # Include only the identically named ingredient group, never another stage's quantities.
    for row in rows:
        if row['section']!='计算':continue
        lines=row['content'].splitlines()
        for i,line in enumerate(lines):
            if re.fullmatch(r'-\s*\*\*'+re.escape(subject)+r'\*\*\s*',line):
                end=next((j for j in range(i+1,len(lines)) if re.match(r'^-\s',lines[j])),len(lines))
                add(row,row['content_start_line']+i,row['content_start_line']+end-1)
    # Merge chunk overlaps by absolute source lines. Stop before secondary FAQ topics.
    last=0;finished=False;numbered=False
    for row in sorted((r for r in rows if r['section']==section),key=lambda r:r['content_start_line']):
        lines=row['content'].splitlines();first=row['content_start_line'];start=max(first,last+1);end=first+len(lines)-1
        if start>end or finished:continue
        if last and start>last+1:return None
        for offset in range(start-first,len(lines)):
            line=lines[offset]
            if numbered and re.match(r'^\*\*.+\*\*\s*$',line):end=first+offset-1;finished=True;break
            if re.match(r'^\d+[.、]\s',line):numbered=True
        if start<=end:add(row,start,end)
        last=end
    if not numbered:return None
    return {'claims':claims,'missing_information':''}


def prepare_process_answer(question, references, named_source):
    match=re.search(r'如何处理([^、，,？?]{1,20})[、，,].{0,20}(?:什么时候|何时)(?:加入|放入|倒入)([^、，,？?。]{1,20})',question)
    if not match or not named_source:return None
    subject,target=(item.strip() for item in match.groups())
    if not subject or not target:return None
    operations=[r for r in references if r['source']==named_source and r['section'].startswith('操作')]
    if len(operations)!=1:return None
    row=operations[0]
    lines=row['content'].splitlines()
    numbered=[]
    for offset,line in enumerate(lines):
        found=re.match(r'^\s*(\d+)[.、．]\s*(.*)',line)
        if found:numbered.append((int(found.group(1)),offset,line))
    if not numbered or len({n for n,_,_ in numbered})!=len(numbered):return None
    starts=[i for i,(_,_,line) in enumerate(numbered) if subject in line]
    events=[i for i,(_,_,line) in enumerate(numbered) if re.search(r'(?:加入|放入|倒入).{0,12}'+re.escape(target),line)]
    if not starts or not events:return None
    first=starts[0]
    last=next((i for i in events if i>=first),None)
    if last is None or last-first>4:return None
    numbers=[n for n,_,_ in numbered[first:last+1]]
    if numbers!=list(range(numbers[0],numbers[-1]+1)):return None
    start=numbered[first][1]
    end=numbered[last+1][1] if last+1<len(numbered) else len(lines)
    excerpt='\n'.join(lines[start:end]).strip()
    if not excerpt or len(excerpt)>1600:return None
    return {'claims':[{'text':'原文按以下顺序处理，'+target+'在第'+str(numbers[-1])+'步加入：\n'+excerpt,
                        'evidence':[{'reference_id':row['reference_id'],
                                     'start_line':row['content_start_line']+start,
                                     'end_line':row['content_start_line']+end-1}]}],
            'missing_information':''}
