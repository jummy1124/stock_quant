"""選股篩選器 — 用 6 條規則篩出符合的個股。

對「當日K(today)」與「歷史日K(history，時間升冪、結束於前一交易日)」檢查:
  1. 紅K            : 收盤 > 開盤
  2. 漲幅 3%~漲停前一檔: (收-昨收)/昨收 ≥ 3%，且 收盤 ≤ 漲停前一檔
  3. 上影線 ≤ 1%    : (最高 - max(開,收)) / 收盤 ≤ 1%
  4. 量增 1.2 倍     : 今日量 ≥ 1.2 × 昨日量 (盤中依已過時段比例換算為「同時段」公平比較)
  5. 前一日收盤 < MA5: 昨收 < 最近五日(含昨日)收盤均線
  6. 今日現價 > 昨日最高: 今日收盤(即時現價) > 昨日最高價

⚠️ 量需同單位 (本專案統一為「股」)。技術面選股為機率性參考，非投資建議。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

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
    change_pct: Optional[float] = None        # 漲幅 %
    upper_shadow_pct: Optional[float] = None   # 上影線 %
    vol_ratio: Optional[float] = None          # 今日量 / 昨日量 (原始，未換算時段)
    vol_pace_ratio: Optional[float] = None     # 量比 / 已過時段比例 (盤中同時段公平比較；rule 4 用此值)
    close: Optional[float] = None
    ma5: Optional[float] = None                # 前一日 MA5
    prev_high: Optional[float] = None          # 昨日最高
    note: str = ""


class BreakoutScreen:
    def __init__(self, min_change_pct: float = 3.0, max_upper_shadow_pct: float = 1.0,
                 min_vol_ratio: float = 1.2, ma_period: int = 5):
        self.min_change_pct = min_change_pct
        self.max_upper_shadow_pct = max_upper_shadow_pct
        self.min_vol_ratio = min_vol_ratio
        self.ma_period = ma_period

    def check(self, today: DailyQuote, history: Sequence[DailyQuote],
              session_fraction: float = 1.0) -> ScreenResult:
        """today: 當日K(可為即時)；history: 升冪歷史日K，結束於前一交易日。

        session_fraction: 今日已過的盤中時段比例 (0<f≤1)。盤中用即時資料時，今日量是
        「到目前為止的累積量」，拿去比昨日整日量並不公平 (早盤天生偏低)。改成把昨日量
        依已過時段比例縮放 -> 等於比較「同時段成交速度」。1.0 = 收盤後/完整日K，行為不變。
        """
        if not history:
            return ScreenResult(False, note="無歷史")
        prev = history[-1]
        o, h, c = _f(today.open), _f(today.high), _f(today.close)
        v = today.volume
        pc, ph, pv = _f(prev.close), _f(prev.high), prev.volume
        if None in (o, h, c, pc, ph) or v is None or not pv:
            return ScreenResult(False, note="缺 OHLCV 或前一日資料")

        closes = [_f(x.close) for x in history[-self.ma_period:]]
        if len(closes) < self.ma_period or any(x is None for x in closes):
            return ScreenResult(False, note=f"歷史不足 {self.ma_period} 日 (無法算 MA{self.ma_period})")
        ma5 = sum(closes) / self.ma_period            # 前一日 MA5 (含昨日的最近五日收盤均)

        is_red = c > o                                          # 規則1: 紅K — 今日收盤 > 開盤
        change_pct = (c - pc) / pc * 100.0                      # 漲幅% = (今收 - 昨收) / 昨收 ×100
        lu = limit_up_price(pc)                                 # 漲停價 (昨收×1.1，取台股升降單位)
        lu_prev = round(lu - tick_size(lu), 2)                  # 漲停前一檔 = 漲停價往下一個升降單位
        upper_pct = (h - max(o, c)) / c * 100.0 if c else None  # 上影線% = (最高 - max(開,收)) / 收盤 ×100
        vol_ratio = v / pv                                      # 量比 = 今日量 / 昨日量 (原始)
        frac = min(max(session_fraction, 1e-6), 1.0)            # 已過時段比例 (夾在 (0,1])
        vol_pace_ratio = vol_ratio / frac                       # 同時段量比 = 今日累積量 / (昨日量×已過比例)

        # 規則2: 漲幅 ≥ 3% 且 收盤 ≤ 漲停前一檔 (有力道但不追在鎖漲停)
        r2 = (change_pct >= self.min_change_pct) and (c <= lu_prev + 1e-9)
        # 規則3: 上影線 ≤ 1% (上影線短代表買盤積極、收在高檔)
        r3 = upper_pct is not None and upper_pct <= self.max_upper_shadow_pct
        # 規則4: 同時段量增 (放量) — 今日成交速度 ≥ 1.2 × 昨日同時段
        r4 = vol_pace_ratio >= self.min_vol_ratio
        # 規則5: 前一日收盤 < 五日均線 (昨天仍在均線之下，今天才剛轉強)
        r5 = pc < ma5
        # 規則6: 今日現價(收盤) > 昨日最高價 (向上突破昨日高點)
        r6 = c > ph
        # 六條全數成立才入選
        passed = is_red and r2 and r3 and r4 and r5 and r6
        return ScreenResult(passed, is_red, round(change_pct, 2),
                            round(upper_pct, 2) if upper_pct is not None else None,
                            round(vol_ratio, 2), round(vol_pace_ratio, 2),
                            c, round(ma5, 2), ph)
