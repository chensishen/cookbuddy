import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from service_runtime import RuntimeStore, guarded_call, request_budget, remaining, BudgetExceeded, CircuitOpen, retry_delay, retry_after_seconds
from job_queue import JobQueue, QueueFull, IdempotencyConflict


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=RuntimeStore(Path(self.tmp.name)/'runtime.sqlite')

    def test_nested_budget_retains_parent_cancel(self):
        with request_budget(1,cancelled=lambda:True),request_budget(.5):
            with self.assertRaises(BudgetExceeded):remaining()

    def test_json_retries_share_one_phase_budget(self):
        import io
        from urllib.request import Request
        from service_runtime import request_json
        waits=[]
        def open_request(request,timeout):
            waits.append(timeout)
            if len(waits)==1:
                time.sleep(.02)
                raise TimeoutError('test timeout')
            return io.StringIO('{"ok":true}')
        with patch('service_runtime.urlopen',side_effect=open_request),patch('service_runtime.RuntimeStore',return_value=self.store),patch('service_runtime.retry_delay',return_value=0):
            self.assertTrue(request_json(Request('https://example.invalid/chat',data=b'{"model":"test"}'),timeout=1,attempts=2)['ok'])
        self.assertEqual(len(waits),2)
        self.assertLess(waits[1],waits[0]-.01)

    def test_chinese_request_chain_does_not_exhaust_quota_by_counting_bytes(self):
        from service_runtime import estimate_request_tokens
        payload={'model':'deepseek-flash','messages':[{'role':'user','content':'混合面粉和温水。'*1000}],'max_tokens':768}
        old=len(json.dumps(payload,ensure_ascii=False).encode())+768
        estimate=estimate_request_tokens(payload)
        self.assertGreater(old*3,60000)
        self.assertLess(estimate*3,60000)
        for _ in range(3):
            ident=self.store.acquire('chinese',tokens=estimate,tpm=60000)
            self.assertIsNotNone(ident)
            self.store.release(ident,'chinese',True)

    def test_token_estimator_preserves_output_reservation_and_quota_enforcement(self):
        from service_runtime import estimate_request_tokens
        base={'messages':[{'role':'user','content':'测试'}]}
        self.assertGreaterEqual(estimate_request_tokens({**base,'max_tokens':1000})-estimate_request_tokens({**base,'max_tokens':100}),900)
        with self.assertRaises(ValueError):estimate_request_tokens({**base,'max_tokens':True})
        amount=estimate_request_tokens({**base,'max_tokens':100})
        ident=self.store.acquire('bounded',tokens=amount,tpm=amount)
        self.store.release(ident,'bounded',True)
        self.assertIsNone(self.store.acquire('bounded',tokens=amount,tpm=amount))

    def test_completed_requests_use_provider_usage_instead_of_max_reservation(self):
        import os
        with patch.dict(os.environ,{'RECIPE_MODEL_TPM':'40'}),request_budget(.5):
            for _ in range(3):
                guarded_call(lambda _:{'usage':{'total_tokens':5}},phase='settlement',scope='settlement',tokens=30,store=self.store)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT sum(tokens) FROM calls WHERE scope='settlement'").fetchone()[0],15)

    def test_invalid_provider_usage_does_not_free_quota(self):
        for actual in (True,-1,'1',None):
            guarded_call(lambda _:{'usage':{'total_tokens':actual}},phase='invalid-usage',tokens=30,store=self.store)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT sum(tokens) FROM calls').fetchone()[0],120)

    def test_metrics_separate_admission_wait_and_api_execution(self):
        self.assertTrue(guarded_call(lambda _:True,phase='split',store=self.store))
        phases={row['phase'] for row in self.store.metrics()}
        self.assertTrue({'split','split_admission','split_api'}<=phases)

    def test_retry_429_then_success_and_no_payload_in_metrics(self):
        calls=[]
        def invoke(timeout):
            calls.append(timeout)
            if len(calls)==1:raise HTTPError('https://example.invalid',429,'secret-response',{'Retry-After':'0'},None)
            return {'ok':True}
        with request_budget(2):
            result=guarded_call(invoke,phase='generation',model='test',store=self.store)
        self.assertTrue(result['ok']);self.assertEqual(len(calls),2)
        self.assertLessEqual(max(calls),2)
        self.assertNotIn('secret-response',json.dumps(self.store.metrics()))

    def test_authentication_error_is_not_retried(self):
        count=[]
        def invoke(_):
            count.append(1);raise HTTPError('https://example.invalid',401,'bad',{},None)
        with self.assertRaises(HTTPError):guarded_call(invoke,phase='test',store=self.store)
        self.assertEqual(len(count),1)

    def test_business_validation_is_not_retried(self):
        calls=[]
        def invoke(_):calls.append(1);raise ValueError('JSON schema failed')
        with self.assertRaises(ValueError):guarded_call(invoke,phase='test',store=self.store)
        self.assertEqual(len(calls),1)

    def test_retry_after_cannot_extend_total_deadline(self):
        def invoke(_):raise HTTPError('https://example.invalid',429,'busy',{'Retry-After':'5'},None)
        started=time.monotonic()
        with request_budget(.15),self.assertRaises(BudgetExceeded):
            guarded_call(invoke,phase='test',store=self.store)
        self.assertLess(time.monotonic()-started,.5)

    def test_retry_after_protects_other_callers_even_when_first_has_no_retries(self):
        def limited(_):raise HTTPError('https://example.invalid',429,'busy',{'Retry-After':'10'},None)
        with request_budget(1),self.assertRaises(HTTPError):
            guarded_call(limited,phase='agent',scope='shared-model',store=self.store,attempts=1)
        other=RuntimeStore(self.store.path);calls=[]
        with request_budget(.2),self.assertRaises(BudgetExceeded):
            guarded_call(lambda _:calls.append(1),phase='generation',scope='shared-model',store=other,attempts=1)
        self.assertEqual(calls,[])

    def test_retry_after_accepts_dates_and_rejects_nonfinite_values(self):
        with patch('service_runtime.time.time',return_value=0):
            self.assertEqual(retry_after_seconds({'Retry-After':'Thu, 01 Jan 1970 00:00:10 GMT'}),10)
        for value in ('NaN','Infinity','invalid'):
            self.assertIsNone(retry_after_seconds({'Retry-After':value}))
            self.assertEqual(retry_delay({'Retry-After':value},0,rng=lambda:.5),1)

    def test_provider_cooldown_is_persistent_scoped_and_never_shortened(self):
        with patch('service_runtime.time.time',return_value=1000):
            self.store.defer_provider('model',10)
            self.store.defer_provider('model',1)
            other=RuntimeStore(self.store.path);diagnostics={}
            self.assertIsNone(other.acquire('model',diagnostics=diagnostics))
            self.assertEqual(diagnostics['retry_after_seconds'],10)
            self.assertEqual(diagnostics['reason'],'provider_retry_after')
            self.assertIsNotNone(other.acquire('other-model'))
            with other.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM calls WHERE scope='model'").fetchone()[0],0)
        with patch('service_runtime.time.time',return_value=1011):
            self.assertIsNotNone(other.acquire('model',diagnostics=diagnostics))
            self.assertNotIn('retry_after_seconds',diagnostics)

    def test_success_of_inflight_request_does_not_clear_provider_cooldown(self):
        failed=self.store.acquire('model');inflight=self.store.acquire('model')
        self.store.defer_provider('model',10)
        self.store.release(failed,'model',False)
        self.store.release(inflight,'model',True)
        self.assertIsNone(self.store.acquire('model'))

    def test_provider_cooldown_preserves_single_half_open_probe_and_recovery(self):
        with patch('service_runtime.time.time',return_value=1000):
            for _ in range(3):
                ident=self.store.acquire('model');self.store.release(ident,'model',False)
            self.store.defer_provider('model',40)
        other=RuntimeStore(self.store.path)
        with patch('service_runtime.time.time',return_value=1031):
            self.assertIsNone(other.acquire('model'))
        with patch('service_runtime.time.time',return_value=1041):
            probe=other.acquire('model');self.assertIsNotNone(probe)
            self.assertIsNone(self.store.acquire('model'))
            other.release(probe,'model',True)
            self.assertIsNotNone(self.store.acquire('model'))

    def test_nested_budget_and_cancel(self):
        with request_budget(.1):
            with request_budget(100):self.assertLess(remaining(),.11)
        with request_budget(10,cancelled=lambda:True),self.assertRaises(BudgetExceeded):remaining()

    def test_cache_namespace_isolated_and_failed_results_not_cached(self):
        calls=[]
        fn=lambda _: calls.append(1) or {'vector':[1,2]}
        guarded_call(fn,phase='embedding',store=self.store,cache_key='model-v1-dataset-a')
        guarded_call(fn,phase='embedding',store=self.store,cache_key='model-v1-dataset-a')
        guarded_call(fn,phase='embedding',store=self.store,cache_key='model-v2-dataset-a')
        self.assertEqual(len(calls),2)
        with self.assertRaises(ValueError):guarded_call(lambda _:(_ for _ in ()).throw(ValueError()),phase='test',store=self.store,cache_key='invalid')
        self.assertIsNone(self.store.get('invalid'))

    def test_shared_concurrency_lease_between_store_instances(self):
        other=RuntimeStore(self.store.path)
        ident=self.store.acquire('model',concurrency=1)
        self.assertIsNone(other.acquire('model',concurrency=1))
        self.store.release(ident,'model',True)
        second=other.acquire('model',concurrency=1)
        self.assertIsNotNone(second);other.release(second,'model')

    def test_rpm_tpm_and_scope_isolation(self):
        ident=self.store.acquire('a',tokens=9,rpm=1,tpm=10)
        self.store.release(ident,'a',True)
        self.assertIsNone(self.store.acquire('a',tokens=2,rpm=1,tpm=10))
        self.assertIsNotNone(self.store.acquire('b',tokens=2,rpm=1,tpm=10))
        with self.assertRaises(ValueError):self.store.acquire('c',tokens=11,tpm=10)

    def test_circuit_opens_then_single_probe_and_recovers(self):
        for _ in range(3):
            ident=self.store.acquire('broken');self.store.release(ident,'broken',False)
        with self.assertRaises(CircuitOpen):self.store.acquire('broken')
        probe=self.store.acquire('broken',cooldown=0)
        self.assertIsNotNone(probe)
        self.assertIsNone(self.store.acquire('broken',cooldown=0))
        self.store.release(probe,'broken',True)
        self.assertIsNotNone(self.store.acquire('broken'))

    def test_expired_lease_does_not_block_capacity_forever(self):
        self.store.acquire('a',concurrency=1,lease_seconds=-1)
        self.assertIsNotNone(self.store.acquire('a',concurrency=1))

    def test_cancelled_job_releases_its_model_lease_only(self):
        with patch.dict('os.environ',{'RECIPE_JOB_ID':'cancel-me'}):
            self.store.acquire('a',concurrency=1)
        with patch.dict('os.environ',{'RECIPE_JOB_ID':'keep-me'}):
            self.store.acquire('b',concurrency=1)
        self.store.release_job('cancel-me')
        self.assertIsNotNone(self.store.acquire('a',concurrency=1))
        self.assertIsNone(self.store.acquire('b',concurrency=1))


class PersistentQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'jobs.sqlite';self.queue=JobQueue(self.path,capacity=3)
    def test_idempotent_submit_conflict_and_restart(self):
        ident,reused=self.queue.submit({'question':'a'},'key')
        other=JobQueue(self.path)
        again,reused=other.submit({'question':'a'},'key')
        self.assertEqual(ident,again);self.assertTrue(reused)
        with self.assertRaises(IdempotencyConflict):other.submit({'question':'b'},'key')
        self.assertEqual(other.get(ident)['state'],'queued')
    def test_atomic_bounded_admission_under_concurrency(self):
        outcomes=[]
        def submit():
            try:self.queue.submit({'question':'a'});outcomes.append('accepted')
            except QueueFull:outcomes.append('rejected')
        threads=[threading.Thread(target=submit) for _ in range(12)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(outcomes.count('accepted'),3)
        self.assertEqual(outcomes.count('rejected'),9)
    def test_no_double_claim_and_cancel_cannot_be_overwritten(self):
        ident,_=self.queue.submit({'question':'a'})
        first=self.queue.claim('worker-a');second=JobQueue(self.path).claim('worker-b')
        self.assertEqual(first['id'],ident);self.assertIsNone(second)
        self.assertTrue(self.queue.cancel(ident))
        self.queue.finish(ident,'done',{'bad':'late result'})
        self.assertEqual(self.queue.get(ident)['state'],'cancelled')
        self.assertNotIn('result',self.queue.get(ident))
    def test_queue_deadline_includes_waiting_and_orphan_is_explicit(self):
        expired,_=self.queue.submit({'question':'a'},seconds=-1)
        self.assertIsNone(self.queue.claim('worker'));self.assertEqual(self.queue.get(expired)['state'],'expired')
        orphan,_=self.queue.submit({'question':'b'})
        self.queue.claim('worker')
        with self.queue.connect() as db:db.execute('UPDATE jobs SET lease=0 WHERE id=?',(orphan,))
        self.queue.recover();self.assertEqual(self.queue.get(orphan)['state'],'failed')
    def test_progress_result_and_queue_metrics_are_durable(self):
        ident,_=self.queue.submit({'question':'a'});self.queue.claim('worker')
        self.queue.progress(ident,'正在读取');self.queue.finish(ident,'done',{'status':'completed'},'run.json')
        other=JobQueue(self.path)
        self.assertEqual(other.get(ident)['result']['status'],'completed')
        self.assertEqual(other.metrics()['states']['done'],1)


if __name__=='__main__':unittest.main()
