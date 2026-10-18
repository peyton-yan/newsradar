"""TianAPI news client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.ingest.normalize import parse_tianapi_time


class TianApiError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class FetchedNews:
    external_id: str
    title: str
    description: str
    source_name: str
    published_at: datetime | None
    url: str


def _as_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result") or {}
    if isinstance(result, dict):
        value = result.get("list") or result.get("newslist") or []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def fetch_tianapi(
    *,
    base_url: str,
    path: str,
    api_key: str,
    num: int = 50,
    col: int | None = None,
    timeout: int = 15,
) -> list[FetchedNews]:
    if not api_key:
        raise TianApiError("天行 API Key 未配置", code=None)
    params: dict[str, Any] = {
        "key": api_key,
        "num": min(max(int(num), 1), 50),
        "form": 1,
        "page": 1,
    }
    if col is not None:
        params["col"] = int(col)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(f"{base_url.rstrip('/')}{path}", params=params)
    except httpx.TimeoutException as exc:
        raise TianApiError("天行请求超时") from exc
    except httpx.HTTPError as exc:
        raise TianApiError(f"天行网络错误: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise TianApiError(f"天行返回非 JSON（HTTP {response.status_code}）") from exc
    code = payload.get("code")
    if code == 250:
        return []
    if code != 200:
        raise TianApiError(str(payload.get("msg") or f"天行返回 {code}"), code=code)
    items: list[FetchedNews] = []
    for row in _as_list(payload):
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        external_id = str(row.get("id") or "").strip()
        if not external_id:
            external_id = str(row.get("url") or title)[:240]
        items.append(
            FetchedNews(
                external_id=external_id,
                title=title,
                description=str(row.get("description") or ""),
                source_name=str(row.get("source") or ""),
                published_at=parse_tianapi_time(row.get("ctime")),
                url=str(row.get("url") or ""),
            )
        )
    return items
