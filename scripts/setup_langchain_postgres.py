"""Apply official LangGraph checkpoint migrations and restricted app-role grants."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.agent.checkpoints import setup_postgres

if __name__=='__main__':
    asyncio.run(setup_postgres())
    print('PostgreSQL checkpoint schema ready; application RLS enabled.')
