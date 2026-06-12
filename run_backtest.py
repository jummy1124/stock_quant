"""回測進入點 — 重用本專案的 BreakoutScreen 選股邏輯，回測指定區間。

預設回測 2025-01-01 ~ 2026-06-11。第一次在「本機」執行會自動向 TWSE/TPEX 抓全區間
日K 並快取到 .cache/backtest_history.pkl (TWSE 限流時可多跑幾次自動續抓)；之後即用快取。

用法：
  python run_backtest.py                          # 預設區間，抓全市場 (上市+上櫃)
  python run_backtest.py --start 2025-01-01 --end 2026-06-11
  python run_backtest.py --twse-only              # 只回測上市 (較快)
  python run_backtest.py --no-trend               # 關閉第7條多頭趨勢閘門 (只用6規則)
  python run_backtest.py --capital 3000000        # 調整起始資金
  python run_backtest.py --no-fetch               # 不連網，純用既有快取 (離線/示範)
  python run_backtest.py --no-cost                # 不計手續費/證交稅 (看純策略)

規則 (依使用者規範)：
  1 以當日收盤完成日K 篩選；2 多檔時以成交量優先(複合)選最多5檔；
  3 庫存上限5檔、每檔一張；4 收盤跌破MA5 出場；5 漲停不買、跌停不賣。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from stock_quant.analysis import BreakoutScreen
from stock_quant.analysis.pullback import PullbackScreen
from stock_quant.backtest import Backtester, BacktestConfig
from stock_quant.backtest.dataset import build_history
from stock_quant.backtest.report import write_report

ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE = str(ROOT / ".cache" / "backtest_history.pkl")
SEED_CACHE = str(ROOT / ".cache" / "history.pkl")    # 沿用盤中專案既有快取當種子


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    p = argparse.ArgumentParser(description="台股 BreakoutScreen 策略回測")
    p.add_argument("--start", type=_d, default=date(2025, 1, 1))
    p.add_argument("--end", type=_d, default=date(2026, 6, 11))
    p.add_argument("--twse-only", action="store_true", help="只回測上市")
    p.add_argument("--tpex-only", action="store_true", help="只回測上櫃")
    p.add_argument("--no-trend", action="store_true", help="關閉多頭趨勢閘門 (只用6規則)")
    p.add_argument("--strategy", choices=["breakout", "pullback"], default="breakout",
                   help="breakout=追突破(低勝率高賠率,原規範) / pullback=回檔均值回歸(高勝率)")
    p.add_argument("--rsi-period", type=int, default=3, help="pullback: RSI 週期")
    p.add_argument("--rsi-th", type=float, default=15.0, help="pullback: RSI 超賣門檻 (≤此值才買)")
    p.add_argument("--time-stop", type=int, default=10, help="pullback: 最長持有天數")
    p.add_argument("--market-filter", action="store_true", help="只在大盤(等權指數)站上均線時進場 (pullback 預設開)")
    p.add_argument("--no-market-filter", action="store_true", help="關閉大盤多頭過濾")
    p.add_argument("--market-ma", type=int, default=20, help="大盤指數均線天數 (預設20)")
    p.add_argument("--rank", choices=["volume", "change", "turnover"], default="volume",
                   help="選股排序: volume=量優先(原規範) / change=漲幅優先(回測較佳) / turnover=金額")
    p.add_argument("--min-vol", type=int, default=0,
                   help="進場最低成交量(股), e.g. 500000=500張; 可降回撤但報酬也降")
    p.add_argument("--capital", type=float, default=5_000_000.0, help="起始資金")
    p.add_argument("--sizing", choices=["lot", "max_lots", "shares", "amount"], default="lot",
                   help="部位單位: lot=整股固定張數 / max_lots=整股買最大可買張數 / shares=零股固定股數 / amount=零股每檔固定金額")
    p.add_argument("--shares", type=int, default=100, help="shares 模式: 每檔股數 (零股)")
    p.add_argument("--amount", type=float, default=100_000.0, help="amount 模式: 每檔投入金額")
    p.add_argument("--lots", type=int, default=1, help="lot 模式: 每檔幾張")
    p.add_argument("--max-holdings", type=int, default=5, help="庫存最多幾檔")
    p.add_argument("--max-pos-pct", type=float, default=1.0,
                   help="單檔部位上限(佔總權益), e.g. 0.2=每檔最多20%%; 分散風險、避免全押一檔")
    p.add_argument("--stop", type=float, default=0.0, help="硬停損%% (e.g. 5 = 跌5%%出場); 0=關閉")
    p.add_argument("--no-cost", action="store_true", help="不計交易成本")
    p.add_argument("--fee-discount", type=float, default=1.0, help="手續費折扣 (e.g. 0.6=六折)")
    p.add_argument("--min-fee", type=float, default=20.0,
                   help="單筆最低手續費。零股單筆金額小, 20元下限影響大, 視券商可設 1")
    p.add_argument("--no-fetch", action="store_true", help="不連網，純用既有快取")
    p.add_argument("--twse-delay", type=float, default=5.0,
                   help="上市每筆請求間隔秒數 (太小易被 TWSE 限流，預設5)")
    p.add_argument("--block-cooldown", type=float, default=300.0,
                   help="被 TWSE 暫鎖時的長休息秒數 (預設300=5分鐘)")
    p.add_argument("--max-move", type=float, default=0.11,
                   help="資料清洗: 單日漲跌超過此比例視為髒資料剔除 (預設0.11=±11%%, 台股±10%%上限)")
    p.add_argument("--cache", default=DEFAULT_CACHE)
    p.add_argument("--out", default=None, help="Excel 報告輸出路徑")
    args = p.parse_args()

    markets = ("twse", "tpex")
    if args.twse_only:
        markets = ("twse",)
    elif args.tpex_only:
        markets = ("tpex",)

    print(f"=== 回測 {args.start} ~ {args.end}　市場={markets}　"
          f"{'抓取+快取' if not args.no_fetch else '離線(僅快取)'} ===")
    history, trading_dates = build_history(
        args.start, args.end, fetch=not args.no_fetch, cache_path=args.cache,
        seed_cache_path=SEED_CACHE, markets=markets, twse_delay=args.twse_delay,
        block_cooldown=args.block_cooldown, max_daily_move=args.max_move, progress=print)
    print(f"  資料：{len(history)} 檔、{len(trading_dates)} 個交易日 "
          f"({trading_dates[0] if trading_dates else '—'} ~ {trading_dates[-1] if trading_dates else '—'})")
    if not trading_dates:
        print("  ⚠️ 區間內無可用資料。本機請移除 --no-fetch 讓程式抓取，或檢查快取。")
        return

    is_pull = args.strategy == "pullback"
    rank = "oversold" if (is_pull and args.rank == "volume") else args.rank   # pullback 預設最超賣優先
    stop = args.stop if args.stop > 0 else (4.0 if is_pull else 0.0)          # pullback 預設帶 4% 對稱停損
    exit_mode = "revert" if is_pull else "ma_break"
    mkt = is_pull                                                            # pullback 預設開大盤過濾
    if args.market_filter:
        mkt = True
    if args.no_market_filter:
        mkt = False
    cfg = BacktestConfig(
        start=args.start, end=args.end, initial_capital=args.capital,
        max_holdings=args.max_holdings, stop_loss_pct=stop,
        exit_mode=exit_mode, time_stop_days=args.time_stop,
        require_uptrend=not args.no_trend, fee_discount=args.fee_discount,
        fee_rate=0.0 if args.no_cost else 0.001425,
        tax_rate=0.0 if args.no_cost else 0.003,
        min_fee=0.0 if args.no_cost else args.min_fee,
        rank_mode=rank, min_entry_volume=args.min_vol,
        sizing_mode=args.sizing, lots_per_position=args.lots,
        shares_per_position=args.shares, amount_per_position=args.amount,
        max_position_pct=args.max_pos_pct,
        market_filter=mkt, market_ma=args.market_ma)
    if is_pull:
        screen = PullbackScreen(rsi_period=args.rsi_period, rsi_threshold=args.rsi_th)
    else:
        screen = BreakoutScreen(require_uptrend=cfg.require_uptrend)
    result = Backtester(history, trading_dates, cfg, screen=screen).run()

    print("\n=== 績效摘要 ===")
    for k, v in result.metrics.items():
        print(f"  {k:<12}: {v:,}" if isinstance(v, (int, float)) else f"  {k:<12}: {v}")

    out = args.out or str(ROOT / f"backtest_{args.start}_{args.end}.xlsx")
    write_report(result, out)
    print(f"\n✅ Excel 報告：{out}")


if __name__ == "__main__":
    main()
