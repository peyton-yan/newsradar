"""Scheduled DeepSeek batch analysis with token/cost accounting."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.analysis.billing import (
    compute_cost_cny,
    compute_flat_cost_cny,
    format_cny,
    is_peak,
)
from app.analysis.deepseek import DeepSeekError, chat_completion
from app.analysis.schema import is_push_eligible, parse_items, push_score
from app.db import Database, utcnow
from app.models import Analysis, AnalysisBatch, DailyUsage, NewsCluster, NewsItem
from app.push.system import send_system_alert
from app.settings_service import SettingsService


logger = logging.getLogger("newsradar.analysis")
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "analysis_v5.md"
DAY_FORMAT = "%Y-%m-%d"


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().strftime(DAY_FORMAT)


@dataclass
class Candidate:
    id: int
    cluster_id: int | None
    title: str
    description: str
    source_name: str
    published_at: datetime | None
    url: str
    heuristic_score: int


class AnalysisService:
    def __init__(self, db: Database, settings: SettingsService):
        self.db = db
        self.settings = settings

    def prompt_template_text(self) -> str:
        return PROMPT_PATH.read_text(encoding="utf-8")

    def _system_prompt_text(self) -> str:
        template = self.prompt_template_text()
        extra = str(self.settings.get("analysis.prompt_extra", "") or "").strip()
        return template.replace("{{extra_instructions}}", extra or "（无）")

    def _usage(self, session) -> DailyUsage | None:
        return session.get(DailyUsage, _today())

    def _ensure_usage(self, session) -> DailyUsage:
        row = session.get(DailyUsage, _today())
        if row is None:
            row = DailyUsage(day=_today())
            session.add(row)
        return row

    def _last_attempt(self) -> datetime | None:
        with self.db.session() as session:
            batch = session.scalar(
                select(AnalysisBatch).order_by(AnalysisBatch.started_at.desc()).limit(1)
            )
            return batch.started_at if batch else None

    def _candidates(self, limit: int) -> list[Candidate]:
        now = utcnow()
        max_age = int(self.settings.get("analysis.max_age_hours", 48) or 48)
        cutoff = now - timedelta(hours=max(1, max_age))
        with self.db.session() as session:
            session.execute(
                update(NewsItem)
                .where(NewsItem.status == "queued", NewsItem.published_at < cutoff)
                .values(status="expired")
            )
            rows = session.execute(
                select(NewsItem)
                .join(NewsCluster, NewsCluster.representative_news_id == NewsItem.id)
                .where(NewsItem.status == "queued", NewsItem.published_at >= cutoff)
                .order_by(NewsItem.heuristic_score.desc(), NewsItem.published_at.desc())
                .limit(max(1, limit))
            ).scalars().all()
            return [
                Candidate(
                    id=row.id,
                    cluster_id=row.cluster_id,
                    title=row.title,
                    description=row.description,
                    source_name=row.source_name,
                    published_at=row.published_at,
                    url=row.url,
                    heuristic_score=row.heuristic_score,
                )
                for row in rows
            ]

    def _oldest_queued_age_hours(self) -> float | None:
        with self.db.session() as session:
            row = session.scalar(
                select(NewsItem)
                .where(NewsItem.status == "queued")
                .order_by(NewsItem.fetched_at.asc())
                .limit(1)
            )
            if row is None or row.fetched_at is None:
                return None
            return (utcnow() - row.fetched_at).total_seconds() / 3600

    def _cost_limit(self) -> float:
        return max(0.0, float(self.settings.get("analysis.daily_cost_limit_cny", 0) or 0))

    def _cost_today(self) -> float:
        with self.db.session() as session:
            row = self._usage(session)
            return float(row.cost_cny or 0) if row else 0.0

    def ensure_cost_alert(self) -> bool:
        """Send at most one cost-cap alert per day. Returns True if sent."""
        limit = self._cost_limit()
        if limit <= 0:
            return False
        with self.db.session() as session:
            row = self._ensure_usage(session)
            cost = float(row.cost_cny or 0)
            if cost < limit or row.cost_alert_sent:
                return False
            row.cost_alert_sent = True
        title = "NewsRadar：今日费用已达上限"
        content = (
            f"今日 DeepSeek 费用约 {format_cny(cost)}，"
            f"已达到你设置的每日上限 {format_cny(limit)}。\n"
            "分析任务已暂停，明日 00:00 自动恢复；也可以在后台调高上限或等待次日。"
        )
        ok = send_system_alert(self.settings, title, content)
        logger.warning("analysis.cost_limit alert_sent=%s cost=%.4f limit=%.4f", ok, cost, limit)
        return ok

    def should_run(self) -> tuple[bool, str]:
        if not bool(self.settings.get("analysis.enabled", True)):
            return False, "disabled"
        if bool(self.settings.get("app.paused", False)):
            return False, "paused"
        if self._cost_limit() > 0 and self._cost_today() >= self._cost_limit():
            self.ensure_cost_alert()
            return False, "cost_limit"
        with self.db.session() as session:
            usage = self._usage(session)
            runs = int(usage.analysis_runs or 0) if usage else 0
            max_runs = int(self.settings.get("analysis.max_runs_per_day", 8) or 8)
            if runs >= max_runs:
                return False, "max_runs"
            input_tokens = int(usage.input_tokens or 0) if usage else 0
            output_tokens = int(usage.output_tokens or 0) if usage else 0
        input_budget = int(self.settings.get("analysis.daily_input_token_budget", 100000) or 100000)
        output_budget = int(self.settings.get("analysis.daily_output_token_budget", 25000) or 25000)
        soft = float(self.settings.get("analysis.soft_stop_ratio", 0.9) or 0.9)
        if input_tokens >= input_budget * soft or output_tokens >= output_budget * soft:
            return False, "token_budget"
        last = self._last_attempt()
        if last is not None:
            minimum = int(self.settings.get("analysis.min_interval_minutes", 120) or 120)
            if utcnow() - last < timedelta(minutes=minimum):
                return False, "min_interval"
        provider = str(self.settings.get("analysis.provider", "deepseek") or "deepseek")
        if provider != "ollama" and not str(self.settings.get("analysis.api_key", "") or "").strip():
            return False, "no_api_key"
        return True, "ok"

    def analyze_due(self) -> dict[str, Any]:
        ok, reason = self.should_run()
        if not ok:
            return {"skipped": reason}
        max_candidates = int(self.settings.get("analysis.max_candidates_per_run", 20) or 20)
        batch_size = int(self.settings.get("analysis.batch_size", 12) or 12)
        min_candidates = int(self.settings.get("analysis.min_candidates", 1) or 1)
        max_wait = int(self.settings.get("analysis.max_wait_hours", 6) or 6)
        candidates = self._candidates(max_candidates)
        if len(candidates) < min_candidates:
            oldest = self._oldest_queued_age_hours()
            if oldest is None or oldest < max_wait:
                return {"skipped": "not_enough_candidates", "candidates": len(candidates)}
        if not candidates:
            return {"skipped": "no_candidates"}
        selected = candidates[: max(1, batch_size)]
        payload: list[dict[str, Any]] = []
        max_chars = int(self.settings.get("analysis.max_chars_per_item", 500) or 500)
        max_total_chars = int(self.settings.get("analysis.max_total_input_chars", 18000) or 18000)
        total_chars = 0
        for candidate in selected:
            description = (candidate.description or "")[:max_chars]
            entry = {
                "news_id": str(candidate.id),
                "title": candidate.title[:200],
                "description": description,
                "source": candidate.source_name,
                "published_at": candidate.published_at.isoformat() if candidate.published_at else "",
                "url": candidate.url,
            }
            size = len(entry["title"]) + len(description)
            if payload and total_chars + size > max_total_chars:
                break
            payload.append(entry)
            total_chars += size
        if not payload:
            return {"skipped": "empty_payload"}
        per_run_limit = int(self.settings.get("analysis.per_run_input_token_limit", 15000) or 15000)
        while payload and (total_chars / 1.5 + 500) > per_run_limit:
            removed = payload.pop()
            total_chars -= len(removed["title"]) + len(removed["description"])
        if not payload:
            return {"skipped": "per_run_limit"}

        provider = str(self.settings.get("analysis.provider", "deepseek") or "deepseek")
        model = str(self.settings.get("analysis.model", "deepseek-flash") or "deepseek-flash")
        base_url = str(self.settings.get("analysis.base_url", "https://api.deepseek.com"))
        api_key = str(self.settings.get("analysis.api_key", "") or "")
        prompt_version = str(self.settings.get("analysis.prompt_version", "v5") or "v5")
        max_tokens = int(self.settings.get("analysis.max_output_tokens", 4096) or 4096)
        timeout = int(self.settings.get("analysis.timeout_sec", 120) or 120)
        started = utcnow()
        peak = is_peak(datetime.now(timezone.utc))
        with self.db.session() as session:
            batch = AnalysisBatch(
                status="running",
                model=model,
                prompt_version=prompt_version,
                item_count=len(payload),
                started_at=started,
                is_peak=peak,
            )
            session.add(batch)
            session.flush()
            batch_id = batch.id
            usage_row = self._ensure_usage(session)
            usage_row.analysis_runs = int(usage_row.analysis_runs or 0) + 1
            for entry in payload:
                item = session.get(NewsItem, int(entry["news_id"]))
                if item is not None:
                    item.status = "analyzing"

        user_prompt = (
            f"分析以下 {len(payload)} 条新闻。只输出 JSON，不要输出其他内容。\n\n"
            f"<news_json>\n{json.dumps(payload, ensure_ascii=False)}\n</news_json>"
        )
        try:
            content, usage = chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_prompt=self._system_prompt_text(),
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                provider=provider,
            )
            expected = {entry["news_id"] for entry in payload}
            results = parse_items(content, expected)
            if not results:
                raise ValueError("模型没有返回任何可识别条目")
        except (DeepSeekError, ValueError) as exc:
            logger.warning("analysis.failed batch=%s error=%s", batch_id, str(exc)[:300])
            with self.db.session() as session:
                batch = session.get(AnalysisBatch, batch_id)
                if batch is not None:
                    batch.status = "failed"
                    batch.error_message = str(exc)[:1000]
                    batch.finished_at = utcnow()
                for entry in payload:
                    item = session.get(NewsItem, int(entry["news_id"]))
                    if item is not None and item.status == "analyzing":
                        item.status = "queued"
            return {"batch_id": batch_id, "ok": False, "error": str(exc)[:300]}

        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        cache_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        if cache_hit + cache_miss == 0:
            cache_miss = input_tokens
        cost_usd = 0.0
        if provider == "deepseek":
            cost_cny = compute_cost_cny(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_hit_tokens=cache_hit,
                cache_miss_tokens=cache_miss,
                peak=peak,
            )
        else:
            cost_cny = compute_flat_cost_cny(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_price_cny=float(self.settings.get("analysis.input_price_cny", 1.0) or 1.0),
                output_price_cny=float(self.settings.get("analysis.output_price_cny", 4.0) or 4.0),
            )
        # Legacy key name: this field now filters the LLM relevance score.
        min_relevance = int(self.settings.get("push.min_confidence", 50) or 50)
        now = utcnow()
        saved = 0
        with self.db.session() as session:
            batch = session.get(AnalysisBatch, batch_id)
            if batch is not None:
                batch.input_tokens = input_tokens
                batch.output_tokens = output_tokens
                batch.cache_hit_tokens = cache_hit
                batch.cache_miss_tokens = cache_miss
                batch.cost_usd = cost_usd
                batch.cost_cny = cost_cny
                batch.estimated_cost = cost_usd
                batch.status = "ok" if len(results) == len(payload) else "partial"
                batch.finished_at = now
                if len(results) != len(payload):
                    batch.error_message = "部分条目未被模型返回"
            usage_row = self._ensure_usage(session)
            usage_row.input_tokens = int(usage_row.input_tokens or 0) + input_tokens
            usage_row.output_tokens = int(usage_row.output_tokens or 0) + output_tokens
            usage_row.cache_hit_tokens = int(usage_row.cache_hit_tokens or 0) + cache_hit
            usage_row.cache_miss_tokens = int(usage_row.cache_miss_tokens or 0) + cache_miss
            usage_row.cost_usd = float(usage_row.cost_usd or 0) + cost_usd
            usage_row.cost_cny = float(usage_row.cost_cny or 0) + cost_cny
            for result in results:
                item = session.get(NewsItem, int(result.news_id))
                if item is None:
                    continue
                score = push_score(result)
                # The model returns a 0-100 finance/A-share relevance score.
                # That score, not confidence, decides whether a story is on-topic.
                eligible = is_push_eligible(result, min_relevance)
                analysis = session.scalar(
                    select(Analysis).where(Analysis.news_id == item.id).limit(1)
                )
                if analysis is None:
                    analysis = Analysis(news_id=item.id)
                    session.add(analysis)
                analysis.cluster_id = item.cluster_id
                analysis.batch_id = batch_id
                analysis.relevance = result.relevance
                analysis.materiality = result.materiality
                analysis.urgency = result.urgency
                analysis.confidence = result.confidence
                analysis.direction = result.direction
                analysis.horizon = result.horizon
                analysis.summary = result.summary
                analysis.transmission = result.transmission
                analysis.sectors_json = result.sectors
                analysis.watch = result.watch
                analysis.caveat = result.caveat
                analysis.push_score = score
                analysis.push_eligible = eligible
                analysis.push_status = "pending" if eligible else "not_eligible"
                item.status = "analyzed"
                cluster = session.get(NewsCluster, item.cluster_id) if item.cluster_id else None
                if cluster is not None:
                    cluster.analyzed_at = now
                saved += 1
            returned = {result.news_id for result in results}
            for entry in payload:
                if entry["news_id"] in returned:
                    continue
                item = session.get(NewsItem, int(entry["news_id"]))
                if item is not None and item.status == "analyzing":
                    item.status = "queued"

        self.ensure_cost_alert()
        return {
            "batch_id": batch_id,
            "ok": True,
            "items": saved,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cost_usd": round(cost_usd, 6),
            "cost_cny": round(cost_cny, 6),
            "peak": peak,
        }
