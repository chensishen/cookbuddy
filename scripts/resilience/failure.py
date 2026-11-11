import asyncio
import json
from urllib.error import HTTPError,URLError

DEPENDENCY_FAILURES={'timeout','connect_error','http_5xx'}


def classify(error):
    # BudgetExceeded includes cancellation/deadline, not a provider read timeout.
    if type(error).__name__ in ('BudgetExceeded','CancelledError') or isinstance(error,asyncio.CancelledError):return 'client_cancel'
    if type(error).__name__=='AdmissionTimeout':return 'admission_timeout'
    if type(error).__name__=='CircuitOpen':return 'breaker_open'
    if isinstance(error,HTTPError):
        return 'http_429' if error.code==429 else 'http_5xx' if error.code>=500 else 'internal_error'
    if isinstance(error,TimeoutError):return 'timeout'
    if isinstance(error,(ConnectionError,URLError)):return 'connect_error'
    if isinstance(error,json.JSONDecodeError):return 'invalid_json'
    if isinstance(error,ValueError):return 'validation_error'
    return 'internal_error'


def dependency(phase):
    if phase.startswith('nlu_'):return 'router_model'
    if 'embed' in phase or phase in ('intent_nlu_match','intent_nlu_candidate'):return 'embedding'
    if 'rerank' in phase:return 'rerank'
    if phase.startswith('memory_'):return 'memory_model'
    if 'milvus' in phase:return 'milvus'
    return 'main_model'
