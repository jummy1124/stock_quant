"""選股篩選器 — 用 7 條規則篩出符合的個股 (6 條突破型態 + 1 道多頭趨勢閘門)。

對「當日K(today)」與「歷史日K(history，時間升冪、結束於前一交易日)」檢查:
  1. 紅K            : 收盤 > 開盤
  2. 漲幅 3%~漲停前一檔: (收-昨收)/昨收 ≥ 3%，且 收盤 ≤ 漲停前一檔
  3. 上影線 ≤ 1%    : (最高 - max(開,收)) / 收盤 ≤ 1%
  4. 量增 1.2 倍     : 今日量 ≥ 1.2 × 昨日量 (盤中依已過時段比例換算為「同時段」公平比較)
  5. 前一日收盤 < MA5: 昨收 < 最近五日(含昨日)收盤均線 (短線回檔)
  6. 今日現價 > 昨日最高: 今日收盤(即時現價) > 昨日最高價 (突破昨高)
  7. 多頭趨勢 (趨勢閘門): 站上月線 (今價 > MA20) 且 月線 ≥ 季線 (MA20 ≥ MA60)
     且 頭頭高、底底高 (近期擺動高點/低點皆墊高)。用來擋掉空頭股的單日反彈。

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


def _find_swings(highs: Sequence[Optional[float]], lows: Sequence[Optional[float]],
                 k: int) -> tuple[list[float], list[float]]:
    """用 k 根碎形 (fractal) 找出已確認的擺動高點/低點，依時間先後回傳。

    擺動高點 = 該根最高價嚴格高於左右各 k 根；擺動低點 = 最低價嚴格低於左右各 k 根。
    最後 k 根因未來資料不足，無法確認 (這是刻意的，避免用未完成的轉折誤判)。
    """
    n = len(highs)
    sh: list[float] = []
    sl: list[float] = []
    for i in range(k, n - k):
        hi = highs[i]
        if hi is not None:
            nb = [highs[j] for j in range(i - k, i + k + 1) if j != i]
            if all(x is not None and hi > x for x in nb):
                sh.append(hi)
        lo = lows[i]
        if lo is not None:
            nb = [lows[j] for j in range(i - k, i + k + 1) if j != i]
            if all(x is not None and lo < x for x in nb):
                sl.append(lo)
    return sh, sl


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
    ma20: Optional[float] = None               # 月線 (趨勢閘門用)
    ma60: Optional[float] = None               # 季線 (趨勢閘門用)
    uptrend: Optional[bool] = None             # 是否多頭 (require_uptrend 關閉時為 None)
    note: str = ""


class BreakoutScreen:
    def __init__(self, min_change_pct: float = 3.0, max_upper_shadow_pct: float = 1.0,
                 min_vol_ratio: float = 1.2, ma_period: int = 5,
                 require_uptrend: bool = True, ma_month_period: int = 20,
                 ma_quarter_period: int = 60, swing_window: int = 2):
        self.min_change_pct = min_change_pct
        self.max_upper_shadow_pct = max_upper_shadow_pct
        self.min_vol_ratio = min_vol_ratio
        self.ma_period = ma_period
        self.require_uptrend = require_uptrend     # 是否啟用多頭趨勢閘門 (rule 7)
        self.ma_month_period = ma_month_period      # 月線天數 (站上月線)
        self.ma_quarter_period = ma_quarter_period  # 季線天數 (月線≥季線)
        self.swing_window = swing_window            # 碎形視窗 k (頭頭高底底高)

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

        # 規則7: 多頭趨勢閘門 — 站上月線 + 月線≥季線 + 頭頭高底底高 (擋空頭單日反彈)
        ma20 = ma60 = None
        uptrend: Optional[bool] = None
        if self.require_uptrend:
            allc = [_f(x.close) for x in history]
            if len(allc) < self.ma_quarter_period or any(x is None for x in allc[-self.ma_quarter_period:]):
                return ScreenResult(False, ma5=round(ma5, 2),
                                    note=f"歷史不足 {self.ma_quarter_period} 日 (無法判多頭趨勢)")
            ma20 = sum(allc[-self.ma_month_period:]) / self.ma_month_period
            ma60 = sum(allc[-self.ma_quarter_period:]) / self.ma_quarter_period
            sh, sl = _find_swings([_f(x.high) for x in history],
                                  [_f(x.low) for x in history], self.swing_window)
            higher_high = len(sh) >= 2 and sh[-1] > sh[-2]      # 頭頭高: 末兩個擺動高點墊高
            higher_low = len(sl) >= 2 and sl[-1] > sl[-2]       # 底底高: 末兩個擺動低點墊高
            above_month = c > ma20                              # 站上月線 (今價站上 MA20)
            ma_stack = ma20 >= ma60                             # 月線 ≥ 季線 (中長多頭)
            uptrend = bool(above_month and ma_stack and higher_high and higher_low)
        r7 = uptrend if self.require_uptrend else True

        # 七條全數成立才入選 (require_uptrend 關閉時 r7 恆為 True，等同原 6 條)
        passed = is_red and r2 and r3 and r4 and r5 and r6 and r7
        return ScreenResult(
            passed, is_red, round(change_pct, 2),
            round(upper_pct, 2) if upper_pct is not None else None,
            round(vol_ratio, 2), round(vol_pace_ratio, 2),
            c, round(ma5, 2), ph,
            round(ma20, 2) if ma20 is not None else None,
            round(ma60, 2) if ma60 is not None else None,
            uptrend,
        )
