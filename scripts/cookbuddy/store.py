import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg import sql
from check_embedding import ROOT
from .models import settings


def valid_id(value):
    return isinstance(value,str) and bool(re.fullmatch('[a-f0-9]{32}',value))


class Store:
    def __init__(self):self.dsn=settings().get('COOKBUDDY_DATABASE_URL','')
    @contextmanager
    def connect(self,user_id=None,admin=False):
        if not self.dsn:raise RuntimeError('cookbuddy_database_not_configured')
        with psycopg.connect(settings().get('COOKBUDDY_ADMIN_DATABASE_URL',self.dsn) if admin else self.dsn,connect_timeout=3,row_factory=dict_row) as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            if user_id:
                if not valid_id(user_id):raise ValueError('invalid_user_id')
                db.execute("SELECT set_config('cookbuddy.user_id',%s,true)",(user_id,))
            yield db
    def migrate(self):
        with self.connect(admin=True) as db:
            db.execute("SELECT pg_advisory_xact_lock(20261014)")
            for migration in sorted((ROOT/'migrations').glob('*.sql')):db.execute(migration.read_text())
            if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='cookbuddy_app'").fetchone():
                password=settings()['COOKBUDDY_DB_PASSWORD']
                db.execute(sql.SQL('CREATE ROLE cookbuddy_app LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS').format(sql.Literal(password)))
            db.execute('GRANT USAGE ON SCHEMA cookbuddy TO cookbuddy_app')
            db.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA cookbuddy TO cookbuddy_app')
            db.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA cookbuddy TO cookbuddy_app')
    def ensure_user(self,uid):
        with self.connect(uid) as db:
            db.execute('INSERT INTO cookbuddy.tenants(id) VALUES(%s) ON CONFLICT DO NOTHING',(uid,))
    def conversations(self,uid):
        with self.connect(uid) as db:
            return db.execute('SELECT id,title,updated_at::text FROM cookbuddy.conversations ORDER BY updated_at DESC LIMIT 100').fetchall()
    def conversation(self,uid,cid):
        if not valid_id(cid):raise ValueError('invalid_conversation')
        with self.connect(uid) as db:
            conversation=db.execute('SELECT id,title FROM cookbuddy.conversations WHERE id=%s',(cid,)).fetchone()
            if not conversation:raise ValueError('conversation_not_found')
            messages=db.execute('SELECT role,content,record,created_at::text FROM cookbuddy.messages WHERE conversation_id=%s ORDER BY id',(cid,)).fetchall()
            return {**conversation,'messages':messages}
    def create_conversation(self,uid,title):
        cid=uuid.uuid4().hex
        with self.connect(uid) as db:
            db.execute('INSERT INTO cookbuddy.conversations(id,user_id,title) VALUES(%s,%s,%s)',(cid,uid,title[:80]))
        return cid
    def append(self,uid,cid,job,role,content,record=None):
        with self.connect(uid) as db:
            if not db.execute('SELECT id FROM cookbuddy.conversations WHERE id=%s',(cid,)).fetchone():raise ValueError('conversation_not_found')
            db.execute('INSERT INTO cookbuddy.messages(user_id,conversation_id,role,job_id,content,record) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (uid,cid,role,job,content,Jsonb(record) if record else None))
            db.execute("UPDATE cookbuddy.conversations SET updated_at=now(),title=CASE WHEN title='新对话' AND %s='user' THEN %s ELSE title END WHERE id=%s",(role,content[:80],cid))
    def history(self,uid,cid,job):
        with self.connect(uid) as db:
            rows=db.execute('SELECT role,content FROM cookbuddy.messages WHERE conversation_id=%s AND job_id<>%s ORDER BY id DESC LIMIT 8',(cid,job)).fetchall()
        return [{'role':r['role'],'content':r['content'][:1500]} for r in reversed(rows)]
    def user_message_id(self,uid,job):
        with self.connect(uid) as db:
            row=db.execute("SELECT id FROM cookbuddy.messages WHERE job_id=%s AND role='user' ORDER BY id LIMIT 1",(job,)).fetchone()
            return row['id'] if row else None
    def session_context(self,uid,cid,job):
        from .session import SessionState,append_message,compact_assistant,options
        if not valid_id(uid) or not valid_id(cid):raise ValueError('invalid_session_identity')
        with self.connect(uid) as db:
            cutoff=db.execute("SELECT id FROM cookbuddy.messages WHERE conversation_id=%s AND job_id=%s AND role='user'",(cid,job)).fetchone()
            if not cutoff:raise ValueError('session_user_message_missing')
            row=db.execute('''SELECT state,expires_at>now() AS alive FROM cookbuddy.session_states WHERE conversation_id=%s
                AND user_message_id<%s ORDER BY user_message_id DESC LIMIT 1''',(cid,cutoff['id'])).fetchone()
            if row:
                if not row['alive']:return SessionState(uid,cid)
                state=SessionState(**row['state'])
                if state.user_id!=uid or state.session_id!=cid:raise ValueError('session_identity_mismatch')
                return state
            # Bootstrap existing chats only from recent messages. Long tool answers
            # are replaced with recipe identities, never cut into prompt evidence.
            rows=db.execute('''SELECT role,content,record FROM cookbuddy.messages WHERE conversation_id=%s
                AND id<%s AND id>coalesce((SELECT cleared_message_id FROM cookbuddy.memory_settings WHERE user_id=%s),0)
                AND created_at>now()-%s*interval '1 hour' ORDER BY id DESC LIMIT %s''',(cid,cutoff['id'],uid,options()['session_ttl_hours'],options()['window_turns']*2)).fetchall()
        state=SessionState(uid,cid)
        for row in reversed(rows):
            append_message(state,row['role'],row['content'] if row['role']=='user' else compact_assistant(row['record'] or {}))
        return state

    def save_session_context(self,uid,cid,job,state):
        from .session import options
        if not valid_id(uid) or not valid_id(cid) or state.user_id!=uid or state.session_id!=cid:
            raise ValueError('invalid_session_identity')
        with self.connect(uid) as db:
            row=db.execute("SELECT id FROM cookbuddy.messages WHERE conversation_id=%s AND job_id=%s AND role='user'",(cid,job)).fetchone()
            if not row:raise ValueError('session_user_message_missing')
            db.execute('''INSERT INTO cookbuddy.session_states(user_id,conversation_id,user_message_id,state,expires_at)
                VALUES(%s,%s,%s,%s,now()+%s*interval '1 hour') ON CONFLICT(user_id,user_message_id)
                DO UPDATE SET state=excluded.state,expires_at=excluded.expires_at''',
                (uid,cid,row['id'],Jsonb(state.record()),options()['session_ttl_hours']))
    def task_context(self,uid,cid,job):
        from .session import options
        hours=options()['session_ttl_hours']
        # A task transition is attached to its user turn, not a possibly failed
        # assistant response. The message cutoff also prevents future-turn leaks.
        with self.connect(uid) as db:
            cutoff=db.execute("SELECT id FROM cookbuddy.messages WHERE conversation_id=%s AND job_id=%s AND role='user' LIMIT 1",(cid,job)).fetchone()
            cutoff=cutoff['id'] if cutoff else 9223372036854775807
            event=db.execute('''SELECT s.state FROM cookbuddy.task_states s JOIN cookbuddy.messages m ON m.id=s.user_message_id AND m.user_id=s.user_id
                WHERE s.conversation_id=%s AND s.user_message_id<%s AND m.created_at>now()-%s*interval '1 hour'
                ORDER BY s.user_message_id DESC LIMIT 1''',(cid,cutoff,hours)).fetchone()
            if event is not None:return event['state']  # Explicit close is a boundary.
            rows=db.execute('''SELECT role,content,record FROM cookbuddy.messages WHERE conversation_id=%s AND id<%s
                AND id>coalesce((SELECT cleared_message_id FROM cookbuddy.memory_settings WHERE user_id=%s),0)
                AND created_at>now()-%s*interval '1 hour'
                ORDER BY id DESC LIMIT 200''',(cid,cutoff,uid,hours)).fetchall()
        from .task_context import valid_state,replay_task
        latest=next((r['record'] or {} for r in rows if r['role']=='assistant'),{})
        if valid_state(latest.get('task_context')):return latest['task_context']
        # Earlier explicit checkpoints without an interpreter decision remain
        # boundaries. Failed interpreted legacy turns can be reconstructed.
        if 'task_context' in latest and 'context_action' not in latest:return latest['task_context']
        return replay_task(list(reversed(rows)))

    def save_task_context(self,uid,cid,job,task):
        from .task_context import valid_state
        state=task.get('state')
        if state is not None and not valid_state(state):raise ValueError('invalid_task_state')
        decision={k:task[k] for k in ('action','boundary','parser_version','edits') if k in task}
        with self.connect(uid) as db:
            row=db.execute("SELECT id FROM cookbuddy.messages WHERE conversation_id=%s AND job_id=%s AND role='user'",(cid,job)).fetchone()
            if not row:raise ValueError('task_user_message_missing')
            db.execute('''INSERT INTO cookbuddy.task_states(user_id,conversation_id,user_message_id,job_id,state,decision)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,user_message_id)
                DO UPDATE SET state=excluded.state,decision=excluded.decision,updated_at=now()''',
                (uid,cid,row['id'],job,Jsonb(state),Jsonb(decision)))
    def save_result(self,uid,cid,job,question,record,epoch=0):
        text='\n'.join(o['result'].get('answer') or o['result'].get('short_answer','') for o in record.get('outputs',[])) or record.get('error') or '此次未返回回答。'
        self.append(uid,cid,job,'assistant',text,record)
        # The extractor queue has no tool/assistant text. Optional background
        # memory failure must never turn a successful answer into an error.
        try:
            from .ltm.profile_store import ProfileStore
            from .ltm.models import settings as memory_config
            if int(memory_config()['experiments']['level'][1])<3:return
            prefs=ProfileStore(self)
            if not prefs.enabled(uid):return
            pe=prefs.epoch(uid)
            with self.connect(uid) as db:
                db.execute('INSERT INTO cookbuddy.memory_tasks(job_id,user_id,epoch,question,record,profile_epoch) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(job,uid,epoch,question,Jsonb({'conversation_id':cid}),pe))
        except Exception:pass
    def memory(self,uid,mid):
        with self.connect(uid) as db:return db.execute('SELECT * FROM cookbuddy.memories WHERE id=%s',(mid,)).fetchone()
    def active(self,uid,kinds=None,limit=40):
        with self.connect(uid) as db:
            return db.execute("SELECT * FROM cookbuddy.memories WHERE status='active' AND (expires_at IS NULL OR expires_at>now()) AND (%s::text[] IS NULL OR kind=ANY(%s)) ORDER BY importance DESC,updated_at DESC LIMIT %s",(kinds,kinds,limit)).fetchall()
    def upsert_memory(self,uid,kind,key,content,evidence,confidence=.9,importance=.7,ttl_days=None,expected_epoch=None):
        if kind not in ('profile','task','knowledge') or not key or len(key)>120 or len(content)>1600:raise ValueError('invalid_memory')
        expires=datetime.now(timezone.utc)+timedelta(days=ttl_days) if ttl_days else None
        with self.connect(uid) as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(uid,))
            if expected_epoch is not None and db.execute('SELECT memory_epoch FROM cookbuddy.tenants WHERE id=%s',(uid,)).fetchone()['memory_epoch']!=expected_epoch:
                return None
            old=db.execute('SELECT * FROM cookbuddy.memories WHERE user_id=%s AND kind=%s AND key=%s FOR UPDATE',(uid,kind,key)).fetchone()
            # Extractors can complete out of order: order profile updates by the persisted user turn.
            if old and kind=='profile':
                previous_message=old['evidence'].get('message_id')
                incoming_message=evidence.get('message_id')
                if isinstance(previous_message,int) and (not isinstance(incoming_message,int) or incoming_message<previous_message):
                    return None
            if (old and old['content']==content and old['evidence']==evidence and old['status']=='active'
                    and (old['expires_at'] is None or old['expires_at']>datetime.now(timezone.utc))):return old['id']
            mid=old['id'] if old else uuid.uuid4().hex;version=old['version']+1 if old else 1
            previous={'content':old['content'],'status':old['status']} if old else None
            db.execute('INSERT INTO cookbuddy.memory_revisions(user_id,memory_id,version,action,previous) VALUES(%s,%s,%s,%s,%s)',(uid,mid,version,'update' if old else 'create',Jsonb(previous)))
            db.execute("INSERT INTO cookbuddy.memories(id,user_id,kind,key,content,evidence,confidence,importance,version,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,kind,key) DO UPDATE SET content=excluded.content,evidence=excluded.evidence,confidence=excluded.confidence,importance=excluded.importance,version=excluded.version,status='active',expires_at=excluded.expires_at,updated_at=now()",(mid,uid,kind,key,content,Jsonb(evidence),confidence,importance,version,expires))
            db.execute('INSERT INTO cookbuddy.outbox(user_id,memory_id,version,operation) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(uid,mid,version,'upsert'))
        return mid
    def forget(self,uid,ids=None,purge=False,status='forgotten'):
        with self.connect(uid) as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(uid,))
            rows=db.execute("SELECT id,version FROM cookbuddy.memories WHERE status='active' AND (%s::text[] IS NULL OR id=ANY(%s)) FOR UPDATE",(ids,ids)).fetchall()
            for row in rows:
                version=row['version']+1
                db.execute('UPDATE cookbuddy.memories SET status=%s,content=%s,evidence=%s,version=%s,updated_at=now() WHERE id=%s',(status,'' if purge else '[已遗忘]',Jsonb({}),version,row['id']))
                db.execute('INSERT INTO cookbuddy.outbox(user_id,memory_id,version,operation) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(uid,row['id'],version,'delete'))
                db.execute('DELETE FROM cookbuddy.memory_revisions WHERE memory_id=%s',(row['id'],))
            # Pending extraction must not resurrect content after an explicit forget.
            if status=='forgotten' and purge:
                db.execute('UPDATE cookbuddy.tenants SET memory_epoch=memory_epoch+1 WHERE id=%s',(uid,))
                db.execute("UPDATE cookbuddy.memory_tasks SET state='forgotten',question='',record='{}'::jsonb,extraction_progress='{}'::jsonb WHERE user_id=%s AND state IN ('pending','processing','done','failed')",(uid,))
            return len(rows)
    def registry(self):
        with self.connect() as db:return db.execute('SELECT * FROM cookbuddy.intent_registry WHERE enabled ORDER BY id').fetchall()
    def register_intent(self,ident,description,examples,target):
        if not re.fullmatch('[a-z][a-z0-9_]{1,50}',ident) or target not in ('rag','recipe','images','agent','multi_agent','clarify','general'):raise ValueError('invalid_intent_target')
        if not description or len(description)>500 or not isinstance(examples,list) or not 1<=len(examples)<=12 or any(not isinstance(e,str) or not e.strip() or len(e)>200 for e in examples):raise ValueError('invalid_intent_examples')
        with self.connect() as db:
            db.execute('INSERT INTO cookbuddy.intent_registry(id,description,examples,target) VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET description=excluded.description,examples=excluded.examples,target=excluded.target,version=cookbuddy.intent_registry.version+1,enabled=true,updated_at=now()',(ident,description,Jsonb(examples),target))
    def audit_intent(self,uid,job,decision):
        with self.connect(uid) as db:db.execute('INSERT INTO cookbuddy.intent_audit(user_id,job_id,decision) VALUES(%s,%s,%s)',(uid,job,Jsonb(decision)))

    def memory_epoch(self,uid):
        with self.connect(uid) as db:return db.execute('SELECT memory_epoch FROM cookbuddy.tenants WHERE id=%s',(uid,)).fetchone()['memory_epoch']

    def extraction_progress(self,uid,job):
        with self.connect(uid) as db:
            row=db.execute('SELECT extraction_progress FROM cookbuddy.memory_tasks WHERE job_id=%s',(job,)).fetchone()
            return row['extraction_progress'] if row else {}

    def checkpoint_extraction(self,uid,job,epoch,batch,result):
        with self.connect(uid) as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(uid,))
            if db.execute('SELECT memory_epoch FROM cookbuddy.tenants WHERE id=%s',(uid,)).fetchone()['memory_epoch']!=epoch:return False
            return bool(db.execute("UPDATE cookbuddy.memory_tasks SET extraction_progress=extraction_progress || %s WHERE job_id=%s AND epoch=%s AND state<>'forgotten'",
                (Jsonb({batch:result}),job,epoch)).rowcount)

    def update_record(self,uid,job,record):
        with self.connect(uid) as db:db.execute("UPDATE cookbuddy.messages SET record=%s WHERE job_id=%s AND role='assistant'",(Jsonb(record),job))
