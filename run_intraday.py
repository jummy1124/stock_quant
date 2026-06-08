#!/usr/bin/env python3
"""盤中選股: 盤中(09:00-13:30)每分鐘用即時價篩出符合 4 條規則的個股並 print。

篩選規則 (BreakoutScreen):
  1. 紅K (收>開)
  2. 漲幅 3% ~ 漲停前一檔
  3. 上影線 ≤ 1%
  4. 今日量 ≥ 1.2 × 昨日量

流程: 啟動取得歷史日K(全市場逐日整批/指定個股逐檔) -> 每分鐘抓 MIS 即時價做篩選。
只在盤中執行，非盤中自動休眠。⚠️ 技術面選股為機率性參考，非投資建議。

用法:
    python run_intraday.py                 # 盤中每分鐘篩全市場個股
    python run_intraday.py --limit 50      # 只看前 50 檔 (降載)
    python run_intraday.py 2330 2317       # 只看指定個股
    python run_intraday.py --once          # 立刻跑一次就結束 (測試)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from stock_quant.datasource import load_market_history
from stock_quant.domain import Market
from stock_quant.intraday import IntradayScreener
from stock_quant.scheduler import MarketClock, run_market_loop

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "history.pkl")


def _fmt(v):
    return "-" if v is None else f"{v:,.2f}"


def _print_snapshot(now: datetime, rows) -> None:
    hits = [(s, m, r) for s, m, r in rows if r.passed]
    print(f"\n===== 選股快照 {now:%Y-%m-%d %H:%M:%S} — 符合 {len(hits)} 檔 / 掃描 {len(rows)} 檔 =====")
    if not hits:
        print("(目前沒有符合條件的個股)")
        return
    header = f"{'代號':<7}{'市場':<5}{'收盤':>10}{'漲幅%':>8}{'上影線%':>9}{'量比':>8}"
    print(header)
    print("-" * len(header))
    for s, m, r in sorted(hits, key=lambda x: -(x[2].change_pct or 0)):   # 依漲幅排序
        print(f"{s:<7}{m.zh:<5}{_fmt(r.close):>10}{_fmt(r.change_pct):>8}"
              f"{_fmt(r.upper_shadow_pct):>9}{_fmt(r.vol_ratio):>8}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="台股盤中選股 (4 規則篩選，multiprocess，每分鐘)")
    parser.add_argument("symbols", nargs="*", help="指定個股 (給了就覆蓋全市場)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--days", type=int, default=75, help="歷史交易日數 (全市場)")
    parser.add_argument("--months", type=int, default=5, help="歷史月數 (指定個股)")
    parser.add_argument("--interval", type=int, default=60, help="更新間隔秒 (預設 60)")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0, help="最多看幾檔 (0=全部)")
    parser.add_argument("--procs", type=int, default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--once", action="store_true", help="只跑一次就結束 (忽略盤中時段)")
    parser.add_argument("--ignore-hours", action="store_true")
    args = parser.parse_args(argv)

    market = {"twse": Market.TWSE, "tpex": Market.TPEX, "all": None}[args.market]

    if args.symbols:
        pairs = [(s, market) for s in args.symbols]
        screener = IntradayScreener(pairs, months=args.months,
                                    batch_size=args.batch_size, processes=args.procs)
        print(f"抓 {len(pairs)} 檔歷史日K (逐檔) ...")
        ready = screener.prepare()
    else:
        markets = ("twse", "tpex") if args.market == "all" else (args.market,)
        print("逐日整批抓全市場歷史日K (對限流友善，當天會快取) ...")
        hist = load_market_history(markets, days=args.days,
                                   cache_path=None if args.no_cache else _CACHE,
                                   progress=print)
        if args.limit > 0:
            hist = dict(list(hist.items())[:args.limit])
        pairs = [(sym, series[-1].market) for sym, series in hist.items() if series]
        screener = IntradayScreener(pairs, batch_size=args.batch_size, processes=args.procs)
        ready = screener.preload_history(hist)

    print(f"已備妥 {ready} 檔歷史。每 {args.interval}s 篩選一次。\n")
    if ready == 0:
        print("沒有歷史資料，請檢查網路或改用 --limit / 指定個股測試。")
        return 1

    if args.once:
        _print_snapshot(datetime.now(), screener.tick(datetime.now()))
        return 0

    print("進入盤中常駐迴圈 (Ctrl+C 結束) ...")
    try:
        run_market_loop(lambda now: _print_snapshot(now, screener.tick(now)),
                        clock=MarketClock(), interval=args.interval,
                        ignore_hours=args.ignore_hours)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
