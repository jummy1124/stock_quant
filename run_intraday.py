#!/usr/bin/env python3
"""盤中選股: 盤中(09:00-13:30)每分鐘用即時價篩出符合 6 條規則的個股並 print (經穩定層確認)。

篩選規則 (BreakoutScreen，六條全符合才入選):
  1. 紅K: 收盤 > 開盤
  2. 漲幅 3% ~ 漲停前一檔
  3. 上影線 ≤ 1%
  4. 今日量 ≥ 1.2 × 昨日量 (盤中依已過時段比例換算為「同時段」量比)
  5. 前一日收盤 < 五日均線(MA5)
  6. 今日現價 > 昨日最高價

流程: 啟動取得歷史日K(全市場逐日整批/指定個股逐檔) -> 每分鐘抓 MIS 即時價做篩選。
穩定層: 因即時價是「盤中瞬間值」，單次命中會跨分鐘跳動，故需連續 N 次確認 + 寬限窗才報出。
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


def _print_snapshot(now: datetime, rows, screener=None) -> None:
    # 穩定層: 只報已「連續確認」的個股 (stable)，瞬間單次命中不列入 -> 跨分鐘結果穩定
    hits = [row for row in rows if row.stable]
    quoted = len(rows)                                  # 本次實際拿到即時價且有歷史的檔數
    universe = len(screener.pairs) if screener is not None else quoted
    print(f"\n===== 選股快照 {now:%Y-%m-%d %H:%M:%S} — "
          f"確認 {len(hits)} 檔 / 取得即時 {quoted} 檔 / 全市場 {universe} 檔 =====")
    if screener is not None and getattr(screener, "last_warning", None):
        print(f"⚠️ {screener.last_warning} — 本次覆蓋不全，名單可能偏少")
    if not hits:
        print("(目前沒有已確認符合條件的個股)")
        return
    header = f"{'代號':<8}{'市場':<6}{'收盤':>11}{'漲幅%':>9}{'上影線%':>11}{'量比':>11}"
    print(header)
    print("-" * len(header))
    for row in sorted(hits, key=lambda x: -(x.result.change_pct or 0)):   # 依漲幅排序
        r = row.result
        vr = r.vol_pace_ratio if r.vol_pace_ratio is not None else r.vol_ratio
        print(f"{row.symbol:<8}{row.market.zh:<6}{_fmt(r.close):>11}{_fmt(r.change_pct):>9}"
              f"{_fmt(r.upper_shadow_pct):>11}{_fmt(vr):>11}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="台股盤中選股 (6 規則 + 連續確認穩定層，multiprocess，每分鐘)")
    parser.add_argument("symbols", nargs="*", help="指定個股 (給了就覆蓋全市場)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--days", type=int, default=75, help="歷史交易日數 (全市場)")
    parser.add_argument("--months", type=int, default=5, help="歷史月數 (指定個股)")
    parser.add_argument("--interval", type=int, default=60, help="更新間隔秒 (預設 60)")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0, help="最多看幾檔 (0=全部)")
    parser.add_argument("--procs", type=int, default=None)
    parser.add_argument("--confirm-ticks", type=int, default=2,
                        help="需連續通過幾次 tick 才確認入選 (去雜訊，預設 2；1=不過濾)")
    parser.add_argument("--grace-ticks", type=int, default=1,
                        help="確認後即使瞬間不符仍保留幾次 (避免小跳動就消失，預設 1)")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--once", action="store_true", help="只跑一次就結束 (忽略盤中時段)")
    parser.add_argument("--ignore-hours", action="store_true")
    args = parser.parse_args(argv)

    market = {"twse": Market.TWSE, "tpex": Market.TPEX, "all": None}[args.market]
    # --once 只跑一次，無法累積連續確認 -> 退回單次原始命中 (confirm=1)
    confirm = 1 if args.once else args.confirm_ticks

    if args.symbols:
        pairs = [(s, market) for s in args.symbols]
        screener = IntradayScreener(pairs, months=args.months,
                                    batch_size=args.batch_size, processes=args.procs,
                                    confirm_ticks=confirm, grace_ticks=args.grace_ticks)
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
        screener = IntradayScreener(pairs, batch_size=args.batch_size, processes=args.procs,
                                    confirm_ticks=confirm, grace_ticks=args.grace_ticks)
        ready = screener.preload_history(hist)

    print(f"已備妥 {ready} 檔歷史。每 {args.interval}s 篩選一次。\n")
    if ready == 0:
        print("沒有歷史資料，請檢查網路或改用 --limit / 指定個股測試。")
        return 1

    if args.once:
        now = datetime.now()
        _print_snapshot(now, screener.tick(now), screener)
        return 0

    print(f"進入盤中常駐迴圈 (Ctrl+C 結束)；需連續 {confirm} 次確認、寬限 {args.grace_ticks} 次 ...")
    try:
        run_market_loop(lambda now: _print_snapshot(now, screener.tick(now), screener),
                        clock=MarketClock(), interval=args.interval,
                        ignore_hours=args.ignore_hours)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
