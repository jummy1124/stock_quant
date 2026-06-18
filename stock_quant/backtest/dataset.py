"""回測資料層 — 取得整段回測區間的『全市場完成日K』。

  * 上市：逐日整批抓 MI_INDEX (reuse datasource.market_history.fetch_twse_day)。
  * 上櫃：OTC 清單 + 逐檔抓歷史 (reuse datasource.history.get_history)。
  * 逐日/逐檔『增量快取 + 續抓』，被 TWSE 限流中斷也能多跑幾次補齊。
  * 暖身期：選股要算月線(20MA)+斜率回看，故抓取從回測起日往前多抓 warmup 天。
  * 髒價清洗：單日相對前一交易日漲跌 > ±max_daily_move (超出 ±10% 漲跌停) 視為來源錯誤剔除。

⚠️ 沙盒環境無法連 TWSE/TPEX (403)。本機 fetch=True 會自動抓+快取；離線時 fetch=False 用既有快取。
"""
from __future__ import annotations

import pickle
from datetime import date, timedelta
from multiprocessing import Pool
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..domain import DailyQuote, Market

_CACHE_VERSION = 1


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _read_cache(path: Optional[str]) -> dict:
    if not path or not Path(path).exists():
        return {}
    try:
        with open(path, "rb") as f:
            c = pickle.load(f)
        if c.get("ver") == _CACHE_VERSION:
            return c
    except Exception:
        pass
    return {}


def _seed_from_legacy(seed_path: Optional[str]) -> tuple[dict, set, dict]:
    """從盤中專案既有快取 (.cache/history.pkl, ver4) 種子化，省去重抓。"""
    if not seed_path or not Path(seed_path).exists():
        return {}, set(), {}
    try:
        with open(seed_path, "rb") as f:
            c = pickle.load(f)
        if c.get("ver") != 4:
            return {}, set(), {}
        return (dict(c.get("twse_days", {})), set(c.get("twse_done", [])),
                dict(c.get("tpex", {})))
    except Exception:
        return {}, set(), {}


def _drop_bad_ticks(seq: list, max_move: float) -> tuple[list, int]:
    """剔除單日相對『前一筆已保留收盤』漲跌超過 max_move 的明顯錯誤價 (台股 ±10% 上限)。"""
    if max_move <= 0 or len(seq) < 2:
        return seq, 0
    out: list = []
    last = None
    dropped = 0
    for q in seq:
        c = float(q.close) if q.close is not None else None
        if c is None or last is None or last <= 0:
            out.append(q)
            if c is not None:
                last = c
            continue
        if abs(c / last - 1.0) > max_move:
            dropped += 1
            continue
        out.append(q)
        last = c
    return out, dropped


def _otc_worker(payload):
    symbol, months = payload
    from ..datasource.history import get_history
    try:
        return symbol, get_history(symbol, market=Market.TPEX, months=months)
    except Exception:
        return symbol, []


def build_history(
    start: date,
    end: date,
    *,
    fetch: bool = True,
    cache_path: Optional[str] = None,
    seed_cache_path: Optional[str] = None,
    markets: Sequence[str] = ("twse", "tpex"),
    warmup_days: int = 60,
    max_daily_move: float = 0.11,
    twse_timeout: float = 12.0,
    twse_delay: float = 5.0,
    twse_cooldowns: Sequence[float] = (20.0, 60.0, 120.0),
    max_consecutive_fail: int = 4,
    block_cooldown: float = 300.0,
    max_blocks: int = 10,
    processes: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[dict[str, list[DailyQuote]], list[date]]:
    """回傳 (history, trading_dates)。history 含暖身期；trading_dates 為 [start,end] 內有資料的交易日。"""
    log = progress or (lambda _m: None)
    fetch_start = start - timedelta(days=warmup_days + 14)

    cache = _read_cache(cache_path)
    s_days, s_done, s_tpex = _seed_from_legacy(seed_cache_path)
    twse_days: dict[str, list[DailyQuote]] = {**s_days, **cache.get("twse_days", {})}
    twse_done: set[str] = set(cache.get("twse_done", [])) | s_done
    tpex_cache: dict[str, list[DailyQuote]] = {**s_tpex, **cache.get("tpex", {})}

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

    if fetch and "twse" in markets:
        from ..datasource.market_history import fetch_twse_day
        import time as _time

        def _fetch_one_day(d: date):
            for attempt in range(len(twse_cooldowns) + 1):
                try:
                    return fetch_twse_day(d, timeout=twse_timeout)
                except Exception as exc:
                    if attempt < len(twse_cooldowns):
                        cd = twse_cooldowns[attempt]
                        log(f"  [上市] {d} 被擋，{cd:.0f}s 後重試 ({attempt + 1}/{len(twse_cooldowns)}) ...")
                        _time.sleep(cd)
                    else:
                        log(f"  [上市] {d} 暫時放棄: {exc}")
            return None

        need = [d for d in _weekdays(fetch_start, end) if d.isoformat() not in twse_done]
        log(f"  [上市] 需抓 {len(need)} 個交易日 (已快取 {len(twse_done)}) ...")
        consecutive_fail = blocks = done = 0
        i = 0
        while i < len(need):
            d = need[i]
            rows = _fetch_one_day(d)
            if rows is None:
                consecutive_fail += 1
                if consecutive_fail >= max_consecutive_fail:
                    blocks += 1
                    if blocks > max_blocks:
                        log(f"  [上市] 已長休息 {max_blocks} 次仍被擋 → 停止 (已抓的會快取，稍後重跑接續)。")
                        break
                    log(f"  [上市] 連續被擋 → 長休息 {block_cooldown:.0f}s 後續抓 ({blocks}/{max_blocks}) ...")
                    _time.sleep(block_cooldown)
                    consecutive_fail = 0
                continue
            consecutive_fail = 0
            twse_done.add(d.isoformat())
            if rows:
                twse_days[d.isoformat()] = rows
            done += 1
            if done % 10 == 0:
                log(f"  [上市] 已抓 {done}/{len(need)} ...")
                _save()
            i += 1
            _time.sleep(twse_delay)
        _save()

    if fetch and "tpex" in markets and not tpex_cache:
        from ..datasource.tpex import TpexDataSource
        months = _months_between(fetch_start, end) + 1
        try:
            src = TpexDataSource(timeout=max(twse_timeout, 20.0))
            otc = [q.symbol for q in src.fetch(src.list_fetch_units()[0])]
        except Exception:
            otc = []
        log(f"  [上櫃] {len(otc)} 檔，逐檔抓 {months} 個月歷史 ...")
        payloads = [(s, months) for s in otc]
        if payloads:
            n = processes or min(len(payloads), 8)
            if n == 1:
                for p in payloads:
                    sym, q = _otc_worker(p)
                    if q:
                        tpex_cache[sym] = q
            else:
                with Pool(processes=n) as pool:
                    for k, (sym, q) in enumerate(pool.imap_unordered(_otc_worker, payloads), 1):
                        if q:
                            tpex_cache[sym] = q
                        if k % 100 == 0:
                            log(f"  [上櫃] {k}/{len(payloads)} ...")
        _save()

    # 組裝 {代號: 升冪日K}
    hist: dict[str, list[DailyQuote]] = {}
    if "twse" in markets:
        for _iso, rows in twse_days.items():
            for q in rows:
                hist.setdefault(q.symbol, []).append(q)
    if "tpex" in markets:
        for sym, quotes in tpex_cache.items():
            hist.setdefault(sym, []).extend(quotes)

    # 去重 + 升冪 + 限制區間 + 髒價清洗
    dropped = 0
    for sym, quotes in hist.items():
        by_date: dict[date, DailyQuote] = {}
        for q in quotes:
            if fetch_start <= q.trade_date <= end:
                by_date[q.trade_date] = q
        seq = [by_date[d] for d in sorted(by_date)]
        seq, dd = _drop_bad_ticks(seq, max_daily_move)
        dropped += dd
        hist[sym] = seq
    hist = {s: q for s, q in hist.items() if q}
    if dropped:
        log(f"  [清洗] 剔除 {dropped} 筆異常價 (單日漲跌 > ±{max_daily_move * 100:.0f}%)")

    all_dates = set()
    for quotes in hist.values():
        for q in quotes:
            if start <= q.trade_date <= end:
                all_dates.add(q.trade_date)
    return hist, sorted(all_dates)
