"""Worker process: heartbeat plus scheduled collection."""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.analysis.service import AnalysisService
from app.config import APP_VERSION, Settings, load_or_create_master_key
from app.db import Database, utcnow
from app.ingest.service import IngestService
from app.models import WorkerHeartbeat
from app.push.service import PushService
from app.security import Security
from app.settings_service import SettingsService


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # httpx logs the full request URL at INFO, including TianAPI's ?key=...
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    runtime = Settings.from_env()
    configure_logging(runtime.log_level)
    logger = logging.getLogger("newsradar.worker")
    runtime.ensure_directories()
    master_key = load_or_create_master_key(runtime.master_key_file)
    db = Database(runtime.db_path)
    db.init_schema()
    security = Security(master_key)
    settings_service = SettingsService(db, security)
    settings_service.ensure_defaults()
    ingest = IngestService(db, settings_service)
    ingest.ensure_default_sources()
    analysis = AnalysisService(db, settings_service)
    push = PushService(db, settings_service)

    stop_event = threading.Event()

    def heartbeat() -> None:
        try:
            with db.session() as session:
                heartbeat_row = session.get(WorkerHeartbeat, 1)
                if heartbeat_row is None:
                    heartbeat_row = WorkerHeartbeat(id=1)
                    session.add(heartbeat_row)
                heartbeat_row.last_beat_at = utcnow()
                heartbeat_row.version = APP_VERSION
            logger.info(
                "worker.heartbeat settings_version=%s",
                settings_service.settings_version,
            )
        except Exception:
            logger.exception("worker.heartbeat.failed")

    def collect() -> None:
        try:
            result = ingest.collect_due()
            if result.get("sources"):
                logger.info("worker.collect %s", json.dumps(result, ensure_ascii=False)[:2000])
        except Exception:
            logger.exception("worker.collect.failed")

    def analyze() -> None:
        try:
            result = analysis.analyze_due()
            if result.get("skipped") not in {"min_interval", "cost_limit", "max_runs", "token_budget"}:
                logger.info("worker.analyze %s", json.dumps(result, ensure_ascii=False)[:2000])
        except Exception:
            logger.exception("worker.analyze.failed")

    def deliver() -> None:
        try:
            result = push.push_due()
            if any(result.values()):
                logger.info("worker.push %s", json.dumps(result, ensure_ascii=False))
        except Exception:
            logger.exception("worker.push.failed")

    def _stop(_signum, _frame):  # noqa: ANN001
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        heartbeat,
        "interval",
        seconds=30,
        id="heartbeat",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        collect,
        "interval",
        seconds=15,
        id="collect",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        analyze,
        "interval",
        seconds=60,
        id="analyze",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        deliver,
        "interval",
        seconds=60,
        id="push",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("worker.start version=%s db=%s", APP_VERSION, runtime.db_path)
    heartbeat()
    collect()
    stop_event.wait()
    scheduler.shutdown(wait=False)
    logger.info("worker.stop")
    db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
