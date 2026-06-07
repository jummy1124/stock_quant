"""個股即時報價資料來源 (盤中) — 使用臺灣證券交易所 MIS API。

端點: https://mis.twse.com.tw/stock/api/getStockInfo.jsp
特色:
  - 盤中即時更新 (約數秒~十幾秒)，是「當日盤中即時」唯一可行的官方來源。
  - 支援一次帶多檔: ex_ch 用 '|' 串接多個 channel，所以可『批次』查詢。

批次 + 多進程設計:
  把全市場個股切成數十檔一批 (batch_size)，每一批 = 一個 FetchUnit，
  crawler 再把各批 fan-out 到多進程平行抓 —— 這樣全市場一分鐘內可抓完。

⚠️ 注意: MIS 有流量限制，高頻輪詢整個市場可能被暫時封鎖，請斟酌 batch_size 與頻率。

MIS msgArray 主要欄位:
    c=代號 n=名稱 z=成交價 o=開 h=高 l=低 y=昨收 v=累積成交量(張)
    d=日期(YYYYMMDD) t=時間 ex=市場別(tse/otc)
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional, Sequence

from ..domain import DailyQuote, Market
from .base import FetchUnit, IDataSource
from .dates import latest_trading_day, parse_ad_date
from .http import get_json

_EX_TO_MARKET = {"tse": Market.TWSE, "otc": Market.TPEX}
_MARKET_TO_EX = {Market.TWSE: "tse", Market.TPEX: "otc"}


def _num(raw) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "-", "--"):
        return None
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class MisRealtimeDataSource(IDataSource):
    name = "mis"
    market = Market.TWSE  # 混合市場，實際以每筆回傳的 ex 為準

    URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    def __init__(self, pairs: Iterable[tuple[str, Market]], batch_size: int = 40,
                 timeout: float = 15.0):
        """pairs: (股票代號, 市場) 的序列；市場決定 MIS 的 tse_/otc_ 前綴。"""
        self._pairs = [(str(s).strip(), m) for s, m in pairs if str(s).strip()]
        self._batch_size = max(1, batch_size)
        self._timeout = timeout

    def list_fetch_units(self) -> Sequence[FetchUnit]:
        units = []
        for i, batch in enumerate(_chunked(self._pairs, self._batch_size)):
            units.append(FetchUnit(
                source_name=self.name, market=self.market,
                label=f"批次{i + 1}({len(batch)}檔)", params={"pairs": batch},
            ))
        return units

    def _build_url(self, pairs) -> str:
        ex_ch = "|".join(f"{_MARKET_TO_EX.get(m, 'tse')}_{sym}.tw" for sym, m in pairs)
        ts = int(time.time() * 1000)
        return f"{self.URL}?ex_ch={ex_ch}&json=1&delay=0&_={ts}"

    def fetch(self, unit: FetchUnit) -> list[DailyQuote]:
        pairs = unit.params["pairs"]
        data = get_json(self._build_url(pairs), timeout=self._timeout, headers=self.HEADERS)

        rtcode = str(data.get("rtcode", ""))
        if rtcode and rtcode not in ("0000", "0001"):
            raise RuntimeError(f"MIS rtcode={rtcode} {data.get('rtmessage', '')}")

        quotes: list[DailyQuote] = []
        for row in (data.get("msgArray") or []):
            ex = str(row.get("ex", "tse")).lower()
            market = _EX_TO_MARKET.get(ex, Market.TWSE)
            close = _num(row.get("z"))
            prev = _num(row.get("y"))
            change = (close - prev) if (close is not None and prev is not None) else None
            q = DailyQuote.normalize(
                symbol=row.get("c", ""),
                name=row.get("n", ""),
                market=market,
                trade_date=parse_ad_date(row.get("d")) or latest_trading_day(),
                open=row.get("o"),
                high=row.get("h"),
                low=row.get("l"),
                close=row.get("z"),
                change=change,
                volume=row.get("v"),
            )
            if q.is_valid():
                quotes.append(q)
        return quotes
