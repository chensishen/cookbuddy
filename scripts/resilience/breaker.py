"""All transitions run in RuntimeStore's BEGIN IMMEDIATE transaction."""
import json
import logging
import os
from .config import settings
from .failure import DEPENDENCY_FAILURES

LOGGER=logging.getLogger('cookbuddy.resilience')


def schema(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS breaker_samples(scope TEXT,created REAL,category TEXT);
        CREATE INDEX IF NOT EXISTS breaker_window ON breaker_samples(scope,created);
        CREATE TABLE IF NOT EXISTS breaker_transitions(created REAL,dependency TEXT,old_state TEXT,new_state TEXT,
            reason TEXT,counts TEXT,failure_ratio REAL,request_id TEXT);
        CREATE TABLE IF NOT EXISTS failure_counts(dependency TEXT,category TEXT,count INTEGER,
            PRIMARY KEY(dependency,category));
        CREATE TABLE IF NOT EXISTS request_outcomes(request_id TEXT PRIMARY KEY,created REAL,degraded INTEGER);''')
    db.execute('BEGIN IMMEDIATE')
    columns={r[1] for r in db.execute('PRAGMA table_info(circuits)')}
    for name,kind in [('state',"TEXT DEFAULT 'closed'"),('backoff','INTEGER DEFAULT 0'),
                      ('successes','INTEGER DEFAULT 0'),('open_count','INTEGER DEFAULT 0'),
                      ('total_open','REAL DEFAULT 0'),('state_since','REAL DEFAULT 0')]:
        if name not in columns:db.execute('ALTER TABLE circuits ADD COLUMN '+name+' '+kind)
    db.commit()


def policy(threshold=None,cooldown=None):
    p=dict(settings()['breaker'])
    if threshold is not None:p['min_requests']=threshold
    if cooldown is not None:p['initial_open_seconds']=cooldown
    return p


def transition(db,scope,old,new,reason,now):
    rows=db.execute('SELECT category,count(*) FROM breaker_samples WHERE scope=? AND created>=? GROUP BY category',
                    (scope,now-settings()['breaker']['window_seconds'])).fetchall()
    counts=dict(rows);total=sum(counts.values());bad=sum(counts.get(k,0) for k in DEPENDENCY_FAILURES)
    item={'time':now,'dependency':scope.split(':',1)[0],'old_state':old,'new_state':new,
          'reason':reason,'counts':counts,'failure_ratio':bad/total if total else 0,'request_id':os.environ.get('RECIPE_JOB_ID','')}
    db.execute('INSERT INTO breaker_transitions VALUES(?,?,?,?,?,?,?,?)',
               (now,item['dependency'],old,new,reason,json.dumps(counts),item['failure_ratio'],item['request_id']))
    db.execute('UPDATE circuits SET state=?,state_since=? WHERE scope=?',(new,now,scope))
    LOGGER.info(json.dumps(item))


def admit(db,scope,now,p):
    if not p['enabled']:return True
    state=db.execute('SELECT state,opened,probe,backoff FROM circuits WHERE scope=?',(scope,)).fetchone()
    if not state:
        db.execute('INSERT INTO circuits(scope,failures,opened,probe) VALUES(?,0,0,NULL)',(scope,));return True
    name,opened,probe,backoff=state
    if name=='open':
        wait=min(p['max_open_seconds'],p['initial_open_seconds']*2**min(backoff,10))
        if now<opened+wait:return False
    if name=='half_open':
        if probe and db.execute('SELECT 1 FROM leases WHERE id=?',(probe,)).fetchone():return None
        # Only set the probe after quota admission succeeds.
    return True


def activate(db,scope,now,ident):
    row=db.execute('SELECT state,opened FROM circuits WHERE scope=?',(scope,)).fetchone()
    if row is None:return  # Debug-disabled policies do not create a circuit.
    state,opened=row
    if state=='open':
        transition(db,scope,'open','half_open','probe_due',now)
        db.execute('UPDATE circuits SET total_open=total_open+?,successes=0 WHERE scope=?',(now-opened,scope))
    if state in ('open','half_open'):db.execute('UPDATE circuits SET probe=? WHERE scope=?',(ident,scope))


def finish(db,scope,now,category,p,ident):
    dep=scope.split(':',1)[0]
    db.execute('INSERT INTO failure_counts VALUES(?,?,1) ON CONFLICT(dependency,category) DO UPDATE SET count=count+1',(dep,category))
    if not p['enabled']:return
    row=db.execute('SELECT state,probe,backoff,successes FROM circuits WHERE scope=?',(scope,)).fetchone()
    if row is None:return
    state,probe,backoff,successes=row
    if category not in DEPENDENCY_FAILURES|{'ok'}:
        db.execute('UPDATE circuits SET probe=NULL WHERE scope=? AND probe=?',(scope,ident));return
    db.execute('DELETE FROM breaker_samples WHERE scope=? AND created<?',(scope,now-p['window_seconds']))
    db.execute('INSERT INTO breaker_samples VALUES(?,?,?)',(scope,now,category))
    db.execute('UPDATE circuits SET failures=failures+? WHERE scope=?',(int(category!='ok'),scope))
    if state=='half_open' and probe==ident:
        if category=='ok' and successes+1>=p['half_open_successes']:
            transition(db,scope,state,'closed','probe_success',now)
            db.execute('UPDATE circuits SET failures=0,opened=0,probe=NULL,backoff=0,successes=0 WHERE scope=?',(scope,))
            db.execute('DELETE FROM breaker_samples WHERE scope=?',(scope,))
        elif category=='ok':db.execute('UPDATE circuits SET successes=successes+1,probe=NULL WHERE scope=?',(scope,))
        else:
            transition(db,scope,state,'open',category,now)
            db.execute('UPDATE circuits SET opened=?,probe=NULL,backoff=backoff+1,open_count=open_count+1 WHERE scope=?',(now,scope))
        return
    if state!='closed':return  # A late successful request cannot close an open circuit.
    total,bad=db.execute("SELECT count(*),sum(category!='ok') FROM breaker_samples WHERE scope=?",(scope,)).fetchone()
    if total>=p['min_requests'] and bad/total>=p['failure_ratio']:
        transition(db,scope,state,'open',category,now)
        db.execute('UPDATE circuits SET opened=?,probe=NULL,open_count=open_count+1 WHERE scope=?',(now,scope))
