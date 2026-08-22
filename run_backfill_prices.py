#!/usr/bin/env python3
"""把「全市場每日收盤價」回補到 userdata 後端 —— 讓回測頁能算出資料庫既有紀錄的後續漲跌。

為什麼要這支
------------
回測問的是「起漲個股在第 N 個交易日後漲了沒」。篩選快照只記當天被選上的那幾檔，
所以後端另外需要一份普通的全市場日K。日常運作由 run_intraday.py --ingest 收盤後
自動上傳，但那只從「啟用之後」開始有資料 —— 資料庫裡先前累積的篩選快照仍然無價可比。
這支就是拿來補那一段的。

資料來源與 run_intraday 完全相同 (load_market_history)，也共用同一份 .cache，
所以已經抓過的交易日不會再打證交所一次。快取上限約 60 個交易日 (約三個月)，
更早的日子抓不到 —— 那是資料源的限制，不是這支的。

用法:
    python run_backfill_prices.py                 # 回補最近 60 個交易日
    python run_backfill_prices.py --days 30       # 只回補最近 30 個交易日
    python run_backfill_prices.py --dry-run       # 只印出會送什麼，不真的送
    python run_backfill_prices.py --market twse   # 只補上市

設定 (與 --ingest 同一組，擇一):
    A. 專案根目錄 .env:  INGEST_URL=http://localhost:8100   INGEST_TOKEN=你的token
    B. 環境變數 INGEST_URL / INGEST_TOKEN
    C. 命令列 --ingest-url / --ingest-token

⚠️ 資訊參考，非投資建議。
"""
from __future__ import annotations

import argparse
import os
import sys

from stock_quant.datasource import load_market_history
from stock_quant.ingest import IngestConfig
from stock_quant.notify import load_dotenv
from stock_quant.price_ingest import collect_daily_bars, post_daily_prices

_ROOT = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(_ROOT, ".cache", "history.pkl")


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="回補全市場每日收盤價到 userdata 後端 (供回測頁使用)")
    parser.add_argument("--days", type=int, default=60,
                        help="回補最近 N 個交易日 (預設 60；受歷史快取上限限制)")
    parser.add_argument("--market", choices=["twse", "tpex", "all"], default="all")
    parser.add_argument("--ingest-url", default=None,
                        help="userdata 後端位址，例 http://localhost:8100 (預設讀 INGEST_URL)")
    parser.add_argument("--ingest-token", default=None,
                        help="ingestion 服務 token (預設讀 INGEST_TOKEN)")
    parser.add_argument("--no-cache", action="store_true",
                        help="不使用 .cache/history.pkl (會重抓，較慢且容易被限流)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印出每個交易日會送幾檔，不實際上傳")
    args = parser.parse_args(argv)

    cfg = IngestConfig.from_env()
    if args.ingest_url:
        cfg.base_url = args.ingest_url
    if args.ingest_token:
        cfg.token = args.ingest_token
    if not cfg.configured and not args.dry_run:
        print("❌ 缺 INGEST_URL / INGEST_TOKEN (.env、環境變數或 --ingest-url/--ingest-token)。")
        return 2

    markets = ("twse", "tpex") if args.market == "all" else (args.market,)
    print(f"逐日整批抓全市場歷史日K ({'/'.join(markets)}，最近 {args.days} 個交易日) ...")
    hist = load_market_history(
        markets, days=args.days,
        cache_path=None if args.no_cache else _CACHE,
        progress=print,
    )
    if not hist:
        print("❌ 一檔歷史都沒抓到 —— 可能 IP 被證交所限流，等幾分鐘再跑 (會接續補齊)。")
        return 1

    by_day = collect_daily_bars(hist, days=args.days)
    print(f"\n共 {len(hist)} 檔、{len(by_day)} 個交易日待上傳。")

    ok_days = failed = 0
    for trade_date in sorted(by_day):
        quotes = by_day[trade_date]
        if args.dry_run:
            print(f"  [dry-run] {trade_date} -> {len(quotes)} 檔")
            ok_days += 1
            continue
        ok, msg = post_daily_prices(cfg, trade_date, quotes, source="backfill")
        if ok:
            ok_days += 1
            print(f"  ✅ {msg}")
        else:
            failed += 1
            print(f"  ⚠️ {trade_date} 上傳失敗: {msg}")

    print(f"\n完成: {ok_days} 個交易日成功" + (f"、{failed} 個失敗 (可重跑本指令補)" if failed else "。"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
