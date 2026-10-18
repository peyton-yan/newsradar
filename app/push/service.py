"""Push pipeline: urgent, scheduled digest, retries and history."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from sqlalchemy import func, select

from app.db import Database, utcnow
from app.models import Analysis, DailyUsage, NewsItem, PushMessage, PushMessageItem
from app.settings_service import SettingsService


logger = logging.getLogger("newsradar.push")
SHANGHAI = ZoneInfo("Asia/Shanghai")
DIRECTION_LABEL = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "uncertain": "不确定",
}
HORIZON_LABEL = {
    "intraday": "当日",
    "days": "数日",
    "weeks": "数周",
    "months": "数月",
}


@dataclass
class PushResult:
    ok: bool
    retryable: bool = False
    provider_msg_id: str | None = None
    error: str | None = None


class WxPusherProvider:
    def __init__(self, config: dict[str, Any]):
        self.app_token = str(config.get("app_token") or "")
        self.uid = str(config.get("uid") or "")

    def send(self, *, title: str, content: str, url: str | None) -> PushResult:
        if not self.app_token or not self.uid:
            return PushResult(False, False, error="WxPusher appToken/UID 未配置")
        payload = {
            "appToken": self.app_token,
            "content": content[:40000],
            "summary": title[:100],
            "contentType": 1,
            "uids": [self.uid],
        }
        if url:
            payload["url"] = url[:1000]
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.post(
                    "https://wxpusher.zjiecode.com/api/send/message",
                    json=payload,
                )
            body = response.json()
        except httpx.TimeoutException:
            return PushResult(False, True, error="WxPusher 超时")
        except (httpx.HTTPError, ValueError) as exc:
            return PushResult(False, True, error=f"WxPusher 网络/响应错误: {exc}")
        if response.status_code == 429 or response.status_code >= 500:
            return PushResult(False, True, error=f"WxPusher HTTP {response.status_code}")
        if response.status_code >= 400 or body.get("code") != 1000:
            return PushResult(
                False,
                False,
                error=f"WxPusher 拒绝: HTTP {response.status_code} code={body.get('code')} {body.get('msg')}",
            )
        data = body.get("data")
        return PushResult(True, provider_msg_id=str(data) if data is not None else None)


class ServerChanProvider:
    def __init__(self, config: dict[str, Any]):
        self.sendkey = str(config.get("sendkey") or "")

    def send(self, *, title: str, content: str, url: str | None) -> PushResult:
        if not self.sendkey:
            return PushResult(False, False, error="Server酱 SendKey 未配置")
        endpoint = f"https://sctapi.ftqq.com/{self.sendkey}.send"
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.post(endpoint, data={"title": title[:32], "desp": content})
            body = response.json()
        except httpx.TimeoutException:
            return PushResult(False, True, error="Server酱 超时")
        except (httpx.HTTPError, ValueError) as exc:
            return PushResult(False, True, error=f"Server酱 网络/响应错误: {exc}")
        if response.status_code == 429 or response.status_code >= 500:
            return PushResult(False, True, error=f"Server酱 HTTP {response.status_code}")
        if response.status_code >= 400 or body.get("code") != 0:
            return PushResult(
                False,
                False,
                error=f"Server酱 拒绝: HTTP {response.status_code} code={body.get('code')} {body.get('message')}",
            )
        return PushResult(True, provider_msg_id=str(body.get("data") or ""))


class WeComBotProvider:
    def __init__(self, config: dict[str, Any]):
        self.webhook = str(config.get("webhook") or "")

    def send(self, *, title: str, content: str, url: str | None) -> PushResult:
        if not self.webhook:
            return PushResult(False, False, error="企业微信机器人 Webhook 未配置")
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.post(
                    self.webhook,
                    json={"msgtype": "text", "text": {"content": content[:4000]}},
                )
            body = response.json()
        except httpx.TimeoutException:
            return PushResult(False, True, error="企业微信超时")
        except (httpx.HTTPError, ValueError) as exc:
            return PushResult(False, True, error=f"企业微信网络/响应错误: {exc}")
        if response.status_code == 429 or response.status_code >= 500:
            return PushResult(False, True, error=f"企业微信 HTTP {response.status_code}")
        if response.status_code >= 400 or body.get("errcode") != 0:
            return PushResult(
                False,
                False,
                error=f"企业微信拒绝: HTTP {response.status_code} errcode={body.get('errcode')} {body.get('errmsg')}",
            )
        return PushResult(True)


class PushPlusProvider:
    def __init__(self, config: dict[str, Any]):
        self.token = str(config.get("token") or "")

    def send(self, *, title: str, content: str, url: str | None) -> PushResult:
        if not self.token:
            return PushResult(False, False, error="PushPlus token 未配置")
        payload = {
            "token": self.token,
            "title": title[:100],
            "content": content[:40000],
            "template": "txt",
        }
        if url:
            payload["url"] = url[:1000]
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                response = client.post("https://www.pushplus.plus/send", json=payload)
            body = response.json()
        except httpx.TimeoutException:
            return PushResult(False, True, error="PushPlus 超时")
        except (httpx.HTTPError, ValueError) as exc:
            return PushResult(False, True, error=f"PushPlus 网络/响应错误: {exc}")
        if response.status_code == 429 or response.status_code >= 500:
            return PushResult(False, True, error=f"PushPlus HTTP {response.status_code}")
        if response.status_code >= 400 or body.get("code") != 200:
            return PushResult(
                False,
                False,
                error=f"PushPlus 拒绝: HTTP {response.status_code} code={body.get('code')} {body.get('msg')}",
            )
        return PushResult(True, provider_msg_id=str(body.get("data") or ""))


class PushService:
    def __init__(self, db: Database, settings: SettingsService):
        self.db = db
        self.settings = settings

    def _provider(self):
        name = str(self.settings.get("push.provider", "wxpusher") or "wxpusher")
        config = self.settings.get("push.provider_config", {}) or {}
        if not isinstance(config, dict):
            config = {}
        if name == "wxpusher":
            return name, WxPusherProvider(config)
        if name == "pushplus":
            return name, PushPlusProvider(config)
        if name == "serverchan":
            return name, ServerChanProvider(config)
        if name == "wecom_bot":
            return name, WeComBotProvider(config)
        return name, None

    def _in_quiet(self, now: datetime) -> bool:
        value = self.settings.get("push.quiet_hours", "") or ""
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            start_s, end_s = str(value[0]), str(value[1])
        else:
            text = str(value)
            if not text:
                return False
            try:
                start_s, end_s = text.split("-", 1)
            except ValueError:
                return False
        try:
            start = datetime.strptime(start_s.strip(), "%H:%M").time()
            end = datetime.strptime(end_s.strip(), "%H:%M").time()
        except ValueError:
            return False
        current = now.time()
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def _daily_push_count(self) -> int:
        start = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
        with self.db.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(PushMessage)
                    .where(PushMessage.status == "sent", PushMessage.sent_at >= start_utc)
                )
                or 0
            )

    def _limit_reached(self) -> bool:
        limit = int(self.settings.get("push.daily_push_limit", 10) or 10)
        return self._daily_push_count() >= limit

    def _message_exists(self, dedupe_key: str) -> bool:
        with self.db.session() as session:
            return session.scalar(
                select(PushMessage.id).where(PushMessage.dedupe_key == dedupe_key).limit(1)
            ) is not None

    def _create_message(
        self,
        *,
        dedupe_key: str,
        kind: str,
        title: str,
        body: str,
        analysis_ids: list[int],
        url: str | None = None,
    ) -> PushMessage | None:
        with self.db.session() as session:
            existing = session.scalar(
                select(PushMessage).where(PushMessage.dedupe_key == dedupe_key).limit(1)
            )
            if existing is not None:
                return existing
            message = PushMessage(
                dedupe_key=dedupe_key,
                kind=kind,
                title=title[:200],
                body_plain=body,
                body_markdown=body,
                status="pending",
                provider=str(self.settings.get("push.provider", "wxpusher") or "wxpusher"),
                next_attempt_at=utcnow(),
            )
            session.add(message)
            session.flush()
            for analysis_id in analysis_ids:
                session.add(PushMessageItem(message_id=message.id, analysis_id=analysis_id))
            for analysis_id in analysis_ids:
                analysis = session.get(Analysis, analysis_id)
                if analysis is not None:
                    analysis.push_status = "sending"
            message_id = message.id
        return self._get_message(message_id)

    def _get_message(self, message_id: int) -> PushMessage | None:
        with self.db.session() as session:
            return session.get(PushMessage, message_id)

    def _set_analyses(self, message_id: int, status: str) -> None:
        with self.db.session() as session:
            links = list(
                session.scalars(
                    select(PushMessageItem).where(PushMessageItem.message_id == message_id)
                ).all()
            )
            for link in links:
                analysis = session.get(Analysis, link.analysis_id)
                if analysis is not None:
                    analysis.push_status = status

    def _deliver(self, message_id: int, *, check_quota: bool = True) -> bool:
        provider_name, provider = self._provider()
        if provider is None:
            return False
        with self.db.session() as session:
            message = session.get(PushMessage, message_id)
            if message is None or message.status in {"sent", "dead"}:
                return message is not None and message.status == "sent"
            message.status = "sending"
            message.attempts = int(message.attempts or 0) + 1
            attempts = message.attempts
            title = message.title or "财经宏观雷达"
            body = message.body_plain or ""
            url = None
            if message.kind == "urgent":
                link = session.scalar(
                    select(PushMessageItem).where(PushMessageItem.message_id == message.id).limit(1)
                )
                if link is not None:
                    analysis = session.get(Analysis, link.analysis_id)
                    if analysis is not None:
                        news = session.get(NewsItem, analysis.news_id)
                        url = news.url if news else None
        result = provider.send(title=title, content=body, url=url)
        with self.db.session() as session:
            message = session.get(PushMessage, message_id)
            if message is None:
                return result.ok
            if result.ok:
                message.status = "sent"
                message.sent_at = utcnow()
                message.provider = provider_name
                message.provider_msg_id = result.provider_msg_id
                message.last_error = None
                day = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
                usage = session.get(DailyUsage, day)
                if usage is None:
                    usage = DailyUsage(day=day)
                    session.add(usage)
                usage.pushes = int(usage.pushes or 0) + 1
            else:
                message.last_error = (result.error or "unknown")[:1000]
                retry_max = int(self.settings.get("push.retry_max", 3) or 3)
                if result.retryable and attempts < retry_max:
                    message.status = "retrying"
                    delays = [1, 5, 15]
                    minutes = delays[min(attempts - 1, len(delays) - 1)]
                    message.next_attempt_at = utcnow() + timedelta(minutes=minutes)
                else:
                    message.status = "dead"
            message_id_local = message.id
        if result.ok:
            self._set_analyses(message_id_local, "sent")
        elif message.status == "dead":
            self._set_analyses(message_id_local, "failed")
        if result.ok and check_quota:
            self._maybe_quota_alert()
        return result.ok

    def _maybe_quota_alert(self) -> None:
        limit = int(self.settings.get("push.daily_push_limit", 10) or 10)
        if limit <= 0:
            return
        remaining = limit - self._daily_push_count()
        if not 0 < remaining <= 5:
            return
        day = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
        key = f"quota:{day}"
        if self._message_exists(key):
            return
        body = (
            f"NewsRadar 推送额度提醒：今日还剩 {remaining} 条推送额度（上限 {limit} 条）。\n"
            "额度用完前请留意，分析结果将暂时无法继续推送。"
        )
        message = self._create_message(
            dedupe_key=key,
            kind="system",
            title="NewsRadar：推送额度提醒",
            body=body,
            analysis_ids=[],
        )
        if message is not None:
            self._deliver(message.id, check_quota=False)

    def _retry_due(self) -> int:
        now = utcnow()
        with self.db.session() as session:
            ids = list(
                session.scalars(
                    select(PushMessage.id)
                    .where(
                        PushMessage.status.in_(("pending", "retrying")),
                        PushMessage.next_attempt_at.is_not(None),
                        PushMessage.next_attempt_at <= now,
                    )
                    .order_by(PushMessage.next_attempt_at)
                    .limit(10)
                ).all()
            )
        for message_id in ids:
            self._deliver(message_id)
        return len(ids)

    def _urgent_candidates(self, now: datetime) -> list[tuple[Analysis, NewsItem]]:
        urgent = int(self.settings.get("push.urgent_threshold", 85) or 85)
        quiet_urgent = int(self.settings.get("push.quiet_urgent_threshold", 92) or 92)
        in_quiet = self._in_quiet(now)
        with self.db.session() as session:
            rows = session.execute(
                select(Analysis, NewsItem)
                .join(NewsItem, NewsItem.id == Analysis.news_id)
                .where(
                    Analysis.push_status == "pending",
                    Analysis.push_eligible.is_(True),
                    Analysis.push_score >= urgent,
                    Analysis.materiality >= 70,
                    Analysis.confidence >= 50,
                )
                .order_by(Analysis.push_score.desc(), Analysis.id)
                .limit(5)
            ).all()
            if in_quiet:
                rows = [row for row in rows if row[0].push_score >= quiet_urgent]
            return list(rows)

    def _format_urgent(self, analysis: Analysis, news: NewsItem) -> tuple[str, str]:
        title = f"【重要】{(news.title or '')[:60]}"
        direction = DIRECTION_LABEL.get(analysis.direction, "不确定")
        horizon = HORIZON_LABEL.get(analysis.horizon, "数日")
        body = (
            f"{title}\n"
            f"摘要：{analysis.summary or '—'}\n"
            f"影响：{analysis.transmission or '—'}"
            f"（{direction}，置信度 {analysis.confidence}%，影响期：{horizon}）\n"
            f"来源：{news.source_name or '未知'} · {news.url or ''}"
        )
        if bool(self.settings.get("push.include_disclaimer", True)):
            body += "\n由 AI 辅助整理，仅供信息参考，不构成投资建议。"
        max_chars = int(self.settings.get("push.max_chars_per_message", 1200) or 1200)
        return title, body[:max_chars]

    def send_urgent(self, now: datetime | None = None) -> int:
        if not bool(self.settings.get("push.enabled", True)):
            return 0
        if self._limit_reached():
            return 0
        current = now or datetime.now(SHANGHAI)
        sent = 0
        for analysis, news in self._urgent_candidates(current):
            dedupe_key = f"urgent:{analysis.id}:v1"
            if self._message_exists(dedupe_key):
                continue
            title, body = self._format_urgent(analysis, news)
            message = self._create_message(
                dedupe_key=dedupe_key,
                kind="urgent",
                title=title,
                body=body,
                analysis_ids=[analysis.id],
            )
            if message is not None:
                self._deliver(message.id)
                sent += 1
        return sent

    def _format_immediate(self, rows: list[tuple[Analysis, NewsItem]], now: datetime) -> tuple[str, str]:
        urgent = any(row[0].push_score >= int(self.settings.get("push.urgent_threshold", 85) or 85) for row in rows)
        title = "【重要】财经宏观雷达" if urgent else "财经宏观雷达 · 最新分析"
        lines = [f"财经宏观雷达 · {now.strftime('%Y-%m-%d %H:%M')}", f"本轮分析 {len(rows)} 条：", ""]
        for index, (analysis, news) in enumerate(rows, start=1):
            direction = DIRECTION_LABEL.get(analysis.direction, "不确定")
            horizon = HORIZON_LABEL.get(analysis.horizon, "数日")
            lines.extend(
                [
                    f"{index}. {(news.title or '')[:60]}",
                    f"摘要：{analysis.summary or '—'}",
                    f"影响：{analysis.transmission or '—'}（{direction}，置信度 {analysis.confidence}%，影响期：{horizon}）",
                    f"关注：{analysis.watch or '—'}",
                    f"来源：{news.source_name or '未知'} · {news.url or ''}",
                    "",
                ]
            )
        if bool(self.settings.get("push.include_disclaimer", True)):
            lines.append("由 AI 辅助整理，仅供信息参考，不构成投资建议。")
        max_chars = int(self.settings.get("push.max_chars_per_message", 1200) or 1200)
        return title, "\n".join(lines)[:max_chars]

    def send_immediate(self, now: datetime | None = None) -> int:
        """Push every batch of eligible analyses as soon as analysis finishes."""
        if not bool(self.settings.get("push.enabled", True)):
            return 0
        if self._limit_reached():
            with self.db.session() as session:
                pending = list(
                    session.scalars(
                        select(Analysis).where(
                            Analysis.push_status == "pending",
                            Analysis.push_eligible.is_(True),
                        )
                    ).all()
                )
                for analysis in pending:
                    analysis.push_status = "suppressed"
            return 0
        limit = int(self.settings.get("push.max_items_per_digest", 5) or 5)
        with self.db.session() as session:
            rows = session.execute(
                select(Analysis, NewsItem)
                .join(NewsItem, NewsItem.id == Analysis.news_id)
                .where(Analysis.push_status == "pending", Analysis.push_eligible.is_(True))
                .order_by(Analysis.created_at, Analysis.id)
                .limit(max(1, limit))
            ).all()
            rows = list(rows)
        if not rows:
            return 0
        ids = [analysis.id for analysis, _ in rows]
        dedupe_key = f"immediate:{min(ids)}:{max(ids)}"
        if self._message_exists(dedupe_key):
            return 0
        title, body = self._format_immediate(rows, now or datetime.now(SHANGHAI))
        message = self._create_message(
            dedupe_key=dedupe_key,
            kind="immediate",
            title=title,
            body=body,
            analysis_ids=ids,
        )
        if message is None:
            return 0
        return 1 if self._deliver(message.id) else 0

    def _digest_window(self, now: datetime) -> tuple[str, datetime] | None:
        times = self.settings.get("push.digest_times", []) or []
        if isinstance(times, str):
            times = [item.strip() for item in times.split(",") if item.strip()]
        for slot in times:
            try:
                hour, minute = [int(part) for part in str(slot).split(":", 1)]
            except (TypeError, ValueError):
                continue
            slot_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot_at <= now < slot_at + timedelta(minutes=120):
                return str(slot), slot_at
        return None

    def _digest_candidates(self, since: datetime) -> list[tuple[Analysis, NewsItem]]:
        limit = int(self.settings.get("push.max_items_per_digest", 5) or 5)
        with self.db.session() as session:
            rows = session.execute(
                select(Analysis, NewsItem)
                .join(NewsItem, NewsItem.id == Analysis.news_id)
                .where(
                    Analysis.push_status == "pending",
                    Analysis.push_eligible.is_(True),
                    Analysis.created_at >= since,
                )
                .order_by(Analysis.push_score.desc(), Analysis.id)
                .limit(max(1, limit))
            ).all()
            return list(rows)

    def _format_digest(self, rows: list[tuple[Analysis, NewsItem]], now: datetime, slot: str) -> tuple[str, str]:
        header = f"财经宏观雷达 · {now.strftime('%Y-%m-%d')} {slot}"
        lines = [header, f"过去一段时间共 {len(rows)} 条值得关注：", ""]
        for index, (analysis, news) in enumerate(rows, start=1):
            direction = DIRECTION_LABEL.get(analysis.direction, "不确定")
            horizon = HORIZON_LABEL.get(analysis.horizon, "数日")
            lines.extend(
                [
                    f"{index}. {(news.title or '')[:60]}",
                    f"摘要：{analysis.summary or '—'}",
                    f"影响：{analysis.transmission or '—'}（{direction}，置信度 {analysis.confidence}%，影响期：{horizon}）",
                    f"关注：{analysis.watch or '—'}",
                    f"来源：{news.source_name or '未知'} · {news.url or ''}",
                    "",
                ]
            )
        if bool(self.settings.get("push.include_disclaimer", True)):
            lines.append("由 AI 辅助整理，仅供信息参考，不构成投资建议。")
        body = "\n".join(lines)
        max_chars = int(self.settings.get("push.max_chars_per_message", 1200) or 1200)
        return header, body[:max_chars]

    def send_digest(self, now: datetime | None = None) -> bool:
        if not bool(self.settings.get("push.enabled", True)):
            return False
        current = now or datetime.now(SHANGHAI)
        window = self._digest_window(current)
        if window is None:
            return False
        slot, slot_at = window
        dedupe_key = f"digest:{current.strftime('%Y-%m-%d')}:{slot}"
        if self._message_exists(dedupe_key):
            return False
        if self._limit_reached():
            return False
        since = (slot_at - timedelta(hours=24)).astimezone(timezone.utc).replace(tzinfo=None)
        with self.db.session() as session:
            last = session.scalar(
                select(PushMessage)
                .where(PushMessage.kind == "digest", PushMessage.status == "sent")
                .order_by(PushMessage.sent_at.desc())
                .limit(1)
            )
            if last is not None and last.sent_at is not None:
                since = max(since, last.sent_at)
        rows = self._digest_candidates(since)
        if not rows:
            return False
        title, body = self._format_digest(rows, current, slot)
        message = self._create_message(
            dedupe_key=dedupe_key,
            kind="digest",
            title=title,
            body=body,
            analysis_ids=[analysis.id for analysis, _ in rows],
        )
        if message is None:
            return False
        return self._deliver(message.id)

    def push_due(self) -> dict[str, Any]:
        retries = self._retry_due()
        immediate = self.send_immediate()
        return {"retries": retries, "immediate": immediate}
