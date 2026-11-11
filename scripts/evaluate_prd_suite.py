"""固定同一轮报告，失败后仍输出门槛验收，不依赖可变 latest 指针。"""
import argparse
import subprocess
from pathlib import Path

from check_embedding import ROOT
from evaluate_prd import run
from review_prd_eval import review
from accept_prd import write_acceptance


def run_suite(ragas_python,fixture=None):
    report=run(live=True,fixture=fixture) if fixture is not None else run(live=True)
    path=ROOT/report['report_path']
    failed=bool(report['failures'] or any('failure_type' in r for r in report['answers']) or not report['corpus']['passed'])
    try:
        report=review(path,judge=True)
        failed=failed or any(not r.get('llm_judge') for r in report['answers'])
    except Exception as error:
        failed=True
        print('Judge未完成：'+type(error).__name__,flush=True)
    try:
        result=subprocess.run([str(ragas_python),str(ROOT/'scripts/evaluate_ragas.py'),str(path)],cwd=ROOT,check=False)
        failed=failed or result.returncode!=0
    except OSError as error:
        failed=True
        print('Ragas未完成：'+type(error).__name__,flush=True)
    acceptance=write_acceptance(path)
    print('PRD门槛：'+acceptance['overall']+'；明细：'+str(path.with_name('acceptance.md')),flush=True)
    # 执行成功与产品验收分开；严格验收由 make eval-acceptance 执行。
    return not failed


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--ragas-python',default=str(ROOT/'.venv-ragas/bin/python'))
    parser.add_argument('--fixture',type=Path)
    args=parser.parse_args()
    raise SystemExit(0 if run_suite(args.ragas_python,args.fixture) else 1)
