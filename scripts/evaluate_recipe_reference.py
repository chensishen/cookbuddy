"""运行参考题；逐题落盘，机械覆盖检查与人工语义评分分开。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from check_embedding import ROOT
from recipe_agent import execute, save_run


def coverage(evidence, rows):
    items = []
    for span in evidence:
        matched = [row['chunk_id'] for row in rows
                   if row.get('chunk_id') in span['chunk_ids']]
        items.append({'source': span['source'], 'start_line': span['start_line'],
                      'end_line': span['end_line'],
                      'covered': set(span['chunk_ids']).issubset(matched),
                      'partially_covered': bool(matched),
                      'required_chunk_ids': span['chunk_ids'],
                      'matched_chunk_ids': matched})
    return {'covered_spans': sum(item['covered'] for item in items),
            'total_spans': len(items), 'items': items,
            'meaning': '关联章节块覆盖；不等于答案正确或逐句语义支持。'}


def inspect_result(case, run):
    checks = {'semantic_review': 'pending', 'tool_names': [o['tool'] for o in run['outputs']]}
    rag = next((o['result'] for o in run['outputs'] if o['tool'] == 'query_recipes'), None)
    if rag and rag.get('stages'):
        search = json.loads((ROOT / rag['stages']['search']).read_text())
        rerank = json.loads((ROOT / rag['stages']['rerank']).read_text())
        answer = json.loads((ROOT / rag['answer_result']).read_text())
        checks.update(search=coverage(case['evidence'], search['results']),
                      rerank=coverage(case['evidence'], rerank['results']),
                      answer_context=coverage(case['evidence'], answer['references']),
                      answer_method=answer['answer_method'], answer_status=answer['status'])
    evidence_checks = []
    for output in run['outputs']:
        for item in output['result'].get('evidence', []):
            path = ROOT / item['source']
            lines = path.read_text().splitlines()
            start, end = item['start_line'], item['end_line']
            evidence_checks.append(1 <= start <= end <= len(lines) and
                                   '\n'.join(lines[start-1:end]) == item['quote'])
    checks['quote_checks'] = {'checked': len(evidence_checks), 'all_match': all(evidence_checks),
                              'meaning': '无引用时不能据此认为来源合格；匹配也不等于支持结论。'}
    images = [item for output in run['outputs'] for item in output['result'].get('images', [])]
    checks['image_file_checks'] = {
        'checked': len(images),
        'all_match': all(hashlib.sha256((ROOT / item['image_path']).read_bytes()).hexdigest()
                         == item['image_sha256'] for item in images)}
    if case.get('expected_images'):
        expected = {(i['image_path'], i['source_line']) for i in case['expected_images']}
        actual = {(i['image_path'], i['source_line']) for o in run['outputs']
                  for i in o['result'].get('images', [])}
        checks['image_reference_match'] = {'expected_count': len(expected),
                                          'actual_count': len(actual), 'exact_match': expected == actual}
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', default='data/evaluation/baseline-v1')
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--ids', help='仅运行这些题号，例如13,14,16；新复测使用独立目录')
    args = parser.parse_args()
    reference = ROOT / 'data/evaluation/recipe-reference-v1.json'
    data = json.loads(reference.read_text()); digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    folder = (ROOT / args.run_dir).resolve(); folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / 'reference.json'
    if snapshot.exists() and hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
        raise ValueError('参考题已改变，请使用新的运行目录，不能混用批次')
    if not snapshot.exists(): snapshot.write_bytes(reference.read_bytes())
    selected = data['cases'][:args.limit]
    if args.ids:
        numbers = {int(n) for n in args.ids.split(',')}
        if not numbers or not numbers <= set(range(1, len(data['cases'])+1)):
            raise ValueError('题号越界')
        selected = [case for n, case in enumerate(data['cases'], 1) if n in numbers]
    rows = []
    for case in selected:
        result_path = folder / (case['id'] + '.json')
        if result_path.exists():
            rows.append(json.loads(result_path.read_text())); continue
        print(case['id'] + ' 开始：' + case['question'], flush=True)
        started = datetime.now(timezone.utc).isoformat()
        try:
            run = execute(case['question'], mode=case['mode'],
                          progress=lambda message: print('  ' + message, flush=True))
            agent_path = save_run(run)
            checks = inspect_result(case, run)
            row = {'id': case['id'], 'question': case['question'], 'category': case['category'],
                   'mode': case['mode'], 'started_at': started, 'status': run['status'],
                   'elapsed_seconds': run['elapsed_seconds'],
                   'agent_run': str(agent_path.relative_to(ROOT)), 'checks': checks,
                   'semantic_review': {'status': 'pending'},
                   'short_answers': [o['result'].get('short_answer', '') for o in run['outputs']]}
        except Exception as error:
            row = {'id': case['id'], 'question': case['question'], 'category': case['category'],
                   'mode': case['mode'], 'started_at': started, 'status': 'evaluation_error',
                   'error_type': type(error).__name__, 'semantic_review': {'status': 'pending'}}
        result_path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n')
        rows.append(row)
        summary = {'reference_sha256': digest, 'reference_status': data['human_review_status'],
                   'completed_cases': len(rows), 'planned_cases': len(selected), 'cases': rows,
                   'note': '真实执行与机械来源检查；语义待对照人工参考审核，不把工具成功状态当作答案通过。'}
        (folder / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        print(case['id'] + ' 已记录：' + row['status'], flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
