"""Authenticated job entry point for the single online engine."""
def run(uid,cid,job,question,epoch,progress):
    from src.agent.job import run as run_agent
    return run_agent(uid,cid,job,question,progress)
