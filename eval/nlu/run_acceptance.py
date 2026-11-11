"""Real model + real PostgreSQL + existing source guards; isolated tenant, removed afterward."""
import asyncio,json,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from src.agent.config import load_config
from src.agent.context import AppContext
from src.agent.engine import ChatEngine
from src.retrieval.pipeline import RecipeServices
from langgraph.checkpoint.memory import InMemorySaver
from cookbuddy.store import Store

async def main():
    store=Store();uid=uuid.uuid4().hex;store.ensure_user(uid);sid=store.create_conversation(uid,'NLU isolated acceptance')
    cfg=load_config();cfg=cfg.model_copy(update={'trace':cfg.trace.model_copy(update={'structured_log':False})})
    engine=ChatEngine(cfg,RecipeServices(cfg),InMemorySaver());records=[]
    async def ask(case,q):
        r=await engine.answer(q,AppContext(uid,sid));records.append({'case':case,'result':r})
        print(json.dumps({'case':case,'status':r['status'],'seconds':round(r['elapsed_seconds'],3),'tools':r.get('trace',{}).get('tool_calls')},ensure_ascii=False),flush=True)
        return r
    try:
        r=await ask('identity','你是谁')
        assert 'cookbuddy' in r['text'] and not any(s.get('operation')=='search' for s in r['trace']['stages'])
        await ask('select_recipe','查看基础牛奶面包原文')
        a=await ask('chapter_clean','只看酵头制作')
        b=await ask('chapter_noisy','能不能只给我酵投制作的那一小段呀')
        def evidence(r):return [e for o in r['outputs'] for e in o['result'].get('evidence',[])]
        assert evidence(a) and evidence(a)==evidence(b)
        a=await ask('recommend','我有鸡肉，推荐3道菜，不要油炸，说明还缺什么')
        choices=[v for o in a['outputs'] for v in o['result'].get('recommendations',[])]
        assert len(choices)>=2
        rejected=choices[1]['title']
        b=await ask('exclude_noisy','不要刚才第二道1')
        new=[v for o in b['outputs'] for v in o['result'].get('recommendations',[])]
        assert new and rejected not in [r['title'] for r in new]
        c=await ask('outside_scope','今天天气怎么样')
        assert not any(s.get('operation')=='search' for s in c['trace']['stages'])
        report={'passed':True,'scope':'seven real engine turns; includes persistence, NLU, tools, source evidence and exclusions','records':records}
    except Exception as error:
        report={'passed':False,'failure':type(error).__name__,'records':records}
        raise
    finally:
        (Path(__file__).with_name('engine-acceptance.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
        await engine.memory_queue.close()
        with store.connect(admin=True) as db:
            for table in ('intent_audit','messages','conversations','outbox','memory_revisions','memories','memory_tasks'):
                db.execute('DELETE FROM cookbuddy.'+table+' WHERE user_id=%s',(uid,))
            db.execute('DELETE FROM cookbuddy.tenants WHERE id=%s',(uid,))

if __name__=='__main__':asyncio.run(main())
