"""新问题验收：保留每轮原始结果，来源检查与人工语义审阅分开。"""
import argparse
import hashlib
import json
from pathlib import Path

from check_embedding import ROOT
from rag_dataset import VERSION, COLLECTION
from recipe_agent import execute
from service_runtime import request_budget

FIXTURE = ROOT / 'data/evaluation/quality-batch-v1.json'


def evidence_checks(result):
    checks = []
    for output in result.get('outputs', []):
        for item in output['result'].get('evidence', []):
            source = item.get('source', '')
            path = (ROOT / source).resolve()
            if not path.is_relative_to(ROOT / 'data/HowToCook/dishes') or not path.is_file():
                checks.append(False)
                continue
            lines = path.read_text().splitlines()
            start, end = item.get('start_line'), item.get('end_line')
            checks.append(type(start) is int and type(end) is int
                          and 1 <= start <= end <= len(lines)
                          and '\n'.join(lines[start-1:end]) == item['quote'])
    return {'count': len(checks), 'all_exact': bool(checks) and all(checks)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--round', required=True)
    parser.add_argument('--ids', nargs='*')
    args = parser.parse_args()
    if not args.round.replace('-', '').replace('_', '').isalnum():
        raise ValueError('轮次名称无效')
    folder = ROOT / 'data/processed/quality-batch-v1' / args.round
    folder.mkdir(parents=True, exist_ok=True)
    fixture = json.loads(FIXTURE.read_text())
    for case in fixture['cases']:
        if args.ids and case['id'] not in args.ids:
            continue
        path = folder / (case['id'] + '.json')
        if path.exists():
            print('保留已有记录：' + case['id'], flush=True)
            continue
        source_hashes = {source: hashlib.sha256((ROOT/source).read_bytes()).hexdigest()
                         for source in case.get('sources', [])}
        try:
            with request_budget(180):
                result = execute(case['question'], mode=case['mode'])
            row = {'result': result, 'evidence_checks': evidence_checks(result)}
        except Exception as error:
            row = {'failure_type': type(error).__name__}
        row.update(case=case, version=VERSION, collection=COLLECTION,
                   source_hashes=source_hashes, semantic_review='pending')
        path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n')
        print('完成：' + case['id'], flush=True)


if __name__ == '__main__':
    main()
