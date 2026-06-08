"""趨勢掃描器 — 多進程逐檔: 抓歷史日K -> 判斷趨勢。

每一檔個股是一個獨立工作，用 multiprocessing.Pool 平行處理，
所以「全市場每一檔都判斷趨勢」可以分散到多核心同時跑。
任一檔出錯 (查無資料/被限流) 會收斂成 ScanResult.error，不拖垮其他檔。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from multiprocessing import Pool
from typing import Optional, Sequence

from .analysis import Trend, TrendClassifier
from .datasource import get_history
from .domain import Market


@dataclass(slots=True)
class ScanResult:
    symbol: str
    market: Optional[Market]
    trend: Trend
    score: int = 0
    confidence: float = 0.0
    details: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _scan_one(payload) -> ScanResult:
    """子進程實際執行的工作 (模組頂層才能被 pickle)。"""
    symbol, market, months, classifier = payload
    try:
        quotes = get_history(symbol, market=market, months=months)
        res = classifier.classify(quotes)
        return ScanResult(symbol, market, res.trend, res.score, res.confidence, res.details)
    except Exception as exc:
        return ScanResult(symbol, market, Trend.UNKNOWN,
                          error=f"{type(exc).__name__}: {exc}")


class TrendScanner:
    def __init__(self, months: int = 5, processes: Optional[int] = None,
                 classifier: Optional[TrendClassifier] = None):
        self._months = months
        self._processes = processes
        self._classifier = classifier or TrendClassifier()

    def scan(self, pairs: Sequence[tuple[str, Optional[Market]]]) -> list[ScanResult]:
        payloads = [(s, m, self._months, self._classifier) for s, m in pairs]
        if not payloads:
            return []
        n = self._processes or min(len(payloads), 8)
        if len(payloads) == 1 or n == 1:
            return [_scan_one(p) for p in payloads]
        with Pool(processes=n) as pool:
            return pool.map(_scan_one, payloads)
