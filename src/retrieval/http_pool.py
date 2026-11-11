"""Application-owned pool process; inherited socket lets short-lived jobs reuse it."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import httpx

ROOT=Path(__file__).resolve().parents[2]


class NetworkPool:
    def __enter__(self):
        self.previous=os.environ.get('RECIPE_HTTP_POOL_SOCKET')
        self.socket=str(Path(tempfile.gettempdir())/('cb-http-'+uuid.uuid4().hex[:12]+'.sock'))
        self.process=subprocess.Popen([sys.executable,str(ROOT/'scripts/serve_http_pool.py'),self.socket],
            cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            end=time.monotonic()+10
            while time.monotonic()<end:
                if self.process.poll() is not None:break
                if Path(self.socket).exists():
                    try:
                        with httpx.Client(transport=httpx.HTTPTransport(uds=self.socket),timeout=.2,trust_env=False) as client:
                            if client.get('http://pool/health').status_code==200:
                                os.environ['RECIPE_HTTP_POOL_SOCKET']=self.socket
                                return self
                    except httpx.HTTPError:pass
                time.sleep(.025)
            raise RuntimeError('http_pool_start_failed')
        except BaseException:
            self.__exit__(None,None,None);raise

    def __exit__(self,*args):
        if self.previous is None:os.environ.pop('RECIPE_HTTP_POOL_SOCKET',None)
        else:os.environ['RECIPE_HTTP_POOL_SOCKET']=self.previous
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL);self.process.wait()
        Path(self.socket).unlink(missing_ok=True)
