"""Collection pipeline: fetch, normalise, dedupe, cluster and queue."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.db import Database, utcnow
from app.ingest.candidate import NEGATIVE_SEED, POSITIVE_SEED, score_text
from app.ingest.normalize import (
    canonicalize_url,
    clean_html,
    content_hash,
    normalize_title,
    sha1_text,
)
from app.ingest.rss import RssError, fetch_rss
from app.ingest.simhash import bands as simhash_bands
from app.ingest.simhash import hamming, simhash64
from app.ingest.tianapi import FetchedNews, TianApiError, fetch_tianapi
from app.ingest.world import (
    WorldNewsError,
    fetch_finnhub,
    fetch_gnews,
    fetch_newsapi,
)
from app.models import DailyUsage, JobRun, NewsCluster, NewsItem, Source
from app.settings_service import SettingsService


SHANGHAI_DAY = "%Y-%m-%d"
logger = logging.getLogger("newsradar.ingest")


def _day() -> str:
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    return dt.now(ZoneInfo("Asia/Shanghai")).strftime(SHANGHAI_DAY)


DEFAULT_SOURCES: list[dict[str, Any]] = [
    {
        "kind": "tianapi",
        "name": "天行财经新闻",
        "config_json": {"path": "/caijing/index", "num": 50, "form": 1},
        "enabled": True,
        "tier": 20,
        "interval_sec": 300,
    },
    {
        "kind": "tianapi",
        "name": "天行综合新闻",
        "config_json": {"path": "/generalnews/index", "num": 50, "form": 1},
        "enabled": True,
        "tier": 15,
        "interval_sec": 600,
    },
    {
        "kind": "tianapi",
        "name": "天行国际新闻",
        "config_json": {"path": "/allnews/index", "col": 8, "num": 50, "form": 1},
        "enabled": False,
        "tier": 15,
        "interval_sec": 600,
    },
    {
        "kind": "tianapi",
        "name": "天行国内新闻",
        "config_json": {"path": "/allnews/index", "col": 7, "num": 50, "form": 1},
        "enabled": False,
        "tier": 15,
        "interval_sec": 600,
    },
    {
        "kind": "tianapi",
        "name": "天行军事新闻",
        "config_json": {"path": "/allnews/index", "col": 27, "num": 50, "form": 1},
        "enabled": False,
        "tier": 12,
        "interval_sec": 900,
    },
    {
        "kind": "tianapi",
        "name": "天行科技新闻",
        "config_json": {"path": "/allnews/index", "col": 13, "num": 50, "form": 1},
        "enabled": False,
        "tier": 12,
        "interval_sec": 900,
    },
    {
        "kind": "newsapi",
        "name": "NewsAPI 商业头条",
        "config_json": {"category": "business", "language": "en"},
        "enabled": False,
        "tier": 10,
        "interval_sec": 900,
    },
    {
        "kind": "gnews",
        "name": "GNews 商业头条",
        "config_json": {"category": "business", "language": "en"},
        "enabled": False,
        "tier": 10,
        "interval_sec": 900,
    },
    {
        "kind": "finnhub",
        "name": "Finnhub 全球新闻",
        "config_json": {"category": "general"},
        "enabled": False,
        "tier": 10,
        "interval_sec": 900,
    },
]


class IngestService:
    def __init__(self, db: Database, settings: SettingsService):
        self.db = db
        self.settings = settings
        self._last_tianapi_call = 0.0

    def ensure_default_sources(self) -> None:
        with self.db.session() as session:
            existing = {row.name for row in session.scalars(select(Source)).all()}
            for spec in DEFAULT_SOURCES:
                if spec["name"] in existing:
                    continue
                session.add(Source(**spec))

    def _usage_row(self, session, day: str) -> DailyUsage:
        row = session.get(DailyUsage, day)
        if row is None:
            row = DailyUsage(day=day)
            session.add(row)
        return row

    def _increment_usage(self, field: str, amount: int = 1) -> None:
        with self.db.session() as session:
            row = self._usage_row(session, _day())
            setattr(row, field, int(getattr(row, field) or 0) + amount)

    def _tianapi_budget_left(self) -> bool:
        limit = int(self.settings.get("tianapi.daily_call_limit", 2000) or 2000)
        with self.db.session() as session:
            row = session.get(DailyUsage, _day())
            used = int(row.tianapi_calls or 0) if row else 0
        return used < limit

    def _rate_limit(self) -> None:
        qps = max(1, int(self.settings.get("tianapi.qps", 5) or 5))
        minimum = 1.0 / qps
        elapsed = time.monotonic() - self._last_tianapi_call
        if elapsed < minimum:
            time.sleep(minimum - elapsed)
        self._last_tianapi_call = time.monotonic()

    def _fetch_tianapi(self, source: Source, api_key: str) -> list[FetchedNews]:
        config = source.config_json or {}
        base_url = str(self.settings.get("tianapi.base_url"))
        timeout = int(self.settings.get("tianapi.timeout_sec", 15) or 15)
        last_error: Exception | None = None
        for attempt in range(2):
            if not self._tianapi_budget_left():
                raise TianApiError("天行日调用上限已到")
            self._rate_limit()
            self._increment_usage("tianapi_calls")
            try:
                return fetch_tianapi(
                    base_url=base_url,
                    path=str(config.get("path") or "/caijing/index"),
                    api_key=api_key,
                    num=int(config.get("num") or 50),
                    col=int(config["col"]) if config.get("col") is not None else None,
                    timeout=timeout,
                )
            except TianApiError as exc:
                last_error = exc
                if exc.code in {130, 150, 160, 180, 230} or attempt == 1:
                    raise
                time.sleep(2)
        raise last_error or TianApiError("天行请求失败")

    def _fetch_rss(self, source: Source) -> tuple[list[FetchedNews], dict]:
        config = source.config_json or {}
        timeout = int(self.settings.get("rss.timeout_sec", 15) or 15)
        max_items = int(self.settings.get("rss.max_items_per_feed", 50) or 50)
        user_agent = str(self.settings.get("rss.user_agent", "NewsRadar/1.0"))
        allow_private = bool(self.settings.get("app.security.allow_private_webhooks", False))
        result = fetch_rss(
            url=str(config.get("url") or ""),
            cursor=source.cursor_json,
            timeout=timeout,
            max_items=max_items,
            user_agent=user_agent,
            allow_private=allow_private,
        )
        return result.items, result.cursor

    def _mark_source(
        self,
        source_id: int,
        *,
        ok: bool,
        error: str | None = None,
        cursor: dict | None = None,
    ) -> None:
        with self.db.session() as session:
            source = session.get(Source, source_id)
            if source is None:
                return
            source.last_polled_at = utcnow()
            if ok:
                source.last_ok_at = utcnow()
                source.last_error = None
            else:
                source.last_error = (error or "unknown error")[:1000]
            if cursor is not None:
                source.cursor_json = cursor

    def collect_source(self, source_id: int) -> dict[str, Any]:
        with self.db.session() as session:
            source = session.get(Source, source_id)
            if source is None or not source.enabled:
                return {"source_id": source_id, "fetched": 0, "skipped": "disabled"}
            kind = source.kind
            name = source.name
            tier = int(source.tier or 3)
        if kind == "tianapi":
            api_key = str(self.settings.get("tianapi.api_key", "") or "")
            if not api_key:
                self._mark_source(source_id, ok=False, error="未配置天行 API Key")
                return {"source_id": source_id, "source": name, "error": "未配置天行 API Key"}
            try:
                items = self._fetch_tianapi(source, api_key)
            except TianApiError as exc:
                self._mark_source(source_id, ok=False, error=str(exc))
                return {"source_id": source_id, "source": name, "error": str(exc)}
            cursor = None
        elif kind == "rss":
            try:
                items, cursor = self._fetch_rss(source)
            except (RssError, ValueError) as exc:
                self._mark_source(source_id, ok=False, error=str(exc))
                return {"source_id": source_id, "source": name, "error": str(exc)}
        elif kind in {"newsapi", "gnews", "finnhub"}:
            config = source.config_json or {}
            key = str(self.settings.get(f"sources.{kind}_key", "") or "")
            if not key:
                self._mark_source(source_id, ok=False, error=f"{kind} Key 未配置")
                return {"source_id": source_id, "source": name, "error": "API Key 未配置"}
            timeout = int(self.settings.get("rss.timeout_sec", 15) or 15)
            try:
                if kind == "newsapi":
                    items = fetch_newsapi(
                        api_key=key,
                        category=str(config.get("category") or "business"),
                        language=str(config.get("language") or "en"),
                        timeout=timeout,
                    )
                elif kind == "gnews":
                    items = fetch_gnews(
                        api_key=key,
                        category=str(config.get("category") or "business"),
                        language=str(config.get("language") or "en"),
                        timeout=timeout,
                    )
                else:
                    items = fetch_finnhub(
                        api_key=key,
                        category=str(config.get("category") or "general"),
                        timeout=timeout,
                    )
            except WorldNewsError as exc:
                self._mark_source(source_id, ok=False, error=str(exc))
                return {"source_id": source_id, "source": name, "error": str(exc)}
            cursor = None
        else:
            self._mark_source(source_id, ok=False, error=f"未知 source kind: {kind}")
            return {"source_id": source_id, "source": name, "error": "unknown kind"}

        stats = self._process_items(source_id, tier, name, items)
        self._mark_source(source_id, ok=True, cursor=cursor)
        stats.update({"source_id": source_id, "source": name, "fetched": len(items)})
        return stats

    def rescore_all(self) -> dict[str, int]:
        """Recompute scores after a scoring-rule change; duplicates stay duplicates.

        The score is a ranking signal only. Every non-duplicate, non-expired item
        stays in the analysis queue; semantic filtering happens in the model and
        in the confidence gate, not here.
        """
        positive = self.settings.get("candidate.positive_keywords", []) or POSITIVE_SEED
        negative = self.settings.get("candidate.negative_keywords", []) or NEGATIVE_SEED
        queued = filtered = skipped = 0
        with self.db.session() as session:
            rows = session.execute(
                select(NewsItem, Source.tier).join(Source, Source.id == NewsItem.source_id)
            ).all()
            for item, tier in rows:
                if item.status in {"duplicate", "analyzed", "expired"}:
                    skipped += 1
                    continue
                score, _, _ = score_text(
                    item.title,
                    item.description,
                    int(tier or 3),
                    item.published_at,
                    cluster_size=1,
                    positive=positive,
                    negative=negative,
                )
                item.heuristic_score = score
                item.status = "queued"
                queued += 1
        return {"queued": queued, "filtered": filtered, "skipped": skipped}

    def collect_due(self, *, force: bool = False) -> dict[str, Any]:
        if bool(self.settings.get("app.paused", False)):
            return {"skipped": "paused"}
        now = utcnow()
        with self.db.session() as session:
            sources = list(
                session.scalars(
                    select(Source).where(Source.enabled.is_(True)).order_by(Source.id)
                ).all()
            )
            due = [
                source.id
                for source in sources
                if force
                or source.last_polled_at is None
                or (now - source.last_polled_at) >= timedelta(seconds=max(30, source.interval_sec))
            ][:8]
        results = []
        for source_id in due:
            results.append(self.collect_source(source_id))
        if due:
            with self.db.session() as session:
                session.add(
                    JobRun(
                        job_name="collect",
                        status="ok" if not any("error" in item for item in results) else "partial",
                        started_at=now,
                        finished_at=utcnow(),
                        detail_json={"sources": len(due), "results": results},
                    )
                )
        return {"sources": len(due), "results": results}

    def _find_near_duplicate(
        self,
        session,
        value: int,
        band_values: tuple[int, ...],
        window_hours: int = 72,
        title_norm: str | None = None,
    ):
        cutoff = utcnow() - timedelta(hours=max(1, window_hours))
        if title_norm:
            same_title = session.scalar(
                select(NewsItem)
                .where(
                    NewsItem.title_norm == title_norm,
                    NewsItem.fetched_at >= cutoff,
                )
                .order_by(NewsItem.id.desc())
                .limit(1)
            )
            if same_title is not None:
                return same_title
        candidates = session.scalars(
            select(NewsItem)
            .where(
                NewsItem.fetched_at >= cutoff,
                NewsItem.simhash.is_not(None),
                or_(
                    NewsItem.simhash_band0 == band_values[0],
                    NewsItem.simhash_band1 == band_values[1],
                    NewsItem.simhash_band2 == band_values[2],
                    NewsItem.simhash_band3 == band_values[3],
                ),
            )
            .order_by(NewsItem.id.desc())
            .limit(200)
        ).all()
        best = None
        best_distance = 4
        for candidate in candidates:
            try:
                distance = hamming(value, int(str(candidate.simhash), 16))
            except (TypeError, ValueError):
                continue
            if distance <= 3 and distance < best_distance:
                best = candidate
                best_distance = distance
        return best

    def _process_items(
        self,
        source_id: int,
        tier: int,
        source_name: str,
        items: list[FetchedNews],
    ) -> dict[str, int]:
        stats = {"new": 0, "duplicate": 0, "filtered": 0, "queued": 0}
        positive = self.settings.get("candidate.positive_keywords", []) or POSITIVE_SEED
        negative = self.settings.get("candidate.negative_keywords", []) or NEGATIVE_SEED
        dedupe_hours = int(self.settings.get("candidate.dedupe_window_hours", 72) or 72)
        for fetched in items:
            try:
                result = self._process_one(
                    source_id,
                    tier,
                    source_name,
                    fetched,
                    positive,
                    negative,
                    dedupe_hours,
                )
                if result in {"queued", "filtered"}:
                    stats["new"] += 1
                stats[result] = stats.get(result, 0) + 1
            except IntegrityError:
                stats["duplicate"] += 1
            except Exception:
                # A single malformed item must not abort the whole batch.
                logger.exception("ingest.item.failed title=%s", fetched.title[:80])
                stats["filtered"] += 1
        return stats

    def _process_one(
        self,
        source_id: int,
        tier: int,
        source_name: str,
        fetched: FetchedNews,
        positive: list[str],
        negative: list[str],
        dedupe_hours: int,
    ) -> str:
        now = utcnow()
        title = fetched.title.strip()
        description = clean_html(fetched.description)
        title_norm = normalize_title(title)
        raw_url = fetched.url.strip()
        if not raw_url.lower().startswith(("http://", "https://")):
            raw_url = ""
        canonical = canonicalize_url(raw_url)
        external_id = (fetched.external_id or sha1_text(f"{canonical}|{title_norm}"))[:240]
        published_at = fetched.published_at or now
        digest_text = f"{title_norm}{description[:200]}"
        value = simhash64(digest_text)
        band_values = simhash_bands(value)
        simhash_hex = f"{value:016x}"
        raw = {
            "title": title,
            "description": description[:1000],
            "source": fetched.source_name,
            "url": raw_url,
            "published_at": published_at.isoformat(),
        }
        with self.db.session() as session:
            existing = session.scalar(
                select(NewsItem).where(
                    NewsItem.source_id == source_id,
                    NewsItem.external_id == external_id,
                )
            )
            if existing is not None:
                return "duplicate"
            if canonical:
                same_url = session.scalar(
                    select(NewsItem).where(NewsItem.canonical_url == canonical).limit(1)
                )
                if same_url is not None:
                    self._attach_to_cluster(session, same_url.cluster_id, now)
                    return "duplicate"
            near = self._find_near_duplicate(
                session,
                value,
                band_values,
                dedupe_hours,
                title_norm,
            )
            if near is not None:
                item = NewsItem(
                    source_id=source_id,
                    external_id=external_id,
                    cluster_id=near.cluster_id,
                    url=raw_url,
                    canonical_url=canonical,
                    title=title,
                    description=description,
                    source_name=fetched.source_name or source_name,
                    published_at=published_at,
                    fetched_at=now,
                    title_norm=title_norm,
                    simhash=simhash_hex,
                    simhash_band0=band_values[0],
                    simhash_band1=band_values[1],
                    simhash_band2=band_values[2],
                    simhash_band3=band_values[3],
                    content_hash=content_hash(canonical, title_norm),
                    heuristic_score=0,
                    status="duplicate",
                    raw_json=raw,
                )
                session.add(item)
                session.flush()
                self._attach_to_cluster(session, near.cluster_id, now)
                return "duplicate"

            item = NewsItem(
                source_id=source_id,
                external_id=external_id,
                cluster_id=None,
                url=raw_url,
                canonical_url=canonical,
                title=title,
                description=description,
                source_name=fetched.source_name or source_name,
                published_at=published_at,
                fetched_at=now,
                title_norm=title_norm,
                simhash=simhash_hex,
                simhash_band0=band_values[0],
                simhash_band1=band_values[1],
                simhash_band2=band_values[2],
                simhash_band3=band_values[3],
                content_hash=content_hash(canonical, title_norm),
                heuristic_score=0,
                status="new",
                raw_json=raw,
            )
            session.add(item)
            session.flush()
            cluster = NewsCluster(
                representative_news_id=item.id,
                title=title,
                first_seen_at=now,
                last_seen_at=now,
                member_count=1,
                needs_reanalysis=False,
            )
            session.add(cluster)
            session.flush()
            item.cluster_id = cluster.id
            score, _, _ = score_text(
                title,
                description,
                tier,
                published_at,
                cluster_size=1,
                positive=positive,
                negative=negative,
            )
            item.heuristic_score = score
            # Zero hard filter: score only decides processing order. The model
            # and the confidence gate decide whether the item is worth pushing.
            item.status = "queued"
            return "queued"

    @staticmethod
    def _attach_to_cluster(session, cluster_id: int | None, now: datetime) -> None:
        if cluster_id is None:
            return
        cluster = session.get(NewsCluster, cluster_id)
        if cluster is not None:
            cluster.member_count = int(cluster.member_count or 0) + 1
            cluster.last_seen_at = now
