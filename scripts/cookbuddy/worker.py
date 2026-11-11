"""Durable leased jobs and transactional outbox, bounded retries and tombstone replay."""
import argparse
import logging
import threading
import time
from datetime import datetime,timezone,timedelta
from .store import Store
from .memory import MemoryService
from .vectors import VectorStore
from .models import embed, ModelOutputError
from service_runtime import request_budget


def claim(store,table):
    if table not in ('memory_tasks','outbox'):raise ValueError('invalid_worker_table')
    ident='job_id' if table=='memory_tasks' else 'id'
    with store.connect(admin=True) as db:
        return db.execute(f"""UPDATE cookbuddy.{table} SET state='processing',lease_until=now()+interval '180 seconds',attempts=attempts+1
            WHERE {ident}=(SELECT {ident} FROM cookbuddy.{table} WHERE (state='pending' AND available_at<=now()) OR (state='processing' AND lease_until<now())
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""").fetchone()


def finish(store,table,row,error=None):
    ident='job_id' if table=='memory_tasks' else 'id'
    state='done' if error is None else ('failed' if row['attempts']>=5 else 'pending')
    with store.connect(row['user_id']) as db:
        # A foreground forget can invalidate a task while an external extraction call is in flight.
        code=error.code if isinstance(error,ModelOutputError) else type(error).__name__ if error else None
        db.execute(f"UPDATE cookbuddy.{table} SET state=%s,error_code=%s,available_at=%s,lease_until=NULL WHERE {ident}=%s AND state='processing'",(state,code,datetime.now(timezone.utc)+timedelta(seconds=min(300,2**row['attempts'])),row[ident]))


def run_once(store=None):
    store=store or Store();counts={'tasks':0,'vectors':0,'errors':0}
    row=claim(store,'memory_tasks')
    if row:
        try:
            with request_budget(seconds=120):
                if row['epoch']==store.memory_epoch(row['user_id']):
                    from .ltm.worker import process_memory_task
                    process_memory_task(store,row)
            finish(store,'memory_tasks',row);counts['tasks']+=1
        except Exception as error:finish(store,'memory_tasks',row,error);counts['errors']+=1
    for _ in range(8):
        row=claim(store,'outbox')
        if not row:break
        try:
            vectors=VectorStore();vectors.ensure()
            with request_budget(seconds=60):
                current=store.memory(row['user_id'],row['memory_id'])
                # PostgreSQL is authoritative, replay always applies the current revision.
                if not current or current['status']!='active' or current['expires_at'] and current['expires_at']<=datetime.now(timezone.utc):
                    vectors.delete(row['user_id'],row['memory_id'])
                else:
                    vector=embed(current['content'])
                    with store.connect(row['user_id']) as db:
                        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(row['user_id'],))
                        latest=db.execute('SELECT * FROM cookbuddy.memories WHERE id=%s',(current['id'],)).fetchone()
                        if latest and latest['status']=='active' and latest['version']==current['version']:
                            vectors.upsert(current,vector)
                        elif not latest or latest['status']!='active':vectors.delete(row['user_id'],row['memory_id'])
            finish(store,'outbox',row);counts['vectors']+=1
        except Exception as error:finish(store,'outbox',row,error);counts['errors']+=1
    # Independent projection failures do not roll back extracted preferences or
    # block answering. Durable jobs are retried on the next worker pass.
    try:
        with store.connect(admin=True) as db:
            users=db.execute('''SELECT user_id FROM cookbuddy.memory_vector_outbox
                UNION SELECT user_id FROM cookbuddy.memory_purge_outbox LIMIT 8''').fetchall()
        from .ltm.profile_store import ProfileStore
        from .ltm.maintenance import retry_purge
        from .ltm.episodic_store import EpisodicStore
        for user in users:
            prefs=ProfileStore(store)
            try:
                retry_purge(prefs,user['user_id'])
                counts['vectors']+=EpisodicStore(prefs).sync(user['user_id'])
            except Exception:counts['errors']+=1
    except Exception:counts['errors']+=1
    return counts


class MemoryWorker:
    def __init__(self):
        self.maintenance_at=0;self.weekly_at=0;self.reconcile_at=0;self.stopped=threading.Event();self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def loop(self):
        while not self.stopped.is_set():
            try:
                run_once()
                if time.monotonic()-self.maintenance_at>300:
                    store=Store()
                    with store.connect(admin=True) as db:users=db.execute('SELECT id FROM cookbuddy.tenants').fetchall()
                    for user in users:
                        if self.stopped.is_set():break
                        from .ltm.profile_store import ProfileStore
                        from .ltm.maintenance import maintain,retry_purge,migrate_legacy_profiles
                        from .ltm.episodic_store import EpisodicStore
                        prefs=ProfileStore(store)
                        try:
                            retry_purge(prefs,user['id'])
                            migrate_legacy_profiles(prefs,user['id'])
                            maintain(prefs,user['id'],compression=time.monotonic()-self.weekly_at>604800,reconcile=True)
                            EpisodicStore(prefs).sync(user['id'])
                        except Exception:logging.warning('cookbuddy memory maintenance failed')
                    from .ltm.maintenance import flush_idle_sessions
                    flush_idle_sessions(store)
                    if time.monotonic()-self.reconcile_at>86400:
                        from .ltm.maintenance import reconcile_orphan_owners
                        reconcile_orphan_owners(store);self.reconcile_at=time.monotonic()
                    if time.monotonic()-self.weekly_at>604800:self.weekly_at=time.monotonic()
                    self.maintenance_at=time.monotonic()
            except Exception as error:logging.warning('cookbuddy memory worker: %s',type(error).__name__)
            self.stopped.wait(2)
    def close(self):self.stopped.set();self.thread.join(timeout=2)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    if args.once:print(run_once());return
    worker=MemoryWorker()
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:worker.close()

if __name__=='__main__':main()
