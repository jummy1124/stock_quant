"""盤中趨勢監控: 每分鐘用即時價接成『今日即時K』重算趨勢。

歷史日K盤中不變 -> 啟動時取得一次並快取在記憶體;
每分鐘只抓便宜的即時價 (MIS 批次 + 多進程)，接成今日K重算趨勢。

取得歷史的兩條路:
  - 指定少數個股: prepare() 用逐檔 get_history。
  - 全市場: run_intraday 用『逐日整批』load_market_history 取得後 preload_history()。

容錯: 拿不到即時價(限流/未成交)時，退而用歷史日K判趨勢，而不是標「資料不足」，
      只有完全沒有歷史的個股才會是 UNKNOWN。
"""
from __future__ import annotations

from datetime import datetime
from multiprocessing import Pool
from typing import Optional, Sequence

from .analysis import Trend, TrendClassifier
from .datasource import get_history
from .datasource.mis import fetch_realtime
from .domain import DailyQuote, Market
from .scanner import ScanResult


def _history_worker(payload):
    """子進程: 逐檔抓歷史日K。"""
    symbol, market, months = payload
    try:
        return symbol, get_history(symbol, market=market, months=months), None
    except Exception as exc:
        return symbol, None, f"{type(exc).__name__}: {exc}"


class IntradayTrendMonitor:
    def __init__(self, pairs: Sequence[tuple[str, Optional[Market]]], months: int = 5,
                 batch_size: int = 40, processes: Optional[int] = None,
                 classifier: Optional[TrendClassifier] = None):
        self.pairs = list(pairs)
        self.months = months
        self.batch_size = batch_size
        self.processes = processes
        self.classifier = classifier or TrendClassifier()
        self._history: dict[str, list[DailyQuote]] = {}

    def preload_history(self, hist_map: dict[str, list[DailyQuote]]) -> int:
        """直接餵入已組好的歷史 (全市場逐日整批模式用)。"""
        self._history = {s: q for s, q in hist_map.items() if q}
        return len(self._history)

    def prepare(self) -> int:
        """逐檔抓歷史並快取 (指定少數個股時用，多進程)。回傳成功檔數。"""
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

    def tick(self, now: Optional[datetime] = None) -> list[ScanResult]:
        """抓一次即時價，接成今日K重算每檔趨勢；拿不到即時價時退用歷史。"""
        live = fetch_realtime(self.pairs, batch_size=self.batch_size, processes=self.processes)
        live_by = {q.symbol: q for q in live}
        out: list[ScanResult] = []
        for symbol, market in self.pairs:
            hist = self._history.get(symbol)
            if not hist:
                out.append(ScanResult(symbol, market, Trend.UNKNOWN, error="無歷史日K"))
                continue
            lq = live_by.get(symbol)
            if lq is not None:
                series = [q for q in hist if q.trade_date < lq.trade_date] + [lq]
            else:
                series = hist     # 無即時價 -> 用歷史判趨勢，不標資料不足
            res = self.classifier.classify(series)
            out.append(ScanResult(symbol, market, res.trend, res.score, res.confidence, res.details))
        return out
