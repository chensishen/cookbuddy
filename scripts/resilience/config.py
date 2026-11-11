from functools import lru_cache
from pathlib import Path
import yaml


@lru_cache(maxsize=1)
def settings():
    value=yaml.safe_load((Path(__file__).resolve().parents[2]/'config/resilience.yaml').read_text())
    b=value['breaker']
    if b['force_state'] is not None:raise ValueError('force_state_is_test_only_use_injected_policy')
    if not (b['window_seconds']>0 and b['min_requests']>=1 and 0<b['failure_ratio']<=1
            and 0<b['initial_open_seconds']<=b['max_open_seconds'] and b['half_open_successes']>=1):
        raise ValueError('invalid_breaker_policy')
    return value
