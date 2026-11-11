from langchain.agents import create_agent
from .context import AppContext
from .state import RecipeState
from .tools import make_tools
from .prompts import BASE_SYSTEM_PROMPT
from .middleware.trace_mw import TraceMiddleware
from .middleware.resilience_mw import ResilienceMiddleware
from .middleware.memory_mw import MemoryMiddleware
from .middleware.guard_mw import GuardMiddleware
from .middleware.summary_mw import ContextSummaryMiddleware


def build_agent(cfg, run, model, checkpointer, store=None, fallback=None):
    # Verified against LangChain 1.4.4 by test_l0: before hooks run in listed
    # order, after hooks in reverse; wrappers enter forward and leave reverse.
    middleware=[TraceMiddleware(run),ResilienceMiddleware(run,fallback)]
    if cfg.memory.mode!='M0' and run.prepared.get('enabled',True):
        middleware.append(MemoryMiddleware(run))
        if cfg.memory.mode!='M1':middleware.append(ContextSummaryMiddleware(run))
    middleware.append(GuardMiddleware(run))
    return create_agent(model,make_tools(run),system_prompt=BASE_SYSTEM_PROMPT,
        middleware=middleware,state_schema=RecipeState,context_schema=AppContext,
        checkpointer=checkpointer,store=store)
