from typing import NotRequired
from langchain.agents import AgentState


class RecipeState(AgentState):
    slots: NotRequired[dict]
    hard_constraints: NotRequired[list]
    pending_confirmation: NotRequired[dict | None]
    degraded: NotRequired[bool]
    filtered_out_total: NotRequired[int]
