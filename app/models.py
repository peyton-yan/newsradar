"""Database models for the news radar.

The M1 release only writes ``settings``, ``audit_log``, ``worker_heartbeat``,
``daily_usage`` and ``locks``.  The remaining tables are created now so later
phases can add behaviour without a disruptive first migration.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    interval_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_news_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(240), nullable=False)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    title_norm: Mapped[str | None] = mapped_column(Text, nullable=True)
    simhash: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    simhash_band0: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    simhash_band1: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    simhash_band2: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    simhash_band3: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    heuristic_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="new", index=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class NewsCluster(Base):
    __tablename__ = "news_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    representative_news_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    needs_reanalysis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AnalysisBatch(Base):
    __tablename__ = "analysis_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    model: Mapped[str] = mapped_column(String(80), nullable=False, default="deepseek-chat")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v2")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_miss_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_peak: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Kept for old rows/schema compatibility; new analyses use relevance.
    macro_relevant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    relevance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    materiality: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urgency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="uncertain")
    horizon: Mapped[str] = mapped_column(String(16), nullable=False, default="days")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    transmission: Mapped[str | None] = mapped_column(Text, nullable=True)
    sectors_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    watch: Mapped[str | None] = mapped_column(Text, nullable=True)
    caveat: Mapped[str | None] = mapped_column(Text, nullable=True)
    push_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    push_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    push_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_eligible", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class PushMessage(Base):
    __tablename__ = "push_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_plain: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PushMessageItem(Base):
    __tablename__ = "push_message_items"

    message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class JobRequest(Base):
    __tablename__ = "job_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    requested_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_beat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")


class DailyUsage(Base):
    __tablename__ = "daily_usage"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_miss_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    analysis_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tianapi_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pushes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_alert_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target: Mapped[str | None] = mapped_column(String(160), nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Lock(Base):
    __tablename__ = "locks"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
