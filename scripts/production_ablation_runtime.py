"""只由隔离实验 worker 显式安装的生产组件干预；应用不导入本模块。"""
from contextlib import ExitStack
from pathlib import Path
import json
import os
import subprocess
from unittest.mock import patch

PROFILES={
    'E9':{},'P-A1':{'RAG_RERANK':'0'},'P-A2':{'RAG_RETRIEVAL':'vector'},
    'P-A3':{'RAG_VERSION':'ablation_bounded_v4_plain'},'P-A4':{'remove_filters':True},
    'P-A5':{'RAG_VERSION':'ablation_bounded_v4_raw','remove_aliases':True},
    'P-A6':{'remove_agent':True},'P-A7':{'remove_source_fact':True}}


def install(profile,audit):
    """父进程和阶段子进程使用相同局部干预，不关闭长期记忆硬约束。"""
    import ingredient_recommendation as ingredients
    import retrieval_candidates as candidates
    import search_filters as filters
    import recipe_fact_answer as facts
    import recipe_tools as tools
    from check_embedding import ROOT
    stack=ExitStack();options=PROFILES[profile]
    if options.get('remove_aliases'):
        stack.enter_context(patch.dict(ingredients.ALIASES,{},clear=True))
        stack.enter_context(patch.object(candidates,'normalize_recipe_aliases',lambda question:question))
    if options.get('remove_filters'):
        stack.enter_context(patch.object(filters,'current_filters',lambda:{}))
        stack.enter_context(patch.object(filters,'explicit_question_filters',lambda *args,**kwargs:{}))
        recommend=ingredients.recommend
        def unfiltered_recommend(*args,**kwargs):
            kwargs.pop('category',None);kwargs.pop('max_difficulty',None)
            return recommend(*args,**kwargs)
        stack.enter_context(patch.object(ingredients,'recommend',unfiltered_recommend))
    source_fact=facts.source_fact
    def observed_fact(question,chunks):
        result=None if options.get('remove_source_fact') else source_fact(question,chunks)
        audit.append({'stage':'source_fact','disabled':bool(options.get('remove_source_fact')),
                      'matched':result is not None,'answer_method':result.get('answer_method') if result else None})
        return result
    stack.enter_context(patch.object(facts,'source_fact',observed_fact))
    run_stage=tools.run_stage
    def observed_stage(script,argument,folder):
        entry={'stage':script,'outcome':'started'};audit.append(entry)
        try:
            path,result=run_stage(script,argument,folder)
            entry.update(outcome='completed',record=str(path.relative_to(ROOT)))
            if script=='rerank_test_recipes.py':
                entry.update(rerank_enabled=result.get('rerank_enabled'),fallback=result.get('rerank_fallback'))
            if script=='search_test_recipes.py':
                entry.update(collection=result['collection'],retrieval_mode=result['retrieval_mode'],
                    metadata_filters=result['metadata_filters'],metadata_filter_applied=result['metadata_filter_applied'])
            return path,result
        except Exception as error:
            entry.update(outcome='failed',failure_type=type(error).__name__);raise
    stack.enter_context(patch.object(tools,'run_stage',observed_stage))
    # 仅替换本项目三个阶段的 Python 入口；deadline/硬过滤/真实限流环境原样传递。
    original_run=subprocess.run
    allowed={'search_test_recipes.py','rerank_test_recipes.py','generate_test_answer.py'}
    def stage_run(command,*args,**kwargs):
        if (isinstance(command,list) and len(command)>=2 and Path(command[1]).parent==ROOT/'scripts'
                and Path(command[1]).name in allowed):
            command=[command[0],str(ROOT/'scripts/production_ablation_worker.py'),'--stage',command[1],*command[2:]]
        return original_run(command,*args,**kwargs)
    stack.enter_context(patch.object(subprocess,'run',stage_run))
    return stack


def profile_environment(profile):
    return {key:value for key,value in PROFILES[profile].items() if key.startswith('RAG_')}


def audit_summary(audit):
    stages=[row['stage'] for row in audit]
    return {'retrieval_traversed':'search_test_recipes.py' in stages,
            'source_fact_matched':any(row['stage']=='source_fact' and row.get('matched') for row in audit),
            'stage_failures':sum(row.get('outcome')=='failed' for row in audit)}
