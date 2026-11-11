"""Resolve absolute source lines without guessing text or crossing source files."""
import json
from check_embedding import ROOT


def normalize_reference_spans(answer,references):
    payload=json.loads(answer)
    if not isinstance(payload,dict) or not isinstance(payload.get('claims'),list):return answer,[]
    by_id={r['reference_id']:r for r in references};repairs=[];originals={}
    for claim in payload['claims']:
        if not isinstance(claim,dict) or not isinstance(claim.get('evidence'),list):continue
        revised=[]
        for item in claim['evidence']:
            if not isinstance(item,dict):revised.append(item);continue
            ident=item.get('reference_id')
            row=by_id.get(ident) if type(ident) is int else None
            start=item.get('start_line');end=item.get('end_line')
            if not row or type(start) is not int or type(end) is not int or start<1 or end<start:
                revised.append(item);continue
            first=row['content_start_line'];last=first+len(row['content'].splitlines())-1
            if first<=start<=end<=last:revised.append(item);continue
            source=row['source'];path=(ROOT/source).resolve()
            if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file():
                revised.append(item);continue
            if source not in originals:originals[source]=path.read_text().splitlines()
            lines=originals[source]
            if end>len(lines):revised.append(item);continue
            # Only already provided, exact current-source chunks are eligible.
            candidates=[]
            for candidate in references:
                if candidate['source']!=source:continue
                begin=candidate['content_start_line'];body=candidate['content'].splitlines()
                if lines[begin-1:begin-1+len(body)]==body:
                    candidates.append((begin,begin+len(body)-1,candidate['reference_id']))
            spans=[];cursor=start
            while cursor<=end:
                covering=[c for c in candidates if c[0]<=cursor<=c[1]]
                if not covering:break
                begin,finish,ident=max(covering,key=lambda c:(c[1],-c[2]))
                stop=min(finish,end)
                spans.append({**item,'reference_id':ident,'start_line':cursor,'end_line':stop})
                cursor=stop+1
            if cursor<=end:
                revised.append(item);continue  # A gap is not evidence.
            revised.extend(spans)
            repairs.append({'source':source,'before':item,'after':spans})
        claim['evidence']=revised
    return (json.dumps(payload,ensure_ascii=False) if repairs else answer),repairs
