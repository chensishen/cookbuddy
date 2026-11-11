"""Real PostgreSQL/Milvus + optional real LLM integration receipts, no credentials emitted."""
import argparse
import json
import uuid
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
from check_embedding import ROOT
from cookbuddy.store import Store
from cookbuddy.vectors import VectorStore
from cookbuddy.memory import MemoryService
from cookbuddy.router import IntentRouter
from cookbuddy.models import embed
from cookbuddy.worker import run_once
from service_runtime import request_budget


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--llm',action='store_true');args=parser.parse_args()
    s=Store();v=VectorStore();v.ensure();a=uuid.uuid4().hex;b=uuid.uuid4().hex;s.ensure_user(a);s.ensure_user(b)
    receipt={'created_at':datetime.now(timezone.utc).isoformat(),'checks':{},'llm_live':args.llm};checks=receipt['checks'];ids=[];users={a,b}
    def check(name,value):
        checks[name]=bool(value)
        if not value:raise AssertionError(name)
    try:
        mid=s.upsert_memory(a,'profile','taste','我喜欢清淡口味',{'quote':'我喜欢清淡口味'},ttl_days=365);ids.append((a,mid))
        cid=s.create_conversation(a,'持久会话验收');s.append(a,cid,'test-job','user','我喜欢清淡口味')
        check('conversation_persistence',Store().conversation(a,cid)['messages'][0]['content']=='我喜欢清淡口味')
        with s.connect(b) as db:
            check('rls_read_isolation',db.execute('SELECT count(*) AS n FROM cookbuddy.memories').fetchone()['n']==0)
            check('rls_conversation_isolation',db.execute('SELECT count(*) AS n FROM cookbuddy.conversations').fetchone()['n']==0)
            role=db.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            check('app_role_not_superuser',not role['rolsuper'] and not role['rolbypassrls'])
        old=s.memory(a,mid);s.upsert_memory(a,'profile','taste','我现在喜欢微辣',{'quote':'我现在喜欢微辣'},ttl_days=365)
        updated=s.memory(a,mid);check('memory_update_version',updated['version']==old['version']+1 and updated['content']=='我现在喜欢微辣')
        vector=[1.0]+[0.0]*(v.dim-1)
        v.upsert(updated,vector)
        other=s.upsert_memory(b,'task','another-user','另一个用户任务',{'job_id':'another'},ttl_days=90);ids.append((b,other));v.upsert(s.memory(b,other),vector)
        hits=v.search(a,vector)
        check('milvus_tenant_filter',bool(hits) and all(r['user_id']==a and r['id']!=other for r in hits))
        epoch=s.memory_epoch(a);s.forget(a,[mid],purge=True)
        check('forget_immediate',not s.active(a))
        check('late_extraction_cannot_resurrect',s.upsert_memory(a,'profile','taste','旧提取任务',{},expected_epoch=epoch) is None)
        with patch('cookbuddy.memory.embed',return_value=vector):
            context=MemoryService(s,v).retrieve(a,'口味')
        check('stale_vector_not_injected',not context['items'])
        v.delete(a,mid);check('physical_vector_delete',all(r['id']!=mid for r in v.search(a,vector)))
        exp=s.upsert_memory(a,'task','expired','过期任务',{},ttl_days=-1);ids.append((a,exp))
        check('ttl_excluded_before_cleanup',not s.active(a))
        check('ttl_tombstone',MemoryService(s,v).maintain(a)['forgotten']==1)
        # Compression is verified against >40 real structured tasks, not a mock copy of its algorithm.
        for i in range(43):
            m=s.upsert_memory(a,'task','compression_'+str(i),'任务'+str(i),{'sources':['data/HowToCook/dishes/breakfast/蒸水蛋.md']},ttl_days=90);ids.append((a,m))
        maintenance=MemoryService(s,v).maintain(a)
        check('compression_with_provenance',maintenance['compressed']>0 and any(r['evidence'].get('compressed_from') for r in s.active(a,['task'],100)))
        if args.llm:
            live_uid=uuid.uuid4().hex;users.add(live_uid);s.ensure_user(live_uid)
            with request_budget(seconds=180):
                result={'status':'completed','outputs':[]}
                extracted=MemoryService(s,v).extract(live_uid,'live-extract','请记住，我不吃香菜，家里有空气炸锅。',result,s.memory_epoch(live_uid))
                profiles=s.active(live_uid,['profile']);ids.extend((live_uid,r['id']) for r in profiles)
                check('real_llm_profile_extraction',extracted>0 and any('香菜' in r['content'] for r in profiles))
                for row in profiles:v.upsert(row,embed(row['content']))
                retrieved=MemoryService(s,v).retrieve(live_uid,'今天适合做什么菜？')
                check('real_retriever_rerank',bool(retrieved['items']) and not retrieved['degraded'])
                decision=IntentRouter(s).route('查看基础牛奶面包的酵头图片')
                check('real_hybrid_image_route',decision['target']=='images' and bool(decision['embedding_scores']))
                receipt['intent']={k:decision[k] for k in ('intent','target','confidence','margin','degraded','confidence_calibrated')}
                s.register_intent('acceptance_dynamic','仅当用户问题以“动态验收”开头并要求原文时选择这个意图',['动态验收：查看蒸水蛋原文'],'recipe')
                dynamic=IntentRouter(s).route('动态验收：查看蒸水蛋原文')
                check('real_dynamic_intent',dynamic['intent']=='acceptance_dynamic')
                with s.connect() as db:db.execute("UPDATE cookbuddy.intent_registry SET enabled=false WHERE id='acceptance_dynamic'")
        receipt['passed']=all(checks.values())
    except Exception as error:
        receipt['passed']=False;receipt['error_code']=type(error).__name__
        raise
    finally:
        for uid,mid in ids:
            try:v.delete(uid,mid)
            except Exception:pass
        users.update(uid for uid,_ in ids)
        # Only generated diagnostic tenants are removed. User conversation data is untouched.
        with s.connect(admin=True) as db:
            for uid in users:
                for table in ('messages','conversations','outbox','memory_revisions','memories','memory_tasks','intent_audit'):
                    db.execute(f'DELETE FROM cookbuddy.{table} WHERE user_id=%s',(uid,))
                db.execute('DELETE FROM cookbuddy.tenants WHERE id=%s',(uid,))
        folder=ROOT/'data/processed/cookbuddy-acceptance';folder.mkdir(parents=True,exist_ok=True)
        (folder/('integration-live.json' if args.llm else 'integration.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
    print(json.dumps(receipt,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
