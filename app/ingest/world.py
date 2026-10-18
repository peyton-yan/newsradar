"""International news API adapters (NewsAPI, GNews, Finnhub)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.ingest.tianapi import FetchedNews


class WorldNewsError(RuntimeError):
    pass


def _iso_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _get(url: str, params: dict[str, Any], timeout: int) -> Any:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise WorldNewsError("新闻 API 请求超时") from exc
    except httpx.HTTPError as exc:
        raise WorldNewsError(f"新闻 API 网络错误: {exc}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise WorldNewsError(f"新闻 API HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WorldNewsError("新闻 API 返回非 JSON") from exc
    if response.status_code >= 400:
        message = payload.get("message") or payload.get("errors") or response.status_code
        raise WorldNewsError(f"新闻 API 拒绝: {message}")
    return payload


def fetch_newsapi(*, api_key: str, category: str, language: str, timeout: int) -> list[FetchedNews]:
    if not api_key:
        raise WorldNewsError("NewsAPI Key 未配置")
    payload = _get(
        "https://newsapi.org/v2/top-headlines",
        {"apiKey": api_key, "category": category, "language": language, "pageSize": 50},
        timeout,
    )
    items: list[FetchedNews] = []
    for row in payload.get("articles") or []:
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url:
            continue
        items.append(
            FetchedNews(
                external_id=url,
                title=title,
                description=str(row.get("description") or ""),
                source_name=str((row.get("source") or {}).get("name") or "NewsAPI"),
                published_at=_iso_time(row.get("publishedAt")),
                url=url,
            )
        )
    return items


def fetch_gnews(*, api_key: str, category: str, language: str, timeout: int) -> list[FetchedNews]:
    if not api_key:
        raise WorldNewsError("GNews Key 未配置")
    payload = _get(
        "https://gnews.io/api/v4/top-headlines",
        {"apikey": api_key, "category": category, "lang": language, "max": 50},
        timeout,
    )
    items: list[FetchedNews] = []
    for row in payload.get("articles") or []:
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url:
            continue
        items.append(
            FetchedNews(
                external_id=url,
                title=title,
                description=str(row.get("description") or ""),
                source_name=str((row.get("source") or {}).get("name") or "GNews"),
                published_at=_iso_time(row.get("publishedAt")),
                url=url,
            )
        )
    return items


def fetch_finnhub(*, api_key: str, category: str, timeout: int) -> list[FetchedNews]:
    if not api_key:
        raise WorldNewsError("Finnhub Key 未配置")
    payload = _get(
        "https://finnhub.io/api/v1/news",
        {"token": api_key, "category": category},
        timeout,
    )
    items: list[FetchedNews] = []
    for row in payload if isinstance(payload, list) else []:
        title = str(row.get("headline") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url:
            continue
        timestamp = row.get("datetime")
        published = (
            datetime.fromtimestamp(int(timestamp), tz=timezone.utc).replace(tzinfo=None)
            if timestamp
            else None
        )
        items.append(
            FetchedNews(
                external_id=str(row.get("id") or url),
                title=title,
                description=str(row.get("summary") or ""),
                source_name=str(row.get("source") or "Finnhub"),
                published_at=published,
                url=url,
            )
        )
    return items
