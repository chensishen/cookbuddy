import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from service_runtime import RuntimeStore,guarded_call,BudgetExceeded,CircuitOpen
from resilience.failure import classify


class CircuitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=RuntimeStore(Path(self.tmp.name)/'runtime.sqlite')

    def test_non_dependency_errors_never_open_breaker_and_bug_alerts(self):
        errors=[BudgetExceeded('private'),asyncio.CancelledError(),ValueError('private'),
                json.JSONDecodeError('private','',0),HTTPError('https://example.invalid',400,'private',{},None)]
        for error in errors*4:
            with self.assertRaises(type(error)):
                guarded_call(lambda _,e=error:(_ for _ in ()).throw(e),phase='nlu_understanding',scope='provider',store=self.store,attempts=1)
        health=self.store.resilience_health()
        self.assertEqual(health['dependencies']['router_model']['state'],'closed')
        self.assertIn('router_model',health['alerts'])
        self.assertNotIn('private',json.dumps(health))

    def test_rolling_rate_does_not_open_for_small_transient_failure_fraction(self):
        with patch('service_runtime.time.time',return_value=1000):
            for success in [True]*5+[False]*3:
                ident=self.store.acquire('router_model:p');self.store.release(ident,'router_model:p',success)
            self.assertIsNotNone(self.store.acquire('router_model:p'))
        with patch('service_runtime.time.time',return_value=1061):
            for _ in range(3):
                ident=self.store.acquire('router_model:p');self.store.release(ident,'router_model:p',False)
            with self.assertRaises(CircuitOpen):self.store.acquire('router_model:p')

    def test_two_probe_recovery_backoff_and_cross_process_isolation(self):
        scope='router_model:provider';other=RuntimeStore(self.store.path)
        with patch('service_runtime.time.time',return_value=1000):
            for _ in range(3):
                ident=self.store.acquire(scope);self.store.release(ident,scope,False)
            with self.assertRaises(CircuitOpen):other.acquire(scope)
            self.assertIsNotNone(other.acquire('main_model:provider'))
            self.assertIsNotNone(other.acquire('embedding:provider'))
        with patch('service_runtime.time.time',return_value=1011):
            probe=other.acquire(scope);self.assertIsNone(self.store.acquire(scope))
            other.release(probe,scope,False)
        with patch('service_runtime.time.time',return_value=1022):
            with self.assertRaises(CircuitOpen):self.store.acquire(scope)
        with patch('service_runtime.time.time',return_value=1032):
            probe=self.store.acquire(scope);self.store.release(probe,scope,True)
            self.assertEqual(other.resilience_health()['dependencies']['router_model']['state'],'half_open')
            probe=other.acquire(scope);other.release(probe,scope,True)
            self.assertEqual(self.store.resilience_health()['dependencies']['router_model']['state'],'closed')
        with self.store.connection() as db:
            transitions=db.execute('SELECT old_state,new_state,counts,request_id FROM breaker_transitions').fetchall()
            self.assertEqual([r[1] for r in transitions],['open','half_open','open','half_open','closed'])

    def test_http_rate_limit_is_separate_and_invalid_json_is_not_dependency_failure(self):
        self.assertEqual(classify(HTTPError('https://example.invalid',429,'',{},None)),'http_429')
        self.assertEqual(classify(json.JSONDecodeError('','',0)),'invalid_json')
        self.assertEqual(classify(TimeoutError()),'timeout')

    def test_each_remote_operation_has_an_independent_persisted_breaker(self):
        phases={'router_model':'nlu_understanding','main_model':'agent','memory_model':'memory_extract',
                'embedding':'embedding','rerank':'rerank','milvus':'milvus'}
        for expected,phase in phases.items():
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    guarded_call(lambda _:(_ for _ in ()).throw(TimeoutError('private utterance')),
                                 phase=phase,scope='same_provider',store=self.store,attempts=1)
            health=RuntimeStore(self.store.path).resilience_health()
            self.assertEqual(health['dependencies'][expected]['state'],'open')
            self.assertEqual(health['dependencies'][expected]['counts']['timeout'],3)
        self.assertEqual(len(health['dependencies']),6)

    def test_disabled_debug_policy_does_not_crash_or_open(self):
        import copy
        from resilience.config import settings
        cfg=copy.deepcopy(settings());cfg['breaker']['enabled']=False
        with patch('resilience.breaker.settings',return_value=cfg):
            for _ in range(4):
                lease=self.store.acquire('router_model:debug');self.store.release(lease,'router_model:debug',False)
        self.assertEqual(self.store.resilience_health()['dependencies']['router_model']['state'],'closed')

    def test_transition_records_contain_only_codes_and_request_id(self):
        with patch.dict('os.environ',{'RECIPE_JOB_ID':'safe-request'}),self.assertLogs('cookbuddy.resilience',level='INFO') as logs:
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    guarded_call(lambda _:(_ for _ in ()).throw(TimeoutError('private utterance')),
                                 phase='nlu_understanding',scope='provider',store=self.store,attempts=1)
        message=logs.output[-1]
        self.assertIn('safe-request',message);self.assertIn('failure_ratio',message)
        self.assertNotIn('private utterance',message)
