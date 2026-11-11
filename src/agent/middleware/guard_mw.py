import json
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage, SystemMessage
from ..tools import error_result
from ..prompts import BASE_SYSTEM_PROMPT


class GuardMiddleware(AgentMiddleware):
    def __init__(self, run):self.run=run

    async def awrap_model_call(self, request, handler):
        # Even M0 must enforce hard constraints explicitly present this turn.
        if self.run.cfg.memory.mode=='M0':
            request=request.override(system_message=SystemMessage(content=BASE_SYSTEM_PROMPT+'\n【必须遵守】'+json.dumps(self.run.hard,ensure_ascii=False)))
        return await handler(request)

    async def awrap_tool_call(self, request, handler):
        try:
            # The decorated tool schema excludes identity. Strip malicious extras
            # before Pydantic validation, without putting them in error messages.
            call=request.tool_call
            args={k:v for k,v in call['args'].items() if k not in ('user_id','session_id')}
            result=await handler(request.override(tool_call={**call,'args':args}))
            if not isinstance(result,ToolMessage):raise ValueError('unexpected_tool_response')
            value=json.loads(result.content)
            ident=value.get('evidence_id')
            if ident and ident not in self.run.evidence:raise ValueError('unknown_evidence')
            if value.get('status') not in ('ok','empty','error','degraded'):raise ValueError('invalid_tool_status')
            # invoke_tool performs service + code guard before creating this
            # message. Guard also sanitizes framework schema-validation errors.
            if any(len(r.get('snippet',''))>200 for r in value.get('items',[])):raise ValueError('tool_result_too_large')
            return result
        except Exception:
            return ToolMessage(content=json.dumps(error_result(),ensure_ascii=False),
                tool_call_id=request.tool_call['id'],name=request.tool_call['name'],status='error')
