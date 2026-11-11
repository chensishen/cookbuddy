import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch,AsyncMock
from urllib.error import HTTPError,URLError

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from stage_diagnostics import failure_reason,collect,record,sanitized
from service_runtime import request_budget,AdmissionTimeout,BudgetExceeded,CircuitOpen,guarded_call,RuntimeStore


class DiagnosticTests(unittest.TestCase):
    def test_failure_types_are_distinct_and_contain_no_exception_text(self):
        cases=[(subprocess.TimeoutExpired('secret',2),'stage_deadline'),
            (BudgetExceeded('secret'),'stage_deadline'),(AdmissionTimeout('secret'),'admission_timeout'),
            (CircuitOpen('secret'),'circuit_open'),(HTTPError('secret',429,'secret',{},None),'rate_limited'),
            (HTTPError('secret',503,'secret',{},None),'upstream_http_error'),
            (TimeoutError('secret'),'upstream_timeout'),(URLError('secret'),'network_error'),
            (ValueError('secret'),'result_validation'),(RuntimeError('secret'),'stage_failure')]
        for error,reason in cases:
            with self.subTest(reason=reason):self.assertEqual(failure_reason(error),reason)

    def test_real_child_timeout_keeps_last_phase_and_observed_duration(self):
        import ask_test_recipes
        original=subprocess.run
        code="import sys,time;sys.path.insert(0,'scripts');from stage_diagnostics import record;record('imports');record('data_load');record('prepare');record('admission');record('api');time.sleep(3)"
        def launch(*args,**kwargs):return original([sys.executable,'-c',code],**kwargs)
        with patch('ask_test_recipes.subprocess.run',side_effect=launch),request_budget(.3):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                ask_test_recipes.run_stage('rerank_test_recipes.py','private-input','rerank-results')
        diag=caught.exception.stage_diagnostics
        self.assertEqual(diag['reason'],'stage_deadline')
        self.assertEqual(diag['last_phase'],'api');self.assertFalse(diag['finished'])
        self.assertGreater(diag['timings_ms']['api'],100)
        self.assertAlmostEqual(sum(diag['timings_ms'].values()),diag['elapsed_ms'],delta=.01)
        self.assertNotIn('private-input',json.dumps(diag))

    def test_http_and_admission_failures_are_written_before_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'progress.jsonl';store=RuntimeStore(Path(tmp)/'runtime.sqlite')
            import time
            start=time.monotonic()
            with patch.dict(os.environ,{'RECIPE_STAGE_DIAGNOSTICS':str(path)}):
                with self.assertRaises(HTTPError):
                    guarded_call(lambda _: (_ for _ in ()).throw(HTTPError('private',429,'private',{},None)),
                        phase='rerank',store=store,attempts=1)
            diag=collect(path,start,2)
            self.assertEqual(diag['reason'],'rate_limited');self.assertEqual(diag['http_code'],429)
            self.assertNotIn('private',path.read_text())
            path.unlink();start=time.monotonic()
            with patch.dict(os.environ,{'RECIPE_STAGE_DIAGNOSTICS':str(path)}),patch.object(store,'acquire',return_value=None):
                with self.assertRaises(AdmissionTimeout):
                    guarded_call(lambda _:self.fail('must not call API'),phase='rerank',scope='separate',
                        store=store,admission_timeout=.01,attempts=1)
            diag=collect(path,start,2)
            self.assertEqual(diag['reason'],'admission_timeout');self.assertNotIn('api',diag['timings_ms'])

    def test_trace_fields_are_allowlisted(self):
        value={'private':'secret','last_phase':'api','finished':False,'elapsed_ms':123,
            'reason':'private secret','timings_ms':{'api':100,'secret':5,'imports':float('nan')}}
        safe=sanitized(value)
        self.assertNotIn('secret',json.dumps(safe));self.assertEqual(safe['timings_ms'],{'api':100})
        self.assertEqual(safe['reason'],'stage_failure')


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_degraded_service_is_not_logged_as_ok(self):
        from src.agent.runtime import Run
        from src.agent.config import Config
        from src.agent.context import AppContext
        services=AsyncMock()
        services.call.return_value={'degraded':True,'degraded_reason':'HTTP429',
            'rerank_diagnostics':{'last_phase':'api','reason':'rate_limited','private':'secret'}}
        run=Run(Config(),AppContext('u','s'),services,'private question',AsyncMock())
        await run.service('search',{})
        self.assertEqual(run.stages[-1]['status'],'degraded')
        self.assertEqual(run.stages[-1]['reason'],'rate_limited')
        self.assertNotIn('secret',json.dumps(run.trace()));self.assertNotIn('private question',json.dumps(run.trace()))
