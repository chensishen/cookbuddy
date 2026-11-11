"""Presentation derived from the same claims that pass answer validation."""
import hashlib
import re
from check_embedding import ROOT

KINDS={'answer','ingredients','tools','step','note'}
LABELS={'answer':'','ingredients':'用料','tools':'工具','step':'制作步骤','note':'关键提醒'}


def typed_claims(claims,required=False):
    if not claims:return False
    typed=any('kind' in claim for claim in claims)
    if not typed and not required:return False
    for claim in claims:
        if claim.get('kind') not in KINDS:raise ValueError('每项结论需提供有效kind：answer、ingredients、tools、step或note。')
        if re.search(r'^\s*#{1,6}\s',claim['text'],re.M):raise ValueError('内容块text不包含标题，请使用kind标记结构。')
        if claim['kind']=='step' and re.match(r'^\s*\d+[.)、](?!\d)\s*',claim['text']):
            raise ValueError('step内容不自行编号；每个操作单独一个step，保留必要条件。')
    return True


def block_heading(claim,references):
    if claim['kind']!='step':return LABELS[claim['kind']]
    by_id={r['reference_id']:r for r in references}
    sections={by_id[e['reference_id']].get('section','').split(' > ')[-1] for e in claim['evidence']}
    sections.discard('')
    if len(sections)==1:
        title=next(iter(sections))
        if title not in ('操作','制作','做法','制作步骤'):return title
    return '制作步骤'


def source_step_number(claim,references):
    by_id={r['reference_id']:r for r in references};numbers=set()
    for item in claim['evidence']:
        row=by_id[item['reference_id']];first=row['content_start_line']
        for line in row['content'].splitlines()[item['start_line']-first:item['end_line']-first+1]:
            match=re.match(r'^\s*(\d+)[.、)](?!\d)\s*',line)
            if match:numbers.add(int(match[1]))
    return next(iter(numbers)) if len(numbers)==1 else None


def source_step_key(claim,references):
    by_id={r['reference_id']:r for r in references};locations=set()
    for item in claim['evidence']:
        row=by_id[item['reference_id']];first=row['content_start_line']
        for offset,line in enumerate(row['content'].splitlines()[item['start_line']-first:item['end_line']-first+1]):
            if re.match(r'^\s*\d+[.、)](?!\d)\s*',line):locations.add(row['source']+':'+str(item['start_line']+offset))
    return next(iter(locations)) if len(locations)==1 else None


def format_claims(claims,references,typed):
    parts=[];previous=None;previous_step=None;numbers={}
    for claim in claims:
        text=claim['text'].strip()
        if typed:
            heading=block_heading(claim,references)
            group=(claim['kind'],heading)
            if group!=previous and heading:parts.append('### '+heading)
            if claim['kind']=='step':
                key=source_step_key(claim,references)
                if key and key==previous_step and group==previous:text='   '+text
                else:
                    number=source_step_number(claim,references) or numbers.get(heading,0)+1
                    numbers[heading]=number;text=str(number)+'. '+text
                previous_step=key
            else:previous_step=None
            previous=group
        ids=sorted({e['reference_id'] for e in claim['evidence']})
        parts.append(text+' '+''.join(f'[{i}]' for i in ids))
    return ('\n\n' if typed else '\n').join(parts)


def build_display_blocks(payload,references):
    claims=payload.get('claims',[])
    if not typed_claims(claims):return []  # Legacy and deterministic source answers remain compatible.
    by_id={r['reference_id']:r for r in references};blocks=[]
    for index,claim in enumerate(claims):
        anchors=[]
        for item in claim['evidence']:
            row=by_id[item['reference_id']];start=item['start_line'];end=item['end_line']
            first=row['content_start_line'];lines=row['content'].splitlines()
            if type(start) is not int or type(end) is not int or not first<=start<=end<first+len(lines):
                raise ValueError('内容块原文定位无效')
            source=row['source'];path=(ROOT/source).resolve()
            if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file():raise ValueError('内容块原文来源无效')
            raw=path.read_bytes();original=raw.decode().splitlines()
            if original[start-1:end]!=lines[start-first:end-first+1]:raise ValueError('内容块原文已变化')
            anchors.append({'reference_id':item['reference_id'],'source':source,'section':row.get('section',''),
                'start_line':start,'end_line':end,'source_sha256':hashlib.sha256(raw).hexdigest()})
        block={'id':'answer-'+str(index+1),'kind':claim['kind'],'text':claim['text'].strip(),
            'heading':block_heading(claim,references),'anchors':anchors,
            'step_number':source_step_number(claim,references) if claim['kind']=='step' else None,
            'step_key':source_step_key(claim,references) if claim['kind']=='step' else None}
        prior=blocks[-1] if blocks else None
        if prior and block['kind']=='step' and prior['kind']=='step' and block['step_key'] and block['step_key']==prior['step_key'] and block['heading']==prior['heading']:
            prior['text']+='\n'+block['text'];prior['anchors'].extend(block['anchors'])
        else:blocks.append(block)
    return blocks


def build_source_display(payload,references):
    """Only the existing deterministic source assembly may use this path."""
    by_id={r['reference_id']:r for r in references};sections=[]
    for claim in payload['claims']:
        if len(claim['evidence'])!=1:return []
        item=claim['evidence'][0];row=by_id[item['reference_id']]
        start=item['start_line'];end=item['end_line'];path=(ROOT/row['source']).resolve()
        if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file():raise ValueError('原文展示来源无效')
        lines=path.read_text().splitlines()[start-1:end]
        cleaned=[re.sub(r'!\[[^\]]*\]\([^\n]*?\)','',line) for line in lines if not re.search(r'Issue|Pull request',line,re.I)]
        text='\n'.join(cleaned).strip();heading='### '+row.get('section','').split(' > ')[-1]+'\n'
        if claim['text'] not in (text,heading+text):raise ValueError('原文展示不能修改已核验的文本')
        # Keep every source line position, including images, for the existing inline renderer.
        quote='\n'.join('' if re.search(r'Issue|Pull request',line,re.I) else line for line in lines)
        sections.append({'source':row['source'],'section':row.get('section',''),'start_line':start,'end_line':end,'quote':quote})
    return sections


def bind_block_images(blocks,images):
    """Only explicitly cited image lines identify a block; never guess across steps."""
    bound=[];files={};counts={}
    for image in images:
        source=image.get('source');line=image.get('source_line')
        if not source or type(line) is not int:continue
        if source not in files:
            path=(ROOT/source).resolve()
            if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file():continue
            raw=path.read_bytes();files[source]=(hashlib.sha256(raw).hexdigest(),raw.decode().splitlines())
        digest,lines=files[source];direct=[]
        for block in blocks:
            if block['kind'] not in ('step','answer','note'):continue
            for anchor in block['anchors']:
                if (anchor['source']!=source or anchor['source_sha256']!=digest
                        or anchor.get('section')!=image.get('section')):continue
                start=anchor['start_line'];end=anchor['end_line']
                if start<=line<=end:direct.append((end-start,block['id']))

        candidates=direct
        if not candidates:continue
        nearest=min(distance for distance,_ in candidates)
        ids={ident for distance,ident in candidates if distance==nearest}
        if len(ids)==1:
            ident=next(iter(ids))
            if counts.get(ident,0)>=2:continue
            counts[ident]=counts.get(ident,0)+1
            bound.append({**image,'answer_block_id':ident})
    return bound
