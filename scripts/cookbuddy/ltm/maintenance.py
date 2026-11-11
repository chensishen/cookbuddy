"""Lifecycle tasks and clear receipts; hard preferences are never capacity-evicted."""
import json
import math
import uuid
from datetime import datetime,timedelta
from .models import stamp,now,effective,PrefItem,settings,identity
from .episodic_store import EpisodicStore,MemoryVectors


def maintain(prefs,uid,at=None,compression=False,reconcile=False):
    identity(uid);at=at or now();cfg=settings()['long_term'];counts={'archived':0,'expired_events':0,'evicted':0,'compressed':0,'deduplicated':0,'orphaned_vectors':0,'missing_vectors':0}
    if not prefs.enabled(uid):return counts
    with prefs.connect(uid) as db:
        if not prefs.path:
            db.execute('DELETE FROM session_states WHERE user_id=? AND expires_at<=?',(uid,stamp(at)))
            hours=settings()['short_term']['session_ttl_hours']
            db.execute('DELETE FROM task_states WHERE user_id=? AND user_message_id IN (SELECT id FROM messages WHERE user_id=? AND created_at<?)',(uid,uid,stamp(at-timedelta(hours=hours))))
        rows=db.execute("SELECT * FROM user_prefs WHERE user_id=? AND status IN ('active','pending')",(uid,)).fetchall()
        for row in rows:
            p=PrefItem(**dict(row));expired=bool(p.valid_to and p.valid_to<=stamp(at))
            stale_pending=p.status=='pending' and p.created_at<stamp(at-timedelta(days=30))
            decayed=p.strength!='hard' and effective(p,at)<cfg['archive_conf_threshold']
            if expired or stale_pending or decayed:
                db.execute("UPDATE user_prefs SET status='archived' WHERE user_id=? AND id=?",(uid,p.id));counts['archived']+=1
        db.execute('DELETE FROM pending_confirmations WHERE user_id=? AND (expires_at<=? OR turns_left<=0)',(uid,stamp(at)))
        events=[dict(r) for r in db.execute("SELECT * FROM episodic_memory WHERE user_id=? AND status='active'",(uid,)).fetchall()]
        expired=[r for r in events if r['expire_at'] and r['expire_at']<=stamp(at)]
        active=[r for r in events if r not in expired]
        def utility(r):return r['importance']*math.exp(-max(0,(at-datetime.fromisoformat(r['created_at'])).total_seconds()/86400)/30)
        evicted=sorted(active,key=utility)[:max(0,len(active)-cfg['max_events_per_user'])]
        for r in expired+evicted:
            db.execute("UPDATE episodic_memory SET status='archived',version=version+1 WHERE user_id=? AND mem_id=?",(uid,r['mem_id']))
            db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,r['mem_id'],stamp()))
        counts['expired_events']=len(expired);counts['evicted']=len(evicted)
    if compression:
        counts['deduplicated']=deduplicate_events(prefs,uid)
        groups={}
        for r in EpisodicStore(prefs).rows(uid):
            if r['type']=='summary' or r.get('compressed_into'):continue
            groups.setdefault((r['type'],r['dish_refs']),[]).append(r)
        for (kind,dishes),group in groups.items():
            if len(group)<5:continue
            digest=__import__('hashlib').sha256(','.join(sorted(r['mem_id'] for r in group)).encode()).hexdigest()
            facts=list(dict.fromkeys(r['text'] for r in sorted(group,key=lambda r:(r['importance'],r['last_accessed']),reverse=True)))
            text='历史摘要（'+str(len(group))+'条记录）：'
            for fact in facts:
                if len(text)+len(fact)+1<=600:text+=fact+'；'
            mid=EpisodicStore(prefs).write(uid,{'text':text,'type':'summary','dish_refs':json.loads(dishes),'importance':max(r['importance'] for r in group)},'compression:'+digest)
            if mid:
                with prefs.connect(uid) as db:
                    for r in group:
                        db.execute("UPDATE episodic_memory SET importance=importance*0.5,compressed_into=? WHERE user_id=? AND mem_id=?",(mid,uid,r['mem_id']))
                        db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,r['mem_id'],stamp()))
                counts['compressed']+=1
    if reconcile:
        vectors=MemoryVectors();vectors.ensure();remote={r['mem_id']:r for r in vectors.ids(uid)}
        local={r['mem_id']:r for r in EpisodicStore(prefs).rows(uid)}
        for mid in remote.keys()-local.keys():vectors.delete(uid,mid);counts['orphaned_vectors']+=1
        with prefs.connect(uid) as db:
            for mid in local.keys()-remote.keys():
                db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,mid,stamp()));counts['missing_vectors']+=1
    return counts


def clear_user(prefs,uid,vectors=None):
    """A failed Milvus deletion keeps a durable deletion job and explicit receipt."""
    identity(uid);counts={}
    with prefs.connect(uid) as db:
        for key,table in [('preferences','user_prefs'),('events','episodic_memory')]:
            counts[key]=db.execute('SELECT count(*) AS n FROM '+table+' WHERE user_id=?',(uid,)).fetchone()['n']
        ids=[r['mem_id'] for r in db.execute('SELECT mem_id FROM episodic_memory WHERE user_id=?',(uid,)).fetchall()]
        for table in ('pending_confirmations','memory_events','user_prefs','episodic_memory','memory_session_flush'):
            db.execute('DELETE FROM '+table+' WHERE user_id=?',(uid,))
        db.execute('INSERT INTO memory_settings(user_id,enabled,epoch) VALUES(?,1,1) ON CONFLICT(user_id) DO UPDATE SET epoch=memory_settings.epoch+1',(uid,))
        for mid in ids:db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,mid,stamp()))
        if not prefs.path:
            db.execute('INSERT INTO memory_purge_outbox(user_id) VALUES(?) ON CONFLICT DO NOTHING',(uid,))
            # Invalidate in-flight legacy jobs and all short-term checkpoints.
            cutoff=db.execute("SELECT coalesce(max(id),0) AS n FROM messages WHERE user_id=?",(uid,)).fetchone()['n']
            db.execute('UPDATE memory_settings SET cleared_message_id=? WHERE user_id=?',(cutoff,uid))
            db.execute('DELETE FROM session_states WHERE user_id=?',(uid,))
            db.execute('DELETE FROM task_states WHERE user_id=?',(uid,))
            db.execute("UPDATE tenants SET memory_epoch=memory_epoch+1 WHERE id=?",(uid,))
            db.execute("UPDATE memory_tasks SET state='forgotten',question='',record='{}',extraction_progress='{}' WHERE user_id=?",(uid,))
            for table in ('memories','memory_revisions','outbox'):
                db.execute('DELETE FROM '+table+' WHERE user_id=?',(uid,))
    synced=True
    try:
        with prefs.connect(uid) as db:
            vectors=vectors or MemoryVectors();vectors.ensure();vectors.delete(uid)
            db.execute('DELETE FROM memory_vector_outbox WHERE user_id=?',(uid,))
            if not prefs.path:
                from ..vectors import VectorStore
                legacy=VectorStore();legacy.ensure()
                legacy.call('entities/delete',{'collectionName':legacy.collection,'filter':'user_id == '+json.dumps(uid)})
                db.execute('DELETE FROM memory_purge_outbox WHERE user_id=?',(uid,))
            # New post-clear activity may have committed between the two locks.
            for row in db.execute("SELECT mem_id FROM episodic_memory WHERE user_id=? AND status='active'",(uid,)).fetchall():
                db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,row['mem_id'],stamp()))
    except Exception:synced=False
    return {**counts,'cleared':True,'vector_deletion':'complete' if synced else 'pending_retry','cache_cleared':True}


def retry_purge(prefs,uid):
    if prefs.path:return False
    with prefs.connect(uid) as db:
        pending=db.execute('SELECT user_id FROM memory_purge_outbox WHERE user_id=?',(uid,)).fetchone()
        if not pending:return False
        # Lock the same actor during projection deletion so a fresh write cannot
        # be indexed and then deleted by an older clear operation.
        vectors=MemoryVectors();vectors.ensure();vectors.delete(uid)
        from ..vectors import VectorStore
        legacy=VectorStore();legacy.ensure()
        legacy.call('entities/delete',{'collectionName':legacy.collection,'filter':'user_id == '+json.dumps(uid)})
        for row in db.execute("SELECT mem_id FROM episodic_memory WHERE user_id=? AND status='active'",(uid,)).fetchall():
            db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,row['mem_id'],stamp()))
        db.execute('DELETE FROM memory_purge_outbox WHERE user_id=?',(uid,))
    return True


def flush_idle_sessions(store,user_ids=None):
    """Only already answered user turns, via the same idempotent durable queue."""
    minutes=settings()['short_term']['idle_flush_minutes'];count=0
    with store.connect(admin=True) as db:
        users=(db.execute('SELECT id FROM cookbuddy.tenants WHERE id=ANY(%s)',(user_ids,)).fetchall()
               if user_ids is not None else db.execute('SELECT id FROM cookbuddy.tenants').fetchall())
    for user in users:
        uid=user['id']
        from .profile_store import ProfileStore
        prefs=ProfileStore(store)
        if not prefs.enabled(uid):continue
        with store.connect(uid) as db:
            chats=db.execute("""SELECT m.conversation_id,max(m.id) AS last_id FROM cookbuddy.messages m
                JOIN cookbuddy.conversations c ON c.id=m.conversation_id AND c.user_id=m.user_id
                WHERE m.role='user' AND c.updated_at<now()-%s*interval '1 minute'
                AND m.id>coalesce((SELECT cleared_message_id FROM cookbuddy.memory_settings WHERE user_id=m.user_id),0)
                GROUP BY m.conversation_id""",(minutes,)).fetchall()
            for chat in chats:
                flushed=db.execute('SELECT last_message_id FROM cookbuddy.memory_session_flush WHERE user_id=%s AND session_id=%s',(uid,chat['conversation_id'])).fetchone()
                if flushed and flushed['last_message_id']>=chat['last_id']:continue
                rows=db.execute("""SELECT m.job_id,m.content FROM cookbuddy.messages m WHERE m.conversation_id=%s AND m.role='user'
                    AND m.id<=%s AND EXISTS(SELECT 1 FROM cookbuddy.messages a WHERE a.job_id=m.job_id AND a.role='assistant')""",(chat['conversation_id'],chat['last_id'])).fetchall()
                for row in rows:
                    db.execute("""INSERT INTO cookbuddy.memory_tasks(job_id,user_id,epoch,question,record,profile_epoch)
                        VALUES(%s,%s,%s,%s,'{}',%s) ON CONFLICT DO NOTHING""",(row['job_id'],uid,store.memory_epoch(uid),row['content'],prefs.epoch(uid)));count+=1
                db.execute('''INSERT INTO cookbuddy.memory_session_flush(user_id,session_id,last_message_id,flushed_at) VALUES(%s,%s,%s,%s)
                    ON CONFLICT(user_id,session_id) DO UPDATE SET last_message_id=excluded.last_message_id,flushed_at=excluded.flushed_at''',
                    (uid,chat['conversation_id'],chat['last_id'],stamp()))
    return count


def reconcile_orphan_owners(store):
    with store.connect(admin=True) as db:
        known={r['id'] for r in db.execute('SELECT id FROM cookbuddy.tenants').fetchall()}
    vectors=MemoryVectors();vectors.ensure();removed=0
    for row in vectors.all_owners():
        if row['user_id'] not in known:
            vectors.delete(row['user_id'],row['mem_id']);removed+=1
    return removed


def migrate_legacy_profiles(prefs,uid):
    """Only literal, persisted user evidence; no old assistant/source knowledge."""
    if prefs.path or not prefs.enabled(uid) or prefs.epoch(uid)>0:return 0
    with prefs.connect(uid) as db:
        # Never overwrite any preference already written by the new pipeline.
        existing=db.execute('SELECT source_turn_ref FROM user_prefs WHERE user_id=?',(uid,)).fetchall()
        if any(not r['source_turn_ref'].startswith('legacy:') for r in existing):return 0
        rows=db.execute("SELECT evidence FROM memories WHERE user_id=? AND kind='profile' AND status='active'",(uid,)).fetchall()
        quotes=[]
        for row in rows:
            proof=row['evidence']
            if proof.get('origin')!='user_message' or type(proof.get('message_id')) is not int:continue
            message=db.execute("SELECT conversation_id,content,created_at FROM messages WHERE user_id=? AND id=? AND role='user'",(uid,proof['message_id'])).fetchone()
            if not message or not proof.get('quote') or proof['quote'] not in message['content']:continue
            quotes.append((message['conversation_id'],proof['message_id'],proof['quote'],message['created_at']))
    from .extractor import rule_extract,anchor_expiry
    count=0
    for sid,mid,quote,created_at in sorted(quotes,key=lambda row:row[1]):
        for candidate in rule_extract(quote):
            if candidate.scope=='session' or candidate.op=='revoke':continue
            candidate=anchor_expiry(candidate,created_at)
            decision=prefs.apply(uid,sid,'legacy:'+str(mid),candidate,expected_epoch=0)
            count+=decision['decision'] in {'ADD','UPDATE'}
    return count


def deduplicate_events(prefs,uid):
    from ..models import cosine
    rows=EpisodicStore(prefs).rows(uid);kept=[];merged=0
    with prefs.connect(uid) as db:
        for row in sorted(rows,key=lambda r:r['created_at']):
            duplicate=next((r for r in kept if r['type']==row['type'] and r['dish_refs']==row['dish_refs'] and
                (row['type']!='feedback' or row['text']==r['text']) and row['vector'] and r['vector'] and
                cosine(json.loads(row['vector']),json.loads(r['vector']))>.9),None)
            if duplicate:
                db.execute('UPDATE episodic_memory SET importance=?,last_accessed=? WHERE user_id=? AND mem_id=?',
                    (max(row['importance'],duplicate['importance']),max(row['last_accessed'],duplicate['last_accessed']),uid,duplicate['mem_id']))
                db.execute("UPDATE episodic_memory SET status='archived',version=version+1 WHERE user_id=? AND mem_id=?",(uid,row['mem_id']))
                for mid in (row['mem_id'],duplicate['mem_id']):
                    db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,mid,stamp()))
                merged+=1
            else:kept.append(row)
    return merged


def end_session(store,uid,sid):
    from .profile_store import ProfileStore
    prefs=ProfileStore(store)
    if not prefs.enabled(uid):return {'queued':0,'enabled':False}
    epoch=store.memory_epoch(uid);profile_epoch=prefs.epoch(uid)
    with store.connect(uid) as db:
        if not db.execute('SELECT id FROM cookbuddy.conversations WHERE id=%s',(sid,)).fetchone():raise KeyError('conversation_not_found')
        rows=db.execute("""SELECT m.id,m.job_id,m.content FROM cookbuddy.messages m WHERE m.conversation_id=%s AND m.role='user'
            AND m.id>coalesce((SELECT cleared_message_id FROM cookbuddy.memory_settings WHERE user_id=%s),0)
            AND EXISTS(SELECT 1 FROM cookbuddy.messages a WHERE a.job_id=m.job_id AND a.role='assistant')""",(sid,uid)).fetchall()
        queued=0
        for row in rows:
            result=db.execute("""INSERT INTO cookbuddy.memory_tasks(job_id,user_id,epoch,question,record,profile_epoch)
                VALUES(%s,%s,%s,%s,'{}',%s) ON CONFLICT DO NOTHING""",(row['job_id'],uid,epoch,row['content'],profile_epoch))
            queued+=result.rowcount
    return {'queued':queued,'enabled':True}
