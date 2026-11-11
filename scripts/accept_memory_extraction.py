"""Opt-in real-service acceptance; isolated tenant, no prompt/key output, cleanup."""
import argparse
import json
import time
import uuid
from pathlib import Path
from unittest.mock import patch
from cookbuddy import models
from cookbuddy.store import Store
from cookbuddy.memory import MemoryService
from cookbuddy.vectors import VectorStore
from recipe_tools import read_recipe
from check_embedding import ROOT
from service_runtime import request_budget


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',required=True,help='Call configured remote models and local databases')
    parser.parse_args()
    store=Store();uid=uuid.uuid4().hex;other=uuid.uuid4().hex;vectors=VectorStore()
    report={'checks':{},'calls':[]};tracked=set();original=models.request_json
    def observe(*args,**kwargs):
        start=time.monotonic();result=original(*args,**kwargs)
        if kwargs.get('phase','').startswith('memory_extract_'):
            report['calls'].append({'phase':kwargs['phase'],'seconds':round(time.monotonic()-start,3),
                'finish_reason':result['choices'][0]['finish_reason'],'usage':result.get('usage',{}),
                'output_characters':len(result['choices'][0]['message']['content'])})
        return result
    def extract(question,record):
        job=uuid.uuid4().hex;cid=store.create_conversation(uid,'记忆模块隔离验收')
        store.append(uid,cid,job,'user',question)
        # Direct invocation avoids racing the application's worker.
        from psycopg.types.json import Jsonb
        with store.connect(uid) as db:
            db.execute("INSERT INTO cookbuddy.memory_tasks(job_id,user_id,question,record,state) VALUES(%s,%s,%s,%s,'done')",(job,uid,question,Jsonb(record)))
        service=MemoryService(store,vectors)
        service.save_task(uid,job,question,record,0)
        service.extract(uid,job,question,record,0)
        progress=store.extraction_progress(uid,job)
        assert all(v['status']!='failed' for v in progress.values())
        with patch('cookbuddy.memory.json_model',side_effect=AssertionError('completed batch was repeated')):
            service.extract(uid,job,question,record,0)
        return progress
    try:
        store.ensure_user(uid);store.ensure_user(other);vectors.ensure()
        with patch('cookbuddy.models.request_json',side_effect=observe),request_budget(seconds=240):
            extract('请记住，我喜欢清淡口味。',{'status':'completed','outputs':[]})
            report['checks']['profile_extracted']=bool(store.active(uid,['profile']))
            extract('请记住，我现在喜欢微辣口味。',{'status':'completed','outputs':[]})
            profiles=store.active(uid,['profile'])
            report['checks']['new_preference_replaces_old']=len(profiles)==1 and '微辣' in profiles[0]['content']
            result=read_recipe('基础牛奶面包')
            record={'status':'completed','outputs':[{'result':result}]}
            progress=extract('基础牛奶面包的酵头如何制作和判断发酵完成？',record)
            rows=store.active(uid,limit=200);tracked.update(row['id'] for row in rows)
            report['checks']['task_saved']=any(row['kind']=='task' for row in rows)
            report['checks']['knowledge_saved']=any(row['kind']=='knowledge' for row in rows)
            report['batch_statuses']={state:sum(v['status']==state for v in progress.values())
                for state in ('saved','no_memory','no_candidates','failed')}
            for row in rows:vectors.upsert(row,models.embed(row['content']))
            context=MemoryService(store,vectors).retrieve(uid,'按我的口味，基础牛奶面包酵头怎么制作？')
            report['checks']['retrieval_with_rerank']=not context['degraded'] and any(i['kind']=='profile' for i in context['items']) and any(i['kind']=='knowledge' for i in context['items'])
            report['checks']['tenant_isolation']=not MemoryService(store,vectors).retrieve(other,'基础牛奶面包酵头')['items']
            store.forget(uid,purge=True)
            # Even before tombstones reach Milvus, PostgreSQL must filter stale hits.
            report['checks']['forget_hides_stale_vectors']=not MemoryService(store,vectors).retrieve(uid,'基础牛奶面包酵头')['items']
            report['checks']['forget_clears_checkpoints']=all(not store.extraction_progress(uid,r['job_id']) for r in _jobs(store,uid))
            report['checks']['completed_batches_not_repeated']=True
        assert all(report['checks'].values()),'acceptance_check_failed'
    finally:
        with store.connect(admin=True) as db:
            tracked.update(r['id'] for r in db.execute('SELECT id FROM cookbuddy.memories WHERE user_id=%s',(uid,)).fetchall())
        for mid in tracked:vectors.delete(uid,mid)
        with store.connect(admin=True) as db:
            for table in ('messages','conversations','outbox','memory_revisions','memories','memory_tasks','intent_audit'):
                db.execute(f'DELETE FROM cookbuddy.{table} WHERE user_id=ANY(%s)',([uid,other],))
            db.execute('DELETE FROM cookbuddy.tenants WHERE id=ANY(%s)',([uid,other],))
        report['test_data_cleaned']=True
        output=ROOT/'data/processed/cookbuddy-acceptance/memory-extraction-20261016.json'
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False),flush=True)


def _jobs(store,uid):
    with store.connect(uid) as db:return db.execute('SELECT job_id FROM cookbuddy.memory_tasks').fetchall()


if __name__=='__main__':main()
