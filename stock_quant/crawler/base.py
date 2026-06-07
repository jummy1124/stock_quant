"""爬蟲抽象介面與結果型別。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..datasource.base import FetchUnit
from ..domain import DailyQuote


@dataclass(slots=True)
class CrawlResult:
    """單一 FetchUnit 的抓取結果 (成功或失敗)。"""

    unit: FetchUnit
    quotes: list[DailyQuote] = field(default_factory=list)
    error: str | None = None
    elapsed_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


class ICrawler(ABC):
    @abstractmethod
    def crawl(self) -> list[CrawlResult]:
        """執行抓取，回傳每個工作單位的結果。"""
