from dataclasses import dataclass, field, replace, asdict
from datetime import datetime, timezone
import math
import uuid
from check_embedding import ROOT
import json
from contextvars import ContextVar
CONFIG_OVERRIDE=ContextVar('memory_evaluation_configuration',default=None)

SINGLE={'spice_level','max_difficulty','time_budget','household_size','diet_type'}
SESSION_ONLY={'inventory','max_difficulty','time_budget','household_size'}
DIMS=SINGLE|{'allergy','avoid_ingredient','equipment','flavor','cuisine','inventory'}
HARD={'allergy','diet_type','equipment','avoid_ingredient'}

def now():return datetime.now(timezone.utc)
def stamp(value=None):return (value or now()).isoformat()
def settings():return CONFIG_OVERRIDE.get() or json.loads((ROOT/'config/memory.yaml').read_text())
def identity(uid):
    if not isinstance(uid,str) or not uid.strip():raise ValueError('memory_user_required')
    return uid

@dataclass
class Candidate:
    dimension:str
    value:str
    polarity:str='like'
    strength:str='soft'
    source:str='explicit'
    scope:str='global'
    op:str='set'
    confidence:float=.7
    evidence_text:str=''
    valid_to:str|None=None

    def __post_init__(self):
        if (self.dimension not in DIMS or self.polarity not in {'like','dislike','avoid','must'}
            or self.strength not in {'hard','soft'} or self.source not in {'explicit','implicit','feedback','onboarding'}
            or self.scope not in {'session','temporary','global'} or self.op not in {'set','revoke'}
            or not isinstance(self.value,str) or not self.value.strip() or len(self.value)>100
            or type(self.confidence) not in (int,float) or not math.isfinite(self.confidence) or not 0<=self.confidence<=1):
            raise ValueError('invalid_candidate')
        if self.dimension=='flavor':
            self.value={'清淡口味':'清淡','重口味':'重口','重口口味':'重口','酸甜口味':'酸甜'}.get(self.value,self.value)
        ranges={'spice_level':(0,3),'max_difficulty':(1,5),'time_budget':(1,1440),'household_size':(1,100)}
        if self.dimension in ranges:
            lo,hi=ranges[self.dimension]
            if not self.value.isdigit() or not lo<=int(self.value)<=hi:raise ValueError('invalid_dimension_value')
        if self.dimension=='diet_type' and self.value not in {'vegetarian','vegan','halal','none'}:raise ValueError('invalid_diet')
        if self.dimension=='equipment' and not self.value.startswith(('无','仅','没有','只有','不用','有')):raise ValueError('equipment_requires_absence_only_or_ownership')
        if self.dimension in {'allergy','diet_type'}:self.strength='hard'
        if self.dimension=='equipment' and not self.value.startswith('有'):self.strength='hard'
        if self.dimension=='inventory' or self.dimension in SESSION_ONLY and self.op=='set':
            self.scope='session';self.valid_to=None
        if self.valid_to is None and self.scope!='session':
            days=settings()['long_term']['temporary_default_days'] if self.scope=='temporary' else None
            if days:
                from datetime import timedelta
                self.valid_to=stamp(now()+timedelta(days=days))
        if self.valid_to is not None:
            value=datetime.fromisoformat(self.valid_to)
            if value.tzinfo is None:raise ValueError('expiry_requires_timezone')

@dataclass
class PrefItem:
    id:str
    user_id:str
    dimension:str
    value:str
    polarity:str
    strength:str
    confidence:float
    source:str
    scope:str
    status:str='active'
    strength_score:float=.7
    evidence_count:int=1
    session_count:int=1
    valid_from:str=field(default_factory=stamp)
    valid_to:str|None=None
    supersedes:str|None=None
    version:int=1
    sensitive:int=0
    source_turn_ref:str=''
    created_at:str=field(default_factory=stamp)
    updated_at:str=field(default_factory=stamp)
    last_confirmed_at:str|None=None
    last_used_at:str|None=None

@dataclass
class Decision:
    action:str
    target_id:str|None=None
    new_item:PrefItem|None=None
    ask:dict|None=None
    reason:str=''


def item(c,uid='',previous=None,status='active'):
    return PrefItem(uuid.uuid4().hex,uid,c.dimension,c.value,c.polarity,c.strength,c.confidence,c.source,c.scope,
        status=status,valid_to=c.valid_to,supersedes=previous.id if previous else None,
        version=previous.version+1 if previous else 1,sensitive=int(c.dimension=='allergy'))

def effective(p,at=None):
    if p.strength=='hard':return p.confidence
    age=max(0,((at or now())-datetime.fromisoformat(max(p.updated_at,p.last_confirmed_at or p.updated_at))).total_seconds()/86400)
    cfg=settings()['long_term']['half_life_days']
    return p.confidence*2**(-age/cfg['implicit' if p.source=='implicit' else 'explicit_soft'])
