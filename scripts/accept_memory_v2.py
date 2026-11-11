"""Real model, PostgreSQL and Milvus acceptance using disposable test actors."""
import json
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from datetime import datetime,timezone
from cookbuddy.store import Store
from cookbuddy.models import json_model,MEMORY_USAGE
from cookbuddy.ltm.models import Candidate
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.ltm.extractor import extract
from cookbuddy.ltm.episodic_store import EpisodicStore,MemoryVectors,event_from_user
from cookbuddy.ltm.maintenance import clear_user
from cookbuddy.ltm.guard import source_allowed
from cookbuddy.pipeline import run
from check_embedding import ROOT
from llm_config import require_llm
from cookbuddy.session import SessionState,append_message,rewrite_query


def main():
    store=Store();uid=uuid.uuid4().hex;store.ensure_user(uid)
    prefs=ProfileStore(store);cid=store.create_conversation(uid,'isolated live memory acceptance')
    results=[];usage=[];token=MEMORY_USAGE.set(usage)
    try:
        started=time.perf_counter();candidates=[];blocked=None
        try:candidates=extract('我的烹饪难度上限是二级',model=json_model)
        except Exception as error:blocked={'error_type':type(error).__name__,'http_status':getattr(error,'code',None)}
        results.append({'case':'live_extractor','passed':any(c.dimension=='max_difficulty' and c.value=='2' for c in candidates),
                        'seconds':time.perf_counter()-started,'candidate_dimensions':[c.dimension for c in candidates],'blocked':blocked})
        for i,c in enumerate(candidates):prefs.apply(uid,cid,cid+':extract:'+str(i),c)
        state=SessionState(uid,cid)
        state.slots['ingredients_on_hand']=['鸡肉']
        state.slots['temp_constraints']={'excluded_methods':['油炸']}
        state.slots['last_recommendations']=[{'idx':1,'dish_name':'黄焖鸡','source_path':'test-only'}]
        summary_calls=[]
        def online_summary(*args,**kwargs):
            response=json_model(*args,**kwargs)
            summary_calls.append(response)
            return response
        started=time.perf_counter()
        try:
            for _ in range(14):append_message(state,'user','我有鸡肉，不要油炸，已推荐黄焖鸡。',summarizer=online_summary)
            results.append({'case':'live_session_summary','passed':bool(summary_calls) and all(
                isinstance(r.get('summary'),str) and all(v in r['summary'] for v in ('鸡肉','油炸','黄焖鸡')) for r in summary_calls),
                'seconds':time.perf_counter()-started,'model_calls':len(summary_calls)})
        except Exception as error:
            results.append({'case':'live_session_summary','passed':False,'blocked':{'error_type':type(error).__name__,'http_status':getattr(error,'code',None)}})
        started=time.perf_counter()
        try:
            state.slots['selected_dish']=state.slots['last_recommendations'][0]
            rewritten=rewrite_query(state,'它要炖多久？不要油炸',model=json_model)
            results.append({'case':'live_query_rewrite','passed':all(v in rewritten for v in ('黄焖鸡','多久','不要油炸')),
                            'seconds':time.perf_counter()-started})
        except Exception as error:
            results.append({'case':'live_query_rewrite','passed':False,'blocked':{'error_type':type(error).__name__,'http_status':getattr(error,'code',None)}})
        prefs.apply(uid,cid,cid+':allergy',Candidate('allergy','花生','avoid','hard'))
        q='我有鸡肉，推荐3道辣菜';job=uuid.uuid4().hex;store.append(uid,cid,job,'user',q)
        started=time.perf_counter();answer=run(uid,cid,job,q,store.memory_epoch(uid),lambda _:None)
        dishes=[r for o in answer['outputs'] for r in o['result'].get('recommendations',[])]
        hard=prefs.load_profile(uid)['hard_constraints']
        results.append({'case':'live_recommendation_guard','passed':bool(dishes) and all(source_allowed(r['source'],hard) for r in dishes),
                        'seconds':time.perf_counter()-started,'dishes':[r['title'] for r in dishes]})
        event=event_from_user('上次做的红烧肉太咸了');events=EpisodicStore(prefs)
        started=time.perf_counter()
        try:
            mid=events.write(uid,event,cid+':event');events.sync(uid)
            hits=events.recall(uid,'我上次做的红烧肉怎么调整')
            results.append({'case':'live_embedding_milvus_recall','passed':any(h['mem_id']==mid for h in hits),'seconds':time.perf_counter()-started})
        except Exception as error:
            results.append({'case':'live_embedding_milvus_recall','passed':False,'blocked':{'error_type':type(error).__name__,'http_status':getattr(error,'code',None)}})
        receipt=clear_user(prefs,uid)
        results.append({'case':'live_clear','passed':receipt['vector_deletion']=='complete' and not prefs.prefs(uid) and not MemoryVectors().ids(uid)})
        cfg=require_llm()
        report={'run_at':datetime.now(timezone.utc).isoformat(),'model':cfg['model'],'base_url':cfg['base_url'],
                'scope':'真实模型提取、摘要、改写与 Embedding/Milvus/PG，隔离测试用户；小样本冒烟测试，不是生产 SLA。',
                'cases':results,'model_usage':usage,'llm_provider_reported_tokens':sum(item['usage'].get('total_tokens',0) for item in usage) if usage else None,
                'passed':all(r['passed'] for r in results)}
        folder=ROOT/'data/processed/cookbuddy-acceptance';folder.mkdir(parents=True,exist_ok=True)
        path=folder/'memory-v2-live-20261018-siliconflow.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('memory_live_acceptance',report['passed'],str(path.relative_to(ROOT)))
        return 0 if report['passed'] else 1
    finally:
        MEMORY_USAGE.reset(token)
        try:clear_user(prefs,uid)
        except Exception:pass
        with store.connect(admin=True) as db:
            for table in ('memory_tasks','outbox','memory_revisions','memories','intent_audit','messages','conversations'):
                db.execute('DELETE FROM cookbuddy.'+table+' WHERE user_id=%s',(uid,))
            db.execute('DELETE FROM cookbuddy.tenants WHERE id=%s',(uid,))
if __name__=='__main__':raise SystemExit(main())
