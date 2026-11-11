from functools import lru_cache
from pathlib import Path
import yaml

@lru_cache(maxsize=1)
def settings():
    config=yaml.safe_load((Path(__file__).resolve().parents[2]/'config/nlu.yaml').read_text())
    fuzzy=config['fuzzy'];router=config['router']
    if not 0<=fuzzy['t_mid']<fuzzy['t_high']<=1 or not 0<fuzzy['margin']<1:
        raise ValueError('invalid_nlu_thresholds')
    if not 0<router['timeout_s']<=30 or router['retries'] not in (0,1):
        raise ValueError('invalid_nlu_budget')
    return config
