"""Validate and normalise the model's JSON output."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


DIRECTIONS = {"bullish", "bearish", "neutral", "uncertain"}
HORIZONS = {"intraday", "days", "weeks", "months"}
SECTORS = {
    "银行",
    "非银金融",
    "房地产",
    "建筑建材",
    "有色金属",
    "钢铁",
    "煤炭",
    "石油石化",
    "电力设备",
    "新能源",
    "汽车",
    "机械设备",
    "国防军工",
    "电子",
    "半导体",
    "计算机",
    "通信",
    "传媒",
    "食品饮料",
    "家电",
    "医药生物",
    "农业",
    "交通运输",
    "商贸零售",
    "公用事业",
    "环保",
}
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class AnalysisItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    news_id: str
    relevance: int = Field(default=0, ge=0, le=100)
    materiality: int = Field(default=0, ge=0, le=100)
    urgency: int = Field(default=0, ge=0, le=100)
    confidence: int = Field(default=0, ge=0, le=100)
    direction: str = "uncertain"
    horizon: str = "days"
    summary: str = ""
    transmission: str = ""
    sectors: list[str] = Field(default_factory=list)
    watch: str = ""
    caveat: str = ""


def push_score(item: AnalysisItem) -> int:
    return round(0.55 * item.materiality + 0.25 * item.urgency + 0.20 * item.confidence)


def is_push_eligible(item: AnalysisItem, min_relevance: int) -> bool:
    """The LLM's 0-100 relevance score is the only semantic push gate.

    The threshold is the same admin field previously labelled confidence;
    it now filters relevance, not certainty. Confidence remains in the
    analysis for display, but does not decide whether a story is on-topic.
    """
    return item.relevance >= min_relevance


def _clamp(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean(raw: dict[str, Any], expected: set[str]) -> AnalysisItem | None:
    news_id = str(raw.get("news_id") or "").strip()
    if news_id not in expected:
        return None
    direction = str(raw.get("direction") or "").lower()
    horizon = str(raw.get("horizon") or "").lower()
    sectors: list[str] = []
    raw_sectors = raw.get("sectors")
    if isinstance(raw_sectors, list):
        for sector in raw_sectors:
            name = str(sector).strip()
            if name in SECTORS and name not in sectors:
                sectors.append(name)
            if len(sectors) >= 3:
                break
    return AnalysisItem(
        news_id=news_id,
        relevance=_clamp(raw.get("relevance")),
        materiality=_clamp(raw.get("materiality")),
        urgency=_clamp(raw.get("urgency")),
        confidence=_clamp(raw.get("confidence")),
        direction=direction if direction in DIRECTIONS else "uncertain",
        horizon=horizon if horizon in HORIZONS else "days",
        summary=_text(raw.get("summary"), 80),
        transmission=_text(raw.get("transmission"), 120),
        sectors=sectors,
        watch=_text(raw.get("watch"), 60),
        caveat=_text(raw.get("caveat"), 60),
    )


def parse_items(content: str, expected_ids: set[str]) -> list[AnalysisItem]:
    text = FENCE_RE.sub("", content.strip())
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("模型输出不是合法 JSON") from exc
    raw_items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raise ValueError("模型输出缺少 items 数组")
    result: list[AnalysisItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _clean(raw, expected_ids)
        if item is None or item.news_id in seen:
            continue
        seen.add(item.news_id)
        result.append(item)
    return result
