#!/usr/bin/env python3
"""台股個股趨勢掃描: 列出全市場個股 -> 每一檔抓歷史日K判斷趨勢 (多進程) -> print。

用法:
    python run.py                      # 掃描全市場個股 (上市+上櫃) 並印出趨勢
    python run.py --market twse         # 只掃上市 (twse / tpex / all)
    python run.py --limit 50            # 只掃前 50 檔 (測試/降載用)
    python run.py 2330 2317 6488        # 只掃指定個股 (覆蓋全市場)
    python run.py --months 6 --procs 8  # 抓 6 個月日K、8 進程

⚠️ 趨勢需要一段日線，每檔會抓數個月歷史日K；全市場上千檔量很大、且可能被官方限流，
   實務上建議用 --limit 或指定個股。技術分析為機率性參考，非投資建議。
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

from stock_quant.analysis import Trend
from stock_quant.domain import Market
from stock_quant.scanner import TrendScanner
from stock_quant.universe import load_individual_universe


def _fmt(v):
    return "-" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="台股個股趨勢掃描 (多進程，多指標綜合評分)")
    parser.add_argument("symbols", nargs="*", help="指定個股代號 (給了就覆蓋全市場)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--months", type=int, default=5, help="抓幾個月歷史日K (預設 5)")
    parser.add_argument("--limit", type=int, default=0, help="最多掃幾檔 (0=全部)")
    parser.add_argument("--procs", type=int, default=None, help="進程數")
    args = parser.parse_args(argv)

    market = {"twse": Market.TWSE, "tpex": Market.TPEX, "all": None}[args.market]

    if args.symbols:
        pairs = [(s, market) for s in args.symbols]     # market=None 時自動偵測
        print(f"掃描指定的 {len(pairs)} 檔個股 ...")
    else:
        markets = ("twse", "tpex") if args.market == "all" else (args.market,)
        print("載入全市場個股清單 ...")
        pairs = load_individual_universe(markets)
        if args.limit > 0:
            pairs = pairs[:args.limit]
        print(f"準備判斷 {len(pairs)} 檔個股的趨勢 (每檔抓 {args.months} 個月日K) ...")

    scanner = TrendScanner(months=args.months, processes=args.procs)
    t0 = time.perf_counter()
    results = scanner.scan(pairs)
    elapsed = time.perf_counter() - t0

    header = f"{'代號':<7}{'市場':<5}{'趨勢':<7}{'分數':>5}{'信心':>7}{'ADX':>8}   MA5/MA20/MA60"
    print("\n" + header)
    print("-" * len(header))
    counter: Counter = Counter()
    for r in sorted(results, key=lambda x: x.symbol):
        counter[r.trend] += 1
        mk = r.market.zh if r.market else "-"
        if r.ok:
            d = r.details
            mas = f"{_fmt(d.get('ma5'))}/{_fmt(d.get('ma20'))}/{_fmt(d.get('ma60'))}"
            print(f"{r.symbol:<7}{mk:<5}{r.trend.value:<7}{r.score:>+5}{r.confidence:>7}"
                  f"{_fmt(d.get('adx')):>8}   {mas}")
        else:
            print(f"{r.symbol:<7}{mk:<5}{'(失敗)':<7} {r.error}")

    print(f"\n統計: " + "、".join(f"{t.value} {counter.get(t, 0)}" for t in
                                 (Trend.BULLISH, Trend.BEARISH, Trend.RANGING, Trend.UNKNOWN)))
    print(f"共 {len(results)} 檔，耗時 {elapsed:.1f}s。註: 技術分析為機率性參考，非投資建議。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
