"""Use the unified async engine from the existing durable job worker."""
import asyncio
import os
import signal
import time
from .checkpoints import checkpoint_store
from .config import load_config
from .context import AppContext
from .engine import ChatEngine
from .runtime import configure_logging
from ..retrieval.pipeline import RecipeServices


def run(uid,sid,job,question,progress):
    configure_logging()
    async def execute():
        loop=asyncio.get_running_loop();task=asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM,task.cancel)
        cfg=load_config(os.environ.get('COOKBUDDY_AGENT_CONFIG') or None,
                        os.environ.get('COOKBUDDY_AGENT_EXPERIMENT') or None)
        context=AppContext(uid,sid,job,deadline=min(float(os.environ.get('RECIPE_DEADLINE_EPOCH',time.time()+180)),
                                                 time.time()+cfg.agent.request_timeout_s))
        async def emit(event,data):
            if event=='tool':progress('正在核对菜谱来源' if data['status']=='running' else '来源工具已返回')
            elif event=='notice':progress(data['message'])
        async with checkpoint_store(cfg) as saver:
            engine=ChatEngine(cfg,RecipeServices(cfg),saver)
            try:return await engine.answer(question,context,emit)
            finally:await engine.memory_queue.close()
    return asyncio.run(execute())
