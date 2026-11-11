"""Strict configuration-only core ablations; no runtime monkey patch required."""
from pathlib import Path
import re
import yaml


def load_core_profiles(folder):
    folder=Path(folder);cache={}
    def read(name,seen=()):
        if name in seen or not re.fullmatch(r'[A-Za-z0-9-]+',name):raise ValueError('invalid_profile_inheritance')
        if name in cache:return cache[name].copy()
        config=yaml.safe_load((folder/(name+'.yaml')).read_text())
        if not isinstance(config,dict) or set(config)-{'extends','profile'}:raise ValueError('invalid_core_profile')
        value=read(config['extends'],(*seen,name)) if config.get('extends') else {}
        value.update(config.get('profile',{}))
        required={'corpus','prefix','overview','normalize','hybrid','filter','rerank','agent'}
        if set(value)!=required or value['corpus'] not in ('h2','fixed512','closed'):raise ValueError('invalid_core_profile')
        if any(type(v) is not bool for k,v in value.items() if k!='corpus'):raise ValueError('invalid_core_profile')
        cache[name]=value;return value.copy()
    return {name:read(name) for name in [*(f'E{i}' for i in range(9)),*(f'A-{i}' for i in range(1,7))]}
