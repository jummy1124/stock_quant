"""多進程 (multiprocessing) 爬蟲。

為何用 multiprocess 而非 thread?
  - 真正平行: 各市場 / 各批次在獨立進程同時抓取，互不阻塞，
    最大化『即時更新』時的吞吐量。
  - 隔離: 任一子進程崩潰不會拖垮主流程 (錯誤被收斂成 CrawlResult.error)。

切分粒度由 IDataSource.list_fetch_units() 決定:
  - 現在 = 每個市場一個 unit (上市 / 上櫃 → 2 進程平行)。
  - 未來逐檔即時抓取 = 每檔一個 unit → 自動擴展到數百個 unit，
    crawler 程式碼完全不用改。
"""
from __future__ import annotations

import time
from multiprocessing import Pool
from typing import Sequence

from ..datasource.base import FetchUnit, IDataSource
from .base import CrawlResult, ICrawler


def _run_unit(payload: tuple[IDataSource, FetchUnit]) -> CrawlResult:
    """子進程實際執行的工作函式 (必須在模組頂層才能被 pickle)。"""
    source, unit = payload
    start = time.perf_counter()
    try:
        quotes = source.fetch(unit)
        return CrawlResult(unit=unit, quotes=quotes, elapsed_sec=time.perf_counter() - start)
    except Exception as exc:  # 收斂錯誤，不讓單一 unit 失敗炸掉整批
        return CrawlResult(unit=unit, error=f"{type(exc).__name__}: {exc}",
                           elapsed_sec=time.perf_counter() - start)


class MultiProcessCrawler(ICrawler):
    def __init__(self, sources: Sequence[IDataSource], processes: int | None = None):
        """
        sources    : 要抓的資料來源清單 (e.g. [TwseDataSource(), TpexDataSource()])
        processes  : 進程數，預設 = 工作單位數量 (一單位一進程)
        """
        self._sources = list(sources)
        self._processes = processes

    def _build_payloads(self) -> list[tuple[IDataSource, FetchUnit]]:
        payloads: list[tuple[IDataSource, FetchUnit]] = []
        for source in self._sources:
            for unit in source.list_fetch_units():
                payloads.append((source, unit))
        return payloads

    def crawl(self) -> list[CrawlResult]:
        payloads = self._build_payloads()
        if not payloads:
            return []

        n_proc = self._processes or min(len(payloads), 8)
        # 單一 unit 時不啟動進程池，省下 fork 成本、也方便除錯
        if len(payloads) == 1 or n_proc == 1:
            return [_run_unit(p) for p in payloads]

        with Pool(processes=n_proc) as pool:
            results = pool.map(_run_unit, payloads)
        return results
