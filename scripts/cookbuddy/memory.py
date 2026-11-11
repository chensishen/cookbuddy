"""Grounded extraction, version-checked retrieval, bounded prompt context and lifecycle."""
import hashlib
import math
import json
import re
from datetime import datetime, timezone
from .models import json_model, embed, rerank
from .store import Store
from check_embedding import ROOT
from .vectors import VectorStore

PROFILE_KEYS={'taste','diet','allergy','disliked_food','equipment','household_size','cooking_skill','budget'}


def validated_extractions(data,question,evidence):
    rows=data.get('memories',[])
    if not isinstance(rows,list) or len(rows)>8:raise ValueError('invalid_extractions')
    output=[]
    for item in rows:
        if not isinstance(item,dict) or item.get('kind') not in ('profile','knowledge'):continue
        kind=item['kind'];key=item.get('key','');content=item.get('content','');quote=item.get('quote','');source=item.get('source','')
        confidence=item.get('confidence',0);importance=item.get('importance',0)
        if (any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in (confidence,importance))
                or not all(isinstance(v,str) for v in (key,content,quote,source)) or not content.strip() or len(content)>1000
                or not quote.strip() or len(quote)>1000 or not 0<=confidence<=1 or not 0<=importance<=1 or confidence<.85):continue
        if kind=='profile':
            if key not in PROFILE_KEYS or quote not in question or not re.search(r'我|我们|我家|家里|请记住|以后|偏好',quote) or re.search(r'[?？]|假如|假设|如果|比如|例如|吗',quote):continue
            # Do not extract claimed "facts" from historical assistant output.
            proof={'quote':quote,'origin':'user_message'}
        else:
            matched=next((row for row in evidence if row.get('source')==source and quote in row.get('quote','')),None)
            if not matched:continue
            key=hashlib.sha256((source+quote).encode()).hexdigest()[:40]
            path=(ROOT/source).resolve()
            if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file() or quote not in path.read_text():continue
            proof={'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'quote':quote,'source':source,'start_line':matched.get('start_line'),'end_line':matched.get('end_line'),'origin':'verified_recipe_source'}
        output.append({'kind':kind,'key':key,'content':quote,'evidence':proof,'confidence':confidence,'importance':importance})
    return output


class MemoryService:
    """Legacy acceptance compatibility; live routing and writes use ltm.*."""
    def __init__(self,store=None,vectors=None):self.store=store or Store();self.vectors=vectors or VectorStore()
    def save_task(self,uid,job,question,record,epoch=None):
        """Persist completed work independently of optional LLM extraction."""
        if record.get('status')!='completed':return None
        verified=[o['result'] for o in record.get('outputs',[])
                  if o.get('result',{}).get('status') in ('verified','verified_source')]
        evidence=[e for result in verified for e in result.get('evidence',[])]
        if not evidence:return None
        summaries=[result.get('short_answer','') for result in verified]
        return self.store.upsert_memory(uid,'task',job,(question+'\n已完成：'+'\n'.join(summaries))[:1400],
            {'job_id':job,'sources':sorted({e['source'] for e in evidence if e.get('source')})},
            .9,.55,90,expected_epoch=epoch)

    def extract(self,uid,job,question,record,epoch=None):
        from .extraction import (profile_candidates,knowledge_candidates,batches,digest,
            selected_memories,PROFILE_PROMPT,KNOWLEDGE_PROMPT)
        from .models import ModelOutputError
        epoch=self.store.memory_epoch(uid) if epoch is None else epoch
        progress=self.store.extraction_progress(uid,job)
        stored=0;errors=[]
        # Each kind builds its own plan: source errors must not block profile extraction.
        for kind,builder,prompt in (
                ('profile',lambda:profile_candidates(question),PROFILE_PROMPT),
                ('knowledge',lambda:knowledge_candidates(record),KNOWLEDGE_PROMPT)):
            try:
                candidates=builder()
                groups=list(batches(candidates)) or [[]]
                self.store.checkpoint_extraction(uid,job,epoch,kind+'_plan',
                    {'status':'ready','candidates':len(candidates),'batches':len(groups)})
            except Exception as error:
                errors.append(error)
                self.store.checkpoint_extraction(uid,job,epoch,kind+'_plan',
                    {'status':'failed','error_code':error.code if isinstance(error,ModelOutputError) else type(error).__name__})
                continue
            for group in groups:
                batch=kind+'_'+digest({'version':1,'question':question,'candidates':group})
                if progress.get(batch,{}).get('status') in ('saved','no_memory','no_candidates'):continue
                if self.store.memory_epoch(uid)!=epoch:return stored
                try:
                    if not group:
                        self.store.checkpoint_extraction(uid,job,epoch,batch,{'status':'no_candidates','saved':0,'candidates':0})
                        continue
                    # Only IDs and scores return from the model; source text is filled locally.
                    model_candidates=[{k:v for k,v in row.items() if k in ('id','text','source')} for row in group]
                    data=json_model(prompt,{'question':question,'candidates':model_candidates},
                        'memory_extract_'+kind,max_tokens=1200)
                    rows=selected_memories(data,kind,group,question)
                    message_id=self.store.user_message_id(uid,job) if kind=='profile' and rows else None
                    count=0
                    for row in rows:
                        if message_id is not None:row['evidence']['message_id']=message_id
                        if self.store.upsert_memory(uid,**row,ttl_days=365 if kind=='profile' else 180,expected_epoch=epoch):count+=1
                    stored+=count
                    self.store.checkpoint_extraction(uid,job,epoch,batch,
                        {'status':'saved' if rows else 'no_memory','saved':count,'candidates':len(group)})
                except Exception as error:
                    errors.append(error)
                    self.store.checkpoint_extraction(uid,job,epoch,batch,
                        {'status':'failed','error_code':error.code if isinstance(error,ModelOutputError) else type(error).__name__,
                         'candidates':len(group)})
        if errors:raise errors[0]
        return stored
    def retrieve(self,uid,question,progress=lambda _:None):
        progress('正在检索用户画像与历史记忆')
        profiles=self.store.active(uid,['profile'],12)
        candidates={r['id']:r for r in profiles};degraded=[]
        try:
            hits=self.vectors.search(uid,embed(question))
            for hit in hits:
                row=self.store.memory(uid,hit['id'])
                if (not row or row['version']!=hit['version'] or row['status']!='active'
                        or row['expires_at'] and row['expires_at']<=datetime.now(timezone.utc)):continue
                if row['kind']=='knowledge':
                    source=row['evidence'].get('source','');path=(ROOT/source).resolve()
                    if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['evidence'].get('source_sha256'):continue
                candidates[row['id']]=row
        except Exception as error:
            degraded.append('vector:'+type(error).__name__)
            for row in self.store.active(uid,['task','knowledge'],24):candidates[row['id']]=row
        rows=[]
        for row in list(candidates.values())[:36]:
            if row['kind']=='knowledge':
                path=(ROOT/row['evidence'].get('source','')).resolve()
                if not path.is_relative_to(ROOT/'data/HowToCook') or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['evidence'].get('source_sha256'):continue
            rows.append(row)
        if rows:
            progress('正在重排相关记忆')
            try:
                ranks=rerank(question,[r['content'] for r in rows]);ranks.sort(key=lambda r:r['relevance_score'],reverse=True)
                rows=[rows[r['index']] for r in ranks if r['relevance_score']>=.15][:8]
                # Explicit persistent preferences remain available even when low semantic similarity.
                rows=profiles[:8]+[r for r in rows if r['id'] not in {p['id'] for p in profiles[:8]}]
            except Exception as error:
                degraded.append('rerank:'+type(error).__name__);rows=profiles[:8]
        context=[]
        for row in rows:
            # Bound the actual JSON sent to the LLM, including provenance. Never cut a fact mid-sentence.
            proof={key:value for key,value in row['evidence'].items() if key in (
                'origin','message_id','source','source_sha256','start_line','end_line','job_id','sources','compressed_from')}
            item={'id':row['id'],'kind':row['kind'],'content':row['content'],'version':row['version'],'evidence':proof}
            if len(json.dumps(context+[item],ensure_ascii=False))>3500:continue
            context.append(item)
        if context:
            with self.store.connect(uid) as db:db.execute('UPDATE cookbuddy.memories SET last_accessed_at=now() WHERE id=ANY(%s)',([r['id'] for r in context],))
        return {'items':context,'degraded':degraded,'characters':len(json.dumps(context,ensure_ascii=False))}
    def maintain(self,uid):
        epoch=self.store.memory_epoch(uid)
        # TTL forgetting is immediate in PostgreSQL. Vector tombstones are synchronized asynchronously.
        with self.store.connect(uid) as db:
            rows=db.execute("SELECT id FROM cookbuddy.memories WHERE status='active' AND expires_at<now()").fetchall()
        forgotten=self.store.forget(uid,[r['id'] for r in rows]) if rows else 0
        tasks=self.store.active(uid,['task'],200)
        compressed=0
        # Compression is deterministic and provenance-preserving; originals receive tombstones.
        if len(tasks)>40:
            selected=tasks[25:];by_source={}
            for row in selected:
                for source in row['evidence'].get('sources',[]):by_source.setdefault(source,[]).append(row)
            for source,group in by_source.items():
                if len(group)<2:continue
                group=group[:10]
                content='历史任务摘要：\n'+'\n'.join(r['content'][:120] for r in group)
                key='compressed:'+hashlib.sha256((source+''.join(r['id'] for r in group)).encode()).hexdigest()[:32]
                saved=self.store.upsert_memory(uid,'task',key,content[:1500],{'sources':[source],'compressed_from':[r['id'] for r in group]},.9,.5,90,expected_epoch=epoch)
                if not saved:continue
                self.store.forget(uid,[r['id'] for r in group],status='compressed');compressed+=len(group)
        return {'forgotten':forgotten,'compressed':compressed}
