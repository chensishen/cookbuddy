"""Compare original source/citation integrity on the dev RAG sources, not semantic judge scores."""
import argparse,importlib.util,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from recipe_tools import load_chunks,read_recipe
from acceptance_closeout import exact_evidence,citation_metadata

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--before',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    spec=importlib.util.spec_from_file_location('baseline_recipe_tools',a.before);before=importlib.util.module_from_spec(spec);spec.loader.exec_module(before)
    chunks=load_chunks();fixture=json.loads((ROOT/'data/evaluation/prd-dev-v2.json').read_text())
    ids={g for c in fixture['cases'] for g in c.get('gold',[])}
    sources=sorted({r['source'] for r in chunks if r['chunk_id'] in ids})
    records=[]
    for source in sources:
        old=before.read_recipe(source);new=read_recipe(source)
        records.append({'source':source,'same_evidence':old['evidence']==new['evidence'],
            'before_exact':exact_evidence(old),'after_exact':exact_evidence(new),
            'before_citations':citation_metadata(old,chunks),'after_citations':citation_metadata(new,chunks)})
    report={'scope':'verbatim evidence and citation metadata on dev RAG source documents; not generated semantic accuracy',
            'records':records,'sources':len(records),'same_evidence':all(r['same_evidence'] for r in records)}
    for side in ('before','after'):
        total=sum(r[side+'_citations']['count'] for r in records);good=sum(r[side+'_citations']['correct'] for r in records)
        report[side+'_citation_accuracy']=good/total if total else None
        report[side+'_exact_accuracy']=sum(r[side+'_exact']['all_exact'] for r in records)/len(records) if records else None
    report['passed']=bool(records) and report['same_evidence'] and report['after_citation_accuracy']>=report['before_citation_accuracy']-.01
    a.out.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='records'},ensure_ascii=False))
    sys.exit(0 if report['passed'] else 1)
