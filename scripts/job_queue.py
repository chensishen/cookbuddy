"""SQLite 持久任务队列；跨 API/worker 进程原子准入与认领。"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from check_embedding import ROOT
from service_runtime import ClosingConnection,RuntimeStore

TERMINAL = ('done', 'failed', 'cancelled', 'expired')


class QueueFull(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class JobQueue:
    def __init__(self, path=None, capacity=8):
        self.path = Path(path or os.environ.get('RECIPE_JOBS_DB', ROOT / 'data/runtime/jobs.sqlite3'))
        self.capacity = capacity
        if capacity < 1:raise ValueError('任务容量必须为正')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, idem TEXT UNIQUE, payload TEXT, fingerprint TEXT,
                state TEXT, progress TEXT, created REAL, started REAL, finished REAL,
                deadline REAL, owner TEXT, lease REAL, result TEXT, record TEXT)''')
            db.execute('CREATE TABLE IF NOT EXISTS ingress(client TEXT, created REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS job_events(job_id TEXT, message TEXT, at REAL)')

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None, factory=ClosingConnection)
        db.execute('PRAGMA busy_timeout=5000')
        if db.execute('PRAGMA journal_mode').fetchone()[0]!='wal':
            db.execute('PRAGMA journal_mode=WAL')
        return db

    def submit(self, payload, idem=None, seconds=180, client=None, rate=60, ready=True):
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(text.encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE finished < ?)', (now - 86400,))
            db.execute('DELETE FROM jobs WHERE finished < ?', (now - 86400,))
            if idem:
                old = db.execute('SELECT id,fingerprint FROM jobs WHERE idem=?', (idem,)).fetchone()
                if old:
                    if old[1] != digest:
                        db.rollback()
                        raise IdempotencyConflict('同一提交标识不能用于不同问题')
                    db.commit()
                    return old[0], True
            db.execute('DELETE FROM ingress WHERE created < ?', (now-60,))
            if client and db.execute('SELECT count(*) FROM ingress WHERE client=?',(client,)).fetchone()[0]>=rate:
                db.rollback()
                raise QueueFull('提交过于频繁，请稍后再试')
            active = db.execute("SELECT count(*) FROM jobs WHERE state IN ('preparing','queued','running')").fetchone()[0]
            if active >= self.capacity:
                db.rollback()
                raise QueueFull('查询较多，请稍后再试')
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO jobs(id,idem,payload,fingerprint,state,progress,created,deadline) VALUES(?,?,?,?,?,?,?,?)',
                       (ident, idem, text, digest, 'queued' if ready else 'preparing', '等待执行' if ready else '正在保存消息', now, now + seconds))
            if client:db.execute('INSERT INTO ingress VALUES(?,?)',(client,now))
            db.commit()
            return ident, False

    def release(self,ident):
        with self.connect() as db:
            return bool(db.execute("UPDATE jobs SET state='queued',progress='等待执行' WHERE id=? AND state='preparing'",(ident,)).rowcount)

    def recover(self):
        now = time.time()
        with self.connect() as db:
            # 已执行一半的不自动重放；原有来源结果文件仍保留。
            db.execute("UPDATE jobs SET state='failed',progress='执行进程中断，可重新提交',finished=? WHERE state='running' AND lease<?", (now, now))
            db.execute("UPDATE jobs SET state='expired',progress='等待超过查询时间预算',finished=? WHERE state IN ('preparing','queued') AND deadline<=?", (now, now))

    def claim(self, owner):
        self.recover()
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active=db.execute("SELECT count(*) FROM jobs WHERE state='running'").fetchone()[0]
            if active >= int(os.environ.get('RECIPE_GLOBAL_WORKERS','2')):
                db.commit()
                return None
            row = db.execute("SELECT id,payload,deadline,created FROM jobs WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',progress='开始查询',owner=?,lease=?,started=? WHERE id=?", (owner, now + 10, now, row[0]))
            db.commit()
        return {'id':row[0], 'payload':json.loads(row[1]), 'deadline':row[2], 'queued_seconds':now-row[3]} if row else None

    def heartbeat(self, ident, owner):
        with self.connect() as db:
            db.execute("UPDATE jobs SET lease=? WHERE id=? AND owner=? AND state='running'", (time.time()+10, ident, owner))

    def progress(self, ident, message):
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET progress=? WHERE id=? AND state='running'", (message, ident)).rowcount
            if changed:
                db.execute('INSERT INTO job_events VALUES(?,?,?)', (ident, message, time.time()))

    def finish(self, ident, state, result=None, record=None, progress=None):
        if state not in TERMINAL:
            raise ValueError('非法任务状态')
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,result=?,record=?,progress=?,finished=? WHERE id=? AND state='running'",
                       (state, json.dumps(result, ensure_ascii=False) if result else None, record, progress or '执行结束', time.time(), ident))

    def cancel(self, ident):
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET state='cancelled',progress='查询已取消',finished=? WHERE id=? AND state IN ('preparing','queued','running')", (time.time(), ident)).rowcount
        return bool(changed)

    def get(self, ident):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
            events = [dict(item) for item in db.execute('SELECT message,at FROM job_events WHERE job_id=? ORDER BY rowid', (ident,))]
        if not row:
            return None
        result = {k:row[k] for k in ('state','progress','created','started','finished','deadline','record')}
        result['events'] = events
        result['server_time'] = time.time()
        if row['result']:
            result['result'] = json.loads(row['result'])
        return result

    def metrics(self):
        with self.connect() as db:
            counts = dict(db.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall())
            waits = [r[0] for r in db.execute('SELECT started-created FROM jobs WHERE started IS NOT NULL')]
        waits.sort()
        return {'states':counts, 'capacity':self.capacity,
                'queue_p95_seconds':waits[min(len(waits)-1,int(len(waits)*.95))] if waits else None}


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def worker_loop(queue, stopped):
    owner = uuid.uuid4().hex
    while not stopped.is_set():
        job = queue.claim(owner)
        if not job:
            stopped.wait(.1)
            continue
        env = dict(os.environ, RECIPE_JOBS_DB=str(queue.path), RECIPE_JOB_ID=job['id'],
                   RECIPE_DEADLINE_EPOCH=str(job['deadline']))
        process = subprocess.Popen([sys.executable, str(ROOT/'scripts/run_recipe_job.py')],
                                   env=env, cwd=ROOT, start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while process.poll() is None:
            snapshot = queue.get(job['id'])
            if stopped.is_set() or snapshot['state'] == 'cancelled' or time.time() >= job['deadline']:
                stop_process(process)
                queue.finish(job['id'], 'expired' if time.time() >= job['deadline'] else 'failed',
                             progress='查询时间预算已用尽' if time.time() >= job['deadline'] else '服务停止，可重新提交')
                break
            queue.heartbeat(job['id'], owner)
            stopped.wait(.1)
        if queue.get(job['id'])['state'] == 'running':
            queue.finish(job['id'], 'failed', progress='执行进程未正常返回，可查看已保存的阶段结果')
        RuntimeStore().release_job(job['id'])


class WorkerPool:
    def __init__(self, queue, count=2):
        if count<1:raise ValueError('worker 数量必须为正')
        self.stopped = threading.Event()
        self.threads = [threading.Thread(target=worker_loop, args=(queue,self.stopped), daemon=True) for _ in range(count)]
        for thread in self.threads:
            thread.start()
    def close(self):
        self.stopped.set()
        for thread in self.threads:
            thread.join(timeout=3)


if __name__ == '__main__':
    def stop_signal(*args):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop_signal)
    queue = JobQueue()
    pool = WorkerPool(queue, int(os.environ.get('RECIPE_WORKERS','2')))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pool.close()
