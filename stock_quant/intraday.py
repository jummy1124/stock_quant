"""盤中漲幅排行 + 篩選: 每分鐘用即時價當「今日K」算漲幅，篩出「漲幅 3% ~ 漲停前一檔」的個股。

歷史日K盤中不變 -> 啟動取得一次並快取;
資料來源依時間自動切換:
  - 交易時間 (09:00–13:30)：抓 MIS 即時價當「今日K」，與歷史(到昨日)比對算漲幅。
  - 非交易時間：用最後一個交易日的完成日K (history[-1]) 當「今日K」，與更早一日比對。

漲幅% = (今日收盤/現價 - 昨收) / 昨收 ×100。
預設篩選: 漲幅 ≥ 3% 且 收盤 ≤ 漲停前一檔 (排除已鎖漲停)；結果依漲幅由大到小排序。
回傳 list[RankRow]，由 run_intraday 印出並寫進 Excel。
⚠️ 漲幅排行為資訊參考，非投資建議。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from multiprocessing import Pool
from typing import Optional, Sequence

from .datasource import get_history
from .datasource.market_history import fetch_market_eod
from .datasource.mis import fetch_realtime
from .domain import DailyQuote, Market
from .limits import limit_up_prev_tick
from .scheduler import MarketClock


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


@dataclass(slots=True)
class RankRow:
    """一檔個股某次 tick 的漲幅排行列。"""
    symbol: str
    name: str
    market: Market
    close: Optional[float]            # 今日收盤 / 盤中現價
    prev_close: Optional[float]       # 昨收
    change: Optional[float]           # 漲跌價 = 收 - 昨收
    change_pct: Optional[float]       # 漲幅% = (收 - 昨收) / 昨收 ×100
    volume: Optional[int]             # 成交股數 (本專案統一為「股」)
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    trade_date: Optional[date] = None  # 這筆「今日K/現價」對應的交易日 (供前端確認資料日期)

    @property
    def lots(self) -> Optional[float]:
        """成交量換算成「張」(1 張 = 1000 股)。"""
        return None if self.volume is None else self.volume / 1000.0

    @property
    def result(self):
        """向後相容: notify 的彙整推播讀 row.result.change_pct / row.result.close。"""
        return self

    @property
    def stable(self) -> bool:
        """漲幅排行無穩定層，每一列都是有效資料 (給 notify 沿用)。"""
        return True

    # 向後相容: 舊程式用 (symbol, market, row) 解包仍可運作
    def __iter__(self):
        return iter((self.symbol, self.market, self))


def _change_pct(close: Optional[float], prev_close: Optional[float]) -> Optional[float]:
    if close is None or not prev_close:
        return None
    return (close - prev_close) / prev_close * 100.0


def _history_worker(payload):
    symbol, market, months = payload
    try:
        return symbol, get_history(symbol, market=market, months=months), None
    except Exception as exc:
        return symbol, None, f"{type(exc).__name__}: {exc}"


class IntradayRanker:
    """盤中漲幅排行 + 篩選引擎: 啟動載入歷史(取昨收)，每分鐘抓即時價算漲幅、篩選、排序。

    篩選 (apply_filter=True 時):
      - 漲幅 ≥ min_change_pct (預設 3%)
      - exclude_limit_up=True 時，收盤 ≤ 漲停前一檔 (排除已鎖漲停)
    name_map: {代號: 名稱}，用來補上輸出的股票名稱 (來源資料缺名稱時)。
    """

    # 盤後判定「某市場今日完成日K已公布完成」的覆蓋率門檻：該市場有歷史的個股中，
    # 已併入今日K的比例達此值才算完成 (容忍少數暫停交易/未成交個股)。
    _EOD_MARKET_DONE_RATIO = 0.9

    def __init__(self, pairs: Sequence[tuple[str, Optional[Market]]], months: int = 5,
                 batch_size: int = 40, processes: Optional[int] = None,
                 clock: Optional[MarketClock] = None,
                 eod_when_closed: bool = True,
                 apply_filter: bool = True, min_change_pct: float = 3.0,
                 exclude_limit_up: bool = True,
                 name_map: Optional[dict[str, str]] = None):
        self.pairs = list(pairs)
        self.months = months
        self.batch_size = batch_size
        self.processes = processes
        self.clock = clock or MarketClock()
        self.eod_when_closed = eod_when_closed     # 非交易時間是否改用最後交易日完成日K
        self.apply_filter = apply_filter
        self.min_change_pct = min_change_pct
        self.exclude_limit_up = exclude_limit_up
        self.name_map = name_map or {}
        self._history: dict[str, list[DailyQuote]] = {}
        self._eod_day: Optional[date] = None   # 已併入「今日完成日K」的交易日 (避免重抓重併)
        self.last_quoted = 0          # 上次 tick 實際取得即時價/可算漲幅的檔數 (覆蓋率診斷)
        self.last_matched = 0         # 上次 tick 通過篩選的檔數
        self.last_warning: Optional[str] = None   # 上次 tick 的即時報價失敗摘要 (若有)
        self.last_source = "live"     # 上次 tick 的資料來源: "live" 盤中即時 / "eod" 最後交易日

    def preload_history(self, hist_map: dict[str, list[DailyQuote]]) -> int:
        self._history = {s: q for s, q in hist_map.items() if q}
        return len(self._history)

    def history(self, symbol: str) -> list[DailyQuote]:
        """取某代號的歷史日K (由舊到新)；給下游起漲篩選算均線/昨高/昨量用。"""
        return self._history.get(symbol, [])

    def prepare(self) -> int:
        payloads = [(s, m, self.months) for s, m in self.pairs]
        if len(payloads) <= 1 or self.processes == 1:
            results = [_history_worker(p) for p in payloads]
        else:
            with Pool(processes=self.processes or min(len(payloads), 8)) as pool:
                results = pool.map(_history_worker, payloads)
        for symbol, quotes, _err in results:
            if quotes:
                self._history[symbol] = quotes
        return len(self._history)

    def tick_raw(self, now: Optional[datetime] = None) -> list[RankRow]:
        """抓一次資料算漲幅並排序，**不套用漲幅池過濾**。

        回傳全部「可算漲幅」的個股 (依漲幅由大到小)。供 --serve 把原始列存進快照、
        讓 API 端依使用者參數即時重算漲幅池/起漲篩選 (不必重抓即時價)。
        交易時間用即時價；非交易時間用最後交易日完成日K。
        """
        now = now or datetime.now()
        if self.eod_when_closed and not self.clock.is_trading(now):
            rows = self._tick_eod(now)
        else:
            rows = self._tick_live(now)
        return self._sort(rows)

    def tick(self, now: Optional[datetime] = None) -> list[RankRow]:
        """抓一次資料算漲幅、篩選、排序。交易時間用即時價；非交易時間用最後交易日完成日K。

        回傳已依漲幅由大到小排序的 RankRow 清單。apply_filter=True 時只含「漲幅 3%~漲停前一檔」。
        """
        rows = self.tick_raw(now)
        if self.apply_filter:
            rows = [r for r in rows if self._passes(r)]
        self.last_matched = len(rows)
        return self._sort(rows)

    def _passes(self, r: RankRow, min_change_pct: Optional[float] = None,
                exclude_limit_up: Optional[bool] = None) -> bool:
        """漲幅 ≥ min_change_pct 且 (可選) 收盤 ≤ 漲停前一檔。

        參數省略時沿用實例設定 (self.min_change_pct / self.exclude_limit_up)；
        帶入時即用該值，供 API 端依使用者參數重算漲幅池。
        """
        mc = self.min_change_pct if min_change_pct is None else min_change_pct
        excl = self.exclude_limit_up if exclude_limit_up is None else exclude_limit_up
        if r.change_pct is None or r.change_pct < mc:
            return False
        if excl and r.prev_close and r.close is not None:
            if r.close > limit_up_prev_tick(r.prev_close) + 1e-9:
                return False
        return True

    @classmethod
    def filter_pool(cls, rows: Sequence[RankRow], min_change_pct: float = 3.0,
                    exclude_limit_up: bool = True) -> list[RankRow]:
        """對 (未過濾的) RankRow 序列套用漲幅池條件，回傳依漲幅排序的清單。

        無狀態工具: 給 API 端用快照裡的原始列、依使用者參數即時重算漲幅池。
        """
        def _ok(r: RankRow) -> bool:
            if r.change_pct is None or r.change_pct < min_change_pct:
                return False
            if exclude_limit_up and r.prev_close and r.close is not None:
                if r.close > limit_up_prev_tick(r.prev_close) + 1e-9:
                    return False
            return True

        return cls._sort([r for r in rows if _ok(r)])

    @staticmethod
    def _sort(rows: list[RankRow]) -> list[RankRow]:
        # 依漲幅由大到小；None 視為最小，排在最後
        return sorted(rows, key=lambda r: (r.change_pct is not None, r.change_pct or 0.0),
                      reverse=True)

    def _tick_live(self, now: datetime) -> list[RankRow]:
        """交易時間: 抓即時價當今日K，與昨收算漲幅。"""
        self.last_source = "live"
        self.last_warning = None

        def _warn(msg: str) -> None:
            self.last_warning = msg
        live = fetch_realtime(self.pairs, batch_size=self.batch_size,
                              processes=self.processes, on_error=_warn)
        live_by = {q.symbol: q for q in live}
        self.last_quoted = len(live_by)
        out: list[RankRow] = []
        for symbol, market in self.pairs:
            hist = self._history.get(symbol)
            lq = live_by.get(symbol)
            # 算漲幅需要「昨收(歷史)」與「今日現價(即時)」; 缺一就跳過
            if not hist or lq is None:
                continue
            prev_close = _f(hist[-1].close)
            close = _f(lq.close)
            name = lq.name or hist[-1].name
            out.append(self._row(symbol, lq.market, name, lq, prev_close, close))
        return out

    def _markets_with_today(self, day: date) -> set[str]:
        """回傳『今日完成日K已大致併入歷史』的市場集合。

        以「該市場有歷史的個股中，history[-1] 已達 day 的比例」判斷，達門檻
        (``_EOD_MARKET_DONE_RATIO``) 才算該市場今日已公布完成。

        **不可用「該市場有任一檔到今日」判斷**：交易所盤後常先公布一部分個股、稍後
        才補齊其餘個股；若一見到第一檔就把整個市場標記完成，後面陸續公布的個股會被
        永遠卡在前一交易日（且該市場不再被重抓）。改用覆蓋率門檻後，部分公布時該市場
        仍視為未完成，下個 cycle 會繼續補抓、把落後的個股一併補上；少數整天未成交/
        暫停交易的個股不會把比例壓到門檻以下，故仍能正常收斂、不致無限重抓。
        """
        total: dict[str, int] = {}
        at_day: dict[str, int] = {}
        for symbol, market in self.pairs:
            if market is None:
                continue
            hist = self._history.get(symbol)
            if not hist:
                continue
            key = market.name.lower()
            total[key] = total.get(key, 0) + 1
            last = hist[-1].trade_date
            if last is not None and last >= day:
                at_day[key] = at_day.get(key, 0) + 1
        done: set[str] = set()
        for key, n in total.items():
            if n > 0 and at_day.get(key, 0) / n >= self._EOD_MARKET_DONE_RATIO:
                done.add(key)
        return done

    def _merge_eod_today(self, day: date) -> int:
        """盤後把『今日完成日K』(全市場) 併入歷史，使 history[-1] = 今日。

        修正先前「收盤後仍顯示昨日」的問題: 全市場歷史 (market_history) 刻意跳過今天、
        且只在啟動載一次，故收盤後 EOD 模式用的 history[-1] 其實是昨日。這裡在收盤後
        補抓今日完成日K (來源與歷史一致、且做日期核對) 併入，讓 history[-1] 變回今日。

        **只對『今日尚未併齊』的市場重抓** (上市 MI_INDEX 較易被限流、且常比上櫃晚公布)：
        市場以覆蓋率門檻判斷是否已完成 (見 _markets_with_today)，唯有所有市場都達門檻後
        才標記本日完成 (self._eod_day)。否則保持未標記，下個 cycle 繼續補抓仍缺今日的市場
        與個股 —— 避免某市場暫時抓不到、或只先公布一部分個股時，落後者整天停在前一交易日。
        今日資料尚未公布 / 抓不齊 / 抓取失敗時，把待補狀況寫進 last_warning（讓 /api/meta
        看得到，而非靜默沿用昨日資料），並 fallback 顯示最後一個已完成交易日。回傳本次新併入檔數。
        """
        if self._eod_day == day:
            return 0
        all_markets = ({m.name.lower() for _s, m in self.pairs if m is not None}
                       or {"twse", "tpex"})
        pending = all_markets - self._markets_with_today(day)
        if not pending:                                 # 所有市場今日都已併齊 -> 標記完成
            self._eod_day = day
            return 0
        try:
            eod = fetch_market_eod(tuple(sorted(pending)), day)
        except Exception as exc:                        # noqa: BLE001 — 抓取失敗不致命
            self.last_warning = f"盤後當日完成日K抓取失敗: {type(exc).__name__}: {exc}"
            return 0
        merged = 0
        for symbol, q in eod.items():
            hist = self._history.get(symbol)
            if not hist:
                continue                                # 無歷史(昨收) 就無從算漲幅，略過
            last = hist[-1].trade_date
            if last is not None and last >= day:
                continue                                # 今日已在歷史中 -> 不重複併入
            if getattr(q, "trade_date", None) != day:
                continue                                # 防呆: 只併入確為今日的完成日K
            hist.append(q)
            merged += 1
        # 唯有所有市場今日都達覆蓋率門檻才標記完成；否則寫 warning 並讓下個 cycle 繼續補。
        still_pending = all_markets - self._markets_with_today(day)
        if not still_pending:
            self._eod_day = day
        else:
            _miss = ", ".join(sorted(m.upper() for m in still_pending))
            self.last_warning = f"盤後今日完成日K尚未併齊，下列市場仍沿用最後交易日: {_miss}"
        return merged

    def _tick_eod(self, now: datetime) -> list[RankRow]:
        """非交易時間: 用最後一個交易日的完成日K (history[-1])，與前一日算漲幅。

        收盤後先補抓並併入『今日完成日K』(_merge_eod_today)，使 history[-1] = 今日；
        今日尚未公布時則 fallback 用最後一個已完成交易日。
        """
        self.last_source = "eod"
        self.last_warning = None
        self._merge_eod_today(now.date())               # 盤後併入今日完成日K (一天一次)
        out: list[RankRow] = []
        for symbol, market in self.pairs:
            hist = self._history.get(symbol)
            if not hist or len(hist) < 2:               # 需至少「今日(最後交易日)+前一日」
                continue
            today, prev = hist[-1], hist[-2]
            prev_close = _f(prev.close)
            close = _f(today.close)
            out.append(self._row(symbol, today.market, today.name, today, prev_close, close))
        self.last_quoted = len(out)
        return out

    def _row(self, symbol: str, market: Market, name: str, q: DailyQuote,
             prev_close: Optional[float], close: Optional[float]) -> RankRow:
        cp = _change_pct(close, prev_close)
        change = (close - prev_close) if (close is not None and prev_close is not None) else None
        return RankRow(
            symbol=symbol, name=self.name_map.get(symbol) or name or "", market=market,
            close=close, prev_close=prev_close,
            change=round(change, 2) if change is not None else None,
            change_pct=round(cp, 2) if cp is not None else None,
            volume=q.volume,
            open=_f(q.open), high=_f(q.high), low=_f(q.low),
            trade_date=getattr(q, "trade_date", None),
        )
