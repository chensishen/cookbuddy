"""Immutable per-run configuration; experiments only override explicit fields."""
from pathlib import Path
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT=Path(__file__).resolve().parents[2]


class Frozen(BaseModel):
    model_config=ConfigDict(frozen=True, extra='forbid')


class AgentConfig(Frozen):
    enabled: bool=True
    max_model_calls: int=Field(8,ge=1,le=20)
    max_tool_calls: int=Field(5,ge=1,le=10)
    request_timeout_s: float=Field(30,gt=0,le=180)
    stream: bool=True
    direct: bool=True


class RerankConfig(Frozen):
    enabled: bool=True
    timeout_s: float=Field(8,gt=0,le=12)


class RetrievalConfig(Frozen):
    mode: str=Field('hybrid',pattern='^(dense|sparse|hybrid)$')
    recall_top_n: int=Field(30,ge=1,le=50)
    top_k: int=Field(5,ge=1,le=8)
    augment: bool=True
    rerank: RerankConfig=RerankConfig()
    tool_timeout_s: float=Field(8,gt=0,le=60)


class MemoryConfig(Frozen):
    mode: str=Field('M6',pattern='^M[0-6]$')
    window_turns: int=Field(6,ge=1,le=20)
    window_max_tokens: int=Field(4000,ge=100,le=16000)
    queue_capacity: int=Field(64,ge=1,le=1000)

    @field_validator('mode',mode='before')
    @classmethod
    def named_mode(cls,value):
        # M4 is the historical no-confirmation control, available only by its
        # explicit experiment label. Live safety checks remain enabled in M4.
        return {'off':'M0','window':'M1','slots':'M2','profile':'M3',
                'profile_conflict':'M5','full':'M6'}.get(value,value)


class LLMConfig(Frozen):
    timeout_s: float=Field(20,gt=0,le=60)
    max_retries: int=Field(2,ge=0,le=2)
    fallback_env: str='RECIPE_GENERATION_FALLBACK'


class ResilienceConfig(Frozen):
    concurrency: int=Field(2,ge=1,le=32)
    queue_timeout_s: float=Field(5,gt=0,le=30)


class TraceConfig(Frozen):
    structured_log: bool=True
    checkpoint_warn_bytes: int=Field(64000,ge=1000)
    # External content tracing is intentionally unsupported by this adapter.
    langsmith: bool=False


class CheckpointConfig(Frozen):
    backend: str=Field('sqlite',pattern='^(sqlite|postgres)$')
    pool_size: int=Field(4,ge=1,le=16)


class Config(Frozen):
    agent: AgentConfig=AgentConfig()
    retrieval: RetrievalConfig=RetrievalConfig()
    memory: MemoryConfig=MemoryConfig()
    llm: LLMConfig=LLMConfig()
    resilience: ResilienceConfig=ResilienceConfig()
    trace: TraceConfig=TraceConfig()
    checkpoint: CheckpointConfig=CheckpointConfig()


def merge(base, overrides):
    result=dict(base)
    for key,value in overrides.items():
        result[key]=merge(result.get(key,{}),value) if isinstance(value,dict) else value
    return result


def load_config(path=None, experiment=None):
    values=yaml.safe_load(Path(path or ROOT/'config/agent.yaml').read_text()) or {}
    if experiment: values=merge(values,yaml.safe_load(Path(experiment).read_text()) or {})
    cfg=Config.model_validate(values)
    if cfg.trace.langsmith: raise ValueError('external_tracing_not_authorized')
    return cfg
