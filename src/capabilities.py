"""User-facing capabilities and explicit fallback contracts, checked by CI."""
import re
from dataclasses import dataclass

MODEL_STATES=('ok','timeout','5xx','429','invalid_json','breaker_open','slow','partial')
INFRA_STATES=('embedding_down','rerank_down','milvus_down')
STATES=MODEL_STATES+INFRA_STATES


@dataclass(frozen=True)
class Capability:
    full_path:str
    tools:tuple[str,...]
    degraded_paths:dict[str,str]
    unsupported_in_fallback:bool=False


def register_capability(full_path,tools=(),*,fallback='local_source_and_validated_rules',unsupported=False):
    paths={state:fallback for state in STATES}
    paths['ok']=full_path
    return Capability(full_path,tuple(tools),paths,unsupported)


CAPABILITIES={
    'meal_plan':register_capability('template_menu_planner',('plan_meal',),fallback='local_template_planner_independent_of_remote_retrieval'),
    'ingredient_recommend':register_capability('ingredient_table_scan',('recommend_by_ingredients',)),
    'constraint_refine':register_capability('session_reducer_and_source_filter',('task_recommend',)),
    'recipe_lookup':register_capability('named_original_or_checked_facts',('search_recipes','get_recipe_detail'),fallback='named_local_source_or_evidence_only_when_generation_unavailable'),
    'recipe_section':register_capability('source_bound_chapter',('get_recipe_detail',)),
    'recipe_images':register_capability('source_bound_images',('find_recipe_images',)),
    'recipe_ingredients':register_capability('source_ingredient_table',('recipe_ingredients',)),
    'follow_up':register_capability('session_source_reference'),
    'identity_capability':register_capability('capability_template'),
    'chitchat':register_capability('greeting_template'),
    'preference_update':register_capability('explicit_memory_rules_and_profile_store',('remember_preference','forget_preference')),
    'recall_history':register_capability('tenant_event_store',('recall_history',),fallback='tenant_history_or_friendly_unavailable'),
    'out_of_scope':register_capability('scope_explanation'),
    'unclear':register_capability('single_clarification',fallback='friendly_alternative_without_state_mutation',unsupported=True),
}


def validate_registry(registry=CAPABILITIES,covered=None,public_tools=()):
    from .nlu.router import INTENTS
    if not INTENTS<=registry.keys():raise ValueError('capability_intent_missing')
    tools={tool for capability in registry.values() for tool in capability.tools}
    if not set(public_tools)<=tools:raise ValueError('capability_tool_missing')
    for name,capability in registry.items():
        if set(capability.degraded_paths)!=set(STATES) or any(not v for v in capability.degraded_paths.values()):
            raise ValueError('capability_degradation_missing:'+name)
        if covered is not None and any((name,state) not in covered for state in STATES):
            raise ValueError('capability_test_missing:'+name)


def capability_for(plan,task=None):
    slots=plan.get('slots',{})
    if slots.get('request_kind')=='images':return 'recipe_images'
    if slots.get('request_kind')=='ingredients':return 'recipe_ingredients'
    return plan.get('intent','unclear')


def image_evidence_limit(plan):
    slots=plan.get('slots',{})
    if slots.get('request_kind') not in ('images','bundle') and plan.get('intent')!='recipe_images':
        return ''
    question=str(plan.get('original',''))
    if not re.search(r'熟|温度|配比|比例|用量|克|毫升|份量',question):
        return ''
    return '图片只能帮助判断外观和步骤位置，不能单独证明内部熟度、中心温度或配比。'


def degradation(plan,outputs,request_id,*,tool_degraded=False,tool_failed=False,fallback_used=False,limited_reason=None):
    """Separate internal fallback telemetry from actual loss of answer quality."""
    name=capability_for(plan);capability=CAPABILITIES.get(name,CAPABILITIES['unclear'])
    router_down=bool(plan.get('errors')) and plan.get('path') not in ('model','cache','fast')
    unsupported=router_down and capability.unsupported_in_fallback
    limited=tool_degraded or unsupported
    reason=limited_reason or ('router_unavailable' if router_down else 'dependency_unavailable' if tool_degraded else 'model_fallback' if fallback_used else None)
    notice=''
    if unsupported or tool_failed:
        notice='现在暂时无法完成这个请求，你可以告诉我具体想吃的菜名或手边的食材，我先帮你查。'
    elif limited:
        notice=('本次回答未通过依据核验，下面仅展示候选原文，不能作为完整结论。' if reason=='answer_not_verified'
                else '当前为简化模式：精排未完成，按基础检索顺序展示已核实的来源。' if reason=='rerank_unavailable'
                else '当前为简化模式：部分检索或回答服务未完成，已保留可核实的来源内容。')
    image_limit=image_evidence_limit(plan)
    if image_limit:notice=image_limit+('\n'+notice if notice else '')
    if notice:notice+='（请求编号：'+request_id+'）'
    return {'capability':name,'degraded':router_down or tool_degraded or fallback_used,'reason':reason,
        'quality_limited':limited,'unsupported_in_fallback':unsupported,'service_notice':notice,'request_id':request_id}
