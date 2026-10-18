"""Normalisation helpers shared by TianAPI and RSS ingestion."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRACKING_EXACT = {"spm", "from", "ref", "referrer", "sourceid", "share_token", "scene"}
TRACKING_PREFIXES = ("utm_", "share_", "spm_", "from_")
PREFIX_RE = re.compile(r"^\s*(?:【[^】]{0,20}】|快讯|独家|重磅|突发|直播)[:：\s]*")
SPACE_RE = re.compile(r"\s+")
TITLE_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def clean_html(value: str | None, limit: int = 1000) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = SPACE_RE.sub(" ", text).strip()
    return text[:limit]


def canonicalize_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    query: list[tuple[str, str]] = []
    for key, val in parse_qsl(parts.query, keep_blank_values=False):
        lower = key.lower()
        if lower in TRACKING_EXACT or lower.startswith(TRACKING_PREFIXES):
            continue
        query.append((key, val))
    query.sort()
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path or "/",
            urlencode(query, doseq=True),
            "",
        )
    )


def normalize_title(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).strip()
    text = PREFIX_RE.sub("", text)
    text = TITLE_STRIP_RE.sub("", text).lower()
    return text[:300]


def content_hash(canonical_url: str, title_norm: str) -> str:
    payload = f"{canonical_url}\n{title_norm}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def parse_tianapi_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(text[: len(text)], fmt)
            break
        except ValueError:
            continue
    else:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        local = parsed
    if local.tzinfo is None:
        local = local.replace(tzinfo=SHANGHAI)
    return local.astimezone(timezone.utc).replace(tzinfo=None)
