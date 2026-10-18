from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.analysis.billing import compute_cost_cny, is_peak
from app.analysis.schema import AnalysisItem, is_push_eligible, parse_items, push_score
from app.db import utcnow
from app.models import Analysis, DailyUsage, NewsCluster, NewsItem, Source


def test_flash_price_peak_and_offpeak():
    assert is_peak(datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc))  # Monday 02:00 UTC
    assert not is_peak(datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc))  # Sunday
    offpeak = compute_cost_cny(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_miss_tokens=1_000_000,
        peak=False,
    )
    assert abs(offpeak - (1.0 + 4.0)) < 1e-9
    peak = compute_cost_cny(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_miss_tokens=1_000_000,
        peak=True,
    )
    assert abs(peak - (2.0 + 8.0)) < 1e-9


def test_parse_items_clamps_and_filters():
    content = """```json
    {"items":[
      {"news_id":"1","macro_relevant":true,"materiality":120,"urgency":-2,"confidence":80,
       "direction":"bullish","horizon":"days","summary":"摘要","transmission":"传导",
       "sectors":["银行","银行","不存在"],"watch":"观察","caveat":"风险"},
      {"news_id":"99","macro_relevant":true}
    ]}
    ```"""
    items = parse_items(content, {"1"})
    assert len(items) == 1
    item = items[0]
    assert item.materiality == 100
    assert item.urgency == 0
    assert item.sectors == ["银行"]
    assert push_score(item) == round(0.55 * 100 + 0.25 * 0 + 0.20 * 80)


def test_macro_relevance_is_required_before_confidence():
    irrelevant = AnalysisItem(news_id="1", macro_relevant=False, confidence=99)
    assert is_push_eligible(irrelevant, 60) is False

    uncertain = AnalysisItem(news_id="2", macro_relevant=True, confidence=59)
    assert is_push_eligible(uncertain, 60) is False

    relevant = AnalysisItem(news_id="3", macro_relevant=True, confidence=60)
    assert is_push_eligible(relevant, 60) is True

    string_false = parse_items(
        '{"items":[{"news_id":"4","macro_relevant":"false","confidence":99}]}', {"4"}
    )
    assert string_false[0].macro_relevant is False


def test_analysis_saves_results_and_respects_cost_cap(app, monkeypatch):
    settings = app.state.settings_service
    settings.update_group(
        "analysis",
        {
            "api_key": "test-key",
            "model": "deepseek-flash",
            "min_interval_minutes": 0,
            "batch_size": 2,
            "max_candidates_per_run": 5,
            "min_candidates": 1,
            "daily_cost_limit_cny": 10.0,
            "max_runs_per_day": 8,
        },
    )
    now = utcnow()
    with app.state.db.session() as session:
        source = session.scalar(select(Source).order_by(Source.id))
        assert source is not None
        cluster = NewsCluster(
            representative_news_id=None,
            title="央行降准",
            first_seen_at=now,
            last_seen_at=now,
            member_count=1,
        )
        session.add(cluster)
        session.flush()
        item = NewsItem(
            source_id=source.id,
            external_id="m3-1",
            cluster_id=cluster.id,
            url="https://example.com/m3-1",
            canonical_url="https://example.com/m3-1",
            title="央行宣布降准",
            description="释放流动性",
            source_name="测试",
            published_at=now,
            fetched_at=now,
            title_norm="央行宣布降准",
            content_hash="x",
            heuristic_score=80,
            status="queued",
        )
        session.add(item)
        session.flush()
        cluster.representative_news_id = item.id
        item_id = item.id

    def fake_chat(**_kwargs):
        return (
            json.dumps(
                {
                    "items": [
                        {
                            "news_id": str(item_id),
                            "macro_relevant": True,
                            "materiality": 80,
                            "urgency": 80,
                            "confidence": 80,
                            "direction": "bullish",
                            "horizon": "days",
                            "summary": "央行降准。",
                            "transmission": "可能改善流动性。",
                            "sectors": ["银行"],
                            "watch": "关注后续。",
                            "caveat": "原文未披露。",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 1000,
            },
        )

    monkeypatch.setattr("app.analysis.service.chat_completion", fake_chat)
    result = app.state.analysis.analyze_due()
    assert result["ok"] is True
    assert result["items"] == 1
    assert result["cost_cny"] > 0
    with app.state.db.session() as session:
        analysis = session.scalar(select(Analysis))
        assert analysis is not None
        assert analysis.push_score == 80
        assert analysis.push_eligible is True
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        row = session.get(DailyUsage, day)
        assert row is not None
        assert row.analysis_runs == 1
        assert row.cost_cny > 0


def test_cost_alert_is_sent_once(app, monkeypatch):
    settings = app.state.settings_service
    settings.update_group("analysis", {"daily_cost_limit_cny": 0.000001})
    with app.state.db.session() as session:
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        row = session.get(DailyUsage, day)
        if row is None:
            row = DailyUsage(day=day)
            session.add(row)
        row.cost_cny = 1.0
        row.cost_usd = 0.14
    sent: list[str] = []
    monkeypatch.setattr(
        "app.analysis.service.send_system_alert",
        lambda settings, title, content: sent.append(title) or True,
    )
    assert app.state.analysis.ensure_cost_alert() is True
    assert app.state.analysis.ensure_cost_alert() is False
    assert len(sent) == 1
    ok, reason = app.state.analysis.should_run()
    assert ok is False
    assert reason == "cost_limit"
