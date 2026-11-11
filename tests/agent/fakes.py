"""Scripted tool-capable chat model; no network or credentials."""
from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedModel(BaseChatModel):
    replies: list[Any] = Field(default_factory=list)
    position: int = 0

    @property
    def _llm_type(self):
        return 'scripted-acceptance-model'

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self.replies[min(self.position, len(self.replies)-1)] if self.replies else '完成'
        self.position += 1
        if isinstance(reply, Exception):
            raise reply
        message = reply if isinstance(reply, AIMessage) else AIMessage(content=reply)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


def tool_call(name, args, ident='call-1'):
    return AIMessage(content='', tool_calls=[{'name':name, 'args':args, 'id':ident}])
