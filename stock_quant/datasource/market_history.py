"""全市場歷史日K 組裝 — 為「全市場」設計，盡量減少請求並避免限流。

- 上市: TWSE MI_INDEX『逐日整批』(type=ALLBUT0999) —— 一天一次請求拿到當天全市場，
        抓 ~75 個交易日只要 ~75 次請求。
- 上櫃: TPEx 沒有穩定的『逐日整批』端點，故用「先取 OTC 個股清單 + 逐檔抓歷史」
        (history.get_history，已是新版正確端點)，並以多進程加速、本地快取。

回傳 {代號: 時間升冪日K序列}。附本地快取: 同一基準日只抓一次。
"""
from __future__ import annotations

import pickle
from datetime import date, timedelta
from multiprocessing import Pool
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..domain import DailyQuote, Market, is_individual_stock
from .http import get_json

_CACHE_VERSION = 3   # 抓取邏輯變更時 +1，使舊快取自動失效 (v3: 上櫃量改為股)


def _g(row, idx):
    return row[idx] if (idx is not None and idx < len(row)) else None


def fetch_twse_day(d: date, timeout: float = 10.0) -> list[DailyQuote]:
    """抓上市某一天全市場個股 (MI_INDEX)。假日/無資料回傳 []。"""
    url = (f"https://www.twse.com.tw/exchangeReport/MI_INDEX"
           f"?response=json&date={d:%Y%m%d}&type=ALLBUT0999")
    data = get_json(url, timeout=timeout)
    if str(data.get("stat", "")).upper() != "OK":
        return []
    tables = data.get("tables") or []
    if not tables:
        for i in range(1, 10):
            f, dd = data.get(f"fields{i}"), data.get(f"data{i}")
            if f and dd:
                tables.append({"fields": f, "data": dd})
    out: list[DailyQuote] = []
    for t in tables:
        fields = t.get("fields") or []
        idx = {name: i for i, name in enumerate(fields)}
        if "證券代號" not in idx or "收盤價" not in idx:
            continue
        ci, cli = idx["證券代號"], idx["收盤價"]
        oi, hi, li = idx.get("開盤價"), idx.get("最高價"), idx.get("最低價")
        vi = idx.get("成交股數")
        for r in t.get("data", []):
            code = str(_g(r, ci) or "").strip()
            if not is_individual_stock(code):
                continue
            q = DailyQuote.normalize(symbol=code, name="", market=Market.TWSE, trade_date=d,
                                     open=_g(r, oi), high=_g(r, hi), low=_g(r, li),
                                     close=_g(r, cli), volume=_g(r, vi))
            if q.is_valid():
                out.append(q)
    return out


def _weekdays_back(today: date, max_attempts: int):
    d = today - timedelta(days=1)   # 從昨天起 (今天盤中由 MIS 提供)
    for _ in range(max_attempts):
        if d.weekday() < 5:
            yield d
        d -= timedelta(days=1)


def _otc_universe(timeout: float = 20.0) -> list[str]:
    """上櫃個股清單 (用 TPEx OpenAPI 最後交易日)。"""
    from .tpex import TpexDataSource
    src = TpexDataSource(timeout=timeout)
    try:
        return [q.symbol for q in src.fetch(src.list_fetch_units()[0])]
    except Exception:
        return []


def _otc_worker(payload):
    """子進程: 逐檔抓上櫃歷史。"""
    symbol, months = payload
    from .history import get_history
    try:
        return symbol, get_history(symbol, market=Market.TPEX, months=months)
    except Exception:
        return symbol, []


def _expected_as_of(today: date) -> str:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def load_market_history(
    markets: Sequence[str] = ("twse", "tpex"),
    days: int = 75,
    timeout: float = 10.0,
    delay: float = 0.4,
    cache_path: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    processes: Optional[int] = None,
    today: Optional[date] = None,
) -> dict[str, list[DailyQuote]]:
    import time as _time

    today = today or date.today()
    log = progress or (lambda _m: None)
    months = max(4, days // 20 + 1)

    if cache_path and Path(cache_path).exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            if (cached.get("as_of") == _expected_as_of(today)
                    and cached.get("ver") == _CACHE_VERSION):
                log(f"使用快取歷史 ({cache_path})")
                return cached["data"]
        except Exception:
            pass

    hist: dict[str, list[DailyQuote]] = {}

    # 上市: 逐日整批
    if "twse" in markets:
        collected = 0
        for d in _weekdays_back(today, max_attempts=days + 30):
            if collected >= days:
                break
            try:
                rows = fetch_twse_day(d, timeout=timeout)
            except Exception as exc:
                log(f"  [上市] {d} 失敗: {exc}")
                rows = []
            if not rows:
                continue
            for q in rows:
                hist.setdefault(q.symbol, []).append(q)
            collected += 1
            if collected == 1 or collected % 10 == 0:
                log(f"  [上市] 已抓 {collected}/{days} 個交易日 ...")
            if delay:
                _time.sleep(delay)

    # 上櫃: OTC 清單 + 逐檔 (新版端點)，多進程加速
    if "tpex" in markets:
        otc = _otc_universe(timeout=max(timeout, 20.0))
        log(f"  [上櫃] 共 {len(otc)} 檔，逐檔抓歷史 (首次較久，會快取) ...")
        payloads = [(s, months) for s in otc]
        if payloads:
            n = processes or min(len(payloads), 8)
            if n == 1 or len(payloads) == 1:
                for i, p in enumerate(payloads, 1):
                    sym, quotes = _otc_worker(p)
                    if quotes:
                        hist[sym] = quotes
            else:
                with Pool(processes=n) as pool:
                    for i, (sym, quotes) in enumerate(pool.imap_unordered(_otc_worker, payloads), 1):
                        if quotes:
                            hist[sym] = quotes
                        if i % 100 == 0:
                            log(f"  [上櫃] 已處理 {i}/{len(payloads)} 檔 ...")

    for sym in hist:
        hist[sym].sort(key=lambda q: q.trade_date)

    if cache_path:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump({"as_of": _expected_as_of(today), "ver": _CACHE_VERSION, "data": hist}, f)
            log(f"歷史已快取至 {cache_path}")
        except Exception:
            pass

    return hist
