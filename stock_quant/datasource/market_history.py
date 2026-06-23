"""全市場歷史日K 組裝 — 為「全市場」設計，盡量減少請求並避免 TWSE 限流。

TWSE MI_INDEX 對連續請求限流很嚴，觸發後會把 IP 擋上「好幾分鐘」。因此本模組:
  * 每天只打 1 次請求 (rwd 新端點；失敗才退舊端點)，不在封鎖窗內快速重試浪費。
  * 每天之間留較大間隔 (delay 預設 3s)，盡量不要觸發限流。
  * 被擋時冷卻後重試 (cooldowns)；連續多天都被擋就「判定 IP 被鎖、停止抓取」，
    不再硬敲 (硬敲只會養大封鎖)。
  * 逐日『增量快取 + 續抓』: 抓到的每個交易日存進快取；下次啟動由最新往回補「還沒抓到的日子」，
    所以最後交易日永遠會被補上 (被擋而中斷時，過幾分鐘再跑就會接著補齊)。

上櫃用「OTC 個股清單 + 逐檔抓歷史」(history.get_history，新版端點)，多進程加速、一併快取;
快取落後最新交易日時會重抓 (避免一直沿用舊資料)。
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
        ni = idx.get("證券名稱")
        oi, hi, li = idx.get("開盤價"), idx.get("最高價"), idx.get("最低價")
        vi = idx.get("成交股數")
        for r in t.get("data", []):
            code = str(_g(r, ci) or "").strip()
            if not is_individual_stock(code):
                continue
            q = DailyQuote.normalize(symbol=code, name=str(_g(r, ni) or "").strip(),
                                     market=Market.TWSE, trade_date=d,
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


def fetch_market_eod(
    markets: Sequence[str],
    day: date,
    timeout: float = 10.0,
) -> dict[str, DailyQuote]:
    """抓某一交易日『全市場完成日K』，回傳 {代號: DailyQuote}。

    給盤後 (EOD) 模式用: 收盤後當日完成日K公布後即可取得「今日」收盤，
    併入歷史讓 history[-1] = 今日 (而非沿用昨日)。

    來源與歷史快取一致 —— 上市用 MI_INDEX (指定日期)、上櫃用 TPEx 每日收盤行情。
    **僅回傳『交易日 == day』的資料**；當日尚未公布 / 假日 / 抓取失敗時該市場回空
    (絕不混入舊資料)，呼叫端應據此 fallback 到最後一個已完成交易日。
    """
    out: dict[str, DailyQuote] = {}
    if "twse" in markets:
        try:
            for q in fetch_twse_day(day, timeout=timeout):
                if q.trade_date == day and q.is_valid():
                    out[q.symbol] = q
        except Exception:                       # noqa: BLE001 — 抓不到就回空、由上層 fallback
            pass
    if "tpex" in markets:
        from .tpex import TpexDataSource
        try:
            src = TpexDataSource(timeout=max(timeout, 20.0))
            for q in src.fetch(src.list_fetch_units()[0]):
                if q.trade_date == day and q.is_valid():
                    out[q.symbol] = q
        except Exception:                       # noqa: BLE001
            pass
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

    # 上市: 逐日整批 (由最新往回，補齊「最近 days 個交易日」缺漏 + 限流退避)
    #
    # 重點: 由最新交易日往回掃，視窗內每遇到一個「還沒抓過」的交易日就補抓。
    # 計數用「視窗內已確認有資料的交易日數 (seen)」而非「快取總天數」——
    # 否則快取已累積很多天時會一啟動就 break，永遠抓不到最新一天 (最後交易日就會過時)。
    if "twse" in markets:
        if twse_days:
            log(f"  [上市] 快取已有 {len(twse_days)} 個交易日，補抓最新缺漏 ...")
        consecutive_fail = 0
        seen = 0          # 由最新往回數，本視窗內已確認有資料的交易日數
        fetched = 0       # 本次實際新抓到的交易日數 (僅供日誌)
        for d in _weekdays_back(today, max_attempts=days + 40):
            if seen >= days:
                break
            iso = d.isoformat()
            if iso in twse_done:                      # 已抓過 (含假日空白) -> 不重抓
                if iso in twse_days:                  # 有資料才計入視窗
                    seen += 1
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
                seen += 1
                fetched += 1
                if fetched == 1 or fetched % 10 == 0:
                    log(f"  [上市] 已補抓 {fetched} 個交易日 (最新 {iso}) ...")
                _save()                               # 邊抓邊存 -> 中斷也不白費
            if delay:
                _time.sleep(delay)
        if not twse_days:
            log("  [上市] 一天都沒抓到 -> 可能 IP 已被限流，請等幾分鐘再重跑 (會接續)。")

    # 上櫃: OTC 清單 + 逐檔 (新版端點)，多進程加速;
    # 快取「落後最新交易日」才重抓 (否則 6/10 之類的舊資料會一直被沿用、永遠不更新)。
    if "tpex" in markets:
        # 參考的「最後交易日」: 有上市資料時用上市最新日 (同一交易行事曆)；否則退而取最近一個平日
        ref_latest = max(twse_days) if twse_days else None
        if ref_latest is None:
            wk = next(iter(_weekdays_back(today, 1)), None)
            ref_latest = wk.isoformat() if wk else None
        tpex_latest = None
        for series in tpex_cache.values():
            if series:
                di = series[-1].trade_date.isoformat()
                if tpex_latest is None or di > tpex_latest:
                    tpex_latest = di
        stale = ref_latest is not None and (tpex_latest is None or tpex_latest < ref_latest)
        if tpex_cache and not stale:
            log(f"  [上櫃] 快取已是最新 ({tpex_latest})，沿用 {len(tpex_cache)} 檔。")
        else:
            if tpex_cache and stale:
                log(f"  [上櫃] 快取最新 {tpex_latest} 落後 {ref_latest} -> 重抓最新歷史 ...")
            otc = _otc_universe(timeout=max(timeout, 20.0))
            log(f"  [上櫃] 共 {len(otc)} 檔，逐檔抓歷史 (首次/更新較久，會快取) ...")
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
