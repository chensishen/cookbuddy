"""一次生产实验或一个阶段的隔离入口。"""
import json
import os
from pathlib import Path
import runpy
import sys
import time

from production_ablation_runtime import PROFILES,install,audit_summary


def main():
    profile=os.environ['RECIPE_PRODUCTION_ABLATION']
    if profile not in PROFILES:raise ValueError('未知方案')
    audit=[]
    with install(profile,audit):
        if sys.argv[1]=='--stage':
            script=Path(sys.argv[2]);sys.argv=sys.argv[2:]
            runpy.run_path(str(script),run_name='__main__')
            return
        question=sys.argv[1];output=Path(sys.argv[2]);started=time.monotonic()
        from evaluate_full_matrix import failure_diagnostics
        from service_runtime import request_budget
        result={'variant':profile}
        try:
            with request_budget(180):
                if PROFILES[profile].get('remove_agent'):
                    from recipe_tools import query_recipes
                    result['result']=query_recipes(question)
                else:
                    from recipe_agent import execute
                    record=execute(question);result['agent_record']=record
                    outputs=[row['result'] for row in record.get('outputs',[])]
                    if outputs:
                        result['result']=outputs[0] if len(outputs)==1 else {
                            'status':record['status'],'short_answer':'\n'.join(row['short_answer'] for row in outputs),
                            'evidence':[e for row in outputs for e in row.get('evidence',[])]}
                    if not outputs or record['status'] not in ('completed','insufficient'):
                        result['failure_type']='ProductionAgentIncomplete'
        except Exception as error:result.update(failure_diagnostics(error))
        result.update(seconds=time.monotonic()-started,stage_audit=audit,activation=audit_summary(audit))
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
