"""趨勢判斷 — 多指標綜合評分，輸出 多頭 / 空頭 / 盤整。

做法 (綜合投票，降低單一指標雜訊):
  以下每個訊號各投 +1(偏多) / -1(偏空) / 0(中性)：
    1. 均線排列   MA5>MA20>MA60 多頭排列；反向空頭排列
    2. 價格位置   收盤 vs MA20
    3. 均線斜率   MA20 是否上彎 (近 slope_window 日線性迴歸斜率)
    4. MACD       DIF 與 0 軸 / 訊號線的相對位置
    5. DMI 方向   +DI vs -DI
  分數 = 五項加總 (範圍 -5..+5)。
  以 ADX 當「趨勢強度過濾」：ADX < adx_threshold 視為盤整 (不分多空)。
  否則 score >= +score_threshold → 多頭；<= -score_threshold → 空頭；其餘盤整。

⚠️ 技術分析是描述當前結構的機率性工具，會落後、盤整時易失準，非預測保證，亦非投資建議。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from ..domain import DailyQuote
from . import indicators as ind


class Trend(str, Enum):
    BULLISH = "多頭"
    BEARISH = "空頭"
    RANGING = "盤整"
    UNKNOWN = "資料不足"


@dataclass(slots=True)
class TrendResult:
    trend: Trend
    score: int
    votes: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    confidence: float = 0.0


class TrendClassifier:
    def __init__(self, adx_threshold: float = 20.0, score_threshold: int = 2,
                 slope_window: int = 20, min_bars: int = 60):
        self.adx_threshold = adx_threshold
        self.score_threshold = score_threshold
        self.slope_window = slope_window
        self.min_bars = min_bars

    def classify(self, quotes: Sequence[DailyQuote]) -> TrendResult:
        closes = [float(q.close) for q in quotes if q.close is not None]
        if len(closes) < self.min_bars:
            return TrendResult(Trend.UNKNOWN, 0,
                               details={"bars": len(closes), "need": self.min_bars})

        has_hl = all(q.high is not None and q.low is not None for q in quotes)
        highs = [float(q.high) for q in quotes] if has_hl else []
        lows = [float(q.low) for q in quotes] if has_hl else []

        ma5 = ind._last(ind.sma(closes, 5))
        ma20 = ind._last(ind.sma(closes, 20))
        ma60 = ind._last(ind.sma(closes, 60))
        ma20_prev = ind.sma(closes, 20)[-self.slope_window] if len(closes) > self.slope_window else None
        sl = ind.slope(closes, self.slope_window)
        dif_s, sig_s, _ = ind.macd(closes)
        dif, sig = ind._last(dif_s), ind._last(sig_s)
        adx_val, pdi, mdi = ind.adx(highs, lows, closes) if has_hl else (None, None, None)

        votes: dict[str, int] = {}

        # 1. 均線排列
        if None not in (ma5, ma20, ma60):
            votes["ma_align"] = 1 if ma5 > ma20 > ma60 else (-1 if ma5 < ma20 < ma60 else 0)

        # 2. 價格位置
        if ma20 is not None:
            votes["price_vs_ma20"] = 1 if closes[-1] > ma20 else -1

        # 3. 均線斜率 (MA20 上彎/下彎，輔以線性斜率)
        if ma20 is not None and ma20_prev is not None:
            votes["ma20_slope"] = 1 if ma20 > ma20_prev else (-1 if ma20 < ma20_prev else 0)
        elif sl is not None:
            votes["ma20_slope"] = 1 if sl > 0 else (-1 if sl < 0 else 0)

        # 4. MACD
        if dif is not None and sig is not None:
            if dif > 0 and dif >= sig:
                votes["macd"] = 1
            elif dif < 0 and dif <= sig:
                votes["macd"] = -1
            else:
                votes["macd"] = 0

        # 5. DMI 方向
        if pdi is not None and mdi is not None:
            votes["dmi"] = 1 if pdi > mdi else (-1 if mdi > pdi else 0)

        score = sum(votes.values())

        # 趨勢強度過濾
        if adx_val is not None and adx_val < self.adx_threshold:
            trend = Trend.RANGING
        elif score >= self.score_threshold:
            trend = Trend.BULLISH
        elif score <= -self.score_threshold:
            trend = Trend.BEARISH
        else:
            trend = Trend.RANGING

        n = max(1, len(votes))
        strength = abs(score) / n
        if adx_val is not None:
            strength *= min(adx_val / 40.0, 1.0)
        confidence = round(min(strength, 1.0), 2)

        details = {
            "ma5": _r(ma5), "ma20": _r(ma20), "ma60": _r(ma60),
            "slope": _r(sl, 4), "macd_dif": _r(dif, 4), "macd_signal": _r(sig, 4),
            "adx": _r(adx_val), "plus_di": _r(pdi), "minus_di": _r(mdi),
            "bars": len(closes),
        }
        return TrendResult(trend, score, votes, details, confidence)


def _r(v, nd: int = 2):
    return round(v, nd) if v is not None else None
