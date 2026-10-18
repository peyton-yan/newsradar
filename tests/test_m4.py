from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.db import utcnow
from app.models import Analysis, NewsItem, PushMessage
from app.push.service import PushResult


def _seed_analysis(app, *, score: int, materiality: int = 80, confidence: int = 80):
    now = utcnow()
    with app.state.db.session() as session:
        item = NewsItem(
            source_id=1,
            external_id=f"push-{score}-{now.timestamp()}",
            url="https://example.com/push",
            canonical_url="https://example.com/push",
            title="央行宣布降准",
            description="释放流动性",
            source_name="测试源",
            published_at=now,
            fetched_at=now,
            title_norm="央行宣布降准",
            content_hash="x",
            heuristic_score=score,
            status="analyzed",
        )
        session.add(item)
        session.flush()
        analysis = Analysis(
            news_id=item.id,
            macro_relevant=True,
            materiality=materiality,
            urgency=80,
            confidence=confidence,
            direction="bullish",
            horizon="days",
            summary="央行宣布降准。",
            transmission="可能改善流动性。",
            sectors_json=["银行"],
            watch="关注后续。",
            caveat="原文未披露。",
            push_score=score,
            push_eligible=True,
            push_status="pending",
            created_at=now,
        )
        session.add(analysis)
        session.flush()
        return analysis.id


def _configure_push(app, monkeypatch, result: PushResult):
    settings = app.state.settings_service
    settings.update_group(
        "push",
        {
            "enabled": True,
            "provider": "wxpusher",
            "provider_config": {"app_token": "AT_test", "uid": "UID_test"},
            "urgent_threshold": 85,
            "digest_threshold": 60,
            "daily_push_limit": 10,
            "retry_max": 3,
        },
    )
    monkeypatch.setattr(
        "app.push.service.WxPusherProvider.send",
        lambda self, **kwargs: result,
    )


def test_quiet_hours_accepts_configured_list(app):
    settings = app.state.settings_service
    settings.update_group("push", {"quiet_hours": ["23:00", "07:00"]})
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    assert app.state.push._in_quiet(datetime(2026, 10, 18, 23, 30, tzinfo=tz)) is True
    assert app.state.push._in_quiet(datetime(2026, 10, 18, 12, 0, tzinfo=tz)) is False


def test_immediate_push_and_quota_warning(app, monkeypatch):
    settings = app.state.settings_service
    _configure_push(app, monkeypatch, PushResult(True, provider_msg_id="m0"))
    settings.update_group("push", {"daily_push_limit": 6})
    analysis_id = _seed_analysis(app, score=70, materiality=60, confidence=60)
    assert app.state.push.send_immediate() == 1
    with app.state.db.session() as session:
        analysis = session.get(Analysis, analysis_id)
        assert analysis is not None and analysis.push_status == "sent"
        messages = list(session.scalars(select(PushMessage).order_by(PushMessage.id)).all())
        assert len(messages) == 2
        assert messages[0].kind == "immediate"
        assert messages[1].kind == "system"
        assert "还剩 5 条" in (messages[1].body_plain or "")


def test_urgent_push_is_sent_once(app, monkeypatch):
    _configure_push(app, monkeypatch, PushResult(True, provider_msg_id="m1"))
    analysis_id = _seed_analysis(app, score=90)
    assert app.state.push.send_urgent() == 1
    with app.state.db.session() as session:
        analysis = session.get(Analysis, analysis_id)
        assert analysis is not None and analysis.push_status == "sent"
        assert int(session.scalar(select(func.count()).select_from(PushMessage)) or 0) == 1
    assert app.state.push.send_urgent() == 0


def test_digest_push_uses_pending_analysis(app, monkeypatch):
    settings = app.state.settings_service
    _configure_push(app, monkeypatch, PushResult(True, provider_msg_id="m2"))
    now = datetime.now().astimezone()
    settings.update_group("push", {"digest_times": [now.strftime("%H:%M")]})
    analysis_id = _seed_analysis(app, score=70, materiality=60, confidence=60)
    assert app.state.push.send_digest(now) is True
    with app.state.db.session() as session:
        analysis = session.get(Analysis, analysis_id)
        assert analysis is not None and analysis.push_status == "sent"
        message = session.scalar(select(PushMessage))
        assert message is not None and message.kind == "digest"
        assert message.status == "sent"


def test_push_retry_then_success(app, monkeypatch):
    settings = app.state.settings_service
    _configure_push(app, monkeypatch, PushResult(False, True, error="暂时失败"))
    analysis_id = _seed_analysis(app, score=90)
    assert app.state.push.send_urgent() == 1
    with app.state.db.session() as session:
        message = session.scalar(select(PushMessage))
        assert message is not None and message.status == "retrying"
        assert message.attempts == 1
        message_id = message.id
        message.next_attempt_at = utcnow() - timedelta(seconds=1)
    monkeypatch.setattr(
        "app.push.service.WxPusherProvider.send",
        lambda self, **kwargs: PushResult(True, provider_msg_id="m3"),
    )
    assert app.state.push._retry_due() == 1
    with app.state.db.session() as session:
        message = session.get(PushMessage, message_id)
        assert message is not None and message.status == "sent"
        analysis = session.get(Analysis, analysis_id)
        assert analysis is not None and analysis.push_status == "sent"
    assert settings.get("push.provider") == "wxpusher"
