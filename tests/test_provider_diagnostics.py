import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from service_runtime import request_json,langchain_http_client
from usage_ledger import record_usage


class ProviderDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'usage.jsonl'
        env=patch.dict(os.environ,{'RECIPE_USAGE_LEDGER':str(self.path),
            'RECIPE_RUNTIME_DB':str(Path(self.tmp.name)/'runtime.sqlite')})
        env.start();self.addCleanup(env.stop)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_urllib_records_bounded_code_and_allowlisted_headers(self):
        error=HTTPError('https://example.invalid/chat',429,'private-message',
            {'Retry-After':'10','X-RateLimit-Remaining-Tokens':'0','Authorization':'secret'},
            io.BytesIO(b'{"code":50505,"message":"private-prompt"}'))
        with patch('service_runtime.urlopen',side_effect=error):
            with self.assertRaises(HTTPError) as raised:
                request_json(Request(error.url,data=b'{"model":"test"}'),attempts=1)
        self.assertIs(raised.exception,error)
        self.assertEqual(self.rows()[0]['provider_diagnostics'],{
            'error_code':50505,'headers':{'retry-after':'10','x-ratelimit-remaining-tokens':'0'}})
        self.assertNotIn('private',self.path.read_text())
        self.assertNotIn('secret',self.path.read_text())

    def test_httpx_captures_same_diagnostics_without_logging_body(self):
        import httpx
        def handler(request):
            return httpx.Response(401,headers={'X-RateLimit-Limit-Requests':'20'},
                json={'error':{'code':'insufficient_quota','message':'private'}},request=request)
        with patch('httpx.HTTPTransport',return_value=httpx.MockTransport(handler)):
            with langchain_http_client() as client:
                with self.assertRaises(HTTPError) as raised:
                    client.post('https://example.invalid/chat',json={'model':'test'})
        self.assertEqual(raised.exception.code,401)
        self.assertEqual(self.rows()[0]['provider_diagnostics'],{
            'error_code':'insufficient_quota','headers':{'x-ratelimit-limit-requests':'20'}})
        self.assertNotIn('private',self.path.read_text())

    def test_untrusted_or_oversized_fields_are_not_stored(self):
        for body in (b'not JSON private',b'['*1500+b']'*1500,b'{"code":"secret"}',
                     b'{"code":true}',b'{"code":50505,"message":"'+b'x'*9000+b'"}'):
            error=HTTPError('https://example.invalid/chat',429,'private',
                {'Retry-After':'secret','X-RateLimit-Limit-Tokens':'NaN',
                 'X-RateLimit-Remaining-Requests':'-1','Set-Cookie':'secret'},io.BytesIO(body))
            record_usage(error.url,'test','model',error=error)
        for row in self.rows():
            self.assertEqual(row['provider_diagnostics'],{})
        self.assertNotIn('secret',self.path.read_text())
        self.assertNotIn('private',self.path.read_text())

    def test_malformed_and_unreadable_body_preserve_original_http_error(self):
        for stream in (io.BytesIO(b'invalid'),io.BytesIO()):
            stream.close() if not stream.getvalue() else None
            error=HTTPError('https://example.invalid/chat',401,'private',{},stream)
            with patch('service_runtime.urlopen',side_effect=error):
                with self.assertRaises(HTTPError) as raised:
                    request_json(Request(error.url,data=b'{"model":"test"}'),attempts=1)
            self.assertIs(raised.exception,error)

    def test_no_ledger_does_not_read_error_stream(self):
        stream=io.BytesIO(b'{"code":50505}')
        error=HTTPError('https://example.invalid/chat',429,'private',{},stream)
        with patch.dict(os.environ,{'RECIPE_USAGE_LEDGER':''}):
            record_usage(error.url,'test','model',error=error)
        self.assertEqual(stream.tell(),0)

    def test_http_retry_logs_each_attempt_with_no_extra_request(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.server.calls+=1
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(429 if self.server.calls==1 else 200)
                self.send_header('Retry-After','0')
                self.end_headers()
                self.wfile.write(b'{"code":50505,"message":"private"}' if self.server.calls==1 else b'{"ok":true}')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.calls=0
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            request=Request(f'http://127.0.0.1:{server.server_port}/chat',data=b'{"model":"test"}')
            self.assertTrue(request_json(request,attempts=2)['ok'])
            self.assertEqual(server.calls,2)
            self.assertEqual(self.rows()[0]['provider_diagnostics']['error_code'],50505)
            self.assertEqual(self.rows()[1]['outcome'],'http_success')
            self.assertNotIn('private',self.path.read_text())
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_date_header_and_bodyless_error(self):
        error=HTTPError('https://example.invalid/chat',429,'private',
            {'Retry-After':'Wed, 21 Oct 2026 07:28:00 GMT','X-RateLimit-Reset-Tokens':'2s'},None)
        record_usage(error.url,'test','model',error=error)
        self.assertEqual(self.rows()[0]['provider_diagnostics'],{'headers':{
            'retry-after':'Wed, 21 Oct 2026 07:28:00 GMT','x-ratelimit-reset-tokens':'2s'}})


if __name__=='__main__':unittest.main()
