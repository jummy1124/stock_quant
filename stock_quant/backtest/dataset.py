"""回測資料層 — 取得整段回測區間的「全市場完成日K」。

重點：
  * **完成日K (盤後定案資料)**，所以回測完全用收盤價，不碰盤中即時。
  * 上市：逐日整批抓 MI_INDEX (reuse market_history.fetch_twse_day)。
  * 上櫃：OTC 清單 + 逐檔抓歷史 (reuse history.get_history)。
  * 逐日/逐檔『增量快取 + 續抓』，被 TWSE 限流中斷也能多跑幾次補齊。
  * 需要『暖身期』：選股規則 7 (多頭趨勢) 要 ≥60 個交易日歷史，故實際抓取會從
    回測起日往前多抓 warmup 天，模擬迴圈才從正式起日開始。

⚠️ TWSE MI_INDEX 對連續請求限流很嚴：請求間需放慢 (twse_delay)，被擋時分段冷卻重試，
   整批被鎖時長休息 (block_cooldown) 後自動續抓 —— 一次執行即可跑完 (只是較慢)。
   邊抓邊存快取，中途 Ctrl-C 或被擋也不白費，重跑會接續。
"""
from __future__ import annotations

import pickle
from datetime import date, timedelta
from multiprocessing import Pool
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..domain import DailyQuote, Market

_CACHE_VERSION = 1   # 回測專用快取版本


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:        # 只排除週末 (假日靠 API 回空自然略過)
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
    """從盤中專案的既有快取 (.cache/history.pkl, ver4) 種子化，省去重抓已有資料。"""
    if not seed_path or not Path(seed_path).exists():
        return {}, set(), {}
    try:
        with open(seed_path, "rb") as f:
            c = pickle.load(f)
        if c.get("ver") != 4:
            return {}, set(), {}
        return (dict(c.get("twse_days", {})),
                set(c.get("twse_done", [])),
                dict(c.get("tpex", {})))
    except Exception:
        return {}, set(), {}


def _drop_bad_ticks(seq: list, max_move: float) -> tuple[list, int]:
    """剔除明顯錯誤的收盤價：單日相對『前一筆已保留收盤』漲跌超過 max_move。

    台股有 ±10% 漲跌停上限，任何單日 >±11% 的跳動在正常交易下不可能 → 視為來源髒資料。
    以『前一筆已保留收盤』為基準，故單一尖刺被剔除後，後續仍與尖刺前的價比較 (不會連鎖誤刪)。
    """
    if max_move <= 0 or len(seq) < 2:
        return seq, 0
    out: list = []
    last_close = None
    dropped = 0
    for q in seq:
        c = float(q.close) if q.close is not None else None
        if c is None or last_close is None or last_close <= 0:
            out.append(q)
            if c is not None:
                last_close = c
            continue
        if abs(c / last_close - 1.0) > max_move:    # 超出漲跌停上限 → 髒資料，跳過
            dropped += 1
            continue
        out.append(q)
        last_close = c
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
    warmup_days: int = 95,
    max_daily_move: float = 0.11,           # 資料清洗: 單日漲跌 > 此值視為髒資料剔除 (台股±10%上限)
    twse_timeout: float = 12.0,
    twse_delay: float = 5.0,
    twse_cooldowns: Sequence[float] = (20.0, 60.0, 120.0),
    max_consecutive_fail: int = 4,
    block_cooldown: float = 300.0,
    max_blocks: int = 10,
    processes: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[dict[str, list[DailyQuote]], list[date]]:
    """回傳 (history, trading_dates)。

    history       : {代號: 時間升冪 DailyQuote 序列} (含暖身期，故起點早於 start)。
    trading_dates : 落在 [start, end] 且全市場有資料的交易日 (升冪) — 模擬迴圈據此逐日跑。
    """
    log = progress or (lambda _m: None)
    fetch_start = start - timedelta(days=warmup_days + 7)   # 多抓暖身 (給 MA60/趨勢判定)

    # --- 快取 (種子: 沿用盤中專案既有快取，減少重抓) ---
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
            """抓單日；單日內被擋就分段冷卻重試，仍失敗回 None。"""
            for attempt in range(len(twse_cooldowns) + 1):
                try:
                    return fetch_twse_day(d, timeout=twse_timeout)
                except Exception as exc:
                    if attempt < len(twse_cooldowns):
                        cd = twse_cooldowns[attempt]
                        log(f"  [上市] {d} 被擋，{cd:.0f}s 後重試 "
                            f"({attempt + 1}/{len(twse_cooldowns)}) ...")
                        _time.sleep(cd)
                    else:
                        log(f"  [上市] {d} 暫時放棄: {exc}")
            return None

        need = [d for d in _weekdays(fetch_start, end) if d.isoformat() not in twse_done]
        log(f"  [上市] 需抓 {len(need)} 個交易日 (已快取 {len(twse_done)}；"
            f"TWSE 限流嚴，每筆間隔約 {twse_delay:.0f}s，會自動續抓) ...")
        consecutive_fail = 0
        blocks = 0
        done = 0
        i = 0
        while i < len(need):
            d = need[i]
            rows = _fetch_one_day(d)
            if rows is None:                       # 這天連分段冷卻後仍失敗
                consecutive_fail += 1
                if consecutive_fail >= max_consecutive_fail:
                    blocks += 1
                    if blocks > max_blocks:
                        log(f"  [上市] 已長休息 {max_blocks} 次仍被擋 → 停止 "
                            f"(已抓 {done} 天皆快取，稍後重跑會自動接續)。")
                        break
                    log(f"  [上市] 連續 {consecutive_fail} 天被擋 → 判定 IP 被 TWSE 暫鎖，"
                        f"長休息 {block_cooldown:.0f}s 後續抓 (第 {blocks}/{max_blocks} 次) ...")
                    _time.sleep(block_cooldown)
                    consecutive_fail = 0           # 休息後重試同一天 (不前進 i)
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
            _time.sleep(twse_delay)                # 禮貌性延遲 (避免連珠炮被封)
        _save()

    if fetch and "tpex" in markets and not tpex_cache:
        from ..datasource.tpex import TpexDataSource
        months = _months_between(fetch_start, end) + 1
        try:
            src = TpexDataSource(timeout=max(twse_timeout, 20.0))
            otc = [q.symbol for q in src.fetch(src.list_fetch_units()[0])]
        except Exception:
            otc = []
        log(f"  [上櫃] {len(otc)} 檔，逐檔抓 {months} 個月歷史 (首次較久) ...")
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
                    for i, (sym, q) in enumerate(pool.imap_unordered(_otc_worker, payloads), 1):
                        if q:
                            tpex_cache[sym] = q
                        if i % 100 == 0:
                            log(f"  [上櫃] {i}/{len(payloads)} ...")
        _save()

    # --- 由逐日(上市)/逐檔(上櫃)快取，組裝成 {代號: 升冪日K} ---
    hist: dict[str, list[DailyQuote]] = {}
    if "twse" in markets:
        for _iso, rows in twse_days.items():
            for q in rows:
                hist.setdefault(q.symbol, []).append(q)
    if "tpex" in markets:
        for sym, quotes in tpex_cache.items():
            hist.setdefault(sym, []).extend(quotes)

    # 去重 (同代號同日只留一筆) + 升冪排序 + 限制在 [fetch_start, end] + 清洗髒價
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
        log(f"  [清洗] 剔除 {dropped} 筆異常價 (單日漲跌 > ±{max_daily_move * 100:.0f}%, "
            f"超出漲跌停 = 來源髒資料)")

    # 交易日 = 落在 [start, end] 且有任何個股資料的日子
    all_dates = set()
    for quotes in hist.values():
        for q in quotes:
            if start <= q.trade_date <= end:
                all_dates.add(q.trade_date)
    trading_dates = sorted(all_dates)
    return hist, trading_dates
