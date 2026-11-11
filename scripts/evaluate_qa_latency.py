"""同一重排输入对照串行与并行问答；不缓存生成答案，不自动宣称语义正确。"""
import argparse
import hashlib
import json
import os
import time

from ask_test_recipes import run_stage
from check_embedding import ROOT
from evaluate_quality_batch import evidence_checks
from rag_dataset import VERSION, COLLECTION
from service_runtime import request_budget

OUT = ROOT / 'data/processed/qa-latency-v1'
CASES = ROOT / 'data/evaluation/qa-latency-v1.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', choices=('serial', 'balanced', 'both'), default='both')
    parser.add_argument('--ids', nargs='*')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ['RECIPE_ANSWER_CACHE'] = '0'
    for index, case in enumerate(json.loads(CASES.read_text())['cases']):
        if args.ids and case['id'] not in args.ids:
            continue
        input_path = OUT / (case['id'] + '-input.json')
        if not input_path.exists():
            with request_budget(180):
                before = time.monotonic()
                search_path, search = run_stage('search_test_recipes.py', case['question'], 'search-results')
                search_seconds = time.monotonic()-before
                before = time.monotonic()
                rerank_path, rerank = run_stage('rerank_test_recipes.py', str(search_path), 'rerank-results')
                rerank_seconds = time.monotonic()-before
            input_path.write_text(json.dumps({'question': case['question'], 'version': VERSION,
                'collection': COLLECTION, 'search': str(search_path.relative_to(ROOT)),
                'rerank': str(rerank_path.relative_to(ROOT)), 'search_seconds': round(search_seconds, 3),
                'rerank_seconds': round(rerank_seconds, 3), 'embedding_model': search['model'],
                'rerank_model': rerank['rerank_model'], 'retrieval_cache': 'existing caches enabled'},
                ensure_ascii=False, indent=2)+'\n')
        inputs = json.loads(input_path.read_text())
        if inputs['version'] != VERSION or inputs['collection'] != COLLECTION or inputs['question'] != case['question']:
            raise ValueError('固定输入与当前版本或问题不一致，不能继续比较')
        profiles = (['serial', 'balanced'] if index % 2 == 0 else ['balanced', 'serial']) if args.profile == 'both' else [args.profile]
        for profile in profiles:
            path = OUT / (case['id'] + '-' + profile + '.json')
            if path.exists():
                print('保留已有记录：'+path.name, flush=True)
                continue
            os.environ['RECIPE_QA_PROFILE'] = profile
            before = time.monotonic()
            try:
                with request_budget(180):
                    answer_path, answer = run_stage('generate_test_answer.py', str(ROOT/inputs['rerank']), 'answer-results')
                refs = {r['reference_id']: r for r in answer['references']}
                evidence = [{**item, 'source': refs[item['reference_id']]['source']} for item in answer['evidence']]
                checks = evidence_checks({'outputs': [{'result': {'evidence': evidence}}]})
                traces = [json.loads(line) for line in (ROOT/answer['model_attempt_trace']).read_text().splitlines()] if answer['model_attempt_trace'] else []
                row = {'status': answer['status'], 'short_answer': answer['short_answer'],
                    'answer_result': str(answer_path.relative_to(ROOT)), 'evidence_checks': checks,
                    'execution_policy': answer['execution_policy'], 'model_attempts': traces,
                    'model_calls': len(answer['model_calls']), 'answer_method': answer['answer_method']}
            except Exception as error:
                row = {'status': 'failed', 'failure_type': type(error).__name__}
            row.update(case=case, profile=profile, input_record=str(input_path.relative_to(ROOT)),
                generation_wall_seconds=round(time.monotonic()-before, 3),
                source_sha256=hashlib.sha256((ROOT/case['source']).read_bytes()).hexdigest(),
                semantic_review='pending')
            path.write_text(json.dumps(row, ensure_ascii=False, indent=2)+'\n')
            print('完成：'+case['id']+'/'+profile+'；'+row['status'], flush=True)


if __name__ == '__main__':
    main()
