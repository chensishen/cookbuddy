"""可追溯的 MD/PDF/DOCX 解析与长章节子块；OCR 候选需复核。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count_tokens(text):
    import tiktoken
    # 是拆块长度代理；并非宣称与 Qwen tokenizer 完全一致。
    return len(tiktoken.get_encoding('cl100k_base').encode(text, disallowed_special=()))


def markdown_blocks(text):
    lines = text.splitlines()
    title, section, hierarchy, start, body, fence = '未命名文档', '简介', {}, 1, [], None
    rows = []
    def emit(end):
        content = '\n'.join(body).strip()
        if content and any(x.strip() and not re.match(r'^#{1,6}\s+',x) for x in content.splitlines()):
            rows.append({'title':title,'section':section,'content':content,
                         'source_start_line':start,'source_end_line':end,'locator_type':'line'})
    i = 0
    while i < len(lines):
        line = lines[i]
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0]==fence[0] and len(value)>=len(fence):
                fence = None
            body.append(line);i+=1;continue
        heading = None if fence else re.match(r'^ {0,3}(#{1,6})\s+(.+?)\s*#*$',line)
        setext = not fence and i+1<len(lines) and line.strip() and re.fullmatch(r' {0,3}(=+|-+)\s*',lines[i+1])
        if heading or setext:
            level = len(heading[1]) if heading else (1 if lines[i+1].strip().startswith('=') else 2)
            label = heading[2] if heading else line.strip()
            emit(i)
            if level==1:
                title=label.removesuffix('的做法');section='简介';hierarchy={};body=[]
            else:
                hierarchy={k:v for k,v in hierarchy.items() if k<level};hierarchy[level]=label
                section=' > '.join(hierarchy.values());body=[line]+([lines[i+1]] if setext else [])
            start=i+1 if level>1 else i+2+(1 if setext else 0)
            i+=2 if setext else 1
        else:
            body.append(line);i+=1
    emit(len(lines))
    return rows


def bounded_chunks(block, max_tokens=768, overlap_lines=0, token_counter=count_tokens):
    if max_tokens<8 or overlap_lines<0:
        raise ValueError('分块上限过小或 overlap 非法')
    lines = block['content'].splitlines()
    result, cursor = [], 0
    while cursor < len(lines):
        end = cursor
        while end<len(lines) and token_counter('\n'.join(lines[cursor:end+1]))<=max_tokens:
            end+=1
        if end==cursor:
            # 单行不可无损拆分又保留完整引用；明确阻止，而非静默截断。
            raise ValueError('单行超过块上限，请先按步骤或段落整理该来源')
        content='\n'.join(lines[cursor:end])
        row={**block,'content':content,'parent_section':block['section'],
             'part':len(result)+1,'token_count_proxy':token_counter(content),
             'tokenizer_proxy':'cl100k_base','overlap_lines':overlap_lines}
        if block.get('locator_type')=='line':
            row['source_start_line']=block['source_start_line']+cursor
            row['source_end_line']=block['source_start_line']+end-1
        result.append(row)
        if end==len(lines):break
        cursor=max(cursor+1,end-min(overlap_lines,end-cursor-1))
    return result


def parse_docx(path):
    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    with zipfile.ZipFile(path) as archive:
        if sum(x.file_size for x in archive.infolist())>50_000_000:
            raise ValueError('DOCX 解压体积过大')
        root=ET.fromstring(archive.read('word/document.xml'))
    rows=[];headings=[]
    for node in root.findall('w:body/*',ns):
        extra={}
        if node.tag.endswith('tbl'):
            values=[[''.join(t.text or '' for t in cell.findall('.//w:t',ns))
                for cell in tr.findall('w:tc',ns)] for tr in node.findall('w:tr',ns)]
            text='\n'.join(' | '.join(row) for row in values)
            extra['table_rows']=values
            kind='table'
        else:
            text=''.join(t.text or '' for t in node.findall('.//w:t',ns));kind='paragraph'
            style=node.find('w:pPr/w:pStyle',ns)
            match=re.fullmatch(r'heading\s*([1-6])',style.get('{'+ns['w']+'}val',''),re.I) if style is not None else None
            if match:
                level=int(match[1]);headings=headings[:level-1]+[text]
                extra['level']=level;extra['type']='title'
        if text.strip():
            rows.append({'title':path.stem,'section':kind,'content':text,
                         'locator_type':'document_node','node_index':len(rows)+1,'parse_method':'docx_xml',
                         'heading_path':list(headings),**extra})
    return rows


def parse_pdf(path, ocr=False):
    from pypdf import PdfReader
    rows=[]
    for page_number,page in enumerate(PdfReader(path).pages,1):
        text=page.extract_text() or ''
        method, observations='pdf_text',[]
        if not text.strip():
            if not ocr:
                rows.append({'title':path.stem,'section':f'第{page_number}页','content':'',
                             'page':page_number,'locator_type':'page','parse_method':'needs_ocr','review_required':True})
                continue
            renderer=shutil.which('pdftoppm')
            if not renderer:
                raise RuntimeError('扫描PDF需要 pdftoppm 和本地OCR引擎')
            with tempfile.TemporaryDirectory() as directory:
                prefix=str(Path(directory)/'page')
                subprocess.run([renderer,'-f',str(page_number),'-l',str(page_number),'-singlefile','-png','-scale-to','1800',str(path),prefix],check=True,capture_output=True,timeout=30)
                output=subprocess.run(['swift',str(ROOT/'scripts/image_ocr.swift'),prefix+'.png'],check=True,capture_output=True,text=True,timeout=60)
                observations=json.loads(output.stdout)['observations']
            text='\n'.join(x['text'] for x in observations);method='pdf_apple_vision_ocr'
        rows.append({'title':path.stem,'section':f'第{page_number}页','content':text,'page':page_number,
                     'locator_type':'page','parse_method':method,'observations':observations,
                     'review_required':method!='pdf_text'})
    return rows


def ingest(path, max_tokens=768, overlap_lines=0, ocr=False, include_path=False):
    path=Path(path).resolve()
    if path.suffix.lower()=='.md':
        blocks=markdown_blocks(path.read_text(encoding='utf-8'))
    elif path.suffix.lower()=='.docx':blocks=parse_docx(path)
    elif path.suffix.lower()=='.pdf':blocks=parse_pdf(path,ocr)
    else:raise ValueError('当前支持 MD、PDF、DOCX；图片候选使用已有OCR管线')
    digest=sha(path)
    source=path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    rows=[]
    for block in blocks:
        if not block['content'].strip():continue
        for row in bounded_chunks(block,max_tokens,overlap_lines):
            locator={k:row[k] for k in ('source_start_line','source_end_line','page','node_index','part') if k in row}
            ident=hashlib.sha256(json.dumps([source,locator,row['content']],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            text=f"菜名：{row['title']}\n章节：{row['section']}\n"
            if include_path:text+=f'原文路径：{source}\n'
            text+='\n'+row['content']
            rows.append({**row,'source':source,'source_sha256':digest,'chunk_id':ident,
                         'text':text,'evidence_type':'ocr_candidate' if row.get('review_required') else 'parsed_source',
                         'production_enabled':False})
    return {'schema_version':2,'source':source,'source_sha256':digest,'max_tokens_proxy':max_tokens,
            'path_in_embedding':include_path,'blocks':blocks,'chunks':rows,
            'policy':'候选解析结果；非MD来源尚未进入正式配方问答，OCR必须核对'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source');parser.add_argument('--output',required=True)
    parser.add_argument('--max-tokens',type=int,default=768);parser.add_argument('--overlap-lines',type=int,default=0)
    parser.add_argument('--ocr',action='store_true');args=parser.parse_args()
    result=ingest(args.source,args.max_tokens,args.overlap_lines,args.ocr)
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(f'解析完成：{len(result["chunks"])}块，保存到{output}')
