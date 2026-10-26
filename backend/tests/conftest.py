import os
import tempfile

import pytest

# Integration tests need a disposable PostgreSQL database:
#   CP_TEST_DATABASE_URL=postgresql+psycopg://user@host/cp_test pytest
TEST_DB = os.environ.get("CP_TEST_DATABASE_URL")
os.environ.setdefault("CP_ENVIRONMENT", "development")
os.environ.setdefault("CP_SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("CP_STORAGE_DIR", tempfile.mkdtemp(prefix="cp_uploads_"))
os.environ.setdefault("CP_BANK_WEBHOOK_SECRET", "test-webhook-secret")
if TEST_DB:
    os.environ["CP_DATABASE_URL"] = TEST_DB


@pytest.fixture
def db():
    if not TEST_DB:
        pytest.skip("CP_TEST_DATABASE_URL not set")
    import psycopg

    from app.db import engine, session_factory
    from app.migrate import _dsn, migrate
    from app.seed import seed_demo

    with psycopg.connect(_dsn(TEST_DB), autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(TEST_DB)
    engine().dispose()  # pooled connections cache enum type OIDs from the dropped schema
    s = session_factory()()
    seed_demo(s)
    yield s
    s.close()
