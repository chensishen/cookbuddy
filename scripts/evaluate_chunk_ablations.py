"""独立H2/有无前缀向量对照；只报告检索贡献，不冒充端到端消融。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.request import Request

import numpy as np

from check_embedding import ROOT, config
from evaluate_prd import code_fingerprint, metrics, paired_test, summarize_retrieval
from naive_baseline import digest, gold_windows, load_index
from rag_dataset import FOLDER
from recipe_tools import load_chunks
from service_runtime import request_json

VARIANTS = ('h2_plain', 'h2_prefix', 'bounded_plain')
DEFAULT_FOLDER = ROOT/'data/processed/chunk-ablations-v1'


def h2_chunks(source, title, raw):
    """仅在H2边界切分；围栏内的标题属于原文，行号包含空白。"""
    lines = raw.splitlines(keepends=True)
    start = offset = 0
    section = '简介'
    fence = None
    rows = []

    def flush(end):
        content = raw[start:end]
        if not content.strip():
            return
        ident = digest((source+':h2:'+str(start)+':'+str(end)).encode())
        rows.append({'chunk_id':ident, 'source':source, 'title':title, 'section':section,
            'content':content, 'start_char':start, 'end_char':end,
            'source_start_line':raw.count('\n',0,start)+1,
            'source_end_line':raw.count('\n',0,end-1)+1})

    for line in lines:
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})',line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
        elif fence is None:
            heading = re.match(r'^ {0,3}##\s+(.+?)\s*$',line)
            if heading:
                flush(offset)
                start = offset
                section = re.sub(r'\s+#+\s*$','',heading.group(1))
        offset += len(line)
    flush(len(raw))
    return rows


def experiment_rows(chunks, variant):
    if variant not in VARIANTS:
        raise ValueError('未知切块对照')
    sources = {r['source']:r['title'] for r in chunks}
    hashes = {source:digest((ROOT/source).read_bytes()) for source in sorted(sources)}
    if variant == 'bounded_plain':
        rows = [{**r,'text':r['content']} for r in chunks]
    else:
        rows = [r for source,title in sorted(sources.items())
            for r in h2_chunks(source,title,(ROOT/source).read_text())]
        for row in rows:
            row['text'] = (f"菜名：{row['title']}\n章节：{row['section']}\n原文路径：{row['source']}\n\n"
                if variant == 'h2_prefix' else '')+row['content']
    return rows, hashes


def build_index(variant, folder=DEFAULT_FOLDER):
    rows, hashes = experiment_rows(load_chunks(),variant)
    settings = config()
    spec = {'model':settings['EMBEDDING_MODEL'],'dimensions':int(settings['EMBEDDING_DIMENSIONS']),
        'base_url':settings['EMBEDDING_BASE_URL'].rstrip('/'),'source_hashes':hashes,
        'strategy':variant,'prefix':variant=='h2_prefix',
        'input_sha256':digest(json.dumps([(r['chunk_id'],r['text']) for r in rows],ensure_ascii=False).encode())}
    version = digest(json.dumps(spec,sort_keys=True,ensure_ascii=False).encode())
    target = folder/variant
    target.mkdir(parents=True,exist_ok=True)
    manifest_path = target/'manifest.json'
    vectors_path = target/'vectors.jsonl'
    if manifest_path.exists() and json.loads(manifest_path.read_text())['spec_sha256'] != version:
        raise ValueError('来源或模型已变化，请使用新的隔离目录')
    saved = {}
    if vectors_path.exists():
        for line in vectors_path.read_text().splitlines():
            row = json.loads(line)
            if row['spec_sha256'] != version or row['chunk_id'] in saved:
                raise ValueError('重复或不匹配的实验向量')
            saved[row['chunk_id']] = row
    if not set(saved) <= {r['chunk_id'] for r in rows}:
        raise ValueError('未知实验向量')
    manifest = {'state':'building','spec':spec,'spec_sha256':version,
        'production_enabled':False,'chunks':len(rows)}
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    missing = [r for r in rows if r['chunk_id'] not in saved]
    for offset in range(0,len(missing),8):
        batch = missing[offset:offset+8]
        request = Request(spec['base_url']+'/embeddings',
            data=json.dumps({'model':spec['model'],'input':[r['text'] for r in batch],
                'dimensions':spec['dimensions'],'encoding_format':'float'},ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+settings['EMBEDDING_API_KEY'],'Content-Type':'application/json'})
        response = request_json(request,timeout=120,attempts=2,phase='ablation_index',cache_namespace=version)
        with vectors_path.open('a') as stream:
            for row,item in zip(batch,sorted(response['data'],key=lambda r:r.get('index',0))):
                stream.write(json.dumps({**row,'vector':item['embedding'],'spec_sha256':version},ensure_ascii=False)+'\n')
        print(f'{variant}: {len(rows)-len(missing)+min(offset+8,len(missing))}/{len(rows)}',flush=True)
    manifest.update(state='ready',vector_file_sha256=digest(vectors_path.read_bytes()),
        created_at=datetime.now(timezone.utc).isoformat())
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


def rank_batch(queries, rows, k=5):
    """所有方案使用相同精确余弦，隔离切块收益与ANN误差。"""
    vectors = np.asarray([r['vector'] for r in rows],dtype=np.float64)
    query = np.asarray(queries,dtype=np.float64)
    norms = np.linalg.norm(vectors,axis=1)
    query_norms = np.linalg.norm(query,axis=1)
    if not np.isfinite(vectors).all() or not np.isfinite(query).all() or (norms==0).any() or (query_norms==0).any():
        raise ValueError('实验向量无效')
    scores = (query/query_norms[:,None]) @ (vectors/norms[:,None]).T
    return [[rows[i]['chunk_id'] for i in sorted(range(len(rows)),
        key=lambda i:(-values[i],rows[i]['chunk_id']))[:k]] for values in scores]


def compare(report_path, folder=DEFAULT_FOLDER):
    target = folder/'retrieval-report.json'
    if target.exists():
        raise ValueError('已有实验报告，请使用新目录，避免覆盖观测')
    started = time.monotonic()
    fingerprint = code_fingerprint()
    report = json.loads(report_path.read_text())
    fixture = json.loads((ROOT/report['fixture_path']).read_text())
    cases = {r['id']:r for r in fixture['cases']}
    records = [r for r in report['retrieval'] if r['variant']=='hybrid_rerank' and r.get('metrics')]
    questions = [cases[r['case']] for r in records]
    expected = {r['id'] for r in fixture['cases'] if r['gold']}
    if not expected or len({r['case'] for r in records}) != len(records) or {r['case'] for r in records} != expected:
        raise ValueError('查询记录重复或未覆盖全部gold问题')
    vectors = []
    for row,case in zip(records,questions):
        search = json.loads((ROOT/row['search_record']).read_text())
        if search['question'] != case['question']:
            raise ValueError('问题与已有查询向量不匹配')
        vectors.append(search['query_vector'])
    active = load_chunks()
    active_rows = [json.loads(line) for line in (FOLDER/'chunks-with-vectors.jsonl').read_text().splitlines()]
    active_metadata = json.loads((FOLDER/'embedding-metadata.json').read_text())
    if len(active_rows) != len(active) or {r['chunk_id'] for r in active_rows} != {r['chunk_id'] for r in active}:
        raise ValueError('活动向量与文本块不一致')
    active_lookup = {r['chunk_id']:r for r in active}
    if any(any(row[key] != value for key,value in active_lookup[row['chunk_id']].items()) for row in active_rows):
        raise ValueError('活动向量正文与当前文本不一致')
    sources = {source:digest((ROOT/source).read_bytes()) for source in {r['source'] for r in active}}
    rows = []
    manifests = {}
    for variant in ('fixed512','h2_plain','h2_prefix','bounded_plain','bounded_prefix'):
        if variant == 'bounded_prefix':
            index = active_rows
        else:
            manifest,index = load_index(ROOT/'data/processed/naive-512-v1' if variant=='fixed512' else folder/variant)
            spec = manifest['spec']
            if spec['source_hashes'] != sources or spec['model'] != active_metadata['requested_model'] or spec['dimensions'] != active_metadata['dimensions'] or spec['base_url'] != active_metadata['api_base_url']:
                raise ValueError('实验库来源或向量模型不一致')
            manifests[variant] = {k:v for k,v in manifest.items() if k!='spec'}|{'spec':{k:v for k,v in spec.items() if k!='source_hashes'}}
        for case,ids in zip(questions,rank_batch(vectors,index)):
            gold = case['gold'] if variant.startswith('bounded') else gold_windows(case['gold'],active,index)
            if not gold:
                raise ValueError('跨粒度gold无法映射：'+case['id'])
            rows.append({'case':case['id'],'type':case['type'],'variant':variant,
                'metrics':metrics(ids,gold),'results':ids,'mapped_gold':gold})
    comparisons = []
    for before,after in [('fixed512','h2_plain'),('h2_plain','h2_prefix'),('bounded_plain','bounded_prefix')]:
        groups = {}
        lookup = {(r['variant'],r['case']):r for r in rows}
        for case in questions:
            key = tuple(sorted(case.get('sources',[]))) or ('query:'+case['id'],)
            groups.setdefault(key,[]).append(case['id'])
        for metric in ('hit','mrr'):
            left = [sum(lookup[before,i]['metrics'][metric] for i in group)*len(groups)/len(questions) for group in groups.values()]
            right = [sum(lookup[after,i]['metrics'][metric] for i in group)*len(groups)/len(questions) for group in groups.values()]
            comparisons.append({'from':before,'to':after,'metric':metric,'source_clusters':len(groups),
                'queries':len(questions),'test':paired_test(left,right)})
    result = {'schema_version':1,'complete':False,'retrieval_comparison_complete':True,
        'scope':'检索级E1/E2/E3及bounded前缀对照；不代表完整E0–E8/A-1–A-6端到端消融',
        'evaluation_scope':'已消费测试题复用为开发诊断，不是新独立盲测',
        'parent_report':str(report_path.resolve()),'parent_report_sha256':digest(report_path.read_bytes()),
        'fixture_sha256':digest((ROOT/report['fixture_path']).read_bytes()),
        'active_vectors_sha256':digest((FOLDER/'chunks-with-vectors.jsonl').read_bytes()),
        'query_embedding_policy':'复用每题原始查询向量；所有索引精确余弦Top5；无补召回/BM25/重排',
        'gold_policy':'H2/512窗口与原gold来源行范围交叠至少32字符；bounded使用原gold ID；跨粒度Recall不可直接等同',
        'prefix_policy':'E3复用生产格式：菜名、章节、原文路径；H2分块内容完全相同',
        'manifests':manifests,'rows':rows,'aggregates':summarize_retrieval(rows),'comparisons':comparisons,
        'seconds':time.monotonic()-started,'code_sha256':fingerprint,'code_sha256_end':code_fingerprint()}
    result['code_changed_during_run'] = result['code_sha256'] != result['code_sha256_end']
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=('build','compare'))
    parser.add_argument('--variant',choices=VARIANTS)
    parser.add_argument('--folder',type=Path,default=DEFAULT_FOLDER)
    parser.add_argument('--report',type=Path)
    args = parser.parse_args()
    if args.action == 'build':
        if args.variant is None:
            parser.error('build需要--variant')
        build_index(args.variant,args.folder)
    else:
        if args.report is None:
            parser.error('compare需要--report')
        result = compare(args.report,args.folder)
        print(json.dumps({'queries':len(result['rows'])//5,'comparisons':result['comparisons']},ensure_ascii=False))
