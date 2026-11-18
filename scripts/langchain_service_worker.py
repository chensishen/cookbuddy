"""Private JSON-over-stdin service boundary. stdout never contains diagnostics."""
import contextlib
import copy
import hashlib
import io
import json
import os
import re
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'scripts'))

# When long-term preference memory is disabled, keep only the active process'
# short-term dialogue state. It is never persisted to the store.
_SHORT_SESSION_CACHE={}


def configure(cfg):
    os.environ.update(RAG_RETRIEVAL='vector' if cfg['retrieval']['mode']=='dense' else 'hybrid',
        RAG_TOP_K=str(cfg['retrieval']['recall_top_n']),RAG_TOP_N=str(cfg['retrieval']['top_k']),
        RAG_AUGMENT='1' if cfg['retrieval']['augment'] else '0',
        RAG_RERANK='1' if cfg['retrieval']['rerank']['enabled'] else '0')
    os.environ['RECIPE_MODEL_CONCURRENCY']=str(cfg['resilience']['concurrency'])
    from cookbuddy.ltm.models import CONFIG_OVERRIDE,settings
    memory=copy.deepcopy(settings());memory['experiments']['level']=cfg['memory']['mode']
    memory['short_term']['window_turns']=cfg['memory']['window_turns']
    memory['short_term']['window_max_tokens']=cfg['memory']['window_max_tokens']
    CONFIG_OVERRIDE.set(memory)


COMPOUND_RECIPE_INTENTS={'ingredient_recommend','constraint_refine','meal_plan','recipe_lookup','recipe_section','follow_up'}


def compound_recipe_candidates(question):
    """Clauses that may carry a recipe request inside a memory turn."""
    parts=[part.strip(' ，,；;') for part in re.split(r'[。！!？?\n]+',question) if part.strip(' ，,；;')]
    candidates=[]
    for part in reversed(parts):
        segments=[part]
        if re.search(r'记住|记下|不吃|忌口|过敏|有|没有',part) and re.search(r'[，,；;]',part):
            segments=[value.strip(' ，,；;') for value in re.split(r'[，,；;]',part)]
            if re.match(r'^(?:请)?(?:记住|记下)',part):segments=segments[1:]
        candidates.extend(reversed(segments))
    return [value for value in candidates if value]


def prepare(p):
    from cookbuddy.store import Store
    from cookbuddy.ltm.runtime import MemoryRuntime
    from cookbuddy.ltm.management import memory_only_write,parse_command
    from cookbuddy.session import SessionState,append_message,update_slots
    from recipe_tools import load_chunks
    from src.nlu.router import understand
    from src.dialogue import reduce_turn
    store=Store();uid,sid,job,q=(p[k] for k in ('user_id','session_id','request_id','question'))
    store.conversation(uid,sid)  # ownership is enforced by database RLS
    store.append(uid,sid,job,'user',q)
    memory=MemoryRuntime(store,uid,sid,job,q)
    enabled=memory.enabled;level=memory.level
    session_key=(uid,sid)
    session=memory.session or _SHORT_SESSION_CACHE.get(session_key) or SessionState(uid,sid)
    if (memory.management and (parse_command(q) or memory_only_write(q))) or memory.confirmation:
        # Explicit memory commands are complete turns of their own. Incidental
        # session notices must not prevent the same turn from handling a recipe.
        session_enabled=level!=0
        persist_session=memory.session is not None
        append_message(session,'user',q)
        if session_enabled:
            if enabled: store.save_session_context(uid,sid,job,session)
            else: _SHORT_SESSION_CACHE[session_key]=copy.deepcopy(session)
        return {'enabled':enabled,'session_enabled':session_enabled,'persist_session':persist_session,'level':level,
            'hard':memory.hard,'slots':session.slots,'summary':session.summary,'messages':session.messages,
            'task':{'state':None,'action':'none','question':q,'boundary':False,'parser_version':6},
            'pending_confirmation':session.pending_confirmation,'events':memory.events,'profile':[],
            'management':memory.confirmation or memory.management,'memory_confirmation':None,
            'notice':memory.notice,'input_notice':'',
            'epoch':store.memory_epoch(uid),'recipe_conflict':None,
            'nlu':{'intent':'preference_update','confidence':1.,'slots':{},'path':'fast',
                   'answer_mode':'memory','errors':[],'original':q,'normalized':q,
                   'matches':[],'assumptions':[],'options':[],'elapsed_ms':0},
            'named_recipe':None}
    previous=(memory.previous_task or session.slots.get('recommendation_state')) if level>=2 else None
    nlu=understand(q,{'summary':session.summary,'slots':session.slots,'messages':session.messages,
        'previous_task':previous,'tenant':uid,'session':sid},use_model=bool(p.get('config')))
    task,named=reduce_turn(nlu,session,previous,load_chunks())
    for ingredient in nlu['slots']['constraints'].get('excluded_ingredients',[]):
        from cookbuddy.recommendation_schema import EXCLUSION_NAMES
        if ingredient in EXCLUSION_NAMES and not any(c.get('value')==ingredient for c in memory.hard):
            memory.hard.append({'dimension':'avoid_ingredient','value':ingredient,'polarity':'avoid','strength':'hard','scope':'session','source':'explicit'})
    memory.apply_soft(task,session)
    keep_inventory_tastes_explicit(task)
    # A memory statement and a recipe request in one message: apply the memory,
    # then execute the recipe clause in the same turn. Memory-only turns keep
    # their existing single-answer behavior.
    compound_confirmation=None
    pending_memory=memory.confirmation or memory.management
    if pending_memory and not pending_memory.get('needs_confirmation'):
        if task.get('action') in ('recommend','meal_plan','followup','ingredients') or (named and nlu['intent'] in COMPOUND_RECIPE_INTENTS):
            # The turn already produced a recipe task; keep it and show the
            # memory acknowledgement alongside the recipe answer.
            compound_confirmation=pending_memory.get('message','')
        elif nlu['intent'] in ('preference_update','unclear','chitchat'):
            context={'summary':session.summary,'slots':session.slots,'messages':session.messages,
                     'previous_task':previous,'tenant':uid,'session':sid}
            for candidate in compound_recipe_candidates(q):
                sub=understand(candidate,context,use_model=False)
                if sub['intent'] not in COMPOUND_RECIPE_INTENTS:continue
                sub_task,sub_named=reduce_turn(sub,session,previous,load_chunks())
                if sub_task.get('action')=='none' and not sub_named:continue
                task,nlu,named=sub_task,sub,sub_named
                compound_confirmation=pending_memory.get('message','')
                memory.apply_soft(task,session)
                keep_inventory_tastes_explicit(task)
                break
    if task.get('action')=='clarify' and task.get('clarification_options'):
        nlu.update(answer_mode='clarify',clarify_question=task['clarification'],
            options=task['clarification_options'],clarification_slot=nlu.get('clarification_slot') or 'conditions')
    if nlu.get('answer_mode')=='clarify':
        from src.agent.turn_intents import change_dish_request
        session.slots['nlu_pending']={'slot':nlu.get('clarification_slot'),
            'options':nlu.get('options',[]),'intent':nlu['intent'],
            'exclude_choices':change_dish_request(q)}
        if nlu.get('clarification_slot') in ('dish','section'):
            session.slots['nlu_pending']['slots']=copy.deepcopy(nlu['slots'])
        elif nlu.get('clarification_slot')=='conditions' and task.get('state'):
            session.slots['nlu_pending']['state']=copy.deepcopy(task['state'])
    else:session.slots.pop('nlu_pending',None)
    update_slots(session,task)
    if named and task['action'] in ('none','followup','recipe_title'):
        session.slots['selected_section']=nlu['slots']['section']
        session.slots['selected_dish']={'idx':0,'dish_name':named[0],'source_path':named[1]}
    append_message(session,'user',q)
    session_enabled=level!=0
    persist_session=memory.session is not None
    if session_enabled:
        if enabled: store.save_session_context(uid,sid,job,session)
        else: _SHORT_SESSION_CACHE[session_key]=copy.deepcopy(session)
    if enabled and level>=2:store.save_task_context(uid,sid,job,task)
    items=[] if named and task['action']!='recommend' else memory.context_items(recommend=task['action']=='recommend')
    conflict=None
    from cookbuddy.ltm.guard import source_allowed,forbidden,blocked_result
    if named and memory.hard and not source_allowed(named[1],memory.hard):
        names,equipment,diets=forbidden(memory.hard)
        limits='、'.join(names+equipment+diets)
        conflict=blocked_result('无法确认《'+named[0]+'》原配方满足你的限制（'+limits+'）。'
            '因此不能将原配方作为符合限制的完整做法提供，也不能确认删去或替换相关原料后的效果。'
            '本次未删改原配方；可以更换为已核实符合限制的菜谱。')

    management=memory.confirmation or memory.management
    if (management and not management.get('needs_confirmation')
            and management.get('decision') in ('SESSION_ONLY','QUEUED')
            and nlu['path'] in ('model','cache','fast')
            and task['action'] in ('recommend','meal_plan','ingredients')):
        management=None
    if compound_confirmation:management=None
    return {'enabled':enabled,'session_enabled':session_enabled,'persist_session':persist_session,'level':level,'hard':memory.hard,'slots':session.slots,
            'summary':session.summary,'messages':session.messages,'task':task,
            'pending_confirmation':session.pending_confirmation,'events':memory.events,
            'profile':items,'management':management,'memory_confirmation':compound_confirmation,
            'notice':'\n'.join(v for v in (memory.notice,nlu.get('input_notice','')) if v),'input_notice':nlu.get('input_notice',''),
            'epoch':store.memory_epoch(uid),
            'recipe_conflict':conflict,'nlu':nlu,
            'named_recipe':{'dish_name':named[0],'source_path':named[1]} if named else None}


def keep_inventory_tastes_explicit(task):
    """Stored soft taste must not become a hard condition on declared inventory."""
    state=task.get('state')
    if state and state.get('mode')=='inventory' and state.get('memory_tastes')==state.get('tastes'):
        state['tastes']=[];state.pop('memory_tastes',None)
        from cookbuddy.task_context import task_question
        task['question']=task_question(state)


def memory_authorized(operation,args,message):
    """Tool text cannot authorize preference writes, even if a model requests it."""
    if operation=='forget_preference':intent=bool(re.search(r'忘|删除|清空|撤销|取消',message))
    else:intent=bool(re.search(r'记住|记下|以后都|长期保留',message)) and bool(re.search(
        r'喜欢|爱吃|不吃|过敏|忌口|口味|偏好|没有|只有|不用|纯素|素食|清真|我有|家里有|吃辣',message))
    if not intent:return False
    from cookbuddy.ltm.extractor import rule_extract
    candidates=rule_extract(message)
    matched=any(c.dimension==args.get('dimension') and c.value==args.get('value') for c in candidates)
    if args.get('dimension') in ('allergy','diet_type','equipment'):
        return matched
    return matched or bool(args.get('value') and args['value'] in message)


def result_from_answer(answer):
    refs={r['reference_id']:r for r in answer.get('references',[])}
    evidence=[{**r,**{k:refs[r['reference_id']][k] for k in ('source','title','section')}}
              for r in answer.get('evidence',[]) if r['reference_id'] in refs]
    return {**{k:v for k,v in answer.items() if k in ('status','short_answer','answer','answer_method','validation','display_blocks','display_source')},
            'evidence':evidence,'sources':[{'reference_id':i,'source':r['source'],'title':r['title'],
                'section':r['section'],'line':r['source_start_line']}
                for i,r in refs.items() if i in answer.get('cited_reference_ids',[])],'images':[]}


def execute(p):
    from cookbuddy.ltm.guard import policy,check_result,source_allowed
    from recipe_tools import load_chunks,read_recipe,find_recipe_images
    cfg=p['config'];configure(cfg)
    op=p['operation'];a=p['arguments'];uid=p['user_id'];sid=p['session_id'];job=p['request_id']
    with policy(p.get('hard',[])):
        if op=='auth':
            from cookbuddy.store import Store
            store=Store();store.ensure_user(uid)
            if a.get('create'):return {'session_id':store.create_conversation(uid,'新对话')}
            store.conversation(uid,sid);return {'ok':True}
        if op=='prepare':return prepare({**p,'question':a['question']})
        if op=='model':
            from urllib.request import Request
            from llm_config import require_llm
            from service_runtime import request_json
            llm=require_llm();payload=a['body']
            return request_json(Request(llm['base_url']+'/chat/completions',
                data=json.dumps(payload,ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer '+llm['api_key'],'Content-Type':'application/json'}),
                timeout=cfg['llm']['timeout_s'],attempts=cfg['llm']['max_retries']+1,phase='langchain_online')
        if op=='guard':
            result=a['result']
            rows=result.get('items',[])
            good=[r for r in rows if source_allowed(r.get('source_path',r.get('source','')),p.get('hard',[]))]
            return {'allowed':[r.get('source_path',r.get('source','')) for r in good]}
        if op=='fact':
            from recipe_fact_answer import source_fact
            result=source_fact(a['question'],load_chunks())
            return {'result':check_result(result) if result else None}
        if op=='summarize':
            from cookbuddy.models import json_model
            from cookbuddy.ltm.prompts import SUMMARY_PROMPT
            return json_model(SUMMARY_PROMPT,a,'langchain_summary',max_tokens=400)
        if op=='detail':return check_result(read_recipe(a.get('source_path') or a['dish_name'],a.get('section','')))
        if op=='images':
            result=find_recipe_images(a['dish_name'],a.get('section',''))
            return check_result({**result,'answer':result['short_answer'],'answer_layout':'images_only'})
        if op=='recommend':
            from ingredient_recommendation import recommend,normalize_ingredient,unused_inventory_note
            from search_filters import filter_chunks
            rows,_,_=filter_chunks(load_chunks(),category=a.get('category'),max_difficulty=a.get('max_difficulty'))
            rows=[r for r in rows if source_allowed(r['source'],p.get('hard',[]))]
            excluded=set(a.get('exclude_dishes',[]))
            rows=[r for r in rows if r['title'] not in excluded]
            candidates=recommend([normalize_ingredient(x) for x in a['ingredients']],rows,a.get('top_k',5),
                                 category=a.get('category'),max_difficulty=a.get('max_difficulty'))
            shortage=''
            if len(candidates)<a.get('top_k',5):
                shortage=f"你要的{a.get('top_k',5)}道菜，在当前食材和筛选条件下仅找到{len(candidates)}道可核实的菜谱。"
                from cookbuddy.ltm.guard import forbidden
                avoided,_,_=forbidden(p.get('hard',[]))
                if avoided:shortage+='已应用你之前设置的饮食限制：避开'+ '、'.join(avoided)+'。'
                shortage+='\n'
            return {'status':'verified' if candidates else 'insufficient','recommendations':candidates,
                'short_answer':shortage+('好呀，按你手边的食材，帮你挑了'+str(len(candidates))+'道菜。下面是推荐理由和需要补齐的用料。' if candidates else '这次还没找到能用上现有食材、又符合限制的新菜。你可以再告诉我几样食材，我帮你继续挑。')
                    +unused_inventory_note(a['ingredients'],candidates)+'我已对照原料表和计算章节；操作中可能另有用料，开做前也请一起核对。',
                'sources':[{'title':r['title'],'source':r['source'],'section':'必备原料','line':r['evidence'][0]['start_line']} for r in candidates],
                'evidence':[e for r in candidates for e in r['evidence']]}
        if op=='recipe_ingredients':
            from ingredient_recommendation import recipe_ingredient_fields,normalize_ingredient,matched_inventory
            source=a['source_path']
            rows=[r for r in load_chunks() if r['source']==source and ('必备原料' in r['section'] or r['section'].split(' > ')[0]=='计算')]
            if not rows or not source_allowed(source,p.get('hard',[])):
                return {'status':'insufficient','short_answer':'这道菜的原料表暂时没能核实，我先不猜需要补什么。你可以查看原文再确认。','sources':[],'evidence':[]}
            required,optional,tools,uncertain,evidence=recipe_ingredient_fields(rows,(ROOT/source).read_text().splitlines())
            inventory={normalize_ingredient(x) for x in a.get('ingredients',[])}
            missing=sorted(required-inventory);matched=sorted(matched_inventory(required,optional,uncertain,inventory))
            text='好呀，我来帮你对照《'+rows[0]['title']+'》的原料表和计算章节。'
            if matched:text+='\n你已经有：'+'、'.join(matched)+'。'
            if inventory:
                text+='\n'+('还需要确认或准备：'+'、'.join(missing)+'。' if missing else '可识别的必备原料已经齐了。')
            else:
                text+='\n原文必备原料：'+'、'.join(sorted(required))+'。'
            text+='\n可选用料：'+('、'.join(sorted(optional)) if optional else '原料表和计算章节未明确标注')+'。'
            if tools:text+='\n原文工具：'+'、'.join(sorted(tools))+'。'
            if uncertain:text+='\n这些条目还需按原文核对：'+'；'.join(uncertain)+'。'
            if inventory:text+='\n“还需准备”只按你告诉我的库存对照，不代表家里一定没有；具体用量及步骤中的其他配料请一起核对原文。'
            else:text+='\n你还没有告诉我手边有哪些食材；如果告诉我库存，我可以逐项对照还缺什么。'
            return {'status':'verified_source','short_answer':text,'answer':text,'answer_layout':'inventory_check','matched':matched,'missing':missing,
                'sources':[{'title':r['title'],'source':source,'section':r['section'],'line':r['source_start_line']} for r in rows],
                'evidence':evidence,'images':[]}
        if op=='task_recommend':
            from preference_recommendation import recommend_preferences
            from cookbuddy.store import Store
            from cookbuddy.ltm.profile_store import ProfileStore
            from cookbuddy.ltm.retriever import personalize_result
            store=Store();prefs=ProfileStore(store)
            personal=cfg['memory']['mode']=='M6' and prefs.enabled(uid)
            profile=prefs.load_profile(uid) if personal else {'hard_constraints':[],'soft_prefs':[]}
            owned_equipment=[str(p.get('value',''))[1:] for p in profile.get('soft_prefs',[])
                             if p.get('dimension')=='equipment' and str(p.get('value','')).startswith('有')]
            from search_filters import filter_chunks
            rows,_,_=filter_chunks(load_chunks(),category=a.get('category'),max_difficulty=a.get('max_difficulty'))
            varied=a.get('varied_recommendations',False)
            result=recommend_preferences(a['state'],rows,exclude_titles=a.get('exclude_dishes',[]),
                candidate_limit=400 if personal or varied else None,owned_equipment=owned_equipment)
            if personal or varied:
                from random import Random
                # Stable for a retry of this job, different for each request.
                result=personalize_result(result,a['state'],profile,store.session_context(uid,sid,job),
                    rng=Random(job) if varied else None)
            if result:return check_result(result)
            unresolved=a['state'].get('extra_constraints',[])
            detail='、'.join('“'+v+'”' for v in unresolved)
            return {'status':'insufficient','short_answer':('这次还没能识别或核实这些条件：'+detail+'。请确认食材名称，或将食材、口味和其他要求分开说明。我会保留已理解的条件。'
                if detail else '这次选菜条件还没能完整核实，请说明希望使用的食材和口味。'),'sources':[],'evidence':[]}
        if op=='meal':
            from cookbuddy.meal_planning import plan_meal,meal_request
            request=a.get('request') or meal_request(a.get('question',''))
            if not request:return {'status':'insufficient','short_answer':'请说明人数、整餐时间及菜汤数量。','sources':[],'evidence':[]}
            prefs=[]
            if cfg['memory']['mode']=='M6' and not (request.get('constraints') or {}).get('taste'):
                from cookbuddy.store import Store,valid_id
                if valid_id(uid):
                    from cookbuddy.ltm.profile_store import ProfileStore
                    try:
                        prefs=[row for row in ProfileStore(Store()).load_profile(uid)['soft_prefs']
                               if row['dimension'] in ('flavor','spice_level','cuisine','max_difficulty')]
                    except Exception:
                        # Taste preference is an ordering hint; the menu itself
                        # must still be planned when the profile is unavailable.
                        prefs=[]
            return check_result(plan_meal(request,load_chunks(),taste_preferences=prefs))
        if op=='search':
            from ask_test_recipes import run_stage
            from search_filters import filter_policy
            from rag_dataset import FOLDER
            import uuid
            q=a['query']
            with filter_policy(category=a.get('category'),max_difficulty=a.get('max_difficulty')):
                if cfg['retrieval']['mode']=='sparse':
                    from lexical_retrieval import BM25
                    from search_filters import filter_chunks
                    rows,_,_=filter_chunks(load_chunks(),category=a.get('category'),max_difficulty=a.get('max_difficulty'))
                    rows=[r for r in rows if source_allowed(r['source'],p.get('hard',[])) and r['title'] not in a.get('exclude_dishes',[])]
                    hits=BM25(rows).search(q,cfg['retrieval']['recall_top_n'])
                    byid={r['chunk_id']:r for r in rows}
                    hits=[{**byid[r['chunk_id']],**r,'distance':r.get('bm25_score',0)} for r in hits]
                    report={'question':q,'results':hits,'model':'sparse','retrieval_mode':'sparse'}
                    folder=FOLDER/'search-results';folder.mkdir(exist_ok=True)
                    search_path=folder/(uuid.uuid4().hex+'.json');search_path.write_text(json.dumps(report,ensure_ascii=False))
                else:search_path,report=run_stage('search_test_recipes.py',q,'search-results')
                report['results']=[r for r in report['results'] if r['title'] not in a.get('exclude_dishes',[])]
                if a.get('source_path'):
                    report['results']=[r for r in report['results'] if r['source']==a['source_path']]
                # Keep the original artifact immutable when applying per-turn exclusions.
                filtered=search_path.with_name(search_path.stem+'-lc.json')
                filtered.write_text(json.dumps(report,ensure_ascii=False));search_path=filtered
                result={'question':q,'source_search':str(search_path.relative_to(ROOT)),
                    'results':report['results'][:a.get('top_k',5)],'rerank_fallback':None}
                diagnostic=None
                if result['results'] and cfg['retrieval']['rerank']['enabled']:
                    from service_runtime import request_budget
                    try:
                        with request_budget(cfg['retrieval']['rerank']['timeout_s']):
                            _,result=run_stage('rerank_test_recipes.py',str(search_path),'rerank-results')
                        diagnostic=result.pop('_stage_diagnostics',None)
                    except Exception as error:
                        from stage_diagnostics import failure_reason
                        diagnostic=getattr(error,'stage_diagnostics',None)
                        result['rerank_fallback']=(diagnostic or {}).get('reason') or failure_reason(error)
                path=search_path.with_name(search_path.stem+'-selected.json')
                result['results']=[{**row,'original_rank':row.get('original_rank',rank),
                    'rerank_rank':rank,'rerank_score':row.get('rerank_score',row.get('fusion_score',row.get('distance',0)))}
                    for rank,row in enumerate(result['results'][:a.get('top_k',5)],1)]
                path.write_text(json.dumps(result,ensure_ascii=False))
                from stage_diagnostics import normalize_reason
                return {'status':'degraded' if result.get('rerank_fallback') else 'ok',
                    'rows':result['results'],'artifact':str(path.relative_to(ROOT)),
                    'degraded':bool(result.get('rerank_fallback')),
                    'degraded_reason':normalize_reason(result.get('rerank_fallback')),
                    'rerank_diagnostics':diagnostic}
        if op=='answer':
            from ask_test_recipes import run_stage
            from rag_dataset import FOLDER
            path=(ROOT/a['artifact']).resolve()
            if not path.is_relative_to(FOLDER.resolve()) or path.suffix!='.json':raise ValueError('invalid_evidence')
            _,answer=run_stage('generate_test_answer.py',str(path),'answer-results',fallback_reserve=2)
            return check_result(result_from_answer(answer))
        if op in ('remember_preference','forget_preference','recall_history'):
            a=dict(a)
            message=a.pop('_user_message','')
            if op!='recall_history' and not memory_authorized(op,a,message):
                return {'decision':'NOOP','message':'请明确希望记住或忘记的饮食偏好。'}
            from cookbuddy.store import Store
            from cookbuddy.ltm.profile_store import ProfileStore
            from cookbuddy.ltm.models import Candidate
            from cookbuddy.ltm.episodic_store import EpisodicStore
            prefs=ProfileStore(Store())
            if not prefs.enabled(uid):return {'decision':'NOOP','message':'记忆已关闭。'}
            if op=='recall_history':return {'events':EpisodicStore(prefs).recall(uid,a['query'],a.get('top_k',3))}
            args={k:v for k,v in a.items() if k not in {'user_id','session_id'}}
            if op=='forget_preference':
                old=next((r for r in prefs.prefs(uid) if r.dimension==args['dimension'] and r.value==args['value']),None)
                candidate=Candidate(args['dimension'],args['value'],old.polarity if old else 'avoid',old.strength if old else 'soft',op='revoke',evidence_text=message)
            else:candidate=Candidate(**args,evidence_text=message)
            key=hashlib.sha256(json.dumps([op,args],sort_keys=True).encode()).hexdigest()[:20]
            outcome=prefs.apply(uid,sid,sid+':'+job+':'+key,candidate)
            return {**outcome,'_hard':prefs.load_profile(uid)['hard_constraints']}
        if op=='persist':
            from cookbuddy.store import Store
            from cookbuddy.session import SessionState,append_message,compact_assistant,update_slots
            from cookbuddy.task_context import remember_choices
            store=Store();result=a['result'];prepared=a['prepared'];q=a['question']
            task=prepared['task'];task['state']=remember_choices(task.get('state'),result,replacement_index=task.get('replacement_index'))
            if prepared.get('session_enabled',prepared['enabled']):
                session_key=(uid,sid)
                session=(store.session_context(uid,sid,job) if prepared['enabled']
                         else _SHORT_SESSION_CACHE.get(session_key) or SessionState(uid,sid))
                session.slots=prepared['slots']
                session.summary=prepared.get('summary','')
                session.messages=prepared.get('messages',[])
                # The boundary was applied in prepare. Reapplying it here
                # would erase the recipe selected by this turn's tools.
                update_slots(session,{**task,'boundary':False},result)
                for output in result.get('outputs',[]):
                    data=output.get('result',{})
                    if data.get('pending_constraints') and data.get('clarification_options'):
                        from src.agent.turn_intents import change_dish_request
                        pending={'slot':'conditions','options':data['clarification_options'],
                                 'exclude_choices':change_dish_request(q)}
                        if task.get('state'):
                            pending['state']=copy.deepcopy(task['state'])
                            pending['limit']=task['state'].get('limit')
                        session.slots['nlu_pending']=pending
                append_message(session,'assistant',compact_assistant(result))
                if prepared.get('persist_session',prepared['enabled']):store.save_session_context(uid,sid,job,session)
                else:_SHORT_SESSION_CACHE[session_key]=copy.deepcopy(session)
            if prepared['enabled'] and prepared.get('level',6)>=2:store.save_task_context(uid,sid,job,task)
            store.append(uid,sid,job,'assistant',a['text'],result)
            from service_runtime import RuntimeStore
            RuntimeStore().record_request(job,result.get('degraded',False))
            return {'ok':True}
        if op=='enqueue':
            from cookbuddy.store import Store
            from cookbuddy.ltm.profile_store import ProfileStore
            from psycopg.types.json import Jsonb
            store=Store();prefs=ProfileStore(store)
            if cfg['memory']['mode'] in ('M0','M1','M2') or not prefs.enabled(uid):return {'ok':True}
            with store.connect(uid) as db:
                db.execute('INSERT INTO cookbuddy.memory_tasks(job_id,user_id,epoch,question,record,profile_epoch) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                    (job,uid,store.memory_epoch(uid),a['question'],Jsonb({'conversation_id':sid}),prefs.epoch(uid)))
            return {'ok':True}
        raise ValueError('unknown_operation')


if __name__=='__main__':
    try:
        payload=json.loads(sys.stdin.buffer.read(2_000_001))
        # Existing stage commands print source previews. Suppress them at this
        # boundary; only the requested private result crosses stdout.
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            result=execute(payload)
    except Exception as error:
        result={'_error':type(error).__name__,'_status':getattr(error,'code',None)}
    sys.stdout.write(json.dumps(result,ensure_ascii=False,default=str))
