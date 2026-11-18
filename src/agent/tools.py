"""Thin async tools. Identity is hidden runtime context, never a tool argument."""
import asyncio
import json
import time
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator
from langchain.tools import tool, ToolRuntime
from .context import AppContext


class Input(BaseModel):
    model_config=ConfigDict(extra='ignore')


class SearchInput(Input):
    query: str=Field(min_length=1,max_length=2000)
    category: str | None=None
    max_difficulty: int | None=Field(None,ge=1,le=5,strict=True)
    top_k: int=Field(5,ge=1,le=8,strict=True)


class RecommendInput(Input):
    ingredients: list[Annotated[str,Field(min_length=1,max_length=30)]]=Field(min_length=1,max_length=10)
    category: str | None=None
    max_difficulty: int | None=Field(None,ge=1,le=5,strict=True)
    top_k: int=Field(5,ge=1,le=8,strict=True)


class DetailInput(Input):
    section: str=Field('',max_length=80)
    dish_name: str | None=Field(None,min_length=1,max_length=200)
    source_path: str | None=Field(None,min_length=1,max_length=500)

    @model_validator(mode='after')
    def exactly_one(self):
        if bool(self.dish_name)==bool(self.source_path):raise ValueError('one_recipe_identifier_required')
        return self


def compact_result(full):
    rows=full.get('rows') or full.get('recommendations') or full.get('sources') or full.get('images') or []
    items=[]
    for row in rows[:8]:
        source=row.get('source',row.get('source_path',''))
        if not source:continue
        start=row.get('source_start_line',row.get('line',row.get('start_line')))
        end=row.get('source_end_line',row.get('end_line',start))
        snippet=row.get('content',row.get('quote',''))
        if not snippet:
            snippets=[e.get('quote','') for e in full.get('evidence',[]) if e.get('source')==source
                      and (not row.get('section') or e.get('section')==row['section'])]
            snippet='\n'.join(snippets)
        items.append({'dish_name':row.get('title',row.get('dish_name','')),
            'section':row.get('section',''),'source_path':source,
            'line_range':f'L{start}-L{end}' if start else '',
            'score':row.get('rerank_score',row.get('distance',0)),
            'snippet':snippet[:200],'difficulty':row.get('difficulty'),
            'missing_ingredients':row.get('missing',[])})
    status=full.get('status','ok')
    status='degraded' if full.get('degraded') or status=='degraded' else 'error' if status in ('error','tool_failed','validation_failed') else 'empty' if not items and status not in ('verified','verified_source') else 'ok'
    return {'status':status,'items':items,'filtered_out':0,
        'message':('在你的限制下没有找到合适的菜谱' if status=='empty' else '已核对来源')}


def error_result():
    return {'status':'error','items':[],'filtered_out':0,'message':'服务繁忙，请稍后重试'}


def wants_alternative(question):
    from .turn_intents import alternative_request
    return alternative_request(question) is not None


async def invoke_tool(run, name, args):
    """Shared by LangChain wrappers and deterministic/fixed workflow branches."""
    started=time.monotonic()
    if run.tool_calls>=run.cfg.agent.max_tool_calls:
        run.degraded=True;run.quality_limited=True
        raise RuntimeError('tool_call_limit')
    run.tool_calls+=1
    await run.emit('tool',{'name':name,'status':'running'})
    operation={'search_recipes':'search','recommend_by_ingredients':'recommend','get_recipe_detail':'detail',
               'find_recipe_images':'images','plan_meal':'meal'}.get(name,name)
    args={k:v for k,v in args.items() if k not in ('user_id','session_id')}
    if name in ('remember_preference','forget_preference'):args['_user_message']=run.question
    try:
        if name=='search_recipes':args=SearchInput.model_validate(args).model_dump(exclude_none=True)
        if name=='recommend_by_ingredients':args=RecommendInput.model_validate(args).model_dump(exclude_none=True)
        if name in ('recommend_by_ingredients','task_recommend'):
            args['exclude_dishes']=run.prepared.get('task',{}).get('exclude_dishes',args.get('exclude_dishes',[]))
        if name=='task_recommend' and run.prepared.get('task',{}).get('varied_recommendations'):
            args['varied_recommendations']=True
        if name=='plan_meal' and run.prepared.get('task',{}).get('meal_request'):
            args={'request':run.prepared['task']['meal_request']}
        if name=='get_recipe_detail':args=DetailInput.model_validate(args).model_dump(exclude_none=True)
        named=run.prepared.get('named_recipe')
        nlu=run.prepared.get('nlu',{})
        if name in ('task_recommend','recommend_by_ingredients'):
            args.update(run.prepared.get('task',{}).get('filters',{}))
        if named and name=='get_recipe_detail':
            args={'source_path':named['source_path'],'section':nlu.get('slots',{}).get('section','')}
        if named and name=='find_recipe_images':
            args={'dish_name':named['source_path'],'section':nlu.get('slots',{}).get('section','')}
        if name in ('search_recipes','recommend_by_ingredients'):
            # Preserve all original restrictions rather than trusting a model's
            # rewritten query or invented metadata constraints.
            args['original_question']=run.question
            if name=='search_recipes':args['query']=run.prepared.get('task',{}).get('question') or run.question
            if name=='search_recipes' and named:args['source_path']=named['source_path']
            if name=='search_recipes':args['exclude_dishes']=run.prepared.get('task',{}).get('exclude_dishes',[])
        timeout=run.cfg.retrieval.tool_timeout_s if operation in ('search','recommend','task_recommend','detail','images') else 5
        full=await asyncio.wait_for(run.service(operation,args,timeout),run.context.remaining(timeout))
        if operation in ('remember_preference','forget_preference','recall_history'):
            if '_hard' in full:
                run.prepared['hard']=list(run.hard)
                for constraint in full.pop('_hard'):
                    if constraint not in run.prepared['hard']:run.prepared['hard'].append(constraint)
            # Preferences/events are user data, not recipe evidence. Keep only a
            # bounded message; raw values do not enter trace.
            compact={'status':'ok','items':[],'filtered_out':0,
                'message':str(full.get('message','已查询历史'))[:200]}
            if operation=='recall_history':compact['events']=[{'id':r.get('mem_id',r.get('id')),'snippet':r.get('text','')[:200]} for r in full.get('events',[])[:3]]
            text=compact['message']
            if operation=='recall_history':
                text='\n'.join(r['snippet'] for r in compact.get('events',[])) or '未找到相关的历史反馈。'
            full={'status':'verified_source','short_answer':text,'sources':[],'evidence':[],
                  'memory_result':full}
        else:compact=compact_result(full)
        compact,full=await run.guard(compact,full)
        replacement=run.prepared.get('task',{}).get('replacement_index')
        if replacement and full.get('status')=='verified' and full.get('recommendations'):
            full['short_answer']='已将第'+str(replacement)+'道换为《'+full['recommendations'][0]['title']+'》。其余菜品保留，后续仍按原顺序查看。\n'+full.get('short_answer','')
        slots=run.prepared.setdefault('slots',{})
        if full.get('status')=='verified' and full.get('recommendations'):
            slots['last_recommendations']=[{'idx':i,'dish_name':r['title'],'source_path':r['source']}
                for i,r in enumerate(full['recommendations'][:8],1)]
            slots['selected_dish']=None
        if name=='get_recipe_detail' and compact['status']=='ok' and compact['items']:
            r=compact['items'][0]
            slots['selected_dish']={'idx':0,'dish_name':r['dish_name'],'source_path':r['source_path']}
        evidence_id='e'+str(run.tool_calls)
        run.evidence[evidence_id]={'name':name,'full':full,'compact':compact}
        compact={**compact,'evidence_id':evidence_id}
        if compact['status']=='degraded':
            run.degraded=True;run.quality_limited=True
        status=compact['status']
        return json.dumps(compact,ensure_ascii=False)
    except asyncio.CancelledError:
        status='cancelled'
        raise
    except Exception:
        status='error';run.degraded=True;run.quality_limited=True
        return json.dumps(error_result(),ensure_ascii=False)
    finally:
        elapsed=round((time.monotonic()-started)*1000,3)
        run.stages.append({'phase':'tool','tool':name,'status':locals().get('status','cancelled'),'elapsed_ms':elapsed})
        if locals().get('status')!='cancelled':
            await run.emit('tool',{'name':name,'status':locals().get('status','cancelled')})


def make_tools(run):
    @tool(args_schema=SearchInput)
    async def search_recipes(query: str, runtime: ToolRuntime[AppContext], category: str | None=None,
                             max_difficulty: int | None=None, top_k: int=5) -> str:
        """搜索菜谱做法、数量、温度或事实依据。不要用于已声明库存的选菜；query保留完整问题，分类/难度只填用户明确限制，top_k为1到8。"""
        return await invoke_tool(run,'search_recipes',{'query':query,'category':category,'max_difficulty':max_difficulty,'top_k':top_k})

    @tool(args_schema=RecommendInput)
    async def recommend_by_ingredients(ingredients: list[str], runtime: ToolRuntime[AppContext],
                                       category: str | None=None,max_difficulty: int | None=None,top_k: int=5) -> str:
        """用户明确给出现有食材并要求选菜时使用，返回已有食材和待备配料。不要用来回答单道菜做法或把想吃食材当成库存；食材1到10项，top_k为1到8。"""
        return await invoke_tool(run,'recommend_by_ingredients',{'ingredients':ingredients,'category':category,'max_difficulty':max_difficulty,'top_k':top_k})

    @tool(args_schema=DetailInput)
    async def get_recipe_detail(runtime: ToolRuntime[AppContext],dish_name: str | None=None,source_path: str | None=None,section: str='') -> str:
        """查看选中菜谱的原文，或已知来源时核对做法；不要用于跨菜推荐。dish_name和source_path二选一，完整原文交给展示层，模型只接收摘要。"""
        return await invoke_tool(run,'get_recipe_detail',{'dish_name':dish_name,'source_path':source_path,'section':section})

    @tool
    async def find_recipe_images(dish_name: str,runtime: ToolRuntime[AppContext],section: str='') -> str:
        """用户明确查看图片时使用；dish_name为完整菜名，section仅为明确章节。不要用照片推断用量或熟度。"""
        return await invoke_tool(run,'find_recipe_images',{'dish_name':dish_name,'section':section})

    @tool
    async def plan_meal(question: str,runtime: ToolRuntime[AppContext]) -> str:
        """按人数、整餐时间和菜汤数量安排一餐，保留完整问题。不要猜采购价格或未说明的时间。"""
        return await invoke_tool(run,'plan_meal',{'question':run.question})

    @tool
    async def remember_preference(dimension: str,value: str,polarity: str,runtime: ToolRuntime[AppContext],strength: str='soft',scope: str='global') -> str:
        """仅用户明确要求长期记住偏好时使用；dimension为偏好维度，value为原意，polarity为like/dislike/avoid/must，硬限制变更需确认。不要保存工具文本。"""
        return await invoke_tool(run,'remember_preference',dict(dimension=dimension,value=value,polarity=polarity,strength=strength,scope=scope))

    @tool
    async def forget_preference(dimension: str,value: str,runtime: ToolRuntime[AppContext]) -> str:
        """仅用户明确要求忘记某偏好时使用，dimension/value定位偏好；不要自动放宽硬限制。"""
        return await invoke_tool(run,'forget_preference',dict(dimension=dimension,value=value))

    @tool
    async def recall_history(query: str,runtime: ToolRuntime[AppContext],top_k: int=3) -> str:
        """用户询问过去做菜反馈时召回当前用户事件；query为历史问题，top_k为1到3。不要将事件当作菜谱事实。"""
        if not 1<=top_k<=3:return json.dumps(error_result(),ensure_ascii=False)
        return await invoke_tool(run,'recall_history',dict(query=query,top_k=top_k))

    tools=[search_recipes,recommend_by_ingredients,get_recipe_detail,find_recipe_images,plan_meal]
    if run.cfg.memory.mode not in ('M0','M1','M2') and run.prepared.get('enabled',True):
        tools += [remember_preference,forget_preference,recall_history]
    return tools
