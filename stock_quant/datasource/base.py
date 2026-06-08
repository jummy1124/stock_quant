"""資料來源抽象介面。

任何新的資料來源 (券商 API、其他交易所、即時報價 WebSocket...) 只要實作
IDataSource 就能無痛接入 crawler，符合開放封閉原則 (OCP)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

from ..domain import DailyQuote, Market


@dataclass(frozen=True, slots=True)
class FetchUnit:
    """一個「可獨立抓取的工作單位」。

    這是 multiprocess 平行化的最小切分單位。目前一個市場 = 一個 unit；
    未來要做即時逐檔抓取時，一檔股票就是一個 unit，crawler 不需改動。
    """

    source_name: str            # 對應的 IDataSource.name
    market: Market
    label: str                  # 人類可讀標籤，用於 log
    params: dict = None         # 預留: 之後逐檔抓取可放 symbol 等參數


class IDataSource(ABC):
    """資料來源的抽象基底。"""

    #: 唯一名稱，crawler 用它在子進程中重建對應的 data source
    name: str = "base"
    market: Market

    @abstractmethod
    def list_fetch_units(self) -> Sequence[FetchUnit]:
        """回傳這個來源要抓的所有工作單位 (供 crawler 分配到各進程)。"""

    @abstractmethod
    def fetch(self, unit: FetchUnit) -> list[DailyQuote]:
        """實際抓取並回傳『正規化後』的 DailyQuote 清單。

        實作需自行處理 HTTP 重試與髒值清洗，並保證回傳的是乾淨資料。
        """


def pick(row: dict, *candidates: str):
    """從一筆 row 依序嘗試多個可能的欄位名，回傳第一個存在(非空)的值。

    官方欄位名偶有調整，用這個方式讓解析對欄位改名有韌性。
    """
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None
