"""聊天模型独立配置；不影响已入库的 Embedding 空间。"""
from urllib.parse import urlparse
from check_embedding import config


def llm_settings(values=None):
    values = config() if values is None else values
    base = values.get('LLM_BASE_URL', 'https://api.deepseek.com/v1').rstrip('/')
    key = values.get('LLM_API_KEY', '')
    model = values.get('LLM_MODEL', 'deepseek-flash')
    url = urlparse(base)
    local_http = url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')
    if ((url.scheme != 'https' and not local_http) or not url.hostname or url.username or url.password
            or url.query or url.fragment or not model.strip()):
        raise ValueError('LLM 地址或模型配置无效')
    return {'base_url': base, 'api_key': key, 'model': model}


def require_llm(values=None):
    settings = llm_settings(values)
    key = settings['api_key']
    if not key or set(key) <= {'X'}:
        raise ValueError('请在本地 .env 配置 LLM_API_KEY')
    return settings


def chat_parameters(parameters, settings):
    """供应商特有字段仅发送给对应接口；DeepSeek 禁用思考保留 JSON/工具调用。"""
    result = {**parameters, 'model': settings['model']}
    if urlparse(settings['base_url']).hostname == 'api.deepseek.com':
        result.pop('enable_thinking', None)
        result.pop('thinking_budget', None)
        result['thinking'] = {'type': 'disabled'}
    elif urlparse(settings['base_url']).hostname == 'api.siliconflow.cn':
        result.pop('thinking', None)
        result.setdefault('enable_thinking', False)
    elif urlparse(settings['base_url']).hostname == 'apihub.agnes-ai.com':
        result.pop('thinking',None)
        result.pop('enable_thinking',None)
        result.pop('thinking_budget',None)
        result['chat_template_kwargs']={**result.get('chat_template_kwargs',{}),'enable_thinking':False}
    return result
