"""PostgreSQL authoritative events plus an idempotent Milvus projection."""
import json
import math
import re
import uuid
from datetime import datetime
from dataclasses import asdict
from .models import identity,now,stamp,settings
from ..models import embed,cosine,embedding_spec

class MemoryVectors:
    def __init__(self):
        from pymilvus import MilvusClient
        from ..models import settings as service_settings
        self.client=MilvusClient(uri=service_settings().get('COOKBUDDY_MILVUS_URL','http://127.0.0.1:19532'),timeout=5)
        spec,space=embedding_spec();self.dim=int(spec['EMBEDDING_DIMENSIONS'])
        self.collection='user_memory_'+space
    def ensure(self):
        from pymilvus import DataType
        if not self.client.has_collection(self.collection):
            schema=self.client.create_schema(auto_id=False,enable_dynamic_field=False)
            for name,length in [('mem_id',64),('user_id',64),('text',2048),('type',32)]:
                schema.add_field(name,DataType.VARCHAR,max_length=length,is_primary=name=='mem_id')
            schema.add_field('vector',DataType.FLOAT_VECTOR,dim=self.dim)
            schema.add_field('dish_refs',DataType.ARRAY,element_type=DataType.VARCHAR,max_capacity=16,max_length=300)
            schema.add_field('importance',DataType.FLOAT)
            for name in ('created_at','last_accessed','expire_at','version'):schema.add_field(name,DataType.INT64)
            index=self.client.prepare_index_params()
            index.add_index(field_name='vector',index_type='HNSW',metric_type='COSINE',params={'M':16,'efConstruction':200})
            self.client.create_collection(self.collection,schema=schema,index_params=index,consistency_level='Strong')
        self.client.load_collection(self.collection)
    def _filter(self,uid):
        identity(uid)
        return 'user_id == '+json.dumps(uid)
    def upsert(self,uid,row):
        self._filter(uid)
        if row['user_id']!=uid:raise ValueError('event_owner_mismatch')
        def unix(v):return int(datetime.fromisoformat(v).timestamp()) if v else 0
        self.client.upsert(self.collection,[{'mem_id':row['mem_id'],'user_id':uid,'text':row['text'],
            'vector':json.loads(row['vector']),'type':row['type'],'dish_refs':json.loads(row['dish_refs']),
            'importance':row['importance'],'created_at':unix(row['created_at']),'last_accessed':unix(row['last_accessed']),
            'expire_at':unix(row['expire_at']),'version':row['version']}])
    def delete(self,uid,mid=None):
        expr=self._filter(uid)+(' and mem_id == '+json.dumps(mid) if mid else '')
        self.client.delete(self.collection,filter=expr)
    def search(self,uid,vector):
        expr=self._filter(uid)+' and (expire_at == 0 or expire_at > '+str(int(now().timestamp()))+')'
        return self.client.search(self.collection,data=[vector],anns_field='vector',filter=expr,limit=20,
            output_fields=['mem_id','user_id','version'],search_params={'metric_type':'COSINE','params':{'ef':64}},consistency_level='Strong')[0]
    def ids(self,uid):
        return self.client.query(self.collection,filter=self._filter(uid),output_fields=['mem_id','user_id','version'],limit=10000,consistency_level='Strong')
    def all_owners(self):
        iterator=self.client.query_iterator(self.collection,filter='user_id != ""',output_fields=['mem_id','user_id','version'],batch_size=1000,consistency_level='Strong')
        try:
            while True:
                batch=iterator.next()
                if not batch:break
                yield from batch
        finally:iterator.close()


def event_from_user(text):
    from recipe_tools import load_chunks
    from retrieval_candidates import match_titles
    dishes=match_titles(text,{r['title'] for r in load_chunks()})
    if not dishes:
        # User events can name a dish absent from this corpus. Preserve that
        # literal name instead of guessing one of several regional variants.
        named=re.search(r'(?:做的|做了|做过的|做过|煮了|炒了|收藏了?)([\u4e00-\u9fff]{2,20}?)(?=太咸|太淡|太辣|很好吃|好吃|做失败|失败了|不好吃|太油|太甜|[，,。！!]|$)',text)
        if named:dishes=[named[1]]
    if not dishes:return None
    feedback=re.search(r'太咸|太淡|太辣|很好吃|好吃|做失败|失败了|不好吃|太油|太甜',text)
    if feedback:return {'text':'用户反馈'+ '、'.join(dishes)+'：'+feedback[0]+'。','type':'feedback','dish_refs':dishes,'importance':.8}
    if re.search(r'收藏|喜欢这道',text):kind='favorite';importance=.8
    elif re.search(r'不推荐|不想再吃|不要这道',text):kind='rejected';importance=.8
    elif re.search(r'做了|做过|做的|煮了|炒了',text):kind='cooked';importance=.5
    else:return None
    return {'text':'用户'+{'favorite':'收藏','rejected':'拒绝','cooked':'做过'}[kind]+'、'.join(dishes)+'。','type':kind,'dish_refs':dishes,'importance':importance}

class EpisodicStore:
    def __init__(self,prefs,vectors=None,embedding=embed):self.prefs=prefs;self.vectors=vectors;self.embedding=embedding
    def rows(self,uid):
        identity(uid)
        if not self.prefs.enabled(uid):return []
        with self.prefs.connect(uid) as db:
            return [dict(r) for r in db.execute("SELECT * FROM episodic_memory WHERE user_id=? AND status='active' AND (expire_at IS NULL OR expire_at>?)",(uid,stamp())).fetchall()]
    def write(self,uid,event,turn,expected_epoch=None):
        identity(uid)
        if not self.prefs.enabled(uid):return None
        expected_epoch=self.prefs.epoch(uid) if expected_epoch is None else expected_epoch
        if event.get('type') not in {'cooked','feedback','favorite','rejected','summary'}:raise ValueError('invalid_event_type')
        if not isinstance(event.get('text'),str) or not event['text'].strip() or len(event['text'])>600:raise ValueError('event_text_out_of_bounds')
        vector=self.embedding(event['text'])
        if not vector or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector):raise ValueError('invalid_event_vector')
        with self.prefs.connect(uid) as db:
            setting=db.execute('SELECT enabled,epoch FROM memory_settings WHERE user_id=?',(uid,)).fetchone()
            if setting and (not setting['enabled'] or expected_epoch is not None and setting['epoch']!=expected_epoch):return None
            if expected_epoch not in (None,0) and not setting:return None
            same=db.execute('SELECT mem_id FROM episodic_memory WHERE user_id=? AND source_turn_ref=?',(uid,turn)).fetchone()
            if same:return same['mem_id']
            rows=db.execute("SELECT * FROM episodic_memory WHERE user_id=? AND status='active' AND (expire_at IS NULL OR expire_at>?)",(uid,stamp())).fetchall()
            duplicate=next((r for r in rows if r['type']==event['type'] and json.loads(r['dish_refs'])==event['dish_refs']
                and (event['type']!='feedback' or r['text']==event['text']) and r['vector'] and cosine(vector,json.loads(r['vector']))>.9),None)
            mid=duplicate['mem_id'] if duplicate else uuid.uuid4().hex
            if duplicate:
                db.execute('UPDATE episodic_memory SET last_accessed=?,importance=?,version=version+1 WHERE user_id=? AND mem_id=?',
                    (stamp(),max(duplicate['importance'],event['importance']),uid,mid))
            else:
                db.execute('INSERT INTO episodic_memory(mem_id,user_id,text,type,dish_refs,importance,created_at,last_accessed,vector,source_turn_ref) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (mid,uid,event['text'],event['type'],json.dumps(event['dish_refs'],ensure_ascii=False),event['importance'],stamp(),stamp(),json.dumps(vector),turn))
            db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,mid,stamp()))
        return mid
    def recall(self,uid,query,k=3):
        if not self.prefs.enabled(uid):return []
        epoch=self.prefs.epoch(uid)
        cfg=settings()['retrieval'];k=max(0,min(k,cfg['episodic_top_k']));rows=self.rows(uid)
        if not rows or k==0:return []
        vector=self.embedding(query);by_id={r['mem_id']:r for r in rows};hits=None
        try:
            vectors=self.vectors or MemoryVectors()
            hits=vectors.search(uid,vector)
        except Exception:pass
        if hits is not None:
            valid=[]
            for hit in hits:
                entity=hit.get('entity',{});mid=entity.get('mem_id',hit.get('id'));row=by_id.get(mid)
                if row and entity.get('user_id')==uid and entity.get('version')==row['version']:
                    valid.append((row,hit['distance']))
            # Unsynchronized new rows remain readable from their authoritative vector.
            seen={r['mem_id'] for r,_ in valid}
            valid.extend((r,cosine(vector,json.loads(r['vector']))) for r in rows if r['mem_id'] not in seen and r['vector'])
        else:valid=[(r,cosine(vector,json.loads(r['vector']))) for r in rows if r['vector']]
        weights=cfg['episodic_weights'];ranked=[]
        for row,sim in valid:
            if sim<cfg['episodic_sim_threshold']:continue
            age=max(0,(now()-datetime.fromisoformat(row['created_at'])).total_seconds()/86400)
            score=weights['sim']*sim+weights['recency']*math.exp(-age/30)+weights['importance']*row['importance']
            ranked.append({**row,'score':score,'similarity':sim})
        selected=sorted(ranked,key=lambda r:r['score'],reverse=True)[:k]
        with self.prefs.connect(uid) as db:
            setting=db.execute('SELECT enabled,epoch FROM memory_settings WHERE user_id=?',(uid,)).fetchone()
            if setting and (not setting['enabled'] or setting['epoch']!=epoch):return []
            for row in selected:
                db.execute('UPDATE episodic_memory SET last_accessed=? WHERE user_id=? AND mem_id=?',(stamp(),uid,row['mem_id']))
                db.execute('INSERT INTO memory_vector_outbox(id,user_id,mem_id,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,mem_id) DO NOTHING',(uuid.uuid4().hex,uid,row['mem_id'],stamp()))
        return [{k:r[k] for k in ('mem_id','text','type','dish_refs','score','similarity')} for r in selected]
    def sync(self,uid,vectors=None):
        if not self.prefs.enabled(uid):return 0
        vectors=vectors or self.vectors or MemoryVectors();vectors.ensure();count=0
        with self.prefs.connect(uid) as db:
            jobs=db.execute('SELECT * FROM memory_vector_outbox WHERE user_id=? ORDER BY created_at LIMIT 8',(uid,)).fetchall()
            for job in jobs:
                row=db.execute('SELECT * FROM episodic_memory WHERE user_id=? AND mem_id=?',(uid,job['mem_id'])).fetchone()
                if row and row['status']=='active' and (not row['expire_at'] or row['expire_at']>stamp()):vectors.upsert(uid,dict(row))
                else:vectors.delete(uid,job['mem_id'])
                db.execute('DELETE FROM memory_vector_outbox WHERE user_id=? AND id=?',(uid,job['id']));count+=1
        return count
