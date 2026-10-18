"""Minimal RSS/Atom client with conditional GET and a basic SSRF guard."""

from __future__ import annotations

import calendar
import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import feedparser
import httpx

from app.ingest.tianapi import FetchedNews


class RssError(RuntimeError):
    pass


@dataclass
class RssFetchResult:
    items: list[FetchedNews]
    cursor: dict


def validate_feed_url(url: str, *, allow_private: bool = False) -> None:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise RssError("RSS 地址必须是 http/https")
    host = parts.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        if not allow_private:
            raise RssError("默认禁止本机 RSS 地址")
        return
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise RssError(f"无法解析 RSS 域名: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ) and not allow_private:
            raise RssError("默认禁止内网/回环 RSS 地址")


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None) or entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc).replace(
                    tzinfo=None
                )
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def fetch_rss(
    *,
    url: str,
    cursor: dict | None,
    timeout: int = 15,
    max_items: int = 50,
    user_agent: str = "NewsRadar/1.0",
    allow_private: bool = False,
) -> RssFetchResult:
    validate_feed_url(url, allow_private=allow_private)
    headers = {"User-Agent": user_agent}
    if cursor:
        if cursor.get("etag"):
            headers["If-None-Match"] = str(cursor["etag"])
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = str(cursor["last_modified"])
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise RssError("RSS 请求超时") from exc
    except httpx.HTTPError as exc:
        raise RssError(f"RSS 网络错误: {exc}") from exc
    if response.status_code == 304:
        return RssFetchResult(items=[], cursor=dict(cursor or {}))
    if response.status_code >= 400:
        raise RssError(f"RSS 返回 HTTP {response.status_code}")
    parsed = feedparser.parse(response.content)
    feed_title = str(getattr(parsed.feed, "title", "") or "").strip()
    items: list[FetchedNews] = []
    for entry in parsed.entries[:max_items]:
        title = str(entry.get("title") or "").strip()
        link = str(entry.get("link") or "").strip()
        if not title or not link:
            continue
        external_id = str(entry.get("id") or link)
        description = str(entry.get("summary") or entry.get("description") or "")
        items.append(
            FetchedNews(
                external_id=external_id[:240],
                title=title,
                description=description,
                source_name=str(entry.get("author") or feed_title),
                published_at=_entry_time(entry),
                url=link,
            )
        )
    new_cursor = dict(cursor or {})
    if response.headers.get("etag"):
        new_cursor["etag"] = response.headers["etag"]
    if response.headers.get("last-modified"):
        new_cursor["last_modified"] = response.headers["last-modified"]
    if parsed.bozo and not items:
        raise RssError(f"RSS 解析失败: {parsed.bozo_exception}")
    return RssFetchResult(items=items, cursor=new_cursor)
