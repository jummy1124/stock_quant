"""回測進入點 — 重用專案最新的『漲幅池 + 起漲篩選(screen_breakout)』選股邏輯回測。

第一次在本機執行會自動向 TWSE/TPEX 抓全區間日K 並快取到 .cache/backtest_history.pkl
(TWSE 限流時可多跑幾次自動續抓)；之後即用快取。

用法：
  python run_backtest.py                                  # 預設 2025-01-01~2026-06-11
  python run_backtest.py --start 2023-06-11 --end 2026-06-11
  python run_backtest.py --twse-only                      # 只回測上市 (較快)
  python run_backtest.py --capital 5000000 --max-holdings 5
  python run_backtest.py --stop 5                         # 加 5% 硬停損
  python run_backtest.py --sizing max_lots --capital 500000 --max-holdings 1
  python run_backtest.py --no-fetch                       # 離線, 純用既有快取
  python run_backtest.py --no-cost                        # 不計手續費/證交稅 (看純策略)

選股 = 漲幅 ≥3% 且未鎖漲停的池子 → screen_breakout 6 條件起漲篩選 → 強度分排序取前 N 檔。
出場 = 當日收盤跌破 5MA (或硬停損)；漲停不買、跌停不賣；進出場用當日收盤價。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from stock_quant.breakout_screen import BreakoutConfig
from stock_quant.backtest import Backtester, BacktestConfig
from stock_quant.backtest.dataset import build_history
from stock_quant.backtest.report import write_report

ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE = str(ROOT / ".cache" / "backtest_history.pkl")
SEED_CACHE = str(ROOT / ".cache" / "history.pkl")


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    p = argparse.ArgumentParser(description="台股 起漲策略 回測 (重用 screen_breakout)")
    p.add_argument("--start", type=_d, default=date(2025, 1, 1))
    p.add_argument("--end", type=_d, default=date(2026, 6, 11))
    p.add_argument("--twse-only", action="store_true")
    p.add_argument("--tpex-only", action="store_true")
    p.add_argument("--capital", type=float, default=5_000_000.0)
    p.add_argument("--max-holdings", type=int, default=5)
    p.add_argument("--min-change", type=float, default=3.0, help="漲幅池下限%")
    p.add_argument("--vol-ratio", type=float, default=1.2, help="起漲條件3: 量比下限")
    p.add_argument("--stop", type=float, default=0.0, help="硬停損%% (0=關閉)")
    p.add_argument("--ma-exit", type=int, default=5, help="出場均線天數 (跌破即出)")
    p.add_argument("--sizing", choices=["lot", "max_lots", "amount"], default="lot")
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--amount", type=float, default=100_000.0)
    p.add_argument("--no-cost", action="store_true")
    p.add_argument("--fee-discount", type=float, default=1.0)
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--max-move", type=float, default=0.11, help="髒價清洗門檻 (單日漲跌)")
    p.add_argument("--cache", default=DEFAULT_CACHE)
    p.add_argument("--out", default=None)
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
        seed_cache_path=SEED_CACHE, markets=markets, max_daily_move=args.max_move, progress=print)
    print(f"  資料：{len(history)} 檔、{len(trading_dates)} 個交易日 "
          f"({trading_dates[0] if trading_dates else '—'} ~ {trading_dates[-1] if trading_dates else '—'})")
    if not trading_dates:
        print("  ⚠️ 區間內無可用資料。本機請移除 --no-fetch 讓程式抓取。")
        return

    bcfg = BreakoutConfig(vol_ratio_min=args.vol_ratio, ma_short=args.ma_exit)
    cfg = BacktestConfig(
        start=args.start, end=args.end, initial_capital=args.capital,
        max_holdings=args.max_holdings, min_change_pct=args.min_change,
        sizing_mode=args.sizing, lots_per_position=args.lots, amount_per_position=args.amount,
        stop_loss_pct=args.stop, ma_exit=args.ma_exit,
        fee_rate=0.0 if args.no_cost else 0.001425,
        tax_rate=0.0 if args.no_cost else 0.003,
        min_fee=0.0 if args.no_cost else 20.0, fee_discount=args.fee_discount,
        breakout=bcfg)
    result = Backtester(history, trading_dates, cfg).run()

    print("\n=== 績效摘要 ===")
    for k, v in result.metrics.items():
        print(f"  {k:<12}: {v:,}" if isinstance(v, (int, float)) else f"  {k:<12}: {v}")

    out = args.out or str(ROOT / f"backtest_{args.start}_{args.end}.xlsx")
    write_report(result, out)
    print(f"\n✅ Excel 報告：{out}")


if __name__ == "__main__":
    main()
