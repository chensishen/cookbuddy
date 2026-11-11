"""逐个原文来源调用工具，核对展示的每段摘录，不调用模型。"""
import json
from pathlib import Path
from check_embedding import ROOT
from recipe_tools import load_chunks,read_recipe
from rag_dataset import FOLDER


def run():
    rows=[]
    for source in sorted({r['source'] for r in load_chunks()}):
        try:
            result=read_recipe(source)
            passed=result['status']=='verified_source' and all(r['source']==source for r in result['sources'])
            for evidence in result['evidence']:
                lines=(ROOT/evidence['source']).read_text().splitlines()
                passed=passed and evidence['quote']=='\n'.join(lines[evidence['start_line']-1:evidence['end_line']])
            error=None
        except Exception as failure:
            passed=False;error=type(failure).__name__
        rows.append({'source':source,'passed':passed,'error_class':error})
    report={'dataset':str(FOLDER),'recipe_count':len(rows),'passed':sum(r['passed'] for r in rows),
            'duplicate_title_clarification':read_recipe('陈皮排骨汤')['status']=='insufficient','rows':rows}
    out=ROOT/'data/processed/engineering-acceptance';out.mkdir(exist_ok=True)
    (out/'all-source-tools.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


if __name__=='__main__':
    result=run();print({k:v for k,v in result.items() if k!='rows'})
    raise SystemExit(0 if result['passed']==result['recipe_count'] else 1)
