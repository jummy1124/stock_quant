"""回測引擎 — 逐日以『收盤價』重用 BreakoutScreen 選股並模擬下單。

完全對齊使用者規範：
  1. 以『當日收盤價』完成的日K 做篩選 (session_fraction=1.0，與盤後 EOD 模式一致)。
  2. 當日通過 >5 檔時，以『成交量優先 (複合)』排序選最多五檔：
        第一優先 成交量(股) ↓ → 第二 漲幅% ↓ → 第三 上影線% ↑ (越小越好)。
  3. 進場 = 當日選出的個股；庫存上限五檔，每檔買進『一張 (1000股)』，不加碼同一檔。
  4. 每日檢查庫存：當日『收盤價跌破五日均線 (MA5，含當日)』即出場。
  5. 收盤『漲停』不買進；收盤『跌停』不賣出 (出場訊號遞延到下一個非跌停日)。

成交價：進、出場一律用『當日收盤價』(與篩選/出場規則同基準)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..analysis import BreakoutScreen, ScreenResult
from ..analysis.screen import limit_up_price, tick_size
from ..domain import DailyQuote, Market


def limit_down_price(prev_close: float) -> float:
    """跌停價 = 昨收×0.9，無條件進位到升降單位 (不低於 -10%)。"""
    raw = prev_close * 0.9
    t = tick_size(raw)
    return round(math.ceil(raw / t - 1e-9) * t, 2)


@dataclass(slots=True)
class BacktestConfig:
    start: date
    end: date
    initial_capital: float = 5_000_000.0   # 需可同時容納 max_holdings 檔的部位
    max_holdings: int = 5
    # 部位大小模式:
    #   lot      = 整股，每檔買 lot_shares×lots_per_position 股 (原規範: 一張1000股)
    #   max_lots = 整股，用可用現金買「最大可買張數」(股價>本金則買不進)
    #   shares   = 零股，每檔固定買 shares_per_position 股 (可為任意股數, e.g. 100)
    #   amount   = 零股，每檔投入約 amount_per_position 元 -> 股數=floor(金額/收盤價)
    sizing_mode: str = "lot"
    lot_shares: int = 1000                  # 一張 = 1000 股 (lot 模式)
    lots_per_position: int = 1              # 每檔幾張 (lot 模式)
    shares_per_position: int = 100          # 每檔固定股數 (shares 模式)
    amount_per_position: float = 100_000.0  # 每檔投入金額 (amount 模式)
    max_position_pct: float = 1.0           # 單檔部位上限(佔總權益)。<1 啟用分散，e.g. 0.2=每檔≤20%
    require_uptrend: bool = True            # 是否啟用第7條多頭趨勢閘門 (預設啟用，同原專案)
    # 交易成本 (台股)；要看『純策略』可全設 0
    fee_rate: float = 0.001425              # 手續費率
    fee_discount: float = 1.0               # 手續費折扣 (e.g. 0.6 = 六折)；1.0 = 不打折
    tax_rate: float = 0.003                 # 證交稅 (僅賣出)
    min_fee: float = 20.0                   # 單筆最低手續費
    history_cap: int = 80                   # 餵給篩選器的歷史根數上限 (≥60 即可判趨勢，限長加速)
    # 選股排序 (>5 檔時取前五的優先序)。回測實測: change(漲幅優先) 報酬與獲利因子優於 volume。
    #   volume   = 成交量優先 (原規範)
    #   change   = 漲幅%優先 (實測最佳: 27.4%→31.4%, PF 1.9→2.05, 回撤略降)
    #   turnover = 成交金額優先
    rank_mode: str = "volume"
    min_entry_volume: int = 0               # 進場最低成交量(股)。>0 可擋流動性差標的、壓低回撤(但報酬也降)
    stop_loss_pct: float = 0.0              # 硬停損% (>0 啟用)。報酬 ≤ -此值即出場
    # 出場模式:
    #   ma_break = 跌破 MA5 出場 (突破/順勢策略用，原規範)
    #   revert   = 均值回歸出場：回到 MA5 之上『獲利了結』 / 觸停損 / 抱滿 time_stop_days 天
    exit_mode: str = "ma_break"
    time_stop_days: int = 10                # revert 模式: 最長持有天數 (到期未達標就出場)
    # 大盤多頭過濾: 啟用後，只有在「全市場等權指數 > 自身 market_ma 日均線」的日子才進場。
    # 用全市場每日橫斷面平均報酬組成等權指數當大盤proxy (不需額外指數資料源)。
    market_filter: bool = False
    market_ma: int = 20


@dataclass(slots=True)
class Trade:
    symbol: str
    market: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    shares: int
    exit_reason: str           # 跌破MA5 / 期末平倉
    holding_days: int
    gross_pnl: float           # 未計成本損益
    cost: float                # 進+出總成本 (手續費+證交稅)
    pnl: float                 # 淨損益
    return_pct: float          # 淨報酬率 % (相對進場市值)
    entry_rank_note: str = ""  # 進場當日的選股排序依據快照


@dataclass(slots=True)
class _Position:
    symbol: str
    market: str
    entry_date: date
    entry_price: float
    shares: int
    entry_cost: float
    rank_note: str


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade]
    equity_curve: list[tuple[date, float, float, int]]   # (日期, 權益, 現金, 持股檔數)
    daily_selection: list[tuple[date, list[tuple[str, ScreenResult, int]]]]  # 每日選出的(代號,結果,量)
    metrics: dict = field(default_factory=dict)


class Backtester:
    def __init__(self, history: dict[str, list[DailyQuote]], trading_dates: list[date],
                 config: BacktestConfig, screen: Optional[BreakoutScreen] = None):
        self.history = {s: q for s, q in history.items() if q}
        self.dates = list(trading_dates)
        self.cfg = config
        self.screen = screen or BreakoutScreen(require_uptrend=config.require_uptrend)
        # 每檔: date -> index，加速「取當日K」與「取前段歷史」
        self._idx: dict[str, dict[date, int]] = {
            s: {q.trade_date: i for i, q in enumerate(q_list)}
            for s, q_list in self.history.items()
        }
        # 大盤多頭過濾用: 預先算「全市場等權指數是否站上均線」(只在啟用時計算)
        self._regime: dict[date, bool] = self._build_regime() if config.market_filter else {}

    def _build_regime(self) -> dict[date, bool]:
        """以全市場每日橫斷面平均報酬組成『等權指數』，回傳每日是否站上 market_ma 日均線。

        不需額外指數資料源；指數站上均線 = 大盤偏多頭，才允許逢低進場 (空頭段不接刀)。
        """
        from collections import defaultdict
        rsum: dict[date, float] = defaultdict(float)
        rcnt: dict[date, int] = defaultdict(int)
        for q_list in self.history.values():
            for i in range(1, len(q_list)):
                a, b = q_list[i - 1].close, q_list[i].close
                if a is not None and b is not None and float(a) > 0:
                    d = q_list[i].trade_date
                    rsum[d] += float(b) / float(a) - 1.0
                    rcnt[d] += 1
        alld = sorted({q.trade_date for ql in self.history.values() for q in ql})
        levels: list[tuple[date, float]] = []
        lvl = 1.0
        for d in alld:
            if rcnt.get(d):
                lvl *= (1.0 + rsum[d] / rcnt[d])
            levels.append((d, lvl))
        n = max(2, self.cfg.market_ma)
        vals = [l for _, l in levels]
        regime: dict[date, bool] = {}
        for k, (d, l) in enumerate(levels):
            regime[d] = True if k + 1 < n else (l > sum(vals[k - n + 1:k + 1]) / n)
        return regime

    # ---- 部位大小 (整股 / 零股) ----
    def _position_shares(self, price: float, cash: float = 0.0, equity: float = 0.0) -> int:
        """依 sizing_mode 算每檔買進股數。amount/shares 模式允許零股 (不湊整張)。

        max_position_pct (<1.0) 啟用時，再把單檔部位市值壓到 ≤ pct×總權益 ——
        這是分散風險的關鍵: 配合 max_holdings>1, 才不會第一檔就把現金用光。
        """
        m = self.cfg.sizing_mode
        odd = (m in ("amount", "shares"))     # 零股模式 (可不湊整張)
        if m == "max_lots":          # 用可用現金買最大整張數 (含手續費)；股價>本金 → 0 張
            lot_cost = price * self.cfg.lot_shares * (1 + self.cfg.fee_rate * self.cfg.fee_discount)
            shares = int(cash // lot_cost) * self.cfg.lot_shares if lot_cost > 0 else 0
        elif m == "amount":
            shares = int(self.cfg.amount_per_position // price) if price > 0 else 0
        elif m == "shares":
            shares = max(0, int(self.cfg.shares_per_position))
        else:                         # lot (整股)
            shares = self.cfg.lot_shares * self.cfg.lots_per_position
        # 單檔部位上限 (佔總權益比例)；整股模式以「張」為單位向下取整
        pct = self.cfg.max_position_pct
        if 0 < pct < 1.0 and equity > 0 and price > 0 and price * shares > pct * equity:
            cap_shares = pct * equity / price
            if odd:
                shares = int(cap_shares)
            else:
                shares = int(cap_shares // self.cfg.lot_shares) * self.cfg.lot_shares
        return shares

    # ---- 成本 ----
    def _buy_cost(self, price: float, shares: int) -> float:
        fee = price * shares * self.cfg.fee_rate * self.cfg.fee_discount
        return max(fee, self.cfg.min_fee)

    def _sell_cost(self, price: float, shares: int) -> float:
        fee = max(price * shares * self.cfg.fee_rate * self.cfg.fee_discount, self.cfg.min_fee)
        tax = price * shares * self.cfg.tax_rate
        return fee + tax

    def _quote(self, symbol: str, d: date) -> Optional[DailyQuote]:
        i = self._idx[symbol].get(d)
        return self.history[symbol][i] if i is not None else None

    def _ma5_with_today(self, symbol: str, d: date) -> Optional[float]:
        i = self._idx[symbol].get(d)
        if i is None or i < 4:
            return None
        closes = [self.history[symbol][j].close for j in range(i - 4, i + 1)]
        if any(c is None for c in closes):
            return None
        return sum(float(c) for c in closes) / 5.0

    def _last_close_upto(self, symbol: str, d: date) -> Optional[float]:
        """市值評價用：取 ≤ d 的最後一筆收盤 (停牌日沿用前值)。"""
        q_list = self.history.get(symbol)
        if not q_list:
            return None
        last = None
        for q in q_list:
            if q.trade_date > d:
                break
            if q.close is not None:
                last = float(q.close)
        return last

    # ---- 選股 (當日收盤，重用 BreakoutScreen) ----
    def _screen_day(self, d: date) -> list[tuple[str, ScreenResult, int]]:
        cap = self.cfg.history_cap
        out: list[tuple[str, ScreenResult, int]] = []
        for sym, q_list in self.history.items():
            i = self._idx[sym].get(d)
            if i is None or i == 0:
                continue
            today = q_list[i]
            vol = today.volume or 0
            if vol < self.cfg.min_entry_volume:     # 流動性門檻 (可選)
                continue
            hist = q_list[max(0, i - cap):i]    # 結束於前一交易日的歷史
            res = self.screen.check(today, hist, session_fraction=1.0)
            if res.passed:
                out.append((sym, res, vol))
        mode = self.cfg.rank_mode
        if mode == "oversold":        # 最超賣優先 (均值回歸用)：RSI↑ 越低越前面
            key = lambda t: (getattr(t[1], "rsi", 50.0) or 50.0)
        elif mode == "change":        # 漲幅優先 (突破策略最佳)：漲幅%↓ → 量↓
            key = lambda t: (-(t[1].change_pct or 0.0), -t[2])
        elif mode == "turnover":      # 成交金額優先：收盤×量↓
            key = lambda t: -((t[1].close or 0.0) * t[2])
        else:                          # volume：量↓ → 漲幅%↓ → 上影線%↑ (原規範)
            key = lambda t: (-t[2], -(t[1].change_pct or 0.0),
                             (t[1].upper_shadow_pct if t[1].upper_shadow_pct is not None else 1e9))
        out.sort(key=key)
        return out

    def run(self) -> BacktestResult:
        cfg = self.cfg
        cash = cfg.initial_capital
        positions: dict[str, _Position] = {}
        trades: list[Trade] = []
        equity_curve: list[tuple[date, float, float, int]] = []
        daily_selection: list[tuple[date, list[tuple[str, ScreenResult, int]]]] = []

        last_date = self.dates[-1] if self.dates else None
        for d in self.dates:
            # 1) 出場檢查 (先出場、釋放庫存名額)
            for sym in list(positions.keys()):
                pos = positions[sym]
                q = self._quote(sym, d)
                if q is None or q.close is None:
                    continue                       # 當日停牌 → 不動作 (續抱)
                close = float(q.close)
                ret_pct = (close - pos.entry_price) / pos.entry_price * 100.0
                # 出場訊號 (依 exit_mode)
                reason = None
                if cfg.stop_loss_pct > 0 and ret_pct <= -cfg.stop_loss_pct:
                    reason = f"停損{cfg.stop_loss_pct:g}%"
                elif cfg.exit_mode == "revert":    # 均值回歸：回MA5上獲利了結 / 時間到
                    ma5 = self._ma5_with_today(sym, d)
                    held = sum(1 for x in self.dates if pos.entry_date < x <= d)
                    if ma5 is not None and close > ma5:
                        reason = "獲利(回5日線)"
                    elif held >= cfg.time_stop_days:
                        reason = "時間出場"
                else:                              # ma_break (順勢)：跌破 MA5
                    ma5 = self._ma5_with_today(sym, d)
                    if ma5 is not None and close < ma5:
                        reason = "跌破MA5"
                if reason is None:
                    continue                       # 無出場訊號 → 續抱
                # 跌停不賣：當日收盤 ≤ 跌停價則遞延
                i = self._idx[sym][d]
                prev_close = self.history[sym][i - 1].close if i > 0 else None
                if prev_close is not None and close <= limit_down_price(float(prev_close)) + 1e-9:
                    continue
                sell_cost = self._sell_cost(close, pos.shares)
                cash += close * pos.shares - sell_cost
                trades.append(self._make_trade(pos, d, close, reason, sell_cost))
                del positions[sym]

            # 2) 進場 (填滿庫存名額)
            ranked = self._screen_day(d)
            daily_selection.append((d, ranked[:cfg.max_holdings]))
            # 大盤多頭過濾: 大盤不在多頭時，當日不進場 (僅續抱/出場)
            allow_entry = (not cfg.market_filter) or self._regime.get(d, True)
            # 進場前的總權益 (現金 + 持股市值)，給「單檔部位上限」用
            equity_now = cash + sum(p.shares * (self._last_close_upto(s, d) or p.entry_price)
                                    for s, p in positions.items())
            for sym, res, vol in ranked:
                if not allow_entry:
                    break
                if len(positions) >= cfg.max_holdings:
                    break
                if sym in positions:
                    continue
                q = self._quote(sym, d)
                if q is None or q.close is None:
                    continue
                close = float(q.close)
                i = self._idx[sym][d]
                prev_close = self.history[sym][i - 1].close if i > 0 else None
                # 漲停不買 (規則2 已排除，但顯式防呆)
                if prev_close is not None and close >= limit_up_price(float(prev_close)) - 1e-9:
                    continue
                shares = self._position_shares(close, cash, equity_now)
                if shares <= 0:                    # 買不起 (含 max_lots 股價>本金) → 跳過
                    continue
                buy_cost = self._buy_cost(close, shares)
                need = close * shares + buy_cost
                if need > cash:
                    continue                       # 現金不足 → 跳過此檔
                cash -= need
                positions[sym] = _Position(
                    symbol=sym, market=q.market.value, entry_date=d, entry_price=close,
                    shares=shares, entry_cost=buy_cost, rank_note=self._rank_note(res, vol))

            # 3) 期末強制平倉 (最後一個交易日)
            if d == last_date:
                for sym in list(positions.keys()):
                    pos = positions[sym]
                    close = self._last_close_upto(sym, d) or pos.entry_price
                    sell_cost = self._sell_cost(close, pos.shares)
                    cash += close * pos.shares - sell_cost
                    trades.append(self._make_trade(pos, d, close, "期末平倉", sell_cost))
                    del positions[sym]

            # 4) 當日權益 (現金 + 持股市值)
            mv = sum(p.shares * (self._last_close_upto(s, d) or p.entry_price)
                     for s, p in positions.items())
            equity_curve.append((d, cash + mv, cash, len(positions)))

        metrics = self._metrics(cfg, trades, equity_curve)
        return BacktestResult(cfg, trades, equity_curve, daily_selection, metrics)

    # ---- helpers ----
    @staticmethod
    def _rank_note(res: ScreenResult, vol: int) -> str:
        return f"量{vol:,}股 漲{res.change_pct}% 上影{res.upper_shadow_pct}%"

    def _make_trade(self, pos: _Position, exit_date: date, exit_price: float,
                    reason: str, sell_cost: float) -> Trade:
        gross = (exit_price - pos.entry_price) * pos.shares
        cost = pos.entry_cost + sell_cost
        pnl = gross - cost
        basis = pos.entry_price * pos.shares
        hold = sum(1 for d in self.dates if pos.entry_date < d <= exit_date)
        return Trade(
            symbol=pos.symbol, market=pos.market, entry_date=pos.entry_date,
            entry_price=round(pos.entry_price, 2), exit_date=exit_date,
            exit_price=round(exit_price, 2), shares=pos.shares, exit_reason=reason,
            holding_days=hold, gross_pnl=round(gross, 0), cost=round(cost, 0),
            pnl=round(pnl, 0), return_pct=round(pnl / basis * 100, 2) if basis else 0.0,
            entry_rank_note=pos.rank_note)

    @staticmethod
    def _metrics(cfg: BacktestConfig, trades: list[Trade],
                 equity_curve: list[tuple[date, float, float, int]]) -> dict:
        n = len(trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        final_equity = equity_curve[-1][1] if equity_curve else cfg.initial_capital
        # 最大回撤
        peak = -1e18
        max_dd = 0.0
        for _d, eq, _c, _n in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        # 年化 (以權益曲線天數估)
        cagr = None
        if equity_curve:
            days = (equity_curve[-1][0] - equity_curve[0][0]).days
            if days > 0 and cfg.initial_capital > 0:
                cagr = (final_equity / cfg.initial_capital) ** (365.0 / days) - 1.0
        total_cost = sum(t.cost for t in trades)
        return {
            "起始資金": cfg.initial_capital,
            "期末權益": round(final_equity, 0),
            "總損益": round(final_equity - cfg.initial_capital, 0),
            "總報酬率%": round((final_equity / cfg.initial_capital - 1) * 100, 2) if cfg.initial_capital else 0,
            "年化報酬率%": round(cagr * 100, 2) if cagr is not None else None,
            "最大回撤%": round(max_dd * 100, 2),
            "交易次數": n,
            "勝率%": round(len(wins) / n * 100, 2) if n else 0,
            "獲利次數": len(wins),
            "虧損次數": len(losses),
            "平均每筆報酬%": round(sum(t.return_pct for t in trades) / n, 2) if n else 0,
            "平均獲利": round(gross_win / len(wins), 0) if wins else 0,
            "平均虧損": round(-gross_loss / len(losses), 0) if losses else 0,
            "獲利因子": round(gross_win / gross_loss, 2) if gross_loss else None,
            "平均持有天數": round(sum(t.holding_days for t in trades) / n, 1) if n else 0,
            "總交易成本": round(total_cost, 0),
        }
