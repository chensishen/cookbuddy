"""urllib-compatible adapter to the app-owned persistent HTTP connection pool."""
import base64
import io
import os
from urllib.error import HTTPError,URLError
from urllib.request import urlopen as direct_urlopen


def urlopen(request,timeout=30):
    socket=os.environ.get('RECIPE_HTTP_POOL_SOCKET')
    if not socket:return direct_urlopen(request,timeout=timeout)
    import httpx
    payload={'url':request.full_url,'method':request.get_method(),
             'headers':dict(request.header_items()),
             'body':base64.b64encode(request.data or b'').decode(),'timeout':timeout}
    try:
        # Only this short local hop is per operation; the upstream pool survives
        # worker exits. Closing the hop cancels the gateway's upstream coroutine.
        with httpx.Client(transport=httpx.HTTPTransport(uds=socket),timeout=timeout,trust_env=False) as client:
            response=client.post('http://pool/request',json=payload)
            if response.status_code!=200:raise URLError('http_pool_unavailable')
            result=response.json()
    except httpx.TimeoutException:raise TimeoutError('http_pool_timeout') from None
    except httpx.HTTPError:raise URLError('http_pool_unavailable') from None
    if result.get('error')=='timeout':raise TimeoutError('upstream_timeout')
    if result.get('error'):raise URLError('upstream_unavailable')
    body=io.BytesIO(base64.b64decode(result['body']))
    from email.message import Message
    headers=Message()
    for key,value in result['headers'].items():headers[key]=value
    body.status=result['status'];body.headers=headers
    if body.status>=400:raise HTTPError(request.full_url,body.status,'upstream_http_error',body.headers,body)
    return body
