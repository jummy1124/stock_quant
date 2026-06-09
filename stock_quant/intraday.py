"""盤中選股: 每分鐘用即時價當「今日K」，套用 BreakoutScreen 篩出符合的個股。

歷史日K盤中不變 -> 啟動取得一次並快取;
每分鐘只抓即時價 (MIS 批次 + 多進程)，與前一交易日比對做篩選。

回傳 list[(代號, 市場, ScreenResult)]，由 run_intraday 印出 passed 的個股。
"""
from __future__ import annotations

from datetime import datetime
from multiprocessing import Pool
from typing import Optional, Sequence

from .analysis import BreakoutScreen, ScreenResult
from .datasource import get_history
from .datasource.mis import fetch_realtime
from .domain import DailyQuote, Market


def _history_worker(payload):
    symbol, market, months = payload
    try:
        return symbol, get_history(symbol, market=market, months=months), None
    except Exception as exc:
        return symbol, None, f"{type(exc).__name__}: {exc}"


class IntradayScreener:
    def __init__(self, pairs: Sequence[tuple[str, Optional[Market]]], months: int = 5,
                 batch_size: int = 40, processes: Optional[int] = None,
                 screen: Optional[BreakoutScreen] = None):
        self.pairs = list(pairs)
        self.months = months
        self.batch_size = batch_size
        self.processes = processes
        self.screen = screen or BreakoutScreen()
        self._history: dict[str, list[DailyQuote]] = {}

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

    def tick(self, now: Optional[datetime] = None) -> list[tuple[str, Market, ScreenResult]]:
        """抓一次即時價，逐檔做選股篩選。"""
        live = fetch_realtime(self.pairs, batch_size=self.batch_size, processes=self.processes)
        live_by = {q.symbol: q for q in live}
        out: list[tuple[str, Market, ScreenResult]] = []
        for symbol, market in self.pairs:
            hist = self._history.get(symbol)
            lq = live_by.get(symbol)
            # 選股以「今日即時資料」為準: 沒歷史(無昨收)或這分鐘沒拿到即時價 -> 跳過
            if not hist or lq is None:
                continue
            out.append((symbol, lq.market, self.screen.check(lq, hist)))  # 今日即時 vs 歷史(到昨日)
        return out
