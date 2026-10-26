"""Background scheduler process (separate container from the web app, so web
workers never run jobs twice). All jobs are idempotent."""
import logging
from datetime import date

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import get_settings
from .db import session_factory
from .services.billing_service import run_daily

log = logging.getLogger("scheduler")


def daily_job() -> None:
    with session_factory()() as db:
        log.info("run_daily: %s", run_daily(db, date.today()))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sched = BlockingScheduler(timezone=get_settings().timezone)
    sched.add_job(daily_job, "cron", hour=6, minute=0, id="daily", coalesce=True, misfire_grace_time=6 * 3600)
    daily_job()  # catch up immediately on start (e.g. after downtime on the invoice day)
    sched.start()


if __name__ == "__main__":
    main()
