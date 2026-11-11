"""Reproducible NLU evaluation. Offline baseline uses the unchanged production seams.
--live runs the real understanding model; offline results never certify model quality.
"""
import argparse,asyncio,hashlib,json,random,sys,time,statistics,unicodedata
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'tests')]


def generate():
    seeds=[]
    def add(text,intent,slots=None,context=None,clarify=False):
        seeds.append(dict(input=text,context=context or {},gold_intent=intent,gold_slots=slots or {},should_clarify=clarify,
            should_retrieve=intent not in ('identity_capability','chitchat','out_of_scope','unclear') and not clarify,category=intent))
    for text in ['你是谁','你叫啥','介绍一下你自己','你能做什么','你有什么本领','怎么使用cookbuddy','你是什么助手','请介绍你的功能']:
        add(text,'identity_capability')
    for text in ['你好','嗨','在吗','谢啦','谢谢你','早上好','辛苦了','晚上好']:
        add(text,'chitchat')
    from recipe_tools import resolve_title,load_chunks
    title,source=resolve_title('基础牛奶面包',load_chunks())
    context={'slots':{'selected_dish':{'dish_name':title,'source_path':source}}}
    for text in ['只看酵头制作','只看酵头制作部分','酵头制作那一段','看看酵头制作','酵头的制作部分吧','只看酵投制作','怎么发酵头','请展示酵头章节']:
        add(text,'recipe_section',{'section':'酵头的制作'},context)
    for text,dish in [('红烧肉怎么做','红烧肉'),('红烧rou怎么做','红烧肉'),('西红是炒蛋怎么做','西红柿炒鸡蛋'),('蒸水蛋怎么做','蒸水蛋')]:
        add(text,'recipe_lookup',{'dish':dish},clarify=dish=='红烧肉')
    choices=[{'idx':i,'dish_name':v,'source_path':'test/'+v+'.md'} for i,v in enumerate(['红烧肉','宫保鸡丁','可乐鸡翅'],1)]
    for text in ['不要刚才第二道','不要刚才第二道1','不要第二道','别要第 2 个','第二个不要了','换一道，不要刚才第二道。1']:
        add(text,'constraint_refine',{'exclude_refs':[{'type':'ordinal','value':2}]},{'slots':{'last_recommendations':choices}})
    for text in ['今天天气怎么样','帮我写一个网站','最近有什么电影','查一下明天的股票']:
        add(text,'out_of_scope')
    add('只看酵头制作','recipe_section',{'section':'酵头的制作'},clarify=True)
    add('不要第五道','constraint_refine',{'exclude_refs':[{'type':'ordinal','value':5}]},{'slots':{'last_recommendations':choices}},True)
    add('我有鸡蛋和番茄，推荐菜','ingredient_recommend',{'ingredients':['鸡蛋','番茄']})
    add('帮我做宇宙能量饼','recipe_lookup',{'dish':'宇宙能量饼'})
    rng=random.Random(20261026);rows=[]
    for i,seed in enumerate(seeds):
        text=seed['input'];kind=i%7
        if kind==0:perturb=''.join(chr(ord(c)+0xfee0) if '!'<=c<='~' else c for c in text)
        elif kind==1:perturb=text+'。1'
        elif kind==2:perturb=' '.join(text)
        elif kind==3:
            from opencc import OpenCC
            perturb=OpenCC('s2t').convert(text)
        elif kind==4:perturb=text.translate(str.maketrans({'头':'投','柿':'是','蛋':'旦'}))
        elif kind==5 and len(text)>5:perturb=text[:1]+text[2:]
        else:perturb=text+'呀'
        variants=[text,text+'！😊','  '+text+'  ',perturb]
        for j,text in enumerate(variants):rows.append({**seed,'id':f'nlu-{i:03}-{j}','base_id':f'nlu-{i:03}','perturbed':j>0,'input':text})
    rng.shuffle(rows)
    return rows


LEGACY_DETAIL=None

def legacy(case):
    from src.agent.turn_intents import chapter_request,alternative_request,rejected_ordinal
    from recipe_tools import read_recipe
    q=case['input'];section=chapter_request(q)
    import re
    if re.fullmatch(r'(?:你好|您好|谢谢|你能帮我做什么|怎么使用cookbuddy)[！!？?。 ]*',q):
        return {'intent':'chitchat','slots':{},'answer_mode':'general_chat'}
    if section:
        selected=case['context'].get('slots',{}).get('selected_dish')
        r=(LEGACY_DETAIL or read_recipe)(selected['dish_name'],section) if selected else {}
        return {'intent':'recipe_section','slots':{'section':section},'answer_mode':'grounded_recipe' if selected else 'clarify','blocked':r.get('status')=='insufficient'}
    alt=alternative_request(q)
    if alt:
        n=rejected_ordinal(alt['constraint'])
        return {'intent':'constraint_refine','slots':{'exclude_refs':[{'type':'ordinal','value':n}]} if n else {},'answer_mode':'grounded_recipe','blocked':n is None and bool(alt['constraint'])}
    return {'intent':'recipe_lookup','slots':{},'answer_mode':'grounded_recipe'}


def evaluate(cases,live=False,legacy_mode=False,cache_salt=None):
    try:from src.nlu.router import understand
    except ImportError:understand=None
    if legacy_mode:understand=None
    rows=[]
    for c in cases:
        start=time.monotonic()
        context={**c['context'],**({'evaluation_cache_salt':cache_salt} if cache_salt else {})}
        r=understand(c['input'],context,use_model=live) if understand else legacy(c)
        # Flatten gold and predicted structured slots into atomic field/value facts.
        def facts(slots):
            return {(key,json.dumps(value,ensure_ascii=False,sort_keys=True)) for key,value in slots.items() if value not in (None,'',[],{})}
        gold=facts(c['gold_slots']);pred=facts({k:r.get('slots',{}).get(k) for k in c['gold_slots']})
        rows.append({'id':c['id'],'base_id':c['base_id'],'category':c['category'],'perturbed':c['perturbed'],
            'intent_correct':r['intent']==c['gold_intent'],'intent':r['intent'],'slots':r.get('slots',{}),
            'effective_slots':{k:r.get('slots',{}).get(k) for k in c['gold_slots']},
            'slot_tp':len(gold&pred),'slot_fp':len(pred-gold),'slot_fn':len(gold-pred),
            'blocked':bool(r.get('blocked')) or (c['should_retrieve'] and r.get('answer_mode') in ('clarify','out_of_scope','general_chat')),'unnecessary_clarification':r.get('answer_mode')=='clarify' and not c['should_clarify'],
            'clarification_hit':r.get('answer_mode')=='clarify' and c['should_clarify'],'should_clarify':c['should_clarify'],
            'retrieval_correct':(r.get('answer_mode')=='grounded_recipe')==c['should_retrieve'],
            'elapsed_ms':(time.monotonic()-start)*1000,'path':r.get('path','legacy'),'usage':r.get('usage',{}),'answer_mode':r.get('answer_mode')})
    n=len(rows);tp=sum(r['slot_tp'] for r in rows);fp=sum(r['slot_fp'] for r in rows);fn=sum(r['slot_fn'] for r in rows)
    bybase={r['base_id']:r for r in rows if not r['perturbed']};pert=[r for r in rows if r['perturbed']]
    lat=sorted(r['elapsed_ms'] for r in rows)
    return {'scope':'live model routing' if live else 'offline rule fallback and matching; not live model accuracy or production latency',
        'cases':n,'intent_accuracy':sum(r['intent_correct'] for r in rows)/n,'slot_f1':2*tp/max(1,2*tp+fp+fn),
        'false_block_rate':sum(r['blocked'] for r in rows)/n,'unnecessary_clarification_rate':sum(r['unnecessary_clarification'] for r in rows)/n,
        'clarification_recall':sum(r['clarification_hit'] for r in rows)/max(1,sum(r['should_clarify'] for r in rows)),
        'retrieval_accuracy':sum(r['retrieval_correct'] for r in rows)/n,
        'perturbation_stability':sum(r['intent']==bybase[r['base_id']]['intent'] and r['effective_slots']==bybase[r['base_id']]['effective_slots'] and r['answer_mode']==bybase[r['base_id']]['answer_mode'] for r in pert)/len(pert),
        'by_category':{cat:sum(r['intent_correct'] for r in rows if r['category']==cat)/sum(r['category']==cat for r in rows) for cat in sorted({r['category'] for r in rows})},
        'paths':{path:sum(r['path']==path for r in rows) for path in sorted({r['path'] for r in rows})},
        'usage':{key:sum(r['usage'].get(key,0) for r in rows) for key in ('model_calls','prompt_tokens','completion_tokens')},
        'p50_ms':statistics.median(lat),'p95_ms':lat[min(n-1,int(n*.95))],'records':rows}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--generate',action='store_true');p.add_argument('--live',action='store_true');p.add_argument('--out',type=Path,required=True);p.add_argument('--baseline',type=Path);p.add_argument('--legacy-tools',type=Path);p.add_argument('--cache-salt')
    a=p.parse_args();fixture=Path(__file__).with_name('nlu_robust_v1.jsonl')
    if a.generate:fixture.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in generate()))
    if a.legacy_tools:
        import importlib.util
        spec=importlib.util.spec_from_file_location('nlu_baseline_tools',a.legacy_tools)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);LEGACY_DETAIL=module.read_recipe
    cases=[json.loads(line) for line in fixture.read_text().splitlines()];report=evaluate(cases,a.live,bool(a.legacy_tools),a.cache_salt)
    if a.cache_salt:report['cache_salt']=a.cache_salt
    if a.legacy_tools:
        report['scope']='pre-change component baseline, original recipe tool snapshot; not full conversation/end-to-end metrics'
        report['legacy_tools_sha256']=hashlib.sha256(a.legacy_tools.read_bytes()).hexdigest()
    report['fixture_sha256']=hashlib.sha256(fixture.read_bytes()).hexdigest()
    if a.baseline:
        baseline=json.loads(a.baseline.read_text());report['baseline_deltas']={k:report[k]-baseline[k] for k in ['intent_accuracy','slot_f1','false_block_rate','unnecessary_clarification_rate','perturbation_stability','p95_ms']}
    a.out.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='records'},ensure_ascii=False,indent=2))
