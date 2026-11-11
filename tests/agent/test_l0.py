import unittest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import tool, ToolRuntime
from langgraph.checkpoint.memory import InMemorySaver
from dataclasses import dataclass
from .fakes import ScriptedModel, tool_call


class L0Tests(unittest.IsolatedAsyncioTestCase):
    async def test_async_model_tool_context_checkpoint_and_hook_order(self):
        @dataclass
        class Context:
            user_id: str
        marks=[]
        class Marker(AgentMiddleware):
            def __init__(self, label): self.label=label
            @property
            def name(self): return self.label
            async def abefore_agent(self, state, runtime): marks.append(self.label+':before')
            async def aafter_agent(self, state, runtime): marks.append(self.label+':after')
            async def awrap_tool_call(self, request, handler):
                marks.append(self.label+':enter')
                result=await handler(request)
                marks.append(self.label+':exit')
                return result
        @tool
        async def lookup(runtime: ToolRuntime[Context]) -> str:
            """Read the current actor; no model-controlled identity."""
            return runtime.context.user_id
        saver=InMemorySaver()
        model=ScriptedModel(replies=[tool_call('lookup',{}),'完成'])
        agent=create_agent(model, [lookup], context_schema=Context, checkpointer=saver,
                           middleware=[Marker('outer'), Marker('inner')])
        cfg={'configurable':{'thread_id':'l0'}}
        result=await agent.ainvoke({'messages':[{'role':'user','content':'查询'}]},cfg,context=Context('actor-A'))
        self.assertEqual(result['messages'][-2].content,'actor-A')
        self.assertIsNotNone(await saver.aget_tuple(cfg))
        self.assertEqual(marks,['outer:before','inner:before','outer:enter','inner:enter',
                                'inner:exit','outer:exit','inner:after','outer:after'])
        self.assertNotIn('runtime',lookup.args)


if __name__=='__main__': unittest.main()
