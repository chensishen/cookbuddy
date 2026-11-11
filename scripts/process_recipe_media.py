"""附件来源索引与三张图片解析试验；不修改正文或正式向量集合。"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import chunk_test_recipes as splitter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/HowToCook/dishes'
OUT = ROOT / 'data/processed/media_pilot'
IMAGES = {'.jpg', '.jpeg', '.png', '.webp'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temp.replace(path)


def local_file(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(SOURCE.resolve()) or not path.is_file():
        raise ValueError('附件或菜谱不在正式菜谱目录中：'+relative)
    return path


def reference_locations(lines, ref):
    definitions = dict(re.findall(r'^\s*\[([^\]]+)\]:\s*(\S+)', '\n'.join(lines), re.M))
    clean = lambda target: re.split(r'\s+[\"\x27]', target.strip().strip('<>'), maxsplit=1)[0]
    positions=[]
    for number, line in enumerate(lines, 1):
        links=re.findall(r'!\[([^\]]*)\]\(((?:[^()\n]|\([^()]*\))*)\)',line)
        links.extend(('', target) for target in re.findall(r'<img\b[^>]*\bsrc=[\"\x27]([^\"\x27]+)',line,re.I))
        links.extend((alt,definitions.get(ident or alt,ident or alt))
                     for alt,ident in re.findall(r'!\[([^\]]*)\]\[([^\]]*)\]',line))
        if any(alt == ref['alt'] and clean(target) == ref['target'] for alt,target in links):
            positions.append(number)
    return positions


def build_index():
    splitter.SOURCE = SOURCE
    manifest = json.loads((ROOT/'data/processed/all_recipes/asset-manifest.json').read_text())
    chunks = [json.loads(line) for line in
              (ROOT/'data/processed/all_recipes/chunks.jsonl').read_text().splitlines()]
    by_source = {}
    for chunk in chunks:
        by_source.setdefault(chunk['source'], []).append(chunk)
    assets = {}
    for item in manifest['files']:
        path = local_file(item['path']); actual = digest(path)
        if actual != item['sha256']:
            raise ValueError('附件已变化，需要先更新清单：'+item['path'])
        assets[item['path']] = {**item, 'asset_id':actual, 'references':[],
            'media_type':'image' if path.suffix.lower() in IMAGES else 'unsupported',
            'parsing_status':'not_processed'}
    remote = []; unresolved = []
    current_chunks = {}
    for ref in manifest['image_references']:
        path = local_file(ref['recipe']); lines = path.read_text().splitlines()
        positions = reference_locations(lines, ref)
        if ref['kind'] == 'remote':
            remote.append({**ref,'source_lines':positions,'status':'not_downloaded'})
            continue
        if not positions or ref['local_path'] not in assets:
            unresolved.append(ref); continue
        for number in positions:
            matching = [c for c in by_source.get(ref['recipe'], [])
                        if c['source_start_line'] <= number <= c['source_end_line']]
            # 对比本地块和当前原文，防止关联到已经过期的章节。
            if ref['recipe'] not in current_chunks:
                current_chunks[ref['recipe']] = splitter.split_recipe(path)
            for chunk in matching:
                fields=('title','section','source_start_line','source_end_line','content')
                if not any(all(chunk[k] == current[k] for k in fields)
                           for current in current_chunks[ref['recipe']]):
                    raise ValueError('分块与当前原文不一致：'+ref['recipe'])
            link={'source':ref['recipe'],'source_sha256':digest(path),
                  'title':matching[0]['title'] if matching else lines[0].lstrip('# ').removesuffix('的做法'),
                  'section':matching[0]['section'] if matching else '未定位章节',
                  'chunk_ids':[c['chunk_id'] for c in matching], 'source_line':number,
                  'source_quote':lines[number-1], 'alt':ref['alt'], 'target':ref['target']}
            if link not in assets[ref['local_path']]['references']:
                assets[ref['local_path']]['references'].append(link)
    return {'version':'1','assets':list(assets.values()),'remote_references':remote,
            'unresolved_references':unresolved,
            'policy':'本地原图保留；远程链接不下载；非图片附件明确标记unsupported。'}


def check_ocr(result):
    if not isinstance(result.get('observations'), list):
        raise ValueError('OCR结果缺少区域列表')
    for key in ('width','height'):
        if key in result and (type(result[key]) is not int or result[key] <= 0):
            raise ValueError('图片尺寸无效')
    for row in result['observations']:
        if not isinstance(row.get('text'), str) or not row['text'].strip():
            raise ValueError('OCR文字无效')
        confidence=row.get('confidence')
        if type(confidence) not in (int,float) or not 0 <= confidence <= 1:
            raise ValueError('OCR置信度无效')
        box=row.get('bbox',{})
        if set(box) != {'x','y','width','height'} or any(
                type(v) not in (int,float) or not 0 <= v <= 1 for v in box.values()):
            raise ValueError('OCR区域坐标无效')
        if box['x']+box['width'] > 1.0001 or box['y']+box['height'] > 1.0001:
            raise ValueError('OCR区域越界')


def parse_sample(sample, asset):
    path=local_file(sample['image_path'])
    if sample['image_sha256'] != digest(path):
        raise ValueError('样本图片变更，已有视觉描述不能沿用')
    cache=OUT/'ocr-cache'/f"{asset['asset_id']}.json"
    cache.parent.mkdir(exist_ok=True)
    engine_hash=digest(ROOT/'scripts/image_ocr.swift')
    if cache.exists() and json.loads(cache.read_text()).get('engine_source_sha256') == engine_hash:
        ocr=json.loads(cache.read_text())['result']
    else:
        response=subprocess.run(['swift', str(ROOT/'scripts/image_ocr.swift'),str(path)],
            capture_output=True,text=True,timeout=180)
        if response.returncode:
            raise ValueError('本地OCR运行失败：'+response.stderr[-500:])
        ocr=json.loads(response.stdout); check_ocr(ocr)
        save(cache,{'image_sha256':asset['asset_id'],'engine_source_sha256':engine_hash,'result':ocr})
    check_ocr(ocr)
    return {**sample,'ocr':ocr,'references':asset['references'],
            'ocr_review':{'status':'unreviewed' if ocr['observations'] else 'no_text_detected',
                          'low_confidence_region_indices':[i for i,row in enumerate(ocr['observations'])
                                                           if row['confidence'] < 0.8],
                          'confidence_note':'引擎分数只供优先核对，不表示配方正确概率。'},
            'validation':{'image_hash_matches':True,'source_quotes_match':True,
                          'ocr_semantic_status':'unreviewed'},
            'activation':{'enabled':False},
            'warnings':['OCR可能把编号、设备标识或背景文字识别为正文；不得自动转成配料、数量或火候。']}


def make_chunks(record):
    rows=[]
    for ref in record['references']:
        kinds=[('visual_description',record['visual_description'])]
        if record['ocr']['observations']:
            kinds.append(('image_ocr','\n'.join(r['text'] for r in record['ocr']['observations'])))
        for kind, content in kinds:
            key=f"{record['image_sha256']}:{ref['source']}:{ref['source_line']}:{kind}"
            rows.append({'chunk_id':'media::'+hashlib.sha256(key.encode()).hexdigest(),
                'title':ref['title'],'section':ref['section'],'source':ref['source'],
                'source_sha256':ref['source_sha256'],'source_line':ref['source_line'],
                'source_quote':ref['source_quote'],'parent_chunk_ids':ref['chunk_ids'],
                'image_path':record['image_path'],'image_sha256':record['image_sha256'],
                'evidence_type':kind,'content':content,'enabled':False,
                'text':f"菜名：{ref['title']}\n章节：{ref['section']}\n原文路径：{ref['source']}\n"
                       f"图片路径：{record['image_path']}\n证据类型：{kind}（解析草稿）\n\n{content}",
                'limitations':record['limitations']+record['warnings']})
    return rows


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--limit',type=int,default=3)
    args=parser.parse_args()
    if not 1 <= args.limit <= 3:raise ValueError('当前视觉样本仅允许1到3张')
    OUT.mkdir(parents=True,exist_ok=True)
    index=build_index();save(OUT/'asset-index.json',index)
    assets={a['path']:a for a in index['assets']}
    samples=json.loads((OUT/'sample-descriptions.json').read_text())['samples'][:args.limit]
    records=[];failures=[];chunks=[]
    for sample in samples:
        print('处理图片：'+sample['image_path'],flush=True)
        try:
            asset=assets[sample['image_path']];record=parse_sample(sample,asset)
            records.append(record);chunks.extend(make_chunks(record));asset['parsing_status']='sample_processed'
        except (ValueError,KeyError,OSError,subprocess.TimeoutExpired) as error:
            failures.append({'image_path':sample['image_path'],'error':str(error)})
    save(OUT/'asset-index.json',index);save(OUT/'parsed-images.json',{'records':records,'failures':failures})
    (OUT/'media-chunks.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in chunks))
    summary={'created_at':datetime.now(timezone.utc).isoformat(),'asset_count':len(assets),
        'extension_counts':dict(Counter(a['extension'] for a in assets.values())),
        'local_reference_locations':sum(len(a['references']) for a in assets.values()),
        'unreferenced_assets':sum(not a['references'] for a in assets.values()),
        'remote_references':len(index['remote_references']),'unresolved_references':len(index['unresolved_references']),
        'processed_images':len(records),'failed_images':len(failures),'media_chunks':len(chunks),
        'ocr_regions':sum(len(r['ocr']['observations']) for r in records),
        'all_media_chunks_disabled':all(not r['enabled'] for r in chunks),
        'processing':'本地Apple Vision OCR；3张样本视觉描述由助手看图整理，没有视觉API批量调用。'}
    save(OUT/'summary.json',summary)
    preview=['# 图片解析样本\n\nOCR、看图描述与原文图注分别保存。全部解析块暂未启用正式问答。\n\n']
    for record in records:
        preview.extend([f"## {Path(record['image_path']).name}\n\n",
            f"![样本原图]({ROOT/record['image_path']})\n\n",
            f"看图描述：{record['visual_description']}\n\n",
            '限制：'+'；'.join(record['limitations'])+'\n\n',
            'OCR区域（未经语义核对，不是配方）：\n\n'])
        for row in record['ocr']['observations']:
            preview.append('- '+row['text'].replace('\n',' ')+'；置信度 '+str(round(row['confidence'],3))+'\n')
        if not record['ocr']['observations']:preview.append('- 未识别出文字；不代表照片没有信息。\n')
        preview.append('\n原文位置：\n\n')
        for ref in record['references']:
            preview.append(f"- [{ref['title']} / {ref['section']} / L{ref['source_line']}]({ROOT/ref['source']}:{ref['source_line']})；图注：{ref['alt']}\n")
        preview.append('\n')
    for fail in failures:preview.append('- 失败：'+fail['image_path']+' / '+fail['error']+'\n')
    (OUT/'review.md').write_text(''.join(preview))
    print(json.dumps(summary,ensure_ascii=False))
    return 2 if failures or index['unresolved_references'] else 0


if __name__=='__main__':
    raise SystemExit(main())
