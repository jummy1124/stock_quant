"""全市場歷史日K 組裝 — 為「全市場」設計，盡量減少請求並避免 TWSE 限流。

TWSE MI_INDEX 對連續請求限流很嚴，觸發後會把 IP 擋上「好幾分鐘」。因此本模組:
  * 每天只打 1 次請求 (rwd 新端點；失敗才退舊端點)，不在封鎖窗內快速重試浪費。
  * 每天之間留較大間隔 (delay 預設 3s)，盡量不要觸發限流。
  * 被擋時冷卻後重試 (cooldowns)；連續多天都被擋就「判定 IP 被鎖、停止抓取」，
    不再硬敲 (硬敲只會養大封鎖)。
  * 逐日『增量快取 + 續抓』: 抓到的每個交易日存進快取；下次啟動只補「還沒抓到的日子」，
    所以被擋而中斷時，過幾分鐘再跑就會接著補齊，最終仍能抓到全部。

上櫃用「OTC 個股清單 + 逐檔抓歷史」(history.get_history，新版端點)，多進程加速、一併快取。
回傳 {代號: 時間升冪日K序列}。
"""
from __future__ import annotations

import pickle
from datetime import date, timedelta
from multiprocessing import Pool
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..domain import DailyQuote, Market, is_individual_stock
from .http import get_json

_CACHE_VERSION = 4   # v4: 改為逐日增量快取 (twse_days / twse_done / tpex)

_MI_INDEX_URLS = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json",
    "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={ymd}&type=ALLBUT0999",
)
_TWSE_HEADERS = {"Referer": "https://www.twse.com.tw/zh/trading/historical/mi-index.html"}


def _g(row, idx):
    return row[idx] if (idx is not None and idx < len(row)) else None


def _parse_mi_index(data, d: date) -> list[DailyQuote]:
    """把 MI_INDEX 回傳的 JSON 解析成個股 DailyQuote 清單 (假日/無資料回 [])。"""
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


def fetch_twse_day(d: date, timeout: float = 10.0) -> list[DailyQuote]:
    """抓上市某一天全市場個股 (MI_INDEX)。新端點為主、舊端點備援；假日/無資料回 []。

    每個端點只打一次 (retries=1) —— 限流時快速重試只會浪費封鎖時間窗，重試交給上層冷卻。
    端點正常回應 (含假日無資料) 直接採用；全部端點連線失敗才丟出最後一個例外。
    """
    ymd = f"{d:%Y%m%d}"
    last_err: Optional[Exception] = None
    for tmpl in _MI_INDEX_URLS:
        try:
            data = get_json(tmpl.format(ymd=ymd), timeout=timeout, headers=_TWSE_HEADERS, retries=1)
        except Exception as exc:                 # noqa: BLE001 — 換備援端點
            last_err = exc
            continue
        return _parse_mi_index(data, d)
    if last_err is not None:
        raise last_err
    return []


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


def _read_cache(cache_path: Optional[str]) -> dict:
    """讀取增量快取 (版本不符或讀不到 -> 視為空，重新累積)。"""
    if not cache_path or not Path(cache_path).exists():
        return {}
    try:
        with open(cache_path, "rb") as f:
            c = pickle.load(f)
        if c.get("ver") == _CACHE_VERSION:
            return c
    except Exception:
        pass
    return {}


def load_market_history(
    markets: Sequence[str] = ("twse", "tpex"),
    days: int = 75,
    timeout: float = 10.0,
    delay: float = 3.0,
    cooldowns: Sequence[float] = (30.0, 90.0),
    max_consecutive_fail: int = 5,
    cache_path: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    processes: Optional[int] = None,
    today: Optional[date] = None,
) -> dict[str, list[DailyQuote]]:
    import time as _time

    today = today or date.today()
    log = progress or (lambda _m: None)
    months = max(4, days // 20 + 1)

    cache = _read_cache(cache_path)
    twse_days: dict[str, list[DailyQuote]] = dict(cache.get("twse_days", {}))   # iso -> 當日全市場
    twse_done: set[str] = set(cache.get("twse_done", []))                       # 已成功抓過(含假日空)
    tpex_cache: dict[str, list[DailyQuote]] = dict(cache.get("tpex", {}))

    def _save():
        if not cache_path:
            return
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump({"ver": _CACHE_VERSION, "twse_days": twse_days,
                             "twse_done": sorted(twse_done), "tpex": tpex_cache}, f)
        except Exception:
            pass

    # 上市: 逐日整批 (增量續抓 + 限流退避)
    if "twse" in markets:
        collected = len(twse_days)
        if collected:
            log(f"  [上市] 快取已有 {collected} 個交易日，續抓缺的 ...")
        consecutive_fail = 0
        for d in _weekdays_back(today, max_attempts=days + 40):
            if collected >= days:
                break
            iso = d.isoformat()
            if iso in twse_done:                      # 已抓過 -> 跳過 (續抓核心)
                continue
            rows: Optional[list[DailyQuote]] = None
            for attempt in range(len(cooldowns) + 1):
                try:
                    rows = fetch_twse_day(d, timeout=timeout)
                    break
                except Exception as exc:
                    if attempt < len(cooldowns):
                        cd = cooldowns[attempt]
                        log(f"  [上市] {d} 被擋，{cd:.0f}s 後重試 ({attempt + 1}/{len(cooldowns)}) ...")
                        _time.sleep(cd)
                    else:
                        log(f"  [上市] {d} 放棄: {exc}")
            if rows is None:                          # 這天連冷卻後都失敗
                consecutive_fail += 1
                if consecutive_fail >= max_consecutive_fail:
                    log(f"  [上市] 連續 {consecutive_fail} 天被擋 -> 判定 IP 被 TWSE 限流，先停止。"
                        f"已抓的會快取；請等 5~10 分鐘再重跑，會自動接續補齊。")
                    break
                continue
            consecutive_fail = 0
            twse_done.add(iso)
            if rows:
                twse_days[iso] = rows
                collected += 1
                if collected == 1 or collected % 10 == 0:
                    log(f"  [上市] 已抓 {collected}/{days} 個交易日 ...")
                _save()                               # 邊抓邊存 -> 中斷也不白費
            if delay:
                _time.sleep(delay)
        if collected == 0:
            log("  [上市] 一天都沒抓到 -> 可能 IP 已被限流，請等幾分鐘再重跑 (會接續)。")

    # 上櫃: OTC 清單 + 逐檔 (新版端點)，多進程加速；有快取就用快取
    if "tpex" in markets:
        if tpex_cache:
            log(f"  [上櫃] 快取已有 {len(tpex_cache)} 檔，沿用。")
        else:
            otc = _otc_universe(timeout=max(timeout, 20.0))
            log(f"  [上櫃] 共 {len(otc)} 檔，逐檔抓歷史 (首次較久，會快取) ...")
            payloads = [(s, months) for s in otc]
            if payloads:
                n = processes or min(len(payloads), 8)
                if n == 1 or len(payloads) == 1:
                    for p in payloads:
                        sym, quotes = _otc_worker(p)
                        if quotes:
                            tpex_cache[sym] = quotes
                else:
                    with Pool(processes=n) as pool:
                        for i, (sym, quotes) in enumerate(pool.imap_unordered(_otc_worker, payloads), 1):
                            if quotes:
                                tpex_cache[sym] = quotes
                            if i % 100 == 0:
                                log(f"  [上櫃] 已處理 {i}/{len(payloads)} 檔 ...")
            _save()

    _save()

    # 由逐日/逐檔快取組裝成 {代號: 升冪日K}
    hist: dict[str, list[DailyQuote]] = {}
    for _iso, rows in twse_days.items():
        for q in rows:
            hist.setdefault(q.symbol, []).append(q)
    for sym, quotes in tpex_cache.items():
        hist.setdefault(sym, []).extend(quotes)
    for sym in hist:
        hist[sym].sort(key=lambda q: q.trade_date)
    return hist
