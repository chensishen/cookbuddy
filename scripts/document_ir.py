"""多格式候选 → 统一 IR、原子分块、质量分流。独立于已发布的菜谱索引。"""
import argparse
import csv
import hashlib
from io import StringIO
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import zipfile
from document_ingestion import parse_docx, parse_pdf, count_tokens

PARSER_VERSION='ir-v2'


def element(kind, text, **metadata):
    return {'type':kind,'text':text,'raw':text,'level':None,'heading_path':[],
        'page':None,'bbox':None,'line_start':None,'line_end':None,**metadata}


def markdown_ir(text):
    lines=text.splitlines();rows=[];headings=[];i=0
    while i<len(lines):
        start=i;line=lines[i];heading=re.match(r'^\s*(#{1,6})\s+(.+)',line)
        if heading:
            level=len(heading[1]);headings=headings[:level-1]+[heading[2]]
            kind='title';i+=1
        elif re.match(r'^\s*(`{3,}|~{3,})',line):
            marker=re.match(r'^\s*(`{3,}|~{3,})',line)[1];i+=1
            while i<len(lines) and not re.match(r'^\s*'+re.escape(marker[0])+r'{'+str(len(marker))+r',}\s*$',lines[i]):i+=1
            if i==len(lines):raise ValueError('代码围栏未闭合')
            i+=1;kind='code'
        elif '$$' in line or r'\[' in line or r'\(' in line:
            opening=next(x for x in ('$$',r'\[',r'\(') if x in line)
            closing={'$$':'$$',r'\[':r'\]',r'\(':r'\)'}[opening]
            i+=1
            if not (closing in line.split(opening,1)[1]):
                while i<len(lines) and closing not in lines[i]:i+=1
                if i==len(lines):raise ValueError('公式未闭合')
                i+=1
            kind='formula'
        elif i+1<len(lines) and '|' in line and re.match(r'^\s*\|?\s*:?-{3,}',lines[i+1]):
            i+=2
            while i<len(lines) and '|' in lines[i] and lines[i].strip():i+=1
            kind='table'
        else:
            i+=1;kind='image' if re.search(r'!\[[^]]*\]\(',line) else ('list' if re.match(r'^\s*(?:[-*+]|\d+[.)])\s+',line) else 'paragraph')
        content='\n'.join(lines[start:i])
        if content.strip():rows.append(element(kind,content,level=level if kind=='title' else None,
            heading_path=list(headings),line_start=start+1,line_end=i))
    return rows


class HTMLIR(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.buffer=[];self.kind=None;self.heading=[];self.skip=0;self.table=[];self.cells=[];self.cell=None;self.node=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','nav','footer'):self.skip+=1;return
        if self.skip:return
        if tag=='table':self.table=[]
        elif tag=='tr':self.cells=[]
        elif tag in ('td','th'):self.cell=[]
        elif tag in ('p','li') or re.fullmatch(r'h[1-6]',tag):self.kind=tag;self.buffer=[]
    def handle_data(self,data):
        if self.skip:return
        if self.cell is not None:self.cell.append(data)
        elif self.kind:self.buffer.append(data)
    def handle_endtag(self,tag):
        if tag in ('script','style','nav','footer') and self.skip:self.skip-=1;return
        if self.skip:return
        if tag in ('td','th') and self.cell is not None:self.cells.append(''.join(self.cell).strip());self.cell=None
        elif tag=='tr' and self.cells:self.table.append(self.cells)
        elif tag=='table' and self.table:
            self.node+=1;self.rows.append(element('table',table_text(self.table),heading_path=list(self.heading),node_index=self.node,table_rows=self.table))
        elif self.kind==tag:
            text=''.join(self.buffer).strip();level=int(tag[1]) if tag.startswith('h') else None
            if level:self.heading=self.heading[:level-1]+[text]
            if text:
                self.node+=1;self.rows.append(element('title' if level else 'list' if tag=='li' else 'paragraph',text,
                    level=level,heading_path=list(self.heading),node_index=self.node))
            self.kind=None


def table_text(rows):
    if not rows:return ''
    safe=[[cell.replace('|',r'\|').replace('\n',' ') for cell in row] for row in rows]
    return '\n'.join(['| '+' | '.join(safe[0])+' |','| '+' | '.join('---' for _ in safe[0])+' |']+
                     ['| '+' | '.join(row)+' |' for row in safe[1:]])


def convert(path):
    suffix=path.suffix.lower();magic=path.read_bytes()[:8]
    if suffix in ('.md','.html','.htm','.csv','.tsv'):
        text=path.read_text(encoding='utf-8-sig')
        if suffix=='.md':return markdown_ir(text)
        if suffix in ('.html','.htm'):
            parser=HTMLIR();parser.feed(text);return parser.rows
        rows=list(csv.reader(StringIO(text),delimiter='\t' if suffix=='.tsv' else ','))
        return [element('table',table_text(rows),table_rows=rows,row_range=[1,len(rows)],sheet=path.stem)] if rows else []
    if suffix=='.xlsx':
        from openpyxl import load_workbook
        if not magic.startswith(b'PK'):raise ValueError('XLSX 文件头与扩展名不符')
        with zipfile.ZipFile(path) as archive:
            if sum(x.file_size for x in archive.infolist())>50_000_000:raise ValueError('XLSX 解压体积过大')
        book=load_workbook(path,data_only=False,keep_links=False);result=[]
        try:
            for sheet in book:
                if sheet.max_row*sheet.max_column>1_000_000:raise ValueError('工作表范围过大')
                cells=list(sheet.iter_rows())
                nonempty=[i for i,row in enumerate(cells) if any(c.value is not None for c in row)]
                if not nonempty:continue
                start,end=nonempty[0],nonempty[-1]
                values=[['' if c.value is None else str(c.value) for c in row] for row in cells[start:end+1]]
                formulas=[c.coordinate for row in cells for c in row if c.data_type=='f']
                merged=[str(r) for r in sheet.merged_cells.ranges]
                result.append(element('table',table_text(values),table_rows=values,sheet=sheet.title,
                    heading_path=[sheet.title],table_start_row=start+1,merged_ranges=merged,formula_cells=formulas,
                    review_required=bool(merged or formulas),parse_method='openpyxl_uncomputed_formulas'))
        finally:book.close()
        return result
    if suffix=='.pdf' and not magic.startswith(b'%PDF'):raise ValueError('PDF 文件头与扩展名不符')
    if suffix=='.docx':
        if not magic.startswith(b'PK'):raise ValueError('DOCX 文件头与扩展名不符')
        with zipfile.ZipFile(path) as archive:
            if 'word/document.xml' not in archive.namelist():raise ValueError('不是DOCX文档')
    if suffix not in ('.pdf','.docx'):raise ValueError('支持 MD/HTML/CSV/TSV/XLSX/PDF/DOCX')
    blocks=parse_docx(path) if suffix=='.docx' else parse_pdf(path)
    return [element(row.get('type','table' if row['section']=='table' else 'paragraph'),row['content'],
        page=row.get('page'),node_index=row.get('node_index'),review_required=row.get('review_required',False),
        parse_method=row.get('parse_method'),heading_path=row.get('heading_path',[]),
        level=row.get('level'),**({'table_rows':row['table_rows']} if 'table_rows' in row else {})) for row in blocks]


def table_groups(row, max_tokens, token_counter):
    values=row.get('table_rows')
    if not values:
        lines=row['text'].splitlines();values=[[x.strip() for x in line.strip().strip('|').split('|')] for line in lines if not re.fullmatch(r'[| :\-]+',line)]
    if not values:return []
    width=len(values[0])
    if not width or any(len(r)!=width for r in values):raise ValueError('table_degraded: 表格列数不一致')
    origin=row.get('table_start_row',1)
    header=values[0];result=[];group=[];start=origin+1
    for index,value in enumerate(values[1:],origin+1):
        candidate=table_text([header]+group+[value])
        if token_counter(candidate)>max_tokens and group:
            result.append((start,index-1,group));group=[];start=index
        if token_counter(table_text([header,value]))>max_tokens:raise ValueError('单个表格行超过上限')
        group.append(value)
    if group:result.append((start,origin+len(values)-1,group))
    return [{**row,'text':table_text([header]+group),'row_range':[start,end],
        'embedding_text':'\n'.join('；'.join(f'{key}: {value}' for key,value in zip(header,r)) for r in group)} for start,end,group in result]


def chunk_ir(rows, max_tokens=768, token_counter=count_tokens):
    chunks=[];pending=[]
    def flush():
        if pending:
            text='\n'.join(r['text'] for r in pending)
            chunks.append({'text':text,'embedding_text':text,'elements':list(pending),'heading_path':pending[0]['heading_path']})
            pending.clear()
    consumed=set()
    for index,row in enumerate(rows):
        if index in consumed:continue
        if row['type']=='title':flush();continue
        if row['type']=='table':
            flush()
            for group in table_groups(row,max_tokens,token_counter):
                chunks.append({'text':group['text'],'embedding_text':group['embedding_text'],'elements':[group],'heading_path':row['heading_path'],'row_range':group['row_range']})
            continue
        if row['type'] in ('code','formula'):
            bound=[]
            if row['type']=='formula' and pending and pending[-1]['type']=='paragraph':
                previous=pending[-1]
                if token_counter(previous['text']+'\n'+row['text'])<=max_tokens:bound.append(pending.pop())
            flush()
            if token_counter(row['text'])>max_tokens:raise ValueError('原子代码或公式超过块上限，需人工整理')
            bound.append(row)
            if row['type']=='formula' and index+1<len(rows):
                following=rows[index+1]
                if following['type']=='paragraph' and following['heading_path']==row['heading_path'] and token_counter('\n'.join(r['text'] for r in bound+[following]))<=max_tokens:
                    bound.append(following);consumed.add(index+1)
            text='\n'.join(r['text'] for r in bound)
            chunks.append({'text':text,'embedding_text':text,'elements':bound,'heading_path':row['heading_path']});continue
        if token_counter(row['text'])>max_tokens:raise ValueError('单个段落超过上限，需人工整理')
        if pending and (pending[0]['heading_path']!=row['heading_path'] or token_counter('\n'.join(r['text'] for r in pending+[row]))>max_tokens):flush()
        pending.append(row)
    flush();return chunks


def ingest_ir(path, max_tokens=768, known_hashes=()):
    path=Path(path).resolve();digest=None;issues=[];rows=[];chunks=[];score=1.
    try:
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        rows=convert(path);text='\n'.join(r['text'] for r in rows)
        if not text.strip():issues.append('empty_document');score=0
        if '\ufffd' in text:issues.append('encoding_replacement');score-=.6
        if digest in known_hashes:issues.append('duplicate');score=0
        if not any(r['type']=='title' for r in rows):issues.append('structure_missing');score-=.1
        if any(r.get('review_required') for r in rows):issues.append('review_required');score-=.3
        if score>=.5:chunks=chunk_ir(rows,max_tokens)
    except Exception as error:
        # A malformed file or unavailable converter must not abort the batch.
        issues.append('conversion_or_chunking_failed:'+type(error).__name__);score=0
    score=max(0,score);route='normal' if score>=.8 else 'low_quality' if score>=.5 else 'quarantine'
    if route=='quarantine':chunks=[]
    for index,chunk in enumerate(chunks):
        chunk.update(chunk_id=hashlib.sha256(f'{digest}:{index}:{chunk["text"]}'.encode()).hexdigest(),
            source=str(path),source_sha256=digest,parser_version=PARSER_VERSION,production_enabled=False,
            quality_score=score,low_quality=route=='low_quality')
    return {'source':str(path),'source_sha256':digest,'parser_version':PARSER_VERSION,
        'quality_score':score,'route':route,'issues':issues,'elements':rows,'chunks':chunks,
        'production_enabled':False,'policy':'候选管线；没有自动发布到活动知识库。PDF页级定位，OCR仍需单独复核。'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('sources',nargs='+');parser.add_argument('--output',required=True)
    args=parser.parse_args();seen=set();reports=[]
    for source in args.sources:
        report=ingest_ir(source,known_hashes=seen);seen.add(report['source_sha256']);reports.append(report)
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps({'documents':reports,'counts':{k:sum(r['route']==k for r in reports) for k in ('normal','low_quality','quarantine')}},ensure_ascii=False,indent=2)+'\n')
    print('候选解析报告：'+str(out))
