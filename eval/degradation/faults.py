"""Deterministic dependency faults; no network, sleeps, prompts or credentials."""
import copy
import json
from urllib.error import HTTPError

STATES=('ok','timeout','5xx','429','invalid_json','breaker_open','slow','partial')


class FaultyModel:
    def __init__(self,state,response):
        if state not in STATES:raise ValueError('unknown_fault')
        self.state=state;self.response=response;self.calls=0

    def __call__(self,*args):
        self.calls+=1
        if self.state in ('timeout','slow'):raise TimeoutError('injected_read_timeout')
        if self.state in ('5xx','429'):
            raise HTTPError('https://example.invalid',503 if self.state=='5xx' else 429,'injected',{'Retry-After':'0'},None)
        if self.state=='invalid_json':raise json.JSONDecodeError('injected','',0)
        if self.state=='breaker_open':
            from service_runtime import CircuitOpen
            raise CircuitOpen('injected')
        return copy.deepcopy(self.response)
