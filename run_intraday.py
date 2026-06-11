#!/usr/bin/env python3
"""盤中選股: 盤中(09:00-13:30)每分鐘用即時價篩出符合 7 條規則的個股並 print (經穩定層確認)。

篩選規則 (BreakoutScreen，全符合才入選):
  1. 紅K: 收盤 > 開盤
  2. 漲幅 3% ~ 漲停前一檔
  3. 上影線 ≤ 1%
  4. 今日量 ≥ 1.2 × 昨日量 (盤中依已過時段比例換算為「同時段」量比)
  5. 前一日收盤 < 五日均線(MA5)
  6. 今日現價 > 昨日最高價
  7. 多頭趨勢: 站上月線(MA20) + 月線≥季線(MA20≥MA60) + 頭頭高底底高 (擋空頭單日反彈)

資料來源依時間自動切換: 交易時間抓 MIS 即時價當今日K；非交易時間改用最後一個交易日的
完成日K (可用 --no-screen-when-closed 關閉、非盤中就純休眠)。
穩定層: 即時價是「盤中瞬間值」，單次命中會跨分鐘跳動，故盤中需連續 N 次確認 + 寬限窗才報出
(EOD 完成日K是定案資料，直接報出)。
LINE 推播: --notify line 時推到 LINE。預設「每日 13:00 彙整一次」。
⚠️ 技術面選股為機率性參考，非投資建議。

LINE 推播設定 (擇一；token 等同密碼，勿寫進程式/commit):
    A. 專案根目錄放 .env 檔 (建議，已被 .gitignore 忽略):
           LINE_CHANNEL_TOKEN=你的token
           LINE_USER_ID=U你的userId
    B. 直接設環境變數 LINE_CHANNEL_TOKEN / LINE_USER_ID

用法:
    python run_intraday.py --notify line                  # 每日 13:00 彙整推播 (預設)
    python run_intraday.py --once                         # 立刻跑一次 (盤中=即時 / 非盤中=最後交易日)
    python run_intraday.py --no-screen-when-closed        # 非交易時間純休眠，不用 EOD 資料
    python run_intraday.py --no-trend                     # 關閉多頭趨勢閘門 (只跑原 6 條)
    python run_intraday.py 2330 2317                      # 只看指定個股

    python run.py                                         # 盤後全市場篩選
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time

from stock_quant.analysis import BreakoutScreen
from stock_quant.datasource import load_market_history
from stock_quant.domain import Market
from stock_quant.intraday import IntradayScreener
from stock_quant.notify import DailyDigestAlerter, LineNotifier, StableAlerter, load_dotenv
from stock_quant.scheduler import MarketClock, run_market_loop

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "history.pkl")


def _fmt(v):
    return "-" if v is None else f"{v:,.2f}"


def _parse_hhmm(s: str) -> time:
    """把 'HH:MM' 轉成 datetime.time。"""
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _print_snapshot(now: datetime, rows, screener=None) -> None:
    # 穩定層: 只報已「確認(stable)」的個股；非盤中 EOD 模式則直接是當日完成日K的命中
    hits = [row for row in rows if row.stable]
    quoted = len(rows)                                  # 本次可篩 (取得即時價 / EOD 可比) 的檔數
    universe = len(screener.pairs) if screener is not None else quoted
    src = getattr(screener, "last_source", "live") if screener is not None else "live"
    tag = "盤中即時" if src == "live" else "最後交易日"
    print(f"\n===== 選股快照 {now:%Y-%m-%d %H:%M:%S} [{tag}] — "
          f"確認 {len(hits)} 檔 / 可篩 {quoted} 檔 / 全市場 {universe} 檔 =====")
    if screener is not None and getattr(screener, "last_warning", None):
        print(f"⚠️ {screener.last_warning} — 本次覆蓋不全，名單可能偏少")
    if not hits:
        print("(目前沒有符合條件的個股)")
        return
    header = f"{'代號':<8}{'市場':<6}{'收盤':>11}{'漲幅%':>9}{'上影線%':>11}{'量比':>11}"
    print(header)
    print("-" * len(header))
    for row in sorted(hits, key=lambda x: -(x.result.change_pct or 0)):   # 依漲幅排序
        r = row.result
        vr = r.vol_pace_ratio if r.vol_pace_ratio is not None else r.vol_ratio
        print(f"{row.symbol:<8}{row.market.zh:<6}{_fmt(r.close):>11}{_fmt(r.change_pct):>9}"
              f"{_fmt(r.upper_shadow_pct):>11}{_fmt(vr):>11}")


def _build_alerter(enabled: bool, mode: str, notify_time: time):
    """--notify line 時建立推播器；未設 .env/環境變數則停用並提示。"""
    if not enabled:
        return None
    notifier = LineNotifier()
    if not notifier.configured:
        print("⚠️ 已指定 --notify line，但缺 LINE_CHANNEL_TOKEN / LINE_USER_ID "
              "(.env 或環境變數) -> 暫不推播。")
        return None
    if mode == "realtime":
        print(f"LINE 推播: 即時模式 (收訊者 {len(notifier.user_ids)} 人)")
        return StableAlerter(notifier)
    print(f"LINE 推播: 每日 {notify_time:%H:%M} 彙整一次 (收訊者 {len(notifier.user_ids)} 人)")
    return DailyDigestAlerter(notifier, fire_time=notify_time)


def main(argv=None) -> int:
    load_dotenv()        # 啟動先載入專案根目錄的 .env (不覆蓋已存在的環境變數)
    parser = argparse.ArgumentParser(description="台股盤中選股 (7 規則 + 連續確認穩定層，multiprocess，每分鐘)")
    parser.add_argument("symbols", nargs="*", help="指定個股 (給了就覆蓋全市場)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--days", type=int, default=75, help="歷史交易日數 (全市場；需 ≥60 才能判多頭趨勢)")
    parser.add_argument("--months", type=int, default=5, help="歷史月數 (指定個股)")
    parser.add_argument("--interval", type=int, default=60, help="盤中更新間隔秒 (預設 60)")
    parser.add_argument("--idle-interval", type=int, default=300, help="非盤中(EOD模式)更新間隔秒 (預設 300)")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0, help="最多看幾檔 (0=全部)")
    parser.add_argument("--procs", type=int, default=None)
    parser.add_argument("--confirm-ticks", type=int, default=2,
                        help="盤中需連續通過幾次 tick 才確認入選 (去雜訊，預設 2；1=不過濾)")
    parser.add_argument("--grace-ticks", type=int, default=1,
                        help="確認後即使瞬間不符仍保留幾次 (避免小跳動就消失，預設 1)")
    parser.add_argument("--no-trend", action="store_true",
                        help="關閉多頭趨勢閘門 (rule 7)，只跑原 6 條突破規則")
    parser.add_argument("--no-screen-when-closed", action="store_true",
                        help="非交易時間純休眠，不用最後交易日資料 (預設會用)")
    parser.add_argument("--notify", choices=["none", "line"], default="none",
                        help="命中通知方式 (line 需設 LINE_CHANNEL_TOKEN / LINE_USER_ID)")
    parser.add_argument("--notify-mode", choices=["daily", "realtime"], default="daily",
                        help="LINE 推播模式: daily=每日定時彙整一次 (預設)；realtime=命中即時推")
    parser.add_argument("--notify-time", default="13:00",
                        help="daily 模式的推播時間 HH:MM (預設 13:00)")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--once", action="store_true", help="只跑一次就結束 (盤中=即時 / 非盤中=最後交易日)")
    parser.add_argument("--ignore-hours", action="store_true", help="強制當作盤中 (一律抓即時)")
    args = parser.parse_args(argv)

    market = {"twse": Market.TWSE, "tpex": Market.TPEX, "all": None}[args.market]
    # --once 只跑一次，無法累積連續確認 -> 退回單次原始命中 (confirm=1)
    confirm = 1 if args.once else args.confirm_ticks
    screen = BreakoutScreen(require_uptrend=not args.no_trend)
    notify_time = _parse_hhmm(args.notify_time)
    alerter = _build_alerter(args.notify == "line", args.notify_mode, notify_time)
    use_eod = not args.no_screen_when_closed       # 非交易時間是否用最後交易日資料

    if args.symbols:
        pairs = [(s, market) for s in args.symbols]
        screener = IntradayScreener(pairs, months=args.months, screen=screen,
                                    batch_size=args.batch_size, processes=args.procs,
                                    confirm_ticks=confirm, grace_ticks=args.grace_ticks,
                                    eod_when_closed=use_eod)
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
        screener = IntradayScreener(pairs, screen=screen,
                                    batch_size=args.batch_size, processes=args.procs,
                                    confirm_ticks=confirm, grace_ticks=args.grace_ticks,
                                    eod_when_closed=use_eod)
        ready = screener.preload_history(hist)

    trend_note = "關閉" if args.no_trend else "開啟"
    closed_note = "用最後交易日資料" if use_eod else "純休眠"
    print(f"已備妥 {ready} 檔歷史。盤中每 {args.interval}s 篩選。多頭趨勢閘門: {trend_note}；非盤中: {closed_note}\n")
    if ready == 0:
        print("沒有歷史資料，請檢查網路或改用 --limit / 指定個股測試。")
        return 1

    def _cycle(now: datetime) -> None:
        rows = screener.tick(now)
        _print_snapshot(now, rows, screener)
        if alerter is not None:
            pushed = alerter.process(now, rows)
            if pushed:
                print(f"📨 已推 LINE: {', '.join(pushed)}")

    if args.once:
        _cycle(datetime.now())
        return 0

    print(f"進入常駐迴圈 (Ctrl+C 結束)；盤中需連續 {confirm} 次確認、寬限 {args.grace_ticks} 次 ...")
    try:
        run_market_loop(_cycle, clock=MarketClock(), interval=args.interval,
                        idle_interval=args.idle_interval, run_when_closed=use_eod,
                        ignore_hours=args.ignore_hours)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
