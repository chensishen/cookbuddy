"""Request identity shared by service locks and checkpoint adapters."""
from contextvars import ContextVar

checkpoint_thread=ContextVar('checkpoint_thread',default='')
