import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cookbuddy.models import json_model, ModelOutputError
from cookbuddy.worker import finish


def response(reason='stop', content='{"memories":[]}'):
    return {'choices': [{'finish_reason': reason, 'message': {'content': content}}]}


class MemoryModelOutputTests(unittest.TestCase):
    def call_model(self, responses, max_tokens=4096):
        requests = []
        def request(req, **kwargs):
            requests.append(json.loads(req.data))
            return next(responses)
        with patch('cookbuddy.models.require_llm', return_value={
                'base_url': 'https://api.deepseek.com/v1', 'api_key': 'test', 'model': 'deepseek-flash'}), \
                patch('cookbuddy.models.request_json', side_effect=request):
            result = json_model('extract', {'question': 'test'}, 'memory_extract', max_tokens=max_tokens)
        return result, requests

    def test_truncation_retries_with_larger_budget_and_ignores_partial_json(self):
        result, requests = self.call_model(iter([response('length', '{"memories":['), response()]))
        self.assertEqual(result, {'memories': []})
        self.assertEqual([r['max_tokens'] for r in requests], [4096, 8192])
        self.assertEqual(requests[0]['messages'], requests[1]['messages'])

    def test_truncated_but_parseable_output_is_still_rejected_after_bounded_retry(self):
        with self.assertRaises(ModelOutputError) as caught:
            self.call_model(iter([response('length'), response('length')]))
        self.assertEqual(caught.exception.code, 'model_output_truncated')

    def test_hard_ceiling_does_not_retry_identical_budget(self):
        with self.assertRaises(ModelOutputError):
            self.call_model(iter([response('length')]), max_tokens=8192)

    def test_invalid_json_object_and_incomplete_response_have_safe_codes(self):
        for item, code in [(response(content='private invalid text'), 'model_output_invalid_json'),
                           (response(content='[]'), 'model_output_not_object'),
                           (response('content_filter'), 'model_output_incomplete')]:
            with self.subTest(code=code), self.assertRaises(ModelOutputError) as caught:
                self.call_model(iter([item]))
            self.assertEqual(str(caught.exception), code)

    def test_worker_persists_specific_code_without_arbitrary_exception_text(self):
        class FakeStore:
            @contextmanager
            def connect(self, uid):
                yield self
            def execute(self, sql, values):
                self.values = values
        store = FakeStore()
        row = {'user_id': 'test', 'job_id': 'test', 'attempts': 5}
        finish(store, 'memory_tasks', row, ModelOutputError('model_output_truncated'))
        self.assertEqual(store.values[:2], ('failed', 'model_output_truncated'))
        finish(store, 'memory_tasks', row, ValueError('private exception text'))
        self.assertEqual(store.values[:2], ('failed', 'ValueError'))


if __name__ == '__main__':
    unittest.main()
