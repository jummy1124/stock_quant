"""均值回歸選股器 — 高勝率版 (與 BreakoutScreen 的「追突破」相反)。

邏輯：在『中期多頭』的股票上，等它『短線超賣回檔』時買進，反彈回 5 日線就快速獲利了結。
多數回檔會在趨勢中被買盤承接 -> 勝率高、單筆獲利小；少數趨勢真的轉壞 -> 用停損認賠 (少而較大)。

進場條件 (對「當日收盤完成K」與歷史日K檢查，全部成立才入選)：
  1. 中期多頭：MA20 ≥ MA60 (月線 ≥ 季線)
  2. 站穩中期：收盤 > MA60 (在季線之上，確保是上升趨勢中的回檔，而非空頭下跌)
  3. 淺回檔  ：收盤 ≥ MA20 (拉回但沒跌破月線，不是破線崩跌)
  4. 短線拉回：收盤 < MA5 (已回到 5 日線之下)
  5. 短線超賣：RSI(rsi_period) ≤ rsi_threshold (跌深、超賣，反彈機率高)

出場由回測引擎以 exit_mode="revert" 處理：回 5 日線之上獲利了結 / 觸停損 / 時間到。

⚠️ 技術面選股為機率性參考，非投資建議。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ..domain import DailyQuote


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def rsi(closes: Sequence[float], period: int) -> Optional[float]:
    """Wilder 簡化版 RSI：需 period+1 筆收盤。全程上漲回 100，全程下跌回 0。"""
    if len(closes) < period + 1:
        return None
    gain = loss = 0.0
    for a, b in zip(closes[-period - 1:-1], closes[-period:]):
        d = b - a
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    if loss == 0:
        return 100.0
    rs = (gain / period) / (loss / period)
    return 100.0 - 100.0 / (1.0 + rs)


@dataclass(slots=True)
class PullbackResult:
    passed: bool
    # 與 ScreenResult 對齊的欄位 (讓回測引擎排序/報表共用)
    change_pct: Optional[float] = None
    upper_shadow_pct: Optional[float] = None
    vol_ratio: Optional[float] = None
    close: Optional[float] = None
    ma5: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    rsi: Optional[float] = None              # 排序用 (oversold: 越低越優先)
    note: str = ""


class PullbackScreen:
    def __init__(self, rsi_period: int = 3, rsi_threshold: float = 15.0,
                 ma_short: int = 5, ma_month: int = 20, ma_quarter: int = 60,
                 require_above_ma20: bool = True):
        self.rsi_period = rsi_period
        self.rsi_threshold = rsi_threshold
        self.ma_short = ma_short
        self.ma_month = ma_month
        self.ma_quarter = ma_quarter
        self.require_above_ma20 = require_above_ma20

    def check(self, today: DailyQuote, history: Sequence[DailyQuote],
              session_fraction: float = 1.0) -> PullbackResult:
        """today: 當日完成K；history: 升冪歷史日K，結束於前一交易日 (session_fraction 未用，介面對齊)。"""
        if not history:
            return PullbackResult(False, note="無歷史")
        c = _f(today.close)
        if c is None:
            return PullbackResult(False, note="無收盤")
        closes = [_f(x.close) for x in history] + [c]
        if any(x is None for x in closes[-(self.ma_quarter):]):
            return PullbackResult(False, note="收盤含缺值")
        if len(closes) < self.ma_quarter:
            return PullbackResult(False, note=f"歷史不足 {self.ma_quarter} 日")

        ma5 = sum(closes[-self.ma_short:]) / self.ma_short
        ma20 = sum(closes[-self.ma_month:]) / self.ma_month
        ma60 = sum(closes[-self.ma_quarter:]) / self.ma_quarter
        r = rsi(closes, self.rsi_period)
        if r is None:
            return PullbackResult(False, note="RSI 不足")

        prev = history[-1]
        pc, pv = _f(prev.close), prev.volume
        change_pct = (c - pc) / pc * 100.0 if pc else None
        vol_ratio = (today.volume / pv) if (today.volume is not None and pv) else None

        passed = (ma20 >= ma60                       # 1 中期多頭
                  and c > ma60                        # 2 站穩季線之上
                  and (c >= ma20 if self.require_above_ma20 else True)  # 3 淺回檔不破月線
                  and c < ma5                         # 4 短線拉回到5日線下
                  and r <= self.rsi_threshold)        # 5 短線超賣
        return PullbackResult(
            passed,
            change_pct=round(change_pct, 2) if change_pct is not None else None,
            upper_shadow_pct=None,
            vol_ratio=round(vol_ratio, 2) if vol_ratio is not None else None,
            close=c, ma5=round(ma5, 2), ma20=round(ma20, 2), ma60=round(ma60, 2),
            rsi=round(r, 1),
        )
