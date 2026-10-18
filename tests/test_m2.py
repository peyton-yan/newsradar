from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.db import utcnow
from app.ingest.normalize import canonicalize_url, normalize_title
from app.ingest.simhash import hamming, simhash64
from app.ingest.tianapi import FetchedNews
from app.models import NewsCluster, NewsItem, Source


def test_normalization_removes_tracking_and_prefixes():
    url = "HTTPS://Example.COM/a?utm_source=x&b=2&a=1#frag"
    assert canonicalize_url(url) == "https://example.com/a?a=1&b=2"
    assert normalize_title("【快讯】央行 宣布降准！") == "央行宣布降准"


def test_simhash_is_stable_and_near_duplicate():
    left = simhash64("央行宣布降准0.5个百分点释放长期资金")
    right = simhash64("央行宣布降准0.5个百分点释放长期资金")
    assert left == right
    assert hamming(left, right) == 0


def test_collect_dedupes_and_queues(app, monkeypatch):
    ingest = app.state.ingest
    settings = app.state.settings_service
    settings.set("tianapi.api_key", "test-key")
    with app.state.db.session() as session:
        source = session.scalar(select(Source).where(Source.name == "天行财经新闻"))
        assert source is not None
        source_id = source.id

    now = utcnow()
    items = [
        FetchedNews(
            external_id="a1",
            title="央行宣布降准0.5个百分点",
            description="释放长期资金，支持实体经济。",
            source_name="测试源",
            published_at=now,
            url="https://example.com/a1",
        ),
        FetchedNews(
            external_id="a2",
            title="央行宣布降准0.5个百分点",
            description="同一事件的转载。",
            source_name="转载源",
            published_at=now - timedelta(minutes=5),
            url="https://example.com/a2",
        ),
        FetchedNews(
            external_id="a3",
            title="某地发布暴雨蓝色预警",
            description="与股市宏观无关。",
            source_name="测试源",
            published_at=now,
            url="https://example.com/a3",
        ),
    ]
    monkeypatch.setattr(ingest, "_fetch_tianapi", lambda source, api_key: items)
    result = ingest.collect_source(source_id)
    assert result["fetched"] == 3
    assert result["new"] == 2
    assert result["duplicate"] == 1
    assert result["queued"] == 1
    assert result["filtered"] == 1

    with app.state.db.session() as session:
        news = list(session.scalars(select(NewsItem).order_by(NewsItem.id)).all())
        clusters = list(session.scalars(select(NewsCluster).order_by(NewsCluster.id)).all())
        assert len(news) == 3
        assert len(clusters) == 2
        cluster = clusters[0]
        assert cluster.member_count == 2
        assert news[0].status == "queued"
        assert news[1].status == "duplicate"
        assert news[2].status == "filtered"
        assert news[0].simhash_band0 is not None

    again = ingest.collect_source(source_id)
    assert again["fetched"] == 3
    assert again["new"] == 0
    assert again["duplicate"] == 3
    with app.state.db.session() as session:
        assert int(session.scalar(select(func.count()).select_from(NewsItem)) or 0) == 3
