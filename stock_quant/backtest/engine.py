"""回測引擎 — 逐日以『收盤完成K』重用專案最新選股邏輯並模擬下單。

選股 (完全重用最新邏輯，不另寫一套規則)：
  1. 漲幅池：對全市場算當日漲幅，保留『漲幅 ≥ min_change_pct (3%) 且 收盤 ≤ 漲停前一檔』
     (= 排除已鎖漲停 = 漲停不買)。等同 IntradayRanker 的 EOD 篩選。
  2. 起漲篩選：把池子交給 stock_quant.breakout_screen.screen_breakout 跑 6 個硬條件
     (紅K、突破昨高、量增1.2、站上5MA、站上月線且月線上彎、昨日仍在5MA下)，
     並依『強度分』由高到低排序。
  3. 取強度分最高的前 max_holdings 檔進場。

下單規則：
  - 庫存上限 max_holdings 檔；部位大小依 sizing_mode (lot/max_lots/amount)。
  - 出場：當日收盤『跌破 5MA』即出場 (exit_mode='ma_break')；或硬停損 stop_loss_pct。
  - 漲停不買 (池已排除，並顯式防呆)；跌停不賣 (出場遞延到下一個非跌停日)。
  - 進出場一律用『當日收盤價』(與篩選/出場同基準)，計入手續費與證交稅。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ..breakout_screen import BreakoutConfig, ScoredRow, screen_breakout
from ..domain import DailyQuote
from ..intraday import RankRow
from ..limits import limit_up_prev_tick, limit_up_price, tick_size


def limit_down_price(prev_close: float) -> float:
    """跌停價 = 昨收×0.9，無條件進位到升降單位 (不低於 -10%)。"""
    raw = prev_close * 0.9
    t = tick_size(raw)
    return round(math.ceil(raw / t - 1e-9) * t, 2)


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


@dataclass(slots=True)
class BacktestConfig:
    start: date
    end: date
    initial_capital: float = 5_000_000.0
    max_holdings: int = 5
    min_change_pct: float = 3.0             # 漲幅池下限 (與 IntradayRanker 一致)
    # 部位大小: lot=整股固定張數 / max_lots=用現金買最大張數 / amount=每檔固定金額(零股)
    sizing_mode: str = "lot"
    lot_shares: int = 1000
    lots_per_position: int = 1
    amount_per_position: float = 100_000.0
    # 出場
    exit_mode: str = "ma_break"             # 跌破5MA 出場 (順勢)
    stop_loss_pct: float = 0.0              # 硬停損% (>0 啟用)
    ma_exit: int = 5                        # 出場均線天數 (預設5)
    # 交易成本 (台股)；要看純策略可全設 0
    fee_rate: float = 0.001425
    fee_discount: float = 1.0
    tax_rate: float = 0.003
    min_fee: float = 20.0
    history_cap: int = 60                   # 餵給篩選器的歷史根數上限 (≥月線+斜率回看即可)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)


@dataclass(slots=True)
class Trade:
    symbol: str
    market: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    shares: int
    exit_reason: str
    holding_days: int
    gross_pnl: float
    cost: float
    pnl: float
    return_pct: float
    score: float = 0.0
    note: str = ""


@dataclass(slots=True)
class _Position:
    symbol: str
    market: str
    entry_date: date
    entry_price: float
    shares: int
    entry_cost: float
    score: float
    note: str


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade]
    equity_curve: list[tuple[date, float, float, int]]            # (日期, 權益, 現金, 持股檔數)
    daily_selection: list[tuple[date, list[ScoredRow]]]           # 每日選出的 ScoredRow (前 max_holdings)
    metrics: dict = field(default_factory=dict)


class _BacktestRanker:
    """提供 screen_breakout 需要的最小介面：history(symbol) + last_source。

    以『完成日K』回測，故 last_source 固定 'eod' (breakout_screen 會用 hist[-2] 當昨日、
    hist[:-1] 當到昨日的序列)。history() 回傳『結束於當前模擬日 d』的升冪日K切片。
    """
    last_source = "eod"

    def __init__(self, history: dict[str, list[DailyQuote]],
                 idx: dict[str, dict[date, int]], cap: int):
        self._hist = history
        self._idx = idx
        self._cap = cap
        self._d: Optional[date] = None

    def set_date(self, d: date) -> None:
        self._d = d

    def history(self, symbol: str) -> list[DailyQuote]:
        i = self._idx.get(symbol, {}).get(self._d)
        if i is None:
            return []
        return self._hist[symbol][max(0, i - self._cap):i + 1]


class Backtester:
    def __init__(self, history: dict[str, list[DailyQuote]], trading_dates: list[date],
                 config: BacktestConfig):
        self.history = {s: q for s, q in history.items() if q}
        self.dates = list(trading_dates)
        self.cfg = config
        self._idx: dict[str, dict[date, int]] = {
            s: {q.trade_date: i for i, q in enumerate(ql)}
            for s, ql in self.history.items()
        }
        self._ranker = _BacktestRanker(self.history, self._idx, config.history_cap)

    # ---- 成本 / 部位 ----
    def _buy_cost(self, px: float, sh: int) -> float:
        return max(px * sh * self.cfg.fee_rate * self.cfg.fee_discount, self.cfg.min_fee)

    def _sell_cost(self, px: float, sh: int) -> float:
        fee = max(px * sh * self.cfg.fee_rate * self.cfg.fee_discount, self.cfg.min_fee)
        return fee + px * sh * self.cfg.tax_rate

    def _position_shares(self, price: float, cash: float) -> int:
        m = self.cfg.sizing_mode
        if m == "max_lots":
            lot_cost = price * self.cfg.lot_shares * (1 + self.cfg.fee_rate * self.cfg.fee_discount)
            return int(cash // lot_cost) * self.cfg.lot_shares if lot_cost > 0 else 0
        if m == "amount":
            return int(self.cfg.amount_per_position // price) if price > 0 else 0
        return self.cfg.lot_shares * self.cfg.lots_per_position

    # ---- 工具 ----
    def _quote(self, s: str, d: date) -> Optional[DailyQuote]:
        i = self._idx[s].get(d)
        return self.history[s][i] if i is not None else None

    def _ma_with_today(self, s: str, d: date, n: int) -> Optional[float]:
        i = self._idx[s].get(d)
        if i is None or i < n - 1:
            return None
        cs = [self.history[s][j].close for j in range(i - n + 1, i + 1)]
        if any(x is None for x in cs):
            return None
        return sum(float(x) for x in cs) / n

    def _last_close_upto(self, s: str, d: date) -> Optional[float]:
        ql = self.history.get(s)
        if not ql:
            return None
        last = None
        for q in ql:
            if q.trade_date > d:
                break
            if q.close is not None:
                last = float(q.close)
        return last

    # ---- 選股: 漲幅池 + screen_breakout (重用最新邏輯) ----
    def _select(self, d: date) -> list[ScoredRow]:
        pool: list[RankRow] = []
        for s, ql in self.history.items():
            i = self._idx[s].get(d)
            if i is None or i == 0:
                continue
            today, prev = ql[i], ql[i - 1]
            cl, pc = _f(today.close), _f(prev.close)
            if cl is None or not pc:
                continue
            cp = (cl - pc) / pc * 100.0
            if cp < self.cfg.min_change_pct:                       # 漲幅 < 3%
                continue
            if cl > limit_up_prev_tick(pc) + 1e-9:                 # 已鎖漲停 → 排除 (= 漲停不買)
                continue
            pool.append(RankRow(
                symbol=s, name=today.name, market=today.market, close=cl, prev_close=pc,
                change=round(cl - pc, 2), change_pct=round(cp, 2), volume=today.volume,
                open=_f(today.open), high=_f(today.high), low=_f(today.low)))
        if not pool:
            return []
        self._ranker.set_date(d)
        now = datetime(d.year, d.month, d.day, 14, 0)               # 盤後 → EOD 模式
        return screen_breakout(self._ranker, pool, now=now, cfg=self.cfg.breakout)

    def run(self) -> BacktestResult:
        cfg = self.cfg
        cash = cfg.initial_capital
        positions: dict[str, _Position] = {}
        trades: list[Trade] = []
        equity_curve: list[tuple[date, float, float, int]] = []
        daily_selection: list[tuple[date, list[ScoredRow]]] = []
        last_date = self.dates[-1] if self.dates else None

        for d in self.dates:
            # 1) 出場 (先出場、釋放名額)
            for sym in list(positions.keys()):
                pos = positions[sym]
                q = self._quote(sym, d)
                if q is None or q.close is None:
                    continue                                        # 停牌 → 續抱
                close = float(q.close)
                ret_pct = (close - pos.entry_price) / pos.entry_price * 100.0
                reason = None
                if cfg.stop_loss_pct > 0 and ret_pct <= -cfg.stop_loss_pct:
                    reason = f"停損{cfg.stop_loss_pct:g}%"
                else:
                    ma = self._ma_with_today(sym, d, cfg.ma_exit)
                    if ma is not None and close < ma:
                        reason = f"跌破{cfg.ma_exit}MA"
                if reason is None:
                    continue
                i = self._idx[sym][d]
                prev_close = self.history[sym][i - 1].close if i > 0 else None
                if prev_close is not None and close <= limit_down_price(float(prev_close)) + 1e-9:
                    continue                                        # 跌停不賣 → 遞延
                sell_cost = self._sell_cost(close, pos.shares)
                cash += close * pos.shares - sell_cost
                trades.append(self._make_trade(pos, d, close, reason, sell_cost))
                del positions[sym]

            # 2) 進場 (最新選股: 漲幅池 + screen_breakout 強度分排序)
            scored = self._select(d)
            daily_selection.append((d, scored[:cfg.max_holdings]))
            for sr in scored:
                if len(positions) >= cfg.max_holdings:
                    break
                sym = sr.row.symbol
                if sym in positions:
                    continue
                close = float(sr.row.close)
                i = self._idx[sym][d]
                prev_close = self.history[sym][i - 1].close if i > 0 else None
                if prev_close is not None and close >= limit_up_price(float(prev_close)) - 1e-9:
                    continue                                        # 漲停不買 (防呆)
                shares = self._position_shares(close, cash)
                if shares <= 0:
                    continue
                buy_cost = self._buy_cost(close, shares)
                if close * shares + buy_cost > cash:
                    continue
                cash -= close * shares + buy_cost
                note = " / ".join(sr.reasons) if sr.reasons else ""
                positions[sym] = _Position(sym, sr.row.market.value, d, close, shares,
                                           buy_cost, sr.score, note)

            # 3) 期末強制平倉
            if d == last_date:
                for sym in list(positions.keys()):
                    pos = positions[sym]
                    close = self._last_close_upto(sym, d) or pos.entry_price
                    sell_cost = self._sell_cost(close, pos.shares)
                    cash += close * pos.shares - sell_cost
                    trades.append(self._make_trade(pos, d, close, "期末平倉", sell_cost))
                    del positions[sym]

            # 4) 當日權益
            mv = sum(p.shares * (self._last_close_upto(s, d) or p.entry_price)
                     for s, p in positions.items())
            equity_curve.append((d, cash + mv, cash, len(positions)))

        return BacktestResult(cfg, trades, equity_curve, daily_selection,
                              self._metrics(cfg, trades, equity_curve))

    def _make_trade(self, pos: _Position, exit_date: date, exit_price: float,
                    reason: str, sell_cost: float) -> Trade:
        gross = (exit_price - pos.entry_price) * pos.shares
        cost = pos.entry_cost + sell_cost
        pnl = gross - cost
        basis = pos.entry_price * pos.shares
        hold = sum(1 for d in self.dates if pos.entry_date < d <= exit_date)
        return Trade(pos.symbol, pos.market, pos.entry_date, round(pos.entry_price, 2),
                     exit_date, round(exit_price, 2), pos.shares, reason, hold,
                     round(gross, 0), round(cost, 0), round(pnl, 0),
                     round(pnl / basis * 100, 2) if basis else 0.0,
                     round(pos.score, 1), pos.note)

    @staticmethod
    def _metrics(cfg, trades, equity_curve) -> dict:
        n = len(trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        gw = sum(t.pnl for t in wins)
        gl = -sum(t.pnl for t in losses)
        final = equity_curve[-1][1] if equity_curve else cfg.initial_capital
        peak = -1e18
        max_dd = 0.0
        for _d, eq, _c, _n in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        cagr = None
        if equity_curve:
            days = (equity_curve[-1][0] - equity_curve[0][0]).days
            if days > 0 and cfg.initial_capital > 0:
                cagr = (final / cfg.initial_capital) ** (365.0 / days) - 1.0
        return {
            "起始資金": cfg.initial_capital,
            "期末權益": round(final, 0),
            "總損益": round(final - cfg.initial_capital, 0),
            "總報酬率%": round((final / cfg.initial_capital - 1) * 100, 2) if cfg.initial_capital else 0,
            "年化報酬率%": round(cagr * 100, 2) if cagr is not None else None,
            "最大回撤%": round(max_dd * 100, 2),
            "交易次數": n,
            "勝率%": round(len(wins) / n * 100, 2) if n else 0,
            "獲利次數": len(wins),
            "虧損次數": len(losses),
            "平均每筆報酬%": round(sum(t.return_pct for t in trades) / n, 2) if n else 0,
            "平均獲利": round(gw / len(wins), 0) if wins else 0,
            "平均虧損": round(-gl / len(losses), 0) if losses else 0,
            "獲利因子": round(gw / gl, 2) if gl else None,
            "平均持有天數": round(sum(t.holding_days for t in trades) / n, 1) if n else 0,
            "總交易成本": round(sum(t.cost for t in trades), 0),
        }
