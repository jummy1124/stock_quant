#!/usr/bin/env python3
"""盤後選股: 用最近兩個交易日的日K，篩出符合 4 條規則的個股並 print。

篩選規則 (BreakoutScreen): 1.紅K 2.漲幅3%~漲停前一檔 3.上影線≤1% 4.今日量≥1.2×昨日量。
全市場歷史用『逐日整批』取得 (對限流友善 + 本地快取)。⚠️ 非投資建議。

用法:
    python run.py                  # 全市場個股，盤後篩選
    python run.py --market twse
    python run.py --limit 100
"""
from __future__ import annotations

import argparse
import os
import sys

from stock_quant.analysis import BreakoutScreen
from stock_quant.datasource import load_market_history

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "history.pkl")


def _fmt(v):
    return "-" if v is None else f"{v:,.2f}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="台股盤後選股 (4 規則篩選)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--days", type=int, default=75, help="歷史交易日數")
    parser.add_argument("--limit", type=int, default=0, help="最多看幾檔 (0=全部)")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    markets = ("twse", "tpex") if args.market == "all" else (args.market,)
    print("逐日整批抓全市場歷史日K ...")
    hist = load_market_history(markets, days=args.days,
                               cache_path=None if args.no_cache else _CACHE, progress=print)
    items = list(hist.items())
    if args.limit > 0:
        items = items[:args.limit]

    screen = BreakoutScreen()
    hits = []
    for sym, series in items:
        if len(series) < 2:
            continue
        res = screen.check(series[-1], series[-2])      # 當日K vs 前一交易日
        if res.passed:
            hits.append((sym, series[-1].market, res))

    print(f"\n符合 {len(hits)} 檔 / 掃描 {len(items)} 檔")
    header = f"{'代號':<7}{'市場':<5}{'收盤':>10}{'漲幅%':>8}{'上影線%':>9}{'量比':>8}"
    print(header); print("-" * len(header))
    for s, m, r in sorted(hits, key=lambda x: -(x[2].change_pct or 0)):
        print(f"{s:<7}{m.zh:<5}{_fmt(r.close):>10}{_fmt(r.change_pct):>8}"
              f"{_fmt(r.upper_shadow_pct):>9}{_fmt(r.vol_ratio):>8}")
    print("\n註: 技術面選股為機率性參考，非投資建議。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
