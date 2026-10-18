"""DeepSeek V4.1 Flash pricing in RMB.

Official CNY prices per 1M tokens
(https://api-docs.deepseek.com/zh-cn/quick_start/pricing):
  input cache hit:  off-peak ¥0.02 / peak ¥0.04
  input cache miss: off-peak ¥1.00 / peak ¥2.00
  output:           off-peak ¥4.00 / peak ¥8.00

Peak: Beijing time 09:00-12:00 and 14:00-18:00, Monday-Friday.
Chinese public holidays are not in the standard library; treating them as
peak is conservative (it can stop earlier, never overspend).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


MODEL_FLASH = "deepseek-flash"
MODEL_PRO = "deepseek-v4-pro"
BEIJING = ZoneInfo("Asia/Shanghai")

PRICES_CNY_PER_MILLION = {
    "input_cache_hit": {"offpeak": 0.02, "peak": 0.04},
    "input_cache_miss": {"offpeak": 1.00, "peak": 2.00},
    "output": {"offpeak": 4.00, "peak": 8.00},
}

MODEL_LABELS = {
    MODEL_FLASH: "DeepSeek V4.1 Flash",
    MODEL_PRO: "DeepSeek V4 Pro",
}


def is_peak(now_utc: datetime | None = None) -> bool:
    value = now_utc or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(BEIJING)
    if local.weekday() > 4:
        return False
    hour = local.hour
    return 9 <= hour < 12 or 14 <= hour < 18


def compute_cost_cny(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
    peak: bool = False,
) -> float:
    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    cache_hit_tokens = max(0, int(cache_hit_tokens))
    cache_miss_tokens = max(0, int(cache_miss_tokens))
    if cache_hit_tokens + cache_miss_tokens == 0:
        cache_miss_tokens = input_tokens
    period = "peak" if peak else "offpeak"
    return (
        cache_hit_tokens / 1_000_000 * PRICES_CNY_PER_MILLION["input_cache_hit"][period]
        + cache_miss_tokens / 1_000_000 * PRICES_CNY_PER_MILLION["input_cache_miss"][period]
        + output_tokens / 1_000_000 * PRICES_CNY_PER_MILLION["output"][period]
    )


def compute_flat_cost_cny(
    *,
    input_tokens: int,
    output_tokens: int,
    input_price_cny: float,
    output_price_cny: float,
) -> float:
    """For non-DeepSeek providers with a flat per-million-token price."""
    return (
        max(0, int(input_tokens)) / 1_000_000 * max(0.0, float(input_price_cny))
        + max(0, int(output_tokens)) / 1_000_000 * max(0.0, float(output_price_cny))
    )


def format_cny(value: float) -> str:
    return f"¥{max(0.0, value):.4f}"
