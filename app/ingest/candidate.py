"""Local candidate scoring.  This runs before any model call and costs no tokens."""

from __future__ import annotations

from datetime import datetime

from app.db import utcnow


POSITIVE_SEED = """央行 人民银行 货币政策 降准 降息 加息 LPR MLF 逆回购 流动性 社融 信贷
财政 专项债 国债 化债 地方债 减税 补贴 以旧换新 设备更新
GDP CPI PPI PMI 进出口 外汇 人民币 汇率 失业率 就业 消费 投资
证监会 资本市场 IPO 减持 退市 印花税 交易制度 中长期资金 险资 养老金
房地产 楼市 限购 保交楼 银行 保险 券商 金融风险 违约
美联储 美元 美债 关税 制裁 出口管制 地缘 冲突 战争 原油 黄金 铜 稀土
半导体 芯片 新能源 光伏 锂电 汽车 医药 军工 人工智能 算力
政治局 国务院 国常会 中央经济工作会议 两会 三中全会 十五五 规划
A股 港股 中概股 北向资金 国家队 中央汇金""".split()

NEGATIVE_SEED = """涨停 跌停 龙虎榜 游资 打板 股吧 荐股 牛股 黑马 目标价 买卖点
个股 财报 业绩预告 股东减持 人事变动 绯闻 八卦 星座 彩票 体育 娱乐
广告 优惠 抽奖 直播带货 案件 判刑 车祸""".split()


def _hits(text: str, words: list[str]) -> list[str]:
    found: list[str] = []
    for word in words:
        if word and word in text and word not in found:
            found.append(word)
    return found


def score_text(
    title: str,
    description: str,
    source_tier: int,
    published_at: datetime | None,
    *,
    cluster_size: int = 1,
    positive: list[str] | None = None,
    negative: list[str] | None = None,
) -> tuple[int, list[str], list[str]]:
    positive = positive or POSITIVE_SEED
    negative = negative or NEGATIVE_SEED
    text = f"{title} {description}"
    pos_hits = _hits(text, positive)[:3]
    neg_hits = _hits(text, negative)
    score = len(pos_hits) * 12
    score += max(0, min(20, int(source_tier)))
    if published_at is not None:
        age = (utcnow() - published_at).total_seconds() / 3600
        if age <= 1:
            score += 20
        elif age <= 6:
            score += 15
        elif age <= 24:
            score += 8
        elif age <= 48:
            score += 3
    if cluster_size >= 5:
        score += 10
    elif cluster_size >= 3:
        score += 6
    elif cluster_size >= 2:
        score += 3
    title_len = len(title.strip())
    if 10 <= title_len <= 60:
        score += 5
    penalty = min(60, len(neg_hits) * 30)
    if source_tier >= 18:
        penalty //= 2
    score -= penalty
    # Without a positive macro keyword, source tier + recency alone would
    # queue every fresh item.  Keep the queue signal-driven.
    if not pos_hits:
        score -= 30
    return score, pos_hits, neg_hits
