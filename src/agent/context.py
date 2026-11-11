import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AppContext:
    user_id: str
    session_id: str
    request_id: str=field(default_factory=lambda:uuid.uuid4().hex)
    deadline: float=field(default_factory=lambda:time.time()+30)

    def __post_init__(self):
        if not self.user_id or not self.session_id or not self.request_id:
            raise ValueError('identity_required')

    @property
    def thread_id(self):
        # Hashing also avoids delimiter collisions between users and sessions.
        import hashlib
        return hashlib.sha256((self.user_id+'\0'+self.session_id).encode()).hexdigest()

    def remaining(self, cap=180):
        left=min(cap,self.deadline-time.time())
        if left<=0: raise TimeoutError('request_budget_exhausted')
        return left
