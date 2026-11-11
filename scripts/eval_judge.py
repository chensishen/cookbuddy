"""独立评审配置；不更改应用的聊天模型。"""
import os
from urllib.parse import urlparse
from check_embedding import config
from llm_config import require_llm


def execution_completed(row):
    status=(row.get('agent_record') or {}).get('status')
    return not row.get('failure_type') and (status is None or status in {'completed','insufficient'})


def has_evaluable_answer(row):
    result=row.get('result',{})
    return (not row.get('failure_type') and result.get('status') in
        {'verified','verified_source','insufficient','baseline_unreviewed','degraded'}
        and isinstance(result.get('short_answer'),str) and bool(result['short_answer'].strip()))


def judge_settings():
    values=config();production=require_llm(values)
    base=os.environ.get('EVAL_JUDGE_BASE_URL',values.get('EVAL_JUDGE_BASE_URL',production['base_url'])).rstrip('/')
    model=os.environ.get('EVAL_JUDGE_MODEL',values.get('EVAL_JUDGE_MODEL',production['model']))
    key=os.environ.get('EVAL_JUDGE_API_KEY',values.get('EVAL_JUDGE_API_KEY',''))
    if not key and base==production['base_url']:key=production['api_key']
    if urlparse(base).hostname!=urlparse(production['base_url']).hostname and not key:
        raise ValueError('不同评审供应商需要独立密钥')
    return require_llm({'LLM_BASE_URL':base,'LLM_MODEL':model,'LLM_API_KEY':key})
