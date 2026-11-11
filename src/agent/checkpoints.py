"""Explicit checkpoint lifecycle; PostgreSQL never silently falls back locally."""
from contextlib import asynccontextmanager
import sys
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from .config import ROOT
from ..retrieval.session import checkpoint_thread


def database_settings():
    sys.path.insert(0,str(ROOT/'scripts'))
    from cookbuddy.models import settings
    return settings()


class ScopedPostgresSaver(AsyncPostgresSaver):
    @asynccontextmanager
    async def _cursor(self,*,pipeline=False):
        thread=checkpoint_thread.get()
        if not thread:raise RuntimeError('checkpoint_scope_required')
        # A transaction-local identity cannot leak when the pool reuses a connection.
        async with self.conn.connection() as conn,conn.transaction():
            await conn.execute("SELECT set_config('cookbuddy.checkpoint_thread',%s,true)",(thread,))
            async with conn.cursor(binary=True,row_factory=dict_row) as cur:yield cur


async def setup_postgres():
    """Deployment-only DDL with the admin credential; serving uses the app role."""
    values=database_settings()
    async with await AsyncConnection.connect(values['COOKBUDDY_ADMIN_DATABASE_URL'],autocommit=True,
                                             row_factory=dict_row,connect_timeout=3) as conn:
        await conn.execute('SELECT pg_advisory_lock(20261021)')
        try:
            await conn.execute('CREATE SCHEMA IF NOT EXISTS cookbuddy_checkpoint')
            await conn.execute('SET search_path TO cookbuddy_checkpoint')
            await AsyncPostgresSaver(conn).setup()
            for table in ('checkpoints','checkpoint_blobs','checkpoint_writes'):
                from psycopg import sql
                name=sql.Identifier(table)
                await conn.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(name))
                await conn.execute(sql.SQL('ALTER TABLE {} FORCE ROW LEVEL SECURITY').format(name))
                await conn.execute(sql.SQL('DROP POLICY IF EXISTS thread_scope ON {}').format(name))
                await conn.execute(sql.SQL("CREATE POLICY thread_scope ON {} USING (thread_id = current_setting('cookbuddy.checkpoint_thread',true)) WITH CHECK (thread_id = current_setting('cookbuddy.checkpoint_thread',true))").format(name))
                await conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO cookbuddy_app').format(name))
            await conn.execute('GRANT USAGE ON SCHEMA cookbuddy_checkpoint TO cookbuddy_app')
        finally:await conn.execute('SELECT pg_advisory_unlock(20261021)')


@asynccontextmanager
async def checkpoint_store(cfg,path=None):
    if path is not None or cfg.checkpoint.backend=='sqlite':
        path=path or ROOT/'data/runtime/langchain-online.sqlite3'
        from pathlib import Path
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:yield saver
    else:
        values=database_settings()
        async with AsyncConnectionPool(values['COOKBUDDY_DATABASE_URL'],min_size=0,
                max_size=cfg.checkpoint.pool_size,timeout=3,open=False,
                kwargs={'autocommit':True,'prepare_threshold':0,'row_factory':dict_row,
                        'connect_timeout':3,'options':'-c search_path=cookbuddy_checkpoint -c statement_timeout=5000'}) as pool:
            async with pool.connection() as conn:
                row=await (await conn.execute("SELECT to_regclass('checkpoints') AS name")).fetchone()
                if row['name'] is None:raise RuntimeError('checkpoint_schema_not_initialized')
            yield ScopedPostgresSaver(pool)
