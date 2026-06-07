"""股票池 (universe) 提供者。

盤中即時來源 (MIS) 需要先知道『要抓哪些檔』。這裡用 EOD OpenAPI 來源
取得全市場『個股』清單 (已自動濾掉 ETF/權證/特別股等)，作為 MIS 的輸入。

回傳 list[(symbol, Market)]，可直接餵給 MisRealtimeDataSource。
"""
from __future__ import annotations

from typing import Sequence

from .datasource import TpexDataSource, TwseDataSource
from .domain import Market


def load_individual_universe(markets: Sequence[str] = ("twse", "tpex"),
                             timeout: float = 30.0) -> list[tuple[str, Market]]:
    pairs: list[tuple[str, Market]] = []
    if "twse" in markets:
        s = TwseDataSource(timeout=timeout)
        pairs += [(q.symbol, q.market) for q in s.fetch(s.list_fetch_units()[0])]
    if "tpex" in markets:
        s = TpexDataSource(timeout=timeout)
        pairs += [(q.symbol, q.market) for q in s.fetch(s.list_fetch_units()[0])]
    # 去重保序
    seen: dict[str, Market] = {}
    for sym, m in pairs:
        seen.setdefault(sym, m)
    return list(seen.items())
