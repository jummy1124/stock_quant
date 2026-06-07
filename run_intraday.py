#!/usr/bin/env python3
"""盤中即時爬蟲: 盤中時段每分鐘多進程抓全市場個股最新報價並 print。

流程:
  1. 啟動時用 EOD OpenAPI 取得全市場『個股』清單 (universe)。
  2. 用 MIS 即時 API 分批 + 多進程，每分鐘抓一次最新報價。
  3. 只在盤中(09:00-13:30, 週一至五)執行，非盤中自動休眠。只 print，不存檔。

⚠️ 全市場每分鐘輪詢 MIS 量很大、可能被限流/暫時封鎖。可調 --batch-size / --interval，
   或改鎖定少數個股以降低風險。

用法:
    python run_intraday.py                 # 盤中每分鐘抓全市場個股
    python run_intraday.py --once          # 立刻抓一次就結束 (測試用，忽略盤中時段)
    python run_intraday.py --limit 20      # 每次只印前 20 檔
    python run_intraday.py --interval 60 --batch-size 40 --procs 8
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from stock_quant.crawler import MultiProcessCrawler
from stock_quant.datasource import MisRealtimeDataSource
from stock_quant.scheduler import MarketClock, run_market_loop
from stock_quant.universe import load_individual_universe


def _fmt(v, nd: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _print_snapshot(now: datetime, quotes, limit: int) -> None:
    quotes = sorted(quotes, key=lambda q: q.symbol)
    print(f"\n===== 即時快照 {now:%Y-%m-%d %H:%M:%S} — 共 {len(quotes)} 檔 =====")
    rows = quotes if limit <= 0 else quotes[:limit]
    header = f"{'代號':<7}{'名稱':<11}{'市場':<5}{'成交':>10}{'漲跌':>9}{'開':>10}{'高':>10}{'低':>10}{'量':>10}"
    print(header)
    print("-" * len(header))
    for q in rows:
        name = (q.name or "")[:9]
        print(
            f"{q.symbol:<7}{name:<11}{q.market.zh:<5}"
            f"{_fmt(q.close):>10}{_fmt(q.change):>9}"
            f"{_fmt(q.open):>10}{_fmt(q.high):>10}{_fmt(q.low):>10}{_fmt(q.volume, 0):>10}"
        )
    if limit > 0 and len(quotes) > limit:
        print(f"... (還有 {len(quotes) - limit} 檔未顯示)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="台股盤中即時個股爬蟲 (multiprocess，每分鐘，僅 print)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--limit", type=int, default=0, help="每次最多印幾檔 (0=全部)")
    parser.add_argument("--interval", type=int, default=60, help="抓取間隔秒數 (預設 60)")
    parser.add_argument("--batch-size", type=int, default=40, help="每批查詢檔數 (MIS)")
    parser.add_argument("--procs", type=int, default=None, help="進程數 (預設=批次數)")
    parser.add_argument("--once", action="store_true", help="只抓一次就結束 (忽略盤中時段)")
    parser.add_argument("--ignore-hours", action="store_true", help="忽略盤中時段限制，持續執行")
    args = parser.parse_args(argv)

    markets = ("twse", "tpex") if args.market == "all" else (args.market,)
    print(f"載入 universe (全市場個股清單) ...")
    pairs = load_individual_universe(markets)
    print(f"universe 共 {len(pairs)} 檔個股。批次大小 {args.batch_size}，每 {args.interval}s 抓一次。\n")

    source = MisRealtimeDataSource(pairs, batch_size=args.batch_size)
    crawler = MultiProcessCrawler([source], processes=args.procs)

    def task(now: datetime) -> None:
        results = crawler.crawl()
        quotes = [q for r in results if r.ok for q in r.quotes]
        fails = [r for r in results if not r.ok]
        _print_snapshot(now, quotes, args.limit)
        if fails:
            print(f"({len(fails)} 個批次抓取失敗，例: {fails[0].error})")

    if args.once:
        task(datetime.now())
        return 0

    print("進入常駐迴圈 (Ctrl+C 結束) ...")
    try:
        run_market_loop(task, clock=MarketClock(), interval=args.interval,
                        ignore_hours=args.ignore_hours)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
