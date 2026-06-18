#!/usr/bin/env python3
"""盤中起漲篩選: 盤中(09:00-13:30)每分鐘抓全市場即時價，先篩出「漲幅 3% ~ 漲停前一檔」的個股池，
再用起漲點 6 條件篩出當日起漲個股，print 出來並寫進 Excel (漲幅池預設覆蓋 ranking.xlsx)。

流程:
  1. 啟動: 逐日整批抓全市場歷史日K (取昨收+均線)，並建立 {代號:名稱} 對照表。
  2. 每個 cycle: 抓今日K (交易時間=MIS 即時價 / 非交易時間=最後交易日完成日K)。
  3. 第一層: 對所有有報價的個股算漲幅%，篩 3% ~ 漲停前一檔，依漲幅排序。
  4. 第二層 (預設開啟): 起漲點 6 條件 — 紅K、突破昨高、量增 1.2 倍、站上 5MA、
     站上月線且月線上彎、昨日仍在 5MA 下；通過者依強度分排序並印出。
  5. 漲幅池覆蓋寫入 Excel；--notify line 時把起漲個股推到 LINE。

資料來源依時間自動切換: 交易時間抓 MIS 即時價當今日K；非交易時間改用最後一個交易日的
完成日K (可用 --no-screen-when-closed 關閉、非盤中就純休眠)。
⚠️ 篩選為資訊參考，非投資建議。

LINE 推播設定 (擇一；token 等同密碼，勿寫進程式/commit):
    A. 專案根目錄放 .env 檔 (建議，已被 .gitignore 忽略):
           LINE_CHANNEL_TOKEN=你的token
           LINE_USER_ID=U你的userId
    B. 直接設環境變數 LINE_CHANNEL_TOKEN / LINE_USER_ID

用法:
    python run_intraday.py                                # 盤中每分鐘篩起漲個股 + 寫 ranking.xlsx
    python run_intraday.py --once                         # 立刻跑一次 (盤中=即時 / 非盤中=最後交易日)
    python run_intraday.py --serve                        # 篩選 + 同進程內嵌 HTTP API (前端輪詢)
    python run_intraday.py --no-breakout                  # 關起漲篩選，改印全部漲幅排行 (3%~漲停前)
    python run_intraday.py --show-pool                    # 同時印第一層漲幅池完整排行
    python run_intraday.py --breakout-vol-projection      # 條件3 改用全日預估量比昨量 (早盤較公允)
    python run_intraday.py --breakout-excel out/brk.xlsx  # 起漲結果另存 Excel
    python run_intraday.py --notify line                  # 每日 13:00 推當下起漲個股快照
    python run_intraday.py 2330 2317                      # 只看指定個股

"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time

from stock_quant.breakout_screen import BreakoutConfig, save_breakout, screen_breakout
from stock_quant.datasource import load_market_history
from stock_quant.domain import Market
from stock_quant.excel_export import save_ranking
from stock_quant.intraday import IntradayRanker
from stock_quant.netinfo import push_startup_ip
from stock_quant.notify import DailyDigestAlerter, LineNotifier, StableAlerter, load_dotenv
from stock_quant.scheduler import MarketClock, run_market_loop
from stock_quant.universe import load_name_map

_ROOT = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(_ROOT, ".cache", "history.pkl")
_DEFAULT_XLSX = os.path.join(_ROOT, "ranking.xlsx")


def _fmt(v):
    return "-" if v is None else f"{v:,.2f}"


def _parse_hhmm(s: str) -> time:
    """把 'HH:MM' 轉成 datetime.time。"""
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _title(ranker) -> str:
    if not ranker.apply_filter:
        return "漲幅排行"
    cap = "漲停前一檔" if ranker.exclude_limit_up else "漲停"
    return f"漲幅篩選 ({ranker.min_change_pct:g}%~{cap})"


def _print_ranking(now: datetime, rows, ranker=None, top: int = 0) -> None:
    quotable = getattr(ranker, "last_quoted", len(rows)) if ranker is not None else len(rows)
    universe = len(ranker.pairs) if ranker is not None else len(rows)
    src = getattr(ranker, "last_source", "live") if ranker is not None else "live"
    tag = "盤中即時" if src == "live" else "最後交易日"
    title = _title(ranker) if ranker is not None else "漲幅排行"
    print(f"\n===== {title} {now:%Y-%m-%d %H:%M:%S} [{tag}] — "
          f"符合 {len(rows)} 檔 / 可算漲幅 {quotable} 檔 / 全市場 {universe} 檔 =====")
    if ranker is not None and getattr(ranker, "last_warning", None):
        print(f"⚠️ {ranker.last_warning} — 本次覆蓋不全，名單可能偏少")
    if not rows:
        print("(目前沒有符合條件的個股)")
        return
    shown = rows[:top] if top and top > 0 else rows
    header = f"{'#':>4}  {'代號':<8}{'名稱':<12}{'市場':<6}{'現價':>10}{'漲跌':>9}{'漲幅%':>9}{'量(張)':>12}"
    print(header)
    print("-" * 74)
    for i, r in enumerate(shown, start=1):
        lots = "-" if r.lots is None else f"{r.lots:,.0f}"
        print(f"{i:>4}  {r.symbol:<8}{(r.name or ''):<12}{r.market.zh:<6}"
              f"{_fmt(r.close):>10}{_fmt(r.change):>9}{_fmt(r.change_pct):>9}{lots:>12}")
    if top and top > 0 and len(rows) > top:
        print(f"... (其餘 {len(rows) - top} 檔已寫入 Excel)")


def _print_breakout(now: datetime, scored, source: str = "live", top: int = 0,
                    pool: int = None) -> None:
    """印出第二層『起漲點』篩選結果 (6 條件，依強度分排序)。"""
    tag = "盤中即時" if source == "live" else "最後交易日"
    pool_note = f"漲幅池 {pool} 檔 → " if pool is not None else ""
    print(f"\n===== 起漲個股 (紅K+突破昨高+量增+站上5MA+月線上彎+昨日在5MA下) {now:%Y-%m-%d %H:%M:%S} [{tag}] — "
          f"{pool_note}篩出 {len(scored)} 檔 =====")
    if not scored:
        print("(目前沒有符合起漲條件的個股)")
        return
    shown = scored[:top] if top and top > 0 else scored
    header = f"{'#':>4}  {'代號':<8}{'名稱':<12}{'現價':>10}{'漲幅%':>9}{'昨高':>10}{'量比':>7}{'強度分':>8}  理由"
    print(header)
    print("-" * 92)
    for i, sr in enumerate(shown, start=1):
        r = sr.row
        vr = "-" if sr.vol_ratio is None else f"{sr.vol_ratio:.2f}"
        print(f"{i:>4}  {r.symbol:<8}{(r.name or ''):<12}{_fmt(r.close):>10}"
              f"{_fmt(r.change_pct):>9}{_fmt(sr.prev_high):>10}{vr:>7}{sr.score:>8.1f}  "
              f"{' / '.join(sr.reasons)}")
    if top and top > 0 and len(scored) > top:
        print(f"... (其餘 {len(scored) - top} 檔已寫入 Excel)")


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
    print(f"LINE 推播: 每日 {notify_time:%H:%M} 推當下篩選快照 (收訊者 {len(notifier.user_ids)} 人)")
    return DailyDigestAlerter(notifier, fire_time=notify_time)


def _load_names(market_arg: str) -> dict:
    """建立 {代號: 名稱} 對照 (best-effort，失敗就回空字典)。"""
    markets = ("twse", "tpex") if market_arg == "all" else (market_arg,)
    try:
        names = load_name_map(markets)
        print(f"已建立股票名稱對照 {len(names)} 檔。")
        return names
    except Exception as exc:                       # noqa: BLE001 — 名稱對照失敗不致命
        print(f"⚠️ 取股票名稱對照失敗: {exc} -> 改用資料源自帶名稱。")
        return {}


def _start_api_server(ranker, host: str, port: int, origins):
    """合併單一進程: 在背景 thread 起 uvicorn，API 只讀本進程每分鐘 publish 的快照
    (不自行抓即時價 -> 零重複爬取)。回傳 (service, error)；缺 fastapi/uvicorn 回 (None, msg)。"""
    try:
        import threading

        import uvicorn

        from stock_quant.api import create_app
        from stock_quant.screen_service import ApiConfig, ScreenService
    except ImportError as exc:
        return None, (f"缺 API 相依 ({exc}) -> 略過內嵌 API。 "
                      "請 poetry install --with api")

    cfg = ApiConfig.from_env()
    if origins:
        cfg.allowed_origins = tuple(o.strip() for o in origins.split(",") if o.strip()) or ("*",)
    service = ScreenService(cfg)
    service.attach_ranker(ranker)               # 沿用本進程已建好的 ranker
    app = create_app(service=service)

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None   # 非主執行緒不能裝 signal handler
    threading.Thread(target=server.run, name="api-server", daemon=True).start()
    return service, None


def main(argv=None) -> int:
    load_dotenv()        # 啟動先載入專案根目錄的 .env (不覆蓋已存在的環境變數)
    parser = argparse.ArgumentParser(description="台股盤中起漲篩選 (3%~漲停前一檔漲幅池 + 起漲點 6 條件，每分鐘 print + 寫 Excel)")
    parser.add_argument("symbols", nargs="*", help="指定個股 (給了就覆蓋全市場)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--days", type=int, default=35,
                        help="歷史交易日數 (全市場；需 ≥25 才能算 5/20 日均線及月線斜率，預設 35；起漲篩選會自動確保 ≥30)")
    parser.add_argument("--months", type=int, default=2,
                        help="歷史月數 (指定個股；需 ≥2 才能算月均線，預設 2)")
    parser.add_argument("--interval", type=int, default=60, help="盤中更新間隔秒 (預設 60)")
    parser.add_argument("--idle-interval", type=int, default=300, help="非盤中(EOD模式)更新間隔秒 (預設 300)")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0, help="最多看幾檔 (0=全部)")
    parser.add_argument("--procs", type=int, default=None)
    parser.add_argument("--min-change", type=float, default=3.0,
                        help="篩選: 漲幅下限 %% (預設 3)")
    parser.add_argument("--include-limit-up", action="store_true",
                        help="連已鎖漲停 (收盤 > 漲停前一檔) 的也納入")
    parser.add_argument("--no-filter", action="store_true",
                        help="不篩選，輸出全市場漲幅排行")
    parser.add_argument("--no-names", action="store_true",
                        help="不建立股票名稱對照 (省一次 OpenAPI 請求)")
    parser.add_argument("--top", type=int, default=0,
                        help="畫面/LINE 只顯示漲幅前 N 名 (0=全部；Excel 一律存全部)")
    parser.add_argument("--excel", default=_DEFAULT_XLSX,
                        help=f"Excel 輸出路徑 (每次覆蓋；預設 {_DEFAULT_XLSX})")
    parser.add_argument("--no-excel", action="store_true", help="不寫 Excel")
    parser.add_argument("--no-screen-when-closed", action="store_true",
                        help="非交易時間純休眠，不用最後交易日資料 (預設會用)")
    parser.add_argument("--notify", choices=["none", "line"], default="none",
                        help="符合個股通知方式 (line 需設 LINE_CHANNEL_TOKEN / LINE_USER_ID)")
    parser.add_argument("--notify-mode", choices=["daily", "realtime"], default="daily",
                        help="LINE 推播模式: daily=每日定時推當下篩選快照 (預設)；realtime=每次更新就推")
    parser.add_argument("--notify-time", default="13:00",
                        help="daily 模式的推播時間 HH:MM (預設 13:00)")
    parser.add_argument("--notify-top", type=int, default=0,
                        help="LINE 推播的前 N 名 (0=全部符合的)")
    parser.add_argument("--notify-ip-on-start", action="store_true",
                        help="啟動時推一則本機 (GCE VM) 對外 IP 到 LINE；每天開機自動推當日 IP")
    # --- 第二層 起漲點篩選 (6 條件)；預設開啟，畫面直接印「已篩出起漲的個股」 ---
    parser.add_argument("--no-breakout", action="store_true",
                        help="關閉起漲點篩選，改回印全部漲幅排行 (3%%~漲停前)")
    parser.add_argument("--show-pool", action="store_true",
                        help="同時印出第一層漲幅池完整排行 (預設只印一行摘要)")
    parser.add_argument("--breakout-top", type=int, default=0,
                        help="畫面只顯示強度分前 N 名 (0=全部；Excel 一律存全部)")
    parser.add_argument("--breakout-excel", default=None,
                        help="起漲篩選結果另存 Excel 路徑 (預設不另存)")
    parser.add_argument("--breakout-min-score", type=float, default=0.0,
                        help="強度分下限，低於不輸出 (預設 0=只要 6 條件通過就列)")
    parser.add_argument("--breakout-vol-ratio", type=float, default=1.2,
                        help="條件3: 當日量 / 昨量 下限 (預設 1.2 倍)")
    parser.add_argument("--breakout-vol-projection", action="store_true",
                        help="條件3 改用『全日預估量』比昨量 (盤中早盤較公允；預設用當日累積量直接比)")
    # --- 內嵌 HTTP API (合併單一進程: tick 一次同時供 Excel/LINE 與 API，零重複爬取) ---
    parser.add_argument("--serve", action="store_true",
                        help="在同一進程背景起 HTTP API；前端輪詢 GET /api/screen 讀本進程的篩選快照")
    parser.add_argument("--api-host", default="0.0.0.0", help="API 監聽位址 (預設 0.0.0.0)")
    parser.add_argument("--api-port", type=int, default=8000, help="API 監聽埠 (預設 8000)")
    parser.add_argument("--api-origins", default=None,
                        help="API CORS 允許來源，逗號分隔 (前端在別網域時設；預設 * 或 ALLOWED_ORIGINS)")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--once", action="store_true", help="只跑一次就結束 (盤中=即時 / 非盤中=最後交易日)")
    parser.add_argument("--ignore-hours", action="store_true", help="強制當作盤中 (一律抓即時)")
    args = parser.parse_args(argv)

    market = {"twse": Market.TWSE, "tpex": Market.TPEX, "all": None}[args.market]
    notify_time = _parse_hhmm(args.notify_time)
    alerter = _build_alerter(args.notify == "line", args.notify_mode, notify_time)
    if args.notify_ip_on_start:                      # 開機(=app 啟動)推一次當日對外 IP
        push_startup_ip(LineNotifier())
    use_eod = not args.no_screen_when_closed       # 非交易時間是否用最後交易日資料
    excel_path = None if args.no_excel else args.excel
    name_map = {} if args.no_names else _load_names(args.market)
    brk_cfg = None if args.no_breakout else BreakoutConfig(
        vol_ratio_min=args.breakout_vol_ratio,
        use_volume_projection=args.breakout_vol_projection,
        min_score=args.breakout_min_score)
    # 起漲篩選啟用時，自動確保歷史足以算 5/20 日均線與月線斜率，使用者不必另外帶 --days/--months
    if brk_cfg is not None:
        if args.days < 30:
            print(f"ℹ️ 起漲篩選需月均線及斜率，--days {args.days} 自動拉到 30。")
            args.days = 30
        if args.months < 2:
            print(f"ℹ️ 起漲篩選需月均線，--months {args.months} 自動拉到 2。")
            args.months = 2
    ranker_kw = dict(apply_filter=not args.no_filter, min_change_pct=args.min_change,
                     exclude_limit_up=not args.include_limit_up, name_map=name_map,
                     batch_size=args.batch_size, processes=args.procs, eod_when_closed=use_eod)

    if args.symbols:
        pairs = [(s, market) for s in args.symbols]
        ranker = IntradayRanker(pairs, months=args.months, **ranker_kw)
        print(f"抓 {len(pairs)} 檔歷史日K (逐檔) ...")
        ready = ranker.prepare()
    else:
        markets = ("twse", "tpex") if args.market == "all" else (args.market,)
        print("逐日整批抓全市場歷史日K (對限流友善，當天會快取) ...")
        hist = load_market_history(markets, days=args.days,
                                   cache_path=None if args.no_cache else _CACHE,
                                   progress=print)
        if args.limit > 0:
            hist = dict(list(hist.items())[:args.limit])
        pairs = [(sym, series[-1].market) for sym, series in hist.items() if series]
        ranker = IntradayRanker(pairs, **ranker_kw)
        ready = ranker.preload_history(hist)

    filt = "關閉 (全市場排行)" if args.no_filter else _title(ranker)
    closed_note = "用最後交易日資料" if use_eod else "純休眠"
    excel_note = "關閉" if excel_path is None else excel_path
    print(f"已備妥 {ready} 檔歷史。盤中每 {args.interval}s 篩選 ({filt})。"
          f"非盤中: {closed_note}；Excel: {excel_note}\n")
    if brk_cfg is not None:
        hist_n = args.months * 20 if args.symbols else args.days
        print("✅ 畫面顯示『已篩出起漲的個股』(紅K+突破昨高+量增1.2倍+站上5MA+月線上彎+昨日在5MA下)；"
              "漲幅池只印摘要 (--show-pool 看全表，--no-breakout 關閉)。")
        print(f"   均線條件已自動帶入 (歷史約 {hist_n} 根，足以算 5/20MA 及月線斜率)。\n")
    if ready == 0:
        print("沒有歷史資料，請檢查網路或改用 --limit / 指定個股測試。")
        return 1

    api_service = None
    if args.serve:
        api_service, api_err = _start_api_server(ranker, args.api_host, args.api_port,
                                                 args.api_origins)
        if api_err:
            print(f"⚠️ {api_err}")
        else:
            print(f"🌐 內嵌 API 已啟動: http://{args.api_host}:{args.api_port}  "
                  f"(Swagger: /docs；前端輪詢 GET /api/screen)")
            if args.once:
                print("ℹ️ --once 會跑完一次即結束，API 不會常駐；要持續服務請拿掉 --once。")

    def _cycle(now: datetime) -> None:
        rows = ranker.tick(now)
        scored = None
        # 預設: 畫面直接印「已篩出起漲的個股」(第二層)；漲幅池只留一行摘要 (--show-pool 可印全表)
        if brk_cfg is not None:
            if args.show_pool:
                _print_ranking(now, rows, ranker, top=args.top)
            scored = screen_breakout(ranker, rows, now, brk_cfg)
            _print_breakout(now, scored, source=ranker.last_source, top=args.breakout_top,
                            pool=len(rows))
            if args.breakout_excel and scored:
                berr = save_breakout(args.breakout_excel, now, scored, source=ranker.last_source)
                print(f"⚠️ {berr}" if berr else f"💾 已寫入起漲 Excel: {args.breakout_excel} ({len(scored)} 檔)")
        else:
            _print_ranking(now, rows, ranker, top=args.top)
        # Excel 仍存第一層完整漲幅排行 (不受畫面顯示影響)
        if excel_path is not None and rows:
            err = save_ranking(excel_path, now, rows, source=ranker.last_source, title=_title(ranker))
            if err:
                print(f"⚠️ {err}")
            else:
                print(f"💾 已寫入 Excel: {excel_path} ({len(rows)} 檔)")
        if alerter is not None:
            # 起漲篩選啟用時只推「起漲個股」(scored)，否則推漲幅池
            push_src = [s.row for s in scored] if scored is not None else rows
            top_rows = push_src[:args.notify_top] if args.notify_top > 0 else push_src
            pushed = alerter.process(now, top_rows)
            if pushed:
                print(f"📨 已推 LINE: {', '.join(pushed)}")
        # 把本次 tick 的同一份結果 publish 給內嵌 API (前端讀這份快照，不重複爬)
        if api_service is not None:
            api_service.publish(now, rows, scored)

    if args.once:
        _cycle(datetime.now())
        return 0

    print(f"進入常駐迴圈 (Ctrl+C 結束)；每 {args.interval}s 更新一次 ...")
    try:
        run_market_loop(_cycle, clock=MarketClock(), interval=args.interval,
                        idle_interval=args.idle_interval, run_when_closed=use_eod,
                        ignore_hours=args.ignore_hours)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
