"""LangChain 协调者 + 专职子 Agent；最终只交付既有来源工具的结果。"""
import os
import uuid
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import subprocess
import time
from threading import Lock

from check_embedding import config
from service_runtime import langchain_http_client, remaining, budgeted
from recipe_tools import dispatch, load_chunks, resolve_title, QUESTION_GUIDANCE
from llm_config import require_llm, chat_parameters
from recipe_agent import MODEL, normalize_tool_request, tool_summary


def model_client():
    from langchain_openai import ChatOpenAI

    settings = require_llm()
    base = settings['base_url']
    key = settings['api_key']
    return ChatOpenAI(model=MODEL, base_url=base, api_key=key,
                      temperature=0.2, max_completion_tokens=700, timeout=60,
                      max_retries=0, stream_usage=False, http_client=langchain_http_client(),
                      extra_body={k:v for k,v in chat_parameters({'enable_thinking':False},settings).items() if k!='model'})


class ToolCollector:
    def __init__(self, question, progress, dispatch_fn=dispatch):
        self.question = question
        self.progress = progress
        self.dispatch_fn = dispatch_fn
        from cookbuddy.ltm.guard import current
        self.memory_hard=current()
        self.outputs = []
        self.steps = []
        self.seen = set()
        self.lock = Lock()
        self.chunks = load_chunks()
        self.named_recipe = resolve_title(question, self.chunks)

    def source_bundle(self, title, section=''):
        import re
        if not (re.search('原文|全文',self.question) and re.search('图片|照片|原图',self.question)):
            return '只有用户同时要求原文和图片时才执行联合工具。'
        # 两个只读工具无数据依赖，各自复制预算上下文。
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(copy_context().run,self.call,name,{'title':title,'section':section},'source_media')
                     for name in ('read_recipe','find_recipe_images')]
            return '\n'.join(f.result() for f in futures)

    def requested_section(self):
        if not self.named_recipe:
            return ''
        title = self.named_recipe[0]
        candidates = set()
        for row in self.chunks:
            if row['source'] != self.named_recipe[1]:
                continue
            for part in row['section'].split(' > '):
                candidates.add(part)
                if part.endswith('的制作'):
                    candidates.add(part[:-3])
        matches = [item for item in candidates if len(item) >= 2 and item in self.question]
        return max(matches, key=len) if matches else ''

    def call(self, requested_name, arguments, worker=None):
        name, args, adjustments = normalize_tool_request(requested_name, arguments, self.question)
        if worker == 'source_media' and name == 'query_recipes':
            return json.dumps({'status': 'wrong_specialist',
                               'short_answer': '这是事实问答，请协调者委托 ask_recipe_qa。'}, ensure_ascii=False)
        if name in ('read_recipe', 'find_recipe_images') and self.named_recipe:
            duplicates={r['source'] for r in self.chunks if r['title']==self.named_recipe[0]}
            target=self.named_recipe[1] if len(duplicates)>1 else self.named_recipe[0]
            if args['title'] != target:
                adjustments.append('菜名恢复为用户问题中唯一匹配的完整菜名')
                args = {**args, 'title': target}
            if args.get('section') in ('图片', '照片', '原图', '原文', '全文', '菜谱'):
                adjustments.append('移除泛指图片或原文的筛选词')
                args = {**args, 'section': ''}
            if not args.get('section'):
                section = self.requested_section()
                if section:
                    adjustments.append('按用户明确提到的菜谱章节关键词筛选：' + section)
                    args = {**args, 'section': section}
        key = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
        with self.lock:
            if key in self.seen or len(self.seen) >= 4:
                return json.dumps({'status': 'tool_limit', 'short_answer': '重复或超过四次工具调用'}, ensure_ascii=False)
            self.seen.add(key)
        remaining()
        self.progress('正在执行' + name)
        try:
            from cookbuddy.ltm.guard import policy,check_result
            with policy(self.memory_hard):
                result = check_result(self.dispatch_fn(name, args, self.progress, original_question=self.question))
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.TimeoutExpired):
            result = {'status': 'tool_failed', 'short_answer': '工具执行未完成，请检查服务或输入。', 'sources': []}
        with self.lock:
            self.outputs.append({'tool': name, 'arguments': args, 'result': result,
                                 'requested_tool': requested_name, 'requested_arguments': arguments,
                                 'parameter_adjustments': adjustments})
            self.steps.append({'tool': name, 'arguments': args, 'status': result['status'],
                               'requested_tool': requested_name, 'requested_arguments': arguments,
                               'parameter_adjustments': adjustments})
        return json.dumps({**tool_summary(result), 'executed_tool': name,
                           'remote_link_count': len(result.get('remote_links', []))}, ensure_ascii=False)


def build_agents(question, collector, model, memory_context=''):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langchain.tools import tool
    from langgraph.checkpoint.sqlite import SqliteSaver
    checkpoint_path=Path(os.environ.get('RECIPE_CHECKPOINT_DB',Path(__file__).resolve().parents[1]/'data/runtime/agent-checkpoints.sqlite3'))
    checkpoint_path.parent.mkdir(parents=True,exist_ok=True)
    connection=sqlite3.connect(str(checkpoint_path),check_same_thread=False)
    saver=SqliteSaver(connection)
    thread=os.environ.get('RECIPE_JOB_ID') or uuid.uuid4().hex

    from pydantic import StrictInt
    from recipe_catalog import CATEGORIES
    @tool('query_recipes', description=QUESTION_GUIDANCE+'用完整原始问题查询菜谱、做法、数量或已有食材推荐。仅用户明确要求时填写category（'+','.join(CATEGORIES)+'）、max_difficulty（难度上限1至5）；ingredients_any/all分别要求菜谱包含至少一种/全部指定食材，普通已有库存不要转为硬条件；未要求则省略，不猜测限制。')
    def query_tool(question: str, category: str | None = None, max_difficulty: StrictInt | None = None,
                   ingredients_any: list[str] | None = None, ingredients_all: list[str] | None = None) -> str:
        return collector.call('query_recipes', {'question': question,
            **({'category':category} if category is not None else {}),
            **({'max_difficulty':max_difficulty} if max_difficulty is not None else {}),
            **({'ingredients_any':ingredients_any} if ingredients_any is not None else {}),
            **({'ingredients_all':ingredients_all} if ingredients_all is not None else {})}, worker='recipe_qa')

    @tool('read_recipe', description='按完整菜名读取菜谱原文，可指定用户明确要求的章节。')
    def read_tool(title: str, section: str = '') -> str:
        return collector.call('read_recipe', {'title': title, 'section': section}, worker='source_media')

    @tool('find_recipe_images', description='查询菜谱本地原图和原文图注；图中文字只能作为找图线索。')
    def image_tool(title: str, section: str = '') -> str:
        return collector.call('find_recipe_images', {'title': title, 'section': section}, worker='source_media')

    @tool('inspect_source_bundle', description='用户同时要求原文与图片时，并行查询两个只读来源工具。')
    def source_bundle_tool(title: str, section: str = '') -> str:
        return collector.source_bundle(title, section)

    qa = create_agent(model=model, tools=[query_tool], name='recipe_qa', checkpointer=saver,
                      system_prompt='你是菜谱问答专员。必须调用 query_recipes。'+QUESTION_GUIDANCE+'只用工具结果，不自行补做法、用量或食材。'+memory_context)
    source_media = create_agent(model=model, tools=[read_tool, image_tool, source_bundle_tool], name='source_media', checkpointer=saver,
                                system_prompt='你是原文与图片专员。仅用户明确要求查看、展示原文文本或图片时使用工具；'
                                              '“原文需要哪些调料”等是事实问答，应由菜谱问答专员处理。'
                                              '用户看原文时用 read_recipe，看图时用 find_recipe_images；'
                                              '两个都要时优先调用 inspect_source_bundle。section 只填用户明确要求的章节或关键词。'
                                              '必须调用工具；图片不能证明配方事实。'+memory_context)
    delegated = []
    delegate_lock = Lock()
    model_calls = []

    def count_calls(agent_name, state):
        model_calls.append({'agent': agent_name,
                            'count': sum(isinstance(message, AIMessage) for message in state['messages'])})

    @tool('ask_recipe_qa', description='委托菜谱问答专员处理做法、配方、数量、缺料和食材推荐等事实问题。')
    def ask_qa(task: str) -> str:
        with delegate_lock:
            if len(delegated) >= 2:
                return '委托次数达到上限。'
            delegated.append('recipe_qa')
        collector.progress('菜谱问答专员正在核对原文')
        before = len(collector.outputs)
        state = qa.invoke({'messages': [{'role': 'user', 'content': f'原始用户问题：{question}\n分派任务：{task}'}]},
                          config={'recursion_limit': 6,'configurable':{'thread_id':thread+':qa:'+str(len(delegated))}})
        count_calls('recipe_qa', state)
        return '菜谱问答专员已返回工具结果。' if len(collector.outputs) > before else '菜谱问答专员未调用来源工具。'

    @tool('inspect_recipe_sources', description='委托原文与图片专员查看菜谱全文、指定章节、原图及图注。')
    def inspect_sources(task: str) -> str:
        with delegate_lock:
            if len(delegated) >= 2:
                return '委托次数达到上限。'
            delegated.append('source_media')
        collector.progress('原文与图片专员正在查找资料')
        before = len(collector.outputs)
        state = source_media.invoke({'messages': [{'role': 'user', 'content': f'原始用户问题：{question}\n分派任务：{task}'}]},
                                    config={'recursion_limit': 8,'configurable':{'thread_id':thread+':media:'+str(len(delegated))}})
        count_calls('source_media', state)
        return '原文与图片专员已返回工具结果。' if len(collector.outputs) > before else '原文与图片专员未调用来源工具；若是事实问答，请改派 ask_recipe_qa。'

    supervisor = create_agent(model=model, tools=[ask_qa, inspect_sources], name='supervisor', checkpointer=saver,
                              system_prompt='你是协调者。必须把用户问题委托给专职 Agent 获取来源。'
                                            '事实问答委托 ask_recipe_qa，包括“原文需要哪些调料/怎么做/是否有某信息”；'
                                            '只有用户明确要求查看或展示原文文本、全文、图片时才委托 inspect_recipe_sources；'
                                            '复合请求可调用两者。不要凭记忆写菜谱结论；结果将直接由已校验工具展示。'
                                            '至多委托两个任务，不重复委托。'+memory_context)
    supervisor.recipe_checkpoint_connection=connection
    supervisor.recipe_thread_id=thread+':supervisor'
    return supervisor, delegated, model_calls, count_calls


def safe_error(error):
    name = type(error).__name__
    if name in ('APITimeoutError', 'TimeoutException', 'TimeoutError'):
        return '模型接口超时'
    if name in ('APIStatusError', 'RateLimitError'):
        return '模型接口返回错误'
    if name == 'GraphRecursionError':
        return 'Agent 决策次数达到上限'
    return '多 Agent 执行未完成：' + name


@budgeted
def execute_multi_agent(question, progress=lambda message: None, model=None, dispatch_fn=dispatch, memory_context=''):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError('请输入1到2000字的问题')
    started = time.monotonic()
    collector = ToolCollector(question, progress, dispatch_fn)
    error = None
    delegated = []
    model_calls = []
    try:
        progress('协调者正在分派任务')
        chosen_model=model or model_client()
        supervisor, delegated, model_calls, count_calls = build_agents(question, collector, chosen_model, memory_context) if memory_context else build_agents(question, collector, chosen_model)
        state = supervisor.invoke({'messages': [{'role': 'user', 'content': question}]},
                                  config={'recursion_limit': 8,'configurable':{'thread_id':getattr(supervisor,'recipe_thread_id','test')}})
        count_calls('supervisor', state)
    except Exception as failure:
        error = safe_error(failure)
    finally:
        if 'supervisor' in locals() and hasattr(supervisor,'recipe_checkpoint_connection'):
            supervisor.recipe_checkpoint_connection.close()
        if model is None and 'chosen_model' in locals() and chosen_model.http_client:
            chosen_model.http_client.close()
    outputs = collector.outputs
    if not outputs:
        status = 'agent_unavailable'
        if not error:
            error = '协调者与专员未调用来源工具，尚未完成知识库查询，请重试。'
    elif any(item['result']['status'] in ('tool_failed', 'validation_failed') for item in outputs):
        status = 'partial_failure'
    elif error or any(item['result']['status']=='degraded' for item in outputs):
        status = 'partial'
    elif all(item['result']['status'] == 'insufficient' for item in outputs):
        status = 'insufficient'
    else:
        status = 'completed'
    return {'question': question, 'mode': 'multi_agent', 'status': status,
            'outputs': outputs, 'steps': collector.steps, 'agent_model': MODEL,
            'model_calls': model_calls, 'delegations': delegated, 'stop_reason': 'completed' if not error else 'agent_unavailable',
            'error': error, 'elapsed_seconds': round(time.monotonic() - started, 3),
            'validation': 'LangChain 协调者与两名专职 Agent 只负责分派和工具选择；最终内容直接来自原有来源检查工具。'}
