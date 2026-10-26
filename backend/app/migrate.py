"""Minimal, dependency-free migration runner for the plain-SQL migrations."""
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def _dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def migrate(database_url: str) -> list[str]:
    applied_now = []
    with psycopg.connect(_dsn(database_url)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        conn.commit()
        conn.execute("SELECT pg_advisory_lock(727274)")  # one migrator at a time
        conn.commit()
        try:
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            conn.commit()
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if f.name in done:
                    continue
                with conn.transaction():
                    conn.execute(f.read_text())
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.name,))
                applied_now.append(f.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(727274)")
            conn.commit()
    return applied_now
