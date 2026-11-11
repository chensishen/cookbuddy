"""Operator commands; not exposed as unauthenticated runtime intent-registration API."""
import argparse
import json
from cookbuddy.store import Store, valid_id
from cookbuddy.router import DEFAULT_INTENTS
from cookbuddy.vectors import VectorStore
from cookbuddy.memory import MemoryService
from cookbuddy.worker import run_once


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('reindex');sub.add_parser('retry-failed');sub.add_parser('init');sub.add_parser('worker-once');sub.add_parser('health')
    reg=sub.add_parser('register-intent');reg.add_argument('--file',required=True)
    forget=sub.add_parser('forget');forget.add_argument('--user',required=True);forget.add_argument('--memory-id')
    maintain=sub.add_parser('maintain');maintain.add_argument('--user',required=True)
    args=parser.parse_args();store=Store()
    if args.command=='init':
        store.migrate()
        for ident,description,examples,target in DEFAULT_INTENTS:
            if ident not in {r['id'] for r in store.registry()}:store.register_intent(ident,description,examples,target)
        VectorStore().ensure();print('cookbuddy schema, tenant policies, intents and vector collection ready')
    elif args.command=='register-intent':
        from pathlib import Path
        item=json.loads(Path(args.file).read_text());store.register_intent(**item);print('Intent registered; next request loads the new version')
    elif args.command=='reindex':
        VectorStore().ensure()
        with store.connect(admin=True) as db:
            db.execute("INSERT INTO cookbuddy.outbox(user_id,memory_id,version,operation) SELECT user_id,id,version,'upsert' FROM cookbuddy.memories WHERE status='active' AND (expires_at IS NULL OR expires_at>now()) ON CONFLICT(memory_id,version,operation) DO UPDATE SET state='pending',attempts=0,available_at=now(),lease_until=NULL")
        print('Active memories scheduled for vector backfill in the configured embedding space')
    elif args.command=='retry-failed':
        with store.connect(admin=True) as db:
            for table in ('memory_tasks','outbox'):
                db.execute(f"UPDATE cookbuddy.{table} SET state='pending',attempts=0,available_at=now(),lease_until=NULL WHERE state='failed'")
        print('Failed memory tasks/outbox entries scheduled for retry')
    elif args.command=='worker-once':print(run_once(store))
    elif args.command=='health':
        with store.connect() as db:db.execute('SELECT 1')
        with store.connect(admin=True) as db:
            queues={table:db.execute(f'SELECT state,count(*) AS count FROM cookbuddy.{table} GROUP BY state').fetchall()
                    for table in ('memory_tasks','outbox')}
            batches=db.execute("SELECT value->>'status' AS status,count(*) AS count FROM cookbuddy.memory_tasks CROSS JOIN LATERAL jsonb_each(extraction_progress) WHERE key NOT LIKE '%_plan' GROUP BY 1").fetchall()
        vectors=VectorStore();print(json.dumps({'postgres':True,'memory_milvus':vectors.collection in vectors.call('collections/list',{}),
            'intents':len(store.registry()),'queues':queues,'extraction_batches':batches}))
    else:
        if not valid_id(args.user):raise ValueError('invalid_user')
        if args.command=='forget':print({'forgotten':store.forget(args.user,[args.memory_id] if args.memory_id else None,purge=True)})
        else:print(MemoryService(store).maintain(args.user))

if __name__=='__main__':main()
