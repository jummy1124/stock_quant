"""把「全市場每日收盤價」POST 到 userdata 後端，供回測使用。

為什麼需要這個模組
------------------
篩選快照 (ingest.py) 只存「當天被篩出來的起漲個股」。回測要問的卻是反過來的問題:
**那些個股在之後第 N 個交易日值多少?** —— 而那幾天它們多半沒被篩出來，快照裡自然
查不到。所以後端另外需要一份普通的全市場日K。

好消息是選股主迴圈本來就把全市場歷史抱在記憶體裡 (load_market_history / ranker
的 _history)，所以這份上傳**不會多打證交所一次請求** —— 純粹是把已經在手上的資料
轉送給後端。

設計與 ingest.py 一致: 零第三方相依 (只用標準函式庫 urllib)，任何失敗都吞掉、
絕不中斷選股主迴圈。後端以 (trade_date, symbol) 為鍵做 upsert，重送只會覆蓋，
所以行程重啟造成的重送無害。

設定沿用同一組: INGEST_URL / INGEST_TOKEN (見 ingest.py)。
⚠️ 資訊參考，非投資建議。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Callable, Iterable, Optional, Sequence

from .domain import DailyQuote
from .ingest import IngestConfig

_PRICES_PATH = "/userapi/ingest/prices"

# 每次 POST 最多帶幾檔。全市場約 1,800 檔，一次送完 payload 約 200KB —— 對 urllib
# 與後端都還好，但切塊能讓「部分成功」有意義: 網路中斷時已送出的塊仍然留在資料庫，
# 下次補的是缺的那些，而不是從頭再來。
_CHUNK = 900


def _num(value) -> Optional[float]:
    return None if value is None else float(value)


def quote_to_price_item(q: DailyQuote) -> dict:
    return {
        "symbol": q.symbol,
        "name": q.name or "",
        "market_code": getattr(q.market, "value", "") or "",
        "open": _num(q.open),
        "high": _num(q.high),
        "low": _num(q.low),
        "close": _num(q.close),
        "volume": q.volume,
    }


def _post(cfg: IngestConfig, payload: dict) -> tuple[bool, str]:
    url = f"{cfg.base_url.rstrip('/')}{_PRICES_PATH}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Token": cfg.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=max(cfg.timeout, 30.0)) as resp:
            return True, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        return False, f"HTTP {exc.code}: {detail[:200]}"
    except Exception as exc:  # noqa: BLE001 — 上傳失敗不可中斷選股主迴圈
        return False, f"{type(exc).__name__}: {exc}"


def post_daily_prices(
    cfg: IngestConfig,
    trade_date: date,
    quotes: Sequence[DailyQuote],
    source: str = "eod",
) -> tuple[bool, str]:
    """送一個交易日的全市場收盤價 (自動分塊)。回傳 (ok, 說明)。"""
    if not cfg.configured:
        return False, "INGEST_URL / INGEST_TOKEN 未設定"
    items = [quote_to_price_item(q) for q in quotes if q.close is not None]
    if not items:
        return False, f"{trade_date} 沒有可上傳的收盤價"
    total = 0
    for i in range(0, len(items), _CHUNK):
        ok, msg = _post(cfg, {
            "trade_date": trade_date.isoformat(),
            "source": source,
            "items": items[i : i + _CHUNK],
        })
        if not ok:
            return False, f"第 {i // _CHUNK + 1} 塊失敗: {msg}"
        total += len(items[i : i + _CHUNK])
    return True, f"{trade_date} 共 {total} 檔"


def collect_daily_bars(
    history: dict[str, list[DailyQuote]] | Iterable[tuple[str, list[DailyQuote]]],
    days: int = 5,
) -> dict[date, list[DailyQuote]]:
    """把 {代號: 升冪日K} 轉成 {交易日: 當日全市場日K}，只取最近 days 個交易日。

    取「最近幾天」而不是只取最後一天，是為了讓上傳能自我修復: 昨天上傳失敗、機器
    當機、或後端當時正在重啟，今天這一次就會把缺的補回去。後端是 upsert，重送無害。
    """
    if hasattr(history, "items"):
        history = history.items()   # type: ignore[assignment]
    by_day: dict[date, list[DailyQuote]] = {}
    for _symbol, quotes in history:
        if not quotes:
            continue
        for q in quotes[-days:] if days > 0 else quotes:
            if q.close is not None:
                by_day.setdefault(q.trade_date, []).append(q)
    if days > 0 and len(by_day) > days:
        keep = sorted(by_day)[-days:]
        by_day = {d: by_day[d] for d in keep}
    return by_day


class DailyPriceIngestor:
    """每 tick 呼叫 process()，收盤後把最近幾個交易日的全市場收盤價送一次。

    只在 source == "eod" (即 ranker 已改用「最後交易日完成日K」) 時動作 —— 盤中的
    「今日K」是 MIS 即時價，還會再變，存進資料庫當成收盤價會讓回測用到錯的數字。

    以交易日為鍵去重，同一天只送一次 (失敗則不標記，下個 tick 重試)。
    """

    def __init__(self, cfg: IngestConfig, days: int = 5,
                 log: Callable[[str], None] = print):
        self.cfg = cfg
        self.days = days
        self.log = log
        self._sent: set[str] = set()

    def process(self, now: datetime, ranker) -> None:
        if not self.cfg.configured:
            return
        if getattr(ranker, "last_source", "live") != "eod":
            return   # 盤中即時價不是收盤價 —— 不入庫
        history = {sym: ranker.history(sym) for sym, _m in getattr(ranker, "pairs", [])}
        by_day = collect_daily_bars(history, days=self.days)
        for trade_date in sorted(by_day):
            key = trade_date.isoformat()
            if key in self._sent:
                continue
            ok, msg = post_daily_prices(self.cfg, trade_date, by_day[trade_date])
            if ok:
                self._sent.add(key)
                self.log(f"📈 已上傳全市場收盤價: {msg}")
            else:
                self.log(f"⚠️ 上傳收盤價失敗: {msg} (下個 tick 重試)")
