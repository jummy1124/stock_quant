#!/usr/bin/env python3
"""盤中即時趨勢監控: 盤中(09:00-13:30)每分鐘用即時價重算每檔個股趨勢並 print。

流程:
  1. 啟動取得歷史日K:
       - 全市場 -> 用『逐日整批』(MI_INDEX / TPEx 每日)，對限流友善，並本地快取。
       - 指定個股 -> 逐檔抓 (檔數少)。
  2. 盤中每分鐘: 多進程抓 MIS 即時價 -> 接成今日K重算趨勢 -> print 快照。
  3. 只在盤中執行 (週一至五 09:00-13:30)，非盤中自動休眠。

⚠️ 即時報價走 MIS、有流量限制；技術分析為機率性參考，非投資建議。

用法:
    python run_intraday.py                 # 盤中每分鐘監控全市場個股趨勢
    python run_intraday.py --limit 50      # 只監控前 50 檔
    python run_intraday.py 2330 2317       # 只監控指定個股 (逐檔抓歷史)
    python run_intraday.py --once          # 立刻跑一次就結束 (測試)
    python run_intraday.py --days 75 --no-cache
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime

from stock_quant.analysis import Trend
from stock_quant.datasource import load_market_history
from stock_quant.domain import Market
from stock_quant.intraday import IntradayTrendMonitor
from stock_quant.scheduler import MarketClock, run_market_loop

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "history.pkl")


def _fmt(v):
    return "-" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))


def _print_snapshot(now: datetime, results) -> None:
    counter: Counter = Counter()
    print(f"\n===== 盤中趨勢快照 {now:%Y-%m-%d %H:%M:%S} — 共 {len(results)} 檔 =====")
    header = f"{'代號':<7}{'市場':<5}{'趨勢':<7}{'分數':>5}{'信心':>7}{'MA5':>10}{'ADX':>8}"
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda x: x.symbol):
        counter[r.trend] += 1
        mk = r.market.zh if r.market else "-"
        if r.ok:
            d = r.details
            print(f"{r.symbol:<7}{mk:<5}{r.trend.value:<7}{r.score:>+5}{r.confidence:>7}"
                  f"{_fmt(d.get('ma5')):>10}{_fmt(d.get('adx')):>8}")
    bull, bear, rng, unk = (counter.get(t, 0) for t in
                            (Trend.BULLISH, Trend.BEARISH, Trend.RANGING, Trend.UNKNOWN))
    print(f"統計: 多頭 {bull}、空頭 {bear}、盤整 {rng}、資料不足 {unk}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="台股盤中即時趨勢監控 (multiprocess，每分鐘)")
    parser.add_argument("symbols", nargs="*", help="指定個股 (給了就覆蓋全市場)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--days", type=int, default=75, help="歷史交易日數 (全市場模式，預設 75)")
    parser.add_argument("--months", type=int, default=5, help="歷史月數 (指定個股模式，預設 5)")
    parser.add_argument("--interval", type=int, default=60, help="更新間隔秒數 (預設 60)")
    parser.add_argument("--batch-size", type=int, default=40, help="MIS 每批檔數")
    parser.add_argument("--limit", type=int, default=0, help="最多監控幾檔 (0=全部)")
    parser.add_argument("--procs", type=int, default=None, help="進程數")
    parser.add_argument("--no-cache", action="store_true", help="不使用歷史快取")
    parser.add_argument("--once", action="store_true", help="只跑一次就結束 (忽略盤中時段)")
    parser.add_argument("--ignore-hours", action="store_true", help="忽略盤中時段限制")
    args = parser.parse_args(argv)

    market = {"twse": Market.TWSE, "tpex": Market.TPEX, "all": None}[args.market]

    if args.symbols:
        # 少數個股: 逐檔抓歷史
        pairs = [(s, market) for s in args.symbols]
        monitor = IntradayTrendMonitor(pairs, months=args.months,
                                       batch_size=args.batch_size, processes=args.procs)
        print(f"抓 {len(pairs)} 檔歷史日K (逐檔) ...")
        cached = monitor.prepare()
    else:
        # 全市場: 逐日整批歷史 (對限流友善 + 本地快取)
        markets = ("twse", "tpex") if args.market == "all" else (args.market,)
        print("逐日整批抓全市場歷史日K (對限流友善，當天會快取) ...")
        hist = load_market_history(markets, days=args.days,
                                   cache_path=None if args.no_cache else _CACHE,
                                   progress=print)
        if args.limit > 0:
            hist = dict(list(hist.items())[:args.limit])
        pairs = [(sym, series[-1].market) for sym, series in hist.items() if series]
        monitor = IntradayTrendMonitor(pairs, batch_size=args.batch_size, processes=args.procs)
        cached = monitor.preload_history(hist)

    print(f"已備妥 {cached} 檔歷史。每 {args.interval}s 用即時價重算趨勢。\n")
    if cached == 0:
        print("沒有任何歷史資料，請檢查網路或改用 --limit / 指定個股測試。")
        return 1

    if args.once:
        _print_snapshot(datetime.now(), monitor.tick(datetime.now()))
        return 0

    print("進入盤中常駐迴圈 (Ctrl+C 結束) ...")
    try:
        run_market_loop(lambda now: _print_snapshot(now, monitor.tick(now)),
                        clock=MarketClock(), interval=args.interval,
                        ignore_hours=args.ignore_hours)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
