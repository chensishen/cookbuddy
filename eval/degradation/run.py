"""Offline route/planner comparison. Scripted primary is an oracle, not a live LLM evaluation."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from eval.degradation.faults import FaultyModel,STATES


def cases(path):return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate():
    from src.nlu.router import understand,Meal
    from recipe_tools import load_chunks
    from cookbuddy.meal_planning import plan_meal
    routes=[];menus=[]
    for case in cases(ROOT/'eval/degradation/scenarios_v1.jsonl'):
        slots=case.get('slots',{}).copy()
        if case['intent']=='meal_plan':
            slots['meal']={}
            if 'meal_type' in Meal.model_fields:slots['meal']['meal_type']=case.get('meal_type','unspecified')
        oracle={'intent':case['intent'],'confidence':.99,'slots':slots}
        for state in STATES:
            start=time.monotonic()
            fault=FaultyModel(state,oracle)
            result=understand(case['question'],{'tenant':'eval-'+state},model_call=fault)
            if result['path']!='fast':
                assert fault.calls>0,'fault_not_exercised'
                assert result['path']==('model' if state in ('ok','partial') else 'rule'),'unexpected_evaluation_path'
            meal=result['slots'].get('meal') or {}
            correct=result['intent']==case['intent'] and (not case.get('meal_type') or meal.get('meal_type')==case['meal_type'])
            routes.append({'id':case['id'],'fault':state,'correct':correct,'intent':result['intent'],
                'meal_type':meal.get('meal_type'),'path':result['path'],'errors':result['errors'],
                'elapsed_ms':round((time.monotonic()-start)*1000,3)})
    for case in cases(ROOT/'eval/menu/menu_planning_v1.jsonl'):
        result=understand(case['question'],{},use_model=False)
        meal=result['slots'].get('meal')
        data=plan_meal({**meal,'question':case['question'],'constraints':result['slots']['constraints']},load_chunks()) if meal else {}
        menu=data.get('meal_plan',{});rows=data.get('recommendations',[])
        correct=bool(rows) and menu.get('meal_type')==case['meal_type']
        menus.append({'id':case['id'],'correct':correct,'status':data.get('status'),
            'meal_type':menu.get('meal_type'),'count':len(rows),'sources':[r['source'] for r in rows]})
    faults=[r for r in routes if r['fault'] not in ('ok','partial')]
    return {'method':'offline scripted primary oracle; real fallback and local corpus planner; slow is injected timeout, not measured provider latency',
        'routes':routes,'menus':menus,'metrics':{
            'route_correct':sum(r['correct'] for r in routes),'route_total':len(routes),
            'fallback_correct':sum(r['correct'] for r in faults),'fallback_total':len(faults),
            'meal_type_correct':sum(r['correct'] for r in menus),'meal_total':len(menus)}}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);args=parser.parse_args()
    if args.out.exists():raise SystemExit('refuse_to_overwrite_report')
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['RECIPE_RUNTIME_DB']=str(Path(tmp)/'runtime.sqlite3')
        report=evaluate()
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report['metrics']))
