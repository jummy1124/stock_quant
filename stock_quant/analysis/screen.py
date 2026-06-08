"""選股篩選器 — 用 4 條規則篩出符合的個股 (取代原本的趨勢分類)。

規則 (對「當日K」與「前一交易日」):
  1. 紅K            : 收盤 > 開盤
  2. 漲幅 3%~漲停前一檔: (收-昨收)/昨收 ≥ 3%，且 收盤 ≤ 漲停前一檔 (自然排除鎖漲停)
  3. 上影線 ≤ 1%    : (最高 - max(開,收)) / 收盤 ≤ 1%
  4. 量增 1.2 倍     : 今日量 ≥ 1.2 × 昨日量

⚠️ 量必須同單位 (本專案統一為「股」)。技術面選股為機率性參考，非投資建議。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..domain import DailyQuote

# 台股普通股升降單位 (價格帶上限, tick)
_TICKS = [(10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)]


def tick_size(price: float) -> float:
    for hi, t in _TICKS:
        if price < hi:
            return t
    return 5.0


def limit_up_price(prev_close: float) -> float:
    """漲停價 = 昨收×1.1，無條件捨去到升降單位 (不超過 10%)。"""
    raw = prev_close * 1.1
    t = tick_size(raw)
    return round(int(raw / t + 1e-9) * t, 2)


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


@dataclass(slots=True)
class ScreenResult:
    passed: bool
    is_red: bool = False
    change_pct: Optional[float] = None       # 漲幅 %
    upper_shadow_pct: Optional[float] = None  # 上影線 %
    vol_ratio: Optional[float] = None         # 今日量 / 昨日量
    close: Optional[float] = None
    note: str = ""


class BreakoutScreen:
    def __init__(self, min_change_pct: float = 3.0, max_upper_shadow_pct: float = 1.0,
                 min_vol_ratio: float = 1.2):
        self.min_change_pct = min_change_pct
        self.max_upper_shadow_pct = max_upper_shadow_pct
        self.min_vol_ratio = min_vol_ratio

    def check(self, today: DailyQuote, prev: DailyQuote) -> ScreenResult:
        o, h, c = _f(today.open), _f(today.high), _f(today.close)
        v = today.volume
        pc = _f(prev.close)
        pv = prev.volume
        if None in (o, h, c, pc) or v is None or not pv:
            return ScreenResult(False, note="缺 OHLCV 或前一日資料")

        is_red = c > o                                          # 規則1
        change_pct = (c - pc) / pc * 100.0
        lu = limit_up_price(pc)
        lu_prev = round(lu - tick_size(lu), 2)                  # 漲停前一檔
        upper_pct = (h - max(o, c)) / c * 100.0 if c else None  # 規則3 基準=收盤
        vol_ratio = v / pv

        r2 = (change_pct >= self.min_change_pct) and (c <= lu_prev + 1e-9)   # 規則2
        r3 = upper_pct is not None and upper_pct <= self.max_upper_shadow_pct
        r4 = vol_ratio >= self.min_vol_ratio                                  # 規則4
        passed = is_red and r2 and r3 and r4
        return ScreenResult(passed, is_red, round(change_pct, 2),
                            round(upper_pct, 2) if upper_pct is not None else None,
                            round(vol_ratio, 2), c)
