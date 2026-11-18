"""SQLite development / PostgreSQL production, user-scoped transactions and audit."""
import json
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict,replace
from datetime import timedelta,datetime
from pathlib import Path
from .models import identity,PrefItem,Candidate,Decision,item,stamp,now,effective,settings,SESSION_ONLY,SINGLE
from .resolver import resolve,conflicting_values

class DB:
    def __init__(self,db,pg):self.db=db;self.pg=pg
    def execute(self,sql,args=()):return self.db.execute(sql.replace('?', '%s') if self.pg else sql,args)


def effective_taste_rows(rows):
    """Use the latest explicit default when light flavor and heat were saved separately."""
    candidates=[row for row in rows if (row.dimension=='flavor' and row.value=='清淡')
                or (row.dimension=='spice_level' and row.value in {'1','2','3'})]
    if len({row.dimension for row in candidates})<2:return rows
    defaults=[row for row in candidates if row.source=='explicit' and row.strength=='soft' and row.polarity=='like']
    if len({row.dimension for row in defaults})<2:return rows
    latest=max(defaults,key=lambda row:max(row.updated_at,row.last_confirmed_at or row.updated_at))
    return [row for row in rows if row not in defaults or row.dimension==latest.dimension
            or (latest.source_turn_ref and row.source_turn_ref==latest.source_turn_ref)]


class ProfileStore:
    def __init__(self,legacy=None,path=None):
        self.legacy=legacy;self.path=str(path) if path is not None else None
        if self.path:
            if self.path==':memory:':raise ValueError('use_temporary_database_file')
            with self.connect('schema') as db:
                for statement in (Path(__file__).parent/'schema.sql').read_text().split(';'):
                    if statement.strip():db.execute(statement)
        elif legacy is None:
            from ..store import Store
            self.legacy=Store()
    @contextmanager
    def connect(self,uid):
        identity(uid)
        if self.path:
            db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
            try:
                db.execute('BEGIN IMMEDIATE');yield DB(db,False);db.commit()
            except BaseException:db.rollback();raise
            finally:db.close()
        else:
            with self.legacy.connect(uid) as db:
                db.execute('SET LOCAL search_path=cookbuddy,public')
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(uid,))
                yield DB(db,True)
    def enabled(self,uid):
        with self.connect(uid) as db:
            row=db.execute('SELECT enabled FROM memory_settings WHERE user_id=?',(uid,)).fetchone()
            return not row or bool(row['enabled'])
    def epoch(self,uid):
        with self.connect(uid) as db:
            row=db.execute('SELECT epoch FROM memory_settings WHERE user_id=?',(uid,)).fetchone()
            return row['epoch'] if row else 0
    def set_enabled(self,uid,enabled):
        if type(enabled) is not bool:raise ValueError('enabled_must_be_boolean')
        with self.connect(uid) as db:
            db.execute('INSERT INTO memory_settings(user_id,enabled,epoch) VALUES(?,?,1) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,epoch=memory_settings.epoch+1',(uid,int(enabled)))
    def prefs(self,uid,statuses=('active','pending'),include_expired=False):
        with self.connect(uid) as db:
            rows=db.execute('SELECT * FROM user_prefs WHERE user_id=? ORDER BY created_at,id',(uid,)).fetchall()
        return [PrefItem(**dict(r)) for r in rows if r['status'] in statuses and
            (include_expired or not r['valid_to'] or r['valid_to']>stamp())]
    def load_profile(self,uid):
        if not self.enabled(uid):return {'hard_constraints':[],'soft_prefs':[]}
        active=effective_taste_rows([p for p in self.prefs(uid,('active',)) if p.dimension not in SESSION_ONLY])
        return {'hard_constraints':[asdict(p) for p in active if p.strength=='hard'],
                'soft_prefs':[{**asdict(p),'effective_confidence':effective(p)} for p in active if p.strength=='soft' and effective(p)>=.2]}
    def _insert(self,db,p):
        data=asdict(p);keys=list(data)
        db.execute('INSERT INTO user_prefs('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',tuple(data[k] for k in keys))
    def apply(self,uid,sid,turn,c,expected_epoch=None,expected_version=None,target_id=None,*,direct_food_forget=False):
        identity(uid);identity(sid);identity(turn)
        if c.dimension=='inventory' and target_id:raise ValueError('inventory_is_session_memory')
        # Re-extraction recomputes absolute expiry times. They and probabilistic
        # scores must not turn a repeated source turn into a second write.
        signature={k:getattr(c,k) for k in ('dimension','value','polarity','op')}
        digest=hashlib.sha256(json.dumps(signature,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        key=turn+':'+digest
        with self.connect(uid) as db:
            setting=db.execute('SELECT enabled,epoch FROM memory_settings WHERE user_id=?',(uid,)).fetchone()
            if setting and (not setting['enabled'] or expected_epoch is not None and setting['epoch']!=expected_epoch):
                return {'decision':'REJECT','message':'记忆已关闭或任务已失效','needs_confirmation':False}
            if expected_epoch not in (None,0) and not setting:return {'decision':'REJECT','message':'任务已失效','needs_confirmation':False}
            if c.dimension=='inventory':
                return {'decision':'SESSION_ONLY','message':'现有食材仅在当前会话保留，不写入长期画像。','needs_confirmation':False}
            if c.dimension in SESSION_ONLY and c.op=='set':
                return {'decision':'SESSION_ONLY','message':'难度、人数和时间条件仅在当前会话使用，不写入长期画像。','needs_confirmation':False}
            oldevent=db.execute('SELECT decision,pref_id FROM memory_events WHERE user_id=? AND idem_key=?',(uid,key)).fetchone()
            if oldevent:return {'decision':oldevent['decision'],'id':oldevent['pref_id'],'needs_confirmation':oldevent['decision']=='ASK','message':'已处理'}
            db.execute("UPDATE user_prefs SET status='archived' WHERE user_id=? AND status IN ('active','pending') AND valid_to IS NOT NULL AND valid_to<=?",(uid,stamp()))
            rows=[PrefItem(**dict(r)) for r in db.execute("SELECT * FROM user_prefs WHERE user_id=? AND status IN ('active','pending')",(uid,)).fetchall()
                if not r['valid_to'] or r['valid_to']>stamp()]
            if expected_epoch is not None and c.op=='set' and c.dimension in SINGLE and any(
                    p.dimension==c.dimension and p.status=='active' and p.source=='explicit'
                    and p.source_turn_ref==turn and p.value!=c.value for p in rows):
                return {'decision':'NOOP','message':'已保留本轮明确保存的偏好','needs_confirmation':False}
            # Evidence accumulation is committed in the same transaction as resolve.
            rows.sort(key=lambda p:p.status!='active')
            same=next((p for p in rows if p.dimension==c.dimension and p.value==c.value and p.polarity==c.polarity),None)
            if same and c.op=='set' and c.scope!='session':
                sessions=db.execute('SELECT source_turn_ref FROM memory_events WHERE user_id=? AND pref_id=?',(uid,same.id)).fetchall()
                prior_sessions={r['source_turn_ref'].split(':',1)[0] for r in sessions if r['source_turn_ref']}
                same.evidence_count+=1;same.session_count+=int(sid not in prior_sessions)
            decision=resolve(c,rows)
            incoming=turn.rsplit(':',1)[-1]
            relevant=[p for p in rows if p.dimension==c.dimension and (conflicting_values(c.dimension,c.value,p.value) or p.value==c.value)]
            if incoming.isdigit() and any(p.source_turn_ref.rsplit(':',1)[-1].isdigit() and int(p.source_turn_ref.rsplit(':',1)[-1])>int(incoming) for p in relevant):
                decision=Decision('NOOP',reason='stale_user_turn')
            if target_id:
                forced=next((p for p in rows if p.id==target_id),None)
                if not forced:raise KeyError('memory_not_found')
                if c.dimension!=forced.dimension:raise ValueError('dimension_cannot_change')
                if c.op=='revoke' and forced.status=='pending':
                    if forced.strength=='hard':
                        from .resolver import ask
                        decision=ask('relax_hard',forced,c)
                    else:decision=Decision('DELETE',forced.id,reason='delete_pending')
                if c.op=='set' and (forced.value!=c.value or forced.polarity!=c.polarity or forced.strength!=c.strength):
                    if any(p.id!=forced.id and p.status=='active' and p.dimension==c.dimension and p.value==c.value for p in rows):raise ValueError('memory_value_already_active')
                    if forced.strength=='hard':
                        from .resolver import ask
                        decision=ask('relax_hard',forced,c)
                    else:decision=Decision('UPDATE',forced.id,item(c,previous=forced),reason='management_update')
            target=next((p for p in rows if p.id==decision.target_id),None)
            forget_now=(direct_food_forget and decision.action=='ASK' and c.op=='revoke' and c.source=='explicit'
                        and target is not None and target.id==target_id and target.status=='active'
                        and target.dimension=='avoid_ingredient' and target.version==expected_version)
            if forget_now:
                decision=Decision('DELETE',target.id,reason='explicit_food_preference_forget')
            if expected_version is not None and (not target or target.version!=expected_version):raise ValueError('memory_version_conflict')
            if decision.action=='NOOP' and target and c.op=='set':
                alpha=settings()['long_term']['strength_alpha']
                strength=alpha*c.confidence+(1-alpha)*target.strength_score
                db.execute('UPDATE user_prefs SET evidence_count=?,session_count=?,last_confirmed_at=?,strength_score=? WHERE user_id=? AND id=? AND version=?',
                    (target.evidence_count,target.session_count,stamp(),strength,uid,target.id,target.version))
            elif decision.action in {'UPDATE','DELETE'} and target:
                changed=db.execute('UPDATE user_prefs SET status=?,updated_at=? WHERE user_id=? AND id=? AND version=? AND status=?',
                    ('deleted' if decision.action=='DELETE' else 'superseded',stamp(),uid,target.id,target.version,target.status))
                if changed.rowcount!=1:raise ValueError('memory_version_conflict')
                if forget_now:
                    # Invalidate queued extraction from before the deletion, as for confirmed removals.
                    db.execute('INSERT INTO memory_settings(user_id,enabled,epoch) VALUES(?,1,1) ON CONFLICT(user_id) DO UPDATE SET epoch=memory_settings.epoch+1',(uid,))
                    pending=db.execute('SELECT id,payload_json FROM pending_confirmations WHERE user_id=?',(uid,)).fetchall()
                    for row in pending:
                        if json.loads(row['payload_json']).get('target_id')==target.id:
                            db.execute('DELETE FROM pending_confirmations WHERE user_id=? AND id=?',(uid,row['id']))
            new=decision.new_item
            if new:
                new=replace(new,user_id=uid,source_turn_ref=turn)
                if new.supersedes is None:
                    previous=db.execute("SELECT * FROM user_prefs WHERE user_id=? AND dimension=? AND (value=? OR dimension IN ('spice_level','diet_type','time_budget','max_difficulty','household_size')) ORDER BY created_at DESC,version DESC LIMIT 1",(uid,c.dimension,c.value)).fetchone()
                    if previous:new=replace(new,supersedes=previous['id'],version=previous['version']+1)
                if same and decision.reason=='promote':new=replace(new,evidence_count=same.evidence_count,session_count=same.session_count)
                self._insert(db,new)
            if decision.action=='ASK':
                payload={**decision.ask['payload'],'candidate':asdict(c)}
                payload['candidate']['evidence_text']=''
                # Stable dedup across repeat requests while confirmation is pending.
                existing=db.execute('SELECT id FROM pending_confirmations WHERE user_id=? AND session_id=? AND payload_json=? AND expires_at>?',
                    (uid,sid,json.dumps(payload,sort_keys=True),stamp())).fetchone()
                if not existing:
                    db.execute('INSERT INTO pending_confirmations(id,user_id,session_id,action,payload_json,created_at,expires_at,turns_left) VALUES(?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,uid,sid,decision.ask['action'],json.dumps(payload,sort_keys=True),stamp(),stamp(now()+timedelta(hours=settings()['confirmation']['expire_hours'])),settings()['confirmation']['expire_turns']))
            # Audit stores only a normalized dimension and provenance metadata, no quote/value.
            db.execute('INSERT INTO memory_events(id,user_id,pref_id,decision,reason,candidate_json,source_turn_ref,created_at,idem_key) VALUES(?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,uid,new.id if new else decision.target_id,decision.action,decision.reason,
                 json.dumps({'dimension':c.dimension,'strength':c.strength,'scope':c.scope,'source':c.source}),turn,stamp(),key))
        return {'decision':decision.action,'id':new.id if new else decision.target_id,'needs_confirmation':decision.action=='ASK',
                'message':'已更新记忆' if decision.action in {'ADD','UPDATE'} else '需要确认后才能移除或放宽限制' if decision.action=='ASK' else '已处理'}
    def pending(self,uid,sid):
        with self.connect(uid) as db:
            rows=db.execute('SELECT * FROM pending_confirmations WHERE user_id=? AND session_id=? AND expires_at>? AND turns_left>0 ORDER BY created_at',(uid,sid,stamp())).fetchall()
        return [dict(r) for r in rows]
    def confirm(self,uid,sid,text,target_id=None):
        import re
        q=text.strip().strip('。！! ')
        verdict='yes' if q in {'是','确认','好','可以','更新','忘掉'} else 'no' if q in {'不','不用','取消','保留','不更新'} else 'unrelated'
        pending=self.pending(uid,sid)
        if target_id:pending=[p for p in pending if json.loads(p['payload_json'])['target_id']==target_id]
        if not pending:return None
        p=pending[0];payload=json.loads(p['payload_json']);targetid=payload['target_id']
        with self.connect(uid) as db:
            target=db.execute("SELECT * FROM user_prefs WHERE user_id=? AND id=? AND version=? AND status IN ('active','pending')",(uid,targetid,payload['version'])).fetchone()
            if verdict=='unrelated' and target:
                explicit=re.fullmatch(r'(?:确认|是的?|好|可以)\s*(?:要|请)?(?:忘记|删除|移除|放宽)(.+?)[。！! ]*',q)
                if explicit and target['value'] in explicit[1]:verdict='yes'
            if verdict=='unrelated':
                db.execute('UPDATE pending_confirmations SET turns_left=turns_left-1 WHERE user_id=? AND id=?',(uid,p['id']))
                return None
            if verdict=='yes' and target:
                c=Candidate(**payload['candidate']);t=PrefItem(**dict(target))
                if p['action']=='adopt_implicit':c=replace(c,confidence=min(settings()['long_term']['promote_conf_cap'],c.confidence))
                db.execute("UPDATE user_prefs SET status=?,updated_at=? WHERE user_id=? AND id=? AND version=?",('deleted' if c.op=='revoke' else 'superseded',stamp(),uid,t.id,t.version))
                if c.op!='revoke':
                    db.execute("UPDATE user_prefs SET status='superseded' WHERE user_id=? AND dimension=? AND value=? AND status='pending'",(uid,c.dimension,c.value))
                    self._insert(db,replace(item(c,uid,t),source_turn_ref='confirmation:'+p['id']))
                # An older queued extraction must not restore the very constraint
                # the user has just explicitly confirmed removing/changing.
                db.execute('INSERT INTO memory_settings(user_id,enabled,epoch) VALUES(?,1,1) ON CONFLICT(user_id) DO UPDATE SET epoch=memory_settings.epoch+1',(uid,))
            db.execute('DELETE FROM pending_confirmations WHERE user_id=? AND id=?',(uid,p['id']))
            db.execute('INSERT INTO memory_events(id,user_id,pref_id,decision,reason,candidate_json,source_turn_ref,created_at,idem_key) VALUES(?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,uid,targetid,'UPDATE' if verdict=='yes' and target else 'NOOP','confirmed' if verdict=='yes' and target else 'retained','{}','confirmation:'+p['id'],stamp(),'confirmation:'+p['id']))
        return {'decision':'UPDATE' if verdict=='yes' and target else 'NOOP','message':'已按确认更新限制。' if verdict=='yes' and target else '已保留原来的限制。','needs_confirmation':False}
    def pending_message(self,uid,sid):
        rows=self.pending(uid,sid)
        if not rows:return ''
        p=rows[0];payload=json.loads(p['payload_json'])
        target=next((r for r in self.prefs(uid) if r.id==payload['target_id']),None)
        if not target:return ''
        desc='、'.join((target.dimension,target.value))
        if p['action']=='adopt_implicit':return f'最近的选择更偏向新的口味，要更新偏好吗？（回复“更新”或“不用”）'
        return f'你之前设置了「{desc}」。确认要移除或放宽这个限制吗？移除后可能推荐包含它的菜谱。（回复“确认”或“保留”）'
