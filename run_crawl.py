#!/usr/bin/env python3
"""個股爬蟲入口: 多進程抓取台股(上市+上櫃)最後交易日『個股』資訊並 print。

只抓普通個股 —— ETF、權證、特別股、TDR、期貨等非個股商品會在資料來源就被濾掉。
本階段「不存資料庫」，只把抓到的資訊印在終端機。

用法:
    python run_crawl.py                  # 抓上市+上櫃個股，印出全部
    python run_crawl.py --limit 20       # 每個市場只印前 20 檔
    python run_crawl.py --market twse     # 只抓上市 (twse / tpex / all)
    python run_crawl.py --procs 2        # 指定進程數
"""
from __future__ import annotations

import argparse
import sys
import time

from stock_quant.crawler import MultiProcessCrawler
from stock_quant.datasource import TwseDataSource, TpexDataSource


def _fmt(v, nd: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _print_quotes(quotes, limit: int) -> None:
    rows = quotes if limit <= 0 else quotes[:limit]
    header = f"{'代號':<7}{'名稱':<11}{'開':>10}{'高':>10}{'低':>10}{'收':>10}{'漲跌':>9}{'成交量':>14}"
    print(header)
    print("-" * len(header))
    for q in rows:
        name = (q.name or "")[:9]
        print(
            f"{q.symbol:<7}{name:<11}"
            f"{_fmt(q.open):>10}{_fmt(q.high):>10}{_fmt(q.low):>10}{_fmt(q.close):>10}"
            f"{_fmt(q.change):>9}{_fmt(q.volume, 0):>14}"
        )
    if limit > 0 and len(quotes) > limit:
        print(f"... (還有 {len(quotes) - limit} 檔未顯示，用 --limit 0 可印出全部)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="台股上市/上櫃 個股爬蟲 (multiprocess，僅 print)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all",
                        help="要抓的市場 (預設 all)")
    parser.add_argument("--limit", type=int, default=0,
                        help="每個市場最多印幾檔 (0=全部，預設 0)")
    parser.add_argument("--procs", type=int, default=None, help="進程數 (預設=工作單位數)")
    args = parser.parse_args(argv)

    sources = []
    if args.market in ("twse", "all"):
        sources.append(TwseDataSource())
    if args.market in ("tpex", "all"):
        sources.append(TpexDataSource())

    crawler = MultiProcessCrawler(sources, processes=args.procs)

    print("開始多進程抓取最後交易日個股資料 (已濾除 ETF/權證/期貨等非個股) ...\n")
    t0 = time.perf_counter()
    results = crawler.crawl()
    elapsed = time.perf_counter() - t0

    total = 0
    for r in results:
        print(f"===== {r.unit.label} =====")
        if r.ok:
            total += len(r.quotes)
            print(f"抓到 {len(r.quotes)} 檔個股 (耗時 {r.elapsed_sec:.1f}s)\n")
            _print_quotes(r.quotes, args.limit)
        else:
            print(f"[失敗] {r.error}")
        print()

    print(f"完成: 共 {total} 檔個股，總耗時 {elapsed:.1f}s (未寫入資料庫)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
