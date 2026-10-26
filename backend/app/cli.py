"""Operations CLI.

    python -m app.cli migrate
    python -m app.cli create-user --email you@x.com --name "Yosef Golan" --role admin
    python -m app.cli run-daily                       # what the scheduler runs
    python -m app.cli generate-charges --period 2026-10
    python -m app.cli backfill --from 2026-01 --to 2026-10
    python -m app.cli seed-demo                       # demo building for evaluation
    python -m app.cli demo-statement --out extracto.csv
"""
from __future__ import annotations

import argparse
import getpass
import logging
from datetime import date

from sqlalchemy import select

from .config import get_settings
from .db import session_factory
from .migrate import migrate


def _month(s: str) -> date:
    return date.fromisoformat(f"{s}-01")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    u = sub.add_parser("create-user")
    u.add_argument("--email", required=True)
    u.add_argument("--name", required=True)
    u.add_argument("--role", default="admin", choices=["admin", "manager", "viewer"])
    u.add_argument("--password", help="omit to be prompted (recommended)")
    sub.add_parser("run-daily")
    g = sub.add_parser("generate-charges")
    g.add_argument("--period", required=True, help="YYYY-MM")
    b = sub.add_parser("backfill")
    b.add_argument("--from", dest="first", required=True)
    b.add_argument("--to", dest="last", required=True)
    sub.add_parser("seed-demo")
    ds = sub.add_parser("demo-statement")
    ds.add_argument("--out", required=True)
    args = p.parse_args(argv)

    if args.cmd == "demo-statement":
        from pathlib import Path
        from .seed import demo_statement
        Path(args.out).write_bytes(demo_statement())
        print("Written", args.out)
        return
    if args.cmd == "migrate":
        applied = migrate(get_settings().database_url)
        print("Applied:", ", ".join(applied) if applied else "nothing (up to date)")
        return

    from . import models
    from .security import hash_password
    from .services import billing_service

    with session_factory()() as db:
        if args.cmd == "create-user":
            pw = args.password or getpass.getpass("Password: ")
            db.add(models.User(email=args.email.lower(), full_name=args.name, role=args.role,
                               password_hash=hash_password(pw)))
            db.commit()
            print("User created")
        elif args.cmd == "run-daily":
            print(billing_service.run_daily(db, date.today()))
        elif args.cmd == "generate-charges":
            print(billing_service.generate_charges(db, _month(args.period)), "charges created")
        elif args.cmd == "backfill":
            print(billing_service.backfill(db, _month(args.first), _month(args.last)), "charges created")
        elif args.cmd == "seed-demo":
            from .seed import seed_demo
            if db.scalar(select(models.Property.id)):
                print("Database already has a property; seed skipped")
            else:
                seed_demo(db)
                print("Demo data loaded")


if __name__ == "__main__":
    main()
