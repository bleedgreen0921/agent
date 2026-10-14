"""Database tests only clean the explicitly isolated synthetic test database."""
import os
import pytest
import psycopg
from psycopg import sql

@pytest.fixture(autouse=True)
def historical_flow_budgets(monkeypatch, request):
    """Old functional fixtures submit large bursts; dedicated flow tests use defaults."""
    if request.node.path.name == 'test_flow_control.py':
        return
    for prefix in ('AGENT', 'RAG_DOCUMENT', 'RAG_SEARCH'):
        for suffix, value in (('SUBMIT_RATE', '10000'), ('SUBMIT_BURST', '10000'),
                              ('QUEUE_CAPACITY', '10000'), ('UNFINISHED_CAPACITY', '10000')):
            monkeypatch.setenv(prefix + '_' + suffix, value)
    monkeypatch.setenv('MEMORY_UNFINISHED_CAPACITY', '10000')
    monkeypatch.setenv('DOWNSTREAM_REQUEST_RATE', '10000')
    monkeypatch.setenv('DOWNSTREAM_REQUEST_BURST', '10000')
    monkeypatch.setenv('FLOW_UNAVAILABLE_GRACE_SECONDS', '3600')

@pytest.fixture(autouse=True)
def isolated_business_data():
    if os.environ.get('PERSONAL_TEST_DATABASE') != '1':
        yield
        return
    owner_dsn = os.environ['MIGRATION_DATABASE_URL']
    def clear():
        with psycopg.connect(owner_dsn) as conn:
            tables = conn.execute("""SELECT schemaname,tablename FROM pg_tables
                WHERE schemaname IN ('agent','rag','execution')
                  AND tablename NOT IN ('alembic_version','checkpoint_migrations','index_revisions','index_state')""").fetchall()
            conn.execute(sql.SQL('TRUNCATE {} CASCADE').format(sql.SQL(',').join(
                sql.Identifier(s,t) for s,t in tables)))
            conn.execute('DELETE FROM rag.index_state')
            conn.execute('DELETE FROM rag.index_revisions')
            conn.execute("""INSERT INTO rag.index_revisions(id,model,dimensions,document_template,query_template,state)
                VALUES ('00000000-0000-0000-0000-000000000001','Qwen3-Embedding-0.6B',1024,'title-content-v1','qwen-retrieval-v2','active')""")
            conn.execute("INSERT INTO rag.index_state VALUES (true,'00000000-0000-0000-0000-000000000001')")
    clear()
    yield
    clear()


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    import inspect
    import asyncio
    if not inspect.iscoroutinefunction(pyfuncitem.obj):return None
    from tests.native_async import run_test
    arguments={name:pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(run_test(pyfuncitem.obj,arguments))
    return True


@pytest.fixture(autouse=True)
def native_async_stubs(monkeypatch):
    """Keep synchronous synthetic callbacks inline, not on framework default pools."""
    import inspect
    from tests.native_async import async_stub
    original=monkeypatch.setattr
    from _pytest.monkeypatch import notset
    def replace(target,name,value=notset,raising=True):
        # All existing tests use the three-argument setattr form.
        if isinstance(name,str) and inspect.iscoroutinefunction(getattr(target,name,None)) and callable(value):
            value=async_stub(value)
        return original(target,name,value,raising=raising)
    monkeypatch.setattr=replace
