"""個股盤中即時報價 — 臺灣證交所 MIS API，批次 + 多進程。

MIS 盤中約數秒更新一次，是「當日盤中即時」唯一可行的官方來源。
ex_ch 用 '|' 串接可一次查多檔，所以把全市場切成數十檔一批 (batch_size)，
各批用 multiprocessing.Pool 平行抓 -> 全市場一分鐘內可抓完。

⚠️ MIS 有流量限制，高頻輪詢整個市場可能被暫時封鎖，請斟酌 batch_size 與頻率。
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from multiprocessing import Pool
from typing import Optional, Sequence

from ..domain import DailyQuote, Market
from .dates import latest_trading_day, parse_ad_date
from .http import get_json

_EX_TO_MARKET = {"tse": Market.TWSE, "otc": Market.TPEX}
_MARKET_TO_EX = {Market.TWSE: "tse", Market.TPEX: "otc"}

URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
# 只需額外帶 Referer; UA/Accept 由 http.DEFAULT_HEADERS 提供
HEADERS = {"Referer": "https://mis.twse.com.tw/stock/index.jsp"}


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


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _build_url(pairs) -> str:
    ex_ch = "|".join(f"{_MARKET_TO_EX.get(m, 'tse')}_{sym}.tw" for sym, m in pairs)
    return f"{URL}?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time() * 1000)}"


def _fetch_batch(payload) -> list[DailyQuote]:
    """子進程工作: 抓一批 (代號,市場) 的即時報價並正規化。模組頂層才能被 pickle。"""
    pairs, timeout = payload
    data = get_json(_build_url(pairs), timeout=timeout, headers=HEADERS)
    rtcode = str(data.get("rtcode", ""))
    if rtcode and rtcode not in ("0000", "0001"):
        raise RuntimeError(f"MIS rtcode={rtcode} {data.get('rtmessage', '')}")
    out: list[DailyQuote] = []
    for row in (data.get("msgArray") or []):
        market = _EX_TO_MARKET.get(str(row.get("ex", "tse")).lower(), Market.TWSE)
        z = _num(row.get("z"))          # 當前成交價 (可能 '-' 表示尚未成交)
        prev = _num(row.get("y"))       # 昨收
        # 尚未成交時用昨收當現價，避免整筆被丟掉 (否則該檔會變成「資料不足」)
        cur = z if z is not None else prev
        if cur is None:
            continue                    # 連昨收都沒有才放棄
        o, h, l = _num(row.get("o")), _num(row.get("h")), _num(row.get("l"))
        change = (cur - prev) if prev is not None else None
        vlots = _num(row.get("v"))               # MIS 'v' 單位為「張」
        volume = int(vlots * 1000) if vlots is not None else None   # 轉「股」與歷史一致
        q = DailyQuote.normalize(
            symbol=row.get("c", ""), name=row.get("n", ""), market=market,
            trade_date=parse_ad_date(row.get("d")) or latest_trading_day(),
            open=o if o is not None else cur,
            high=h if h is not None else cur,
            low=l if l is not None else cur,
            close=cur, change=change, volume=volume,
        )
        if q.is_valid():
            out.append(q)
    return out


def fetch_realtime(pairs: Sequence[tuple[str, Market]], batch_size: int = 40,
                   processes: Optional[int] = None, timeout: float = 15.0) -> list[DailyQuote]:
    """抓一組 (代號,市場) 的盤中即時報價 (批次 + 多進程)。回傳 DailyQuote 清單。"""
    pairs = [(str(s).strip(), m) for s, m in pairs if str(s).strip()]
    if not pairs:
        return []
    batches = list(_chunked(pairs, max(1, batch_size)))
    payloads = [(b, timeout) for b in batches]
    n = processes or min(len(payloads), 8)
    if len(payloads) == 1 or n == 1:
        results = [_fetch_batch(p) for p in payloads]
    else:
        with Pool(processes=n) as pool:
            results = pool.map(_fetch_batch, payloads)
    return [q for batch in results for q in batch]
