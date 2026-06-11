"""盤中選股: 每分鐘用即時價當「今日K」，套用 BreakoutScreen 篩出符合的個股。

歷史日K盤中不變 -> 啟動取得一次並快取;
資料來源依時間自動切換:
  - 交易時間 (09:00–13:30)：抓 MIS 即時價當「今日K」，與歷史(到昨日)比對。
  - 非交易時間：用最後一個交易日的完成日K (history[-1]) 當「今日K」，與更早歷史比對。

回傳 list[TickRow]，由 run_intraday 印出 stable 的個股 (已連續確認)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Pool
from typing import Optional, Sequence

from .analysis import BreakoutScreen, ScreenResult
from .datasource import get_history
from .datasource.mis import fetch_realtime
from .domain import DailyQuote, Market
from .scheduler import MarketClock


@dataclass(slots=True)
class TickRow:
    """一檔個股某次 tick 的結果。stable=True 才是「已確認、可下單參考」的訊號。"""
    symbol: str
    market: Market
    result: ScreenResult
    stable: bool = False

    # 向後相容: 舊程式用 (symbol, market, result) 三元素解包仍可運作
    def __iter__(self):
        return iter((self.symbol, self.market, self.result))


class _Stabilizer:
    """把單次 tick 的瞬間命中，過濾成「連續確認 + 寬限窗」的穩定訊號。

    - confirm_ticks: 需連續通過幾次 tick 才『確認』入選 (去除單次雜訊)。
    - grace_ticks  : 確認後即使某次瞬間不符，仍保留在清單幾次 (避免價格小幅跳動就消失)。
    confirm_ticks≤1 時等於不過濾 (stable == 當下 passed)，給 --once / 單次測試用。
    """

    def __init__(self, confirm_ticks: int = 2, grace_ticks: int = 1):
        self.confirm_ticks = confirm_ticks
        self.grace_ticks = grace_ticks
        self._streak: dict[str, int] = {}      # 連續通過次數
        self._grace: dict[str, int] = {}       # 確認後剩餘寬限次數 (>0 表已確認且仍在清單)

    def update(self, symbol: str, passed: bool) -> bool:
        if self.confirm_ticks <= 1:
            return passed
        if passed:
            self._streak[symbol] = self._streak.get(symbol, 0) + 1
            if self._streak[symbol] >= self.confirm_ticks:
                self._grace[symbol] = self.grace_ticks      # (重新)確認，補滿寬限
                return True
            return self._grace.get(symbol, 0) > 0            # 確認過且仍在寬限窗內
        # 這次不符
        self._streak[symbol] = 0
        if self._grace.get(symbol, 0) > 0:
            self._grace[symbol] -= 1                          # 消耗一次寬限，暫時保留
            return True
        return False


def _history_worker(payload):
    symbol, market, months = payload
    try:
        return symbol, get_history(symbol, market=market, months=months), None
    except Exception as exc:
        return symbol, None, f"{type(exc).__name__}: {exc}"


class IntradayScreener:
    def __init__(self, pairs: Sequence[tuple[str, Optional[Market]]], months: int = 5,
                 batch_size: int = 40, processes: Optional[int] = None,
                 screen: Optional[BreakoutScreen] = None,
                 confirm_ticks: int = 2, grace_ticks: int = 1,
                 clock: Optional[MarketClock] = None,
                 eod_when_closed: bool = True):
        self.pairs = list(pairs)
        self.months = months
        self.batch_size = batch_size
        self.processes = processes
        self.screen = screen or BreakoutScreen()
        self.clock = clock or MarketClock()
        self.eod_when_closed = eod_when_closed     # 非交易時間是否改用最後交易日完成日K
        self._stab = _Stabilizer(confirm_ticks=confirm_ticks, grace_ticks=grace_ticks)
        self._history: dict[str, list[DailyQuote]] = {}
        self.last_quoted = 0          # 上次 tick 實際取得即時價/可篩的檔數 (覆蓋率診斷)
        self.last_warning: Optional[str] = None   # 上次 tick 的即時報價失敗摘要 (若有)
        self.last_source = "live"     # 上次 tick 的資料來源: "live" 盤中即時 / "eod" 最後交易日

    def preload_history(self, hist_map: dict[str, list[DailyQuote]]) -> int:
        self._history = {s: q for s, q in hist_map.items() if q}
        return len(self._history)

    def prepare(self) -> int:
        payloads = [(s, m, self.months) for s, m in self.pairs]
        if len(payloads) <= 1 or self.processes == 1:
            results = [_history_worker(p) for p in payloads]
        else:
            with Pool(processes=self.processes or min(len(payloads), 8)) as pool:
                results = pool.map(_history_worker, payloads)
        for symbol, quotes, _err in results:
            if quotes:
                self._history[symbol] = quotes
        return len(self._history)

    def tick(self, now: Optional[datetime] = None) -> list[TickRow]:
        """抓一次資料做選股篩選。交易時間用即時價；非交易時間用最後交易日完成日K。"""
        now = now or datetime.now()
        if self.eod_when_closed and not self.clock.is_trading(now):
            return self._tick_eod(now)
        return self._tick_live(now)

    def _tick_live(self, now: datetime) -> list[TickRow]:
        """交易時間: 抓即時價當今日K，套穩定層 (連續確認 + 寬限窗)。"""
        self.last_source = "live"
        frac = self.clock.session_fraction(now)         # 今日已過盤中比例 -> 量比同時段換算
        self.last_warning = None

        def _warn(msg: str) -> None:
            self.last_warning = msg
        live = fetch_realtime(self.pairs, batch_size=self.batch_size,
                              processes=self.processes, on_error=_warn)
        live_by = {q.symbol: q for q in live}
        self.last_quoted = len(live_by)
        out: list[TickRow] = []
        for symbol, market in self.pairs:
            hist = self._history.get(symbol)
            lq = live_by.get(symbol)
            # 選股以「今日即時資料」為準: 沒歷史(無昨收)或這分鐘沒拿到即時價 -> 跳過
            # 注意: 沒拿到即時價時也不更新穩定層 streak (避免覆蓋率波動誤判為「跌出」)
            if not hist or lq is None:
                continue
            res = self.screen.check(lq, hist, session_fraction=frac)   # 今日即時 vs 歷史(到昨日)
            stable = self._stab.update(symbol, res.passed)
            out.append(TickRow(symbol, lq.market, res, stable))
        return out

    def _tick_eod(self, now: datetime) -> list[TickRow]:
        """非交易時間: 用最後一個交易日的完成日K (history[-1]) 當今日K，與更早歷史比對。

        完成日K是定案資料、不會跨分鐘跳動，故不套穩定層 (stable 直接等於 passed)，
        也不需抓網路 (純用啟動時快取的歷史)。
        """
        self.last_source = "eod"
        self.last_warning = None
        out: list[TickRow] = []
        for symbol, market in self.pairs:
            hist = self._history.get(symbol)
            if not hist or len(hist) < 2:               # 需至少「今日(最後交易日)+前一日」
                continue
            today = hist[-1]
            res = self.screen.check(today, hist[:-1], session_fraction=1.0)   # 完整日K -> 量比看整日
            out.append(TickRow(symbol, today.market, res, res.passed))
        self.last_quoted = len(out)
        return out
