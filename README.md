# stock_quant — 台股個股爬蟲

用物件導向、分層、依賴反轉設計的量化系統起步框架，零第三方依賴。
提供兩種爬法，**都只保留普通個股**（ETF、權證、特別股、TDR、期貨等非個股會被濾掉），且都只 print（不存資料庫）：

- **盤後 EOD**（`run_crawl.py`）：官方 OpenAPI 全市場快照，反映「最後交易日收盤」。
- **盤中即時**（`run_intraday.py`）：MIS 即時 API，盤中每分鐘抓全市場個股最新報價。

## EOD 與盤中的差別（重要）

官方 OpenAPI 端點（`STOCK_DAY_ALL` / `tpex_mainboard_daily_close_quotes`）是**盤後 EOD 資料**，
盤中不會逐分鐘更新，要等收盤後才有當日資料 —— 所以它**做不到盤中即時**。
盤中即時要用證交所 **MIS 即時報價 API**（盤中約數秒更新一次），這就是 `run_intraday.py` 用的來源。

## 專案結構

```
stock_market/
├── run_crawl.py              # 盤後 EOD 爬蟲 (全市場快照)
├── run_intraday.py           # 盤中即時爬蟲 (每分鐘常駐迴圈)
├── ARCHITECTURE.md
├── stock_quant/
│   ├── domain/               # DailyQuote / Market + is_individual_stock
│   ├── datasource/           # TWSE/TPEx (EOD OpenAPI) + MIS (盤中即時)
│   ├── crawler/              # MultiProcessCrawler 多進程抓取
│   ├── universe.py           # 由 EOD 取得全市場個股清單，餵給 MIS
│   └── scheduler.py          # 盤中時段判斷 + 每分鐘常駐迴圈
└── tests/                    # 不需網路的單元測試 (14/14 通過)
```

## 盤中每分鐘即時 (你要的功能)

```bash
cd stock_market

# 盤中(09:00-13:30, 週一至五)每分鐘抓全市場個股最新報價並印出，非盤中自動休眠
python run_intraday.py

# 立刻抓一次就結束 (測試用，忽略盤中時段)
python run_intraday.py --once

# 只印前 20 檔 / 調整頻率與批次
python run_intraday.py --limit 20 --interval 60 --batch-size 40 --procs 8
```

運作方式：啟動時用 EOD 取得全市場個股清單(universe) → 把上千檔切成數十檔一批 →
每批一個工作單位 fan-out 到多進程平行查 MIS → 每分鐘印一張即時快照。

> ⚠️ **流量提醒**：全市場每分鐘輪詢 MIS 量很大，可能被**限流或暫時封鎖 IP**。
> 想降低風險可調大 `--interval`、調整 `--batch-size`，或改成只鎖定少數關注個股。
> MIS 需從一般網路環境存取（程式已帶瀏覽器標頭）。盤中才有即時價，非盤中回傳的是最後成交。

## 盤後 EOD

```bash
python run_crawl.py                  # 抓上市+上櫃個股收盤
python run_crawl.py --market twse --limit 20
```

## 資料來源

- EOD 上市：`https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL`
- EOD 上櫃：`https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes`
- 盤中即時：`https://mis.twse.com.tw/stock/api/getStockInfo.jsp`（上市 `tse_`、上櫃 `otc_`）

## 執行測試

```bash
python tests/run_tests.py        # 零依賴執行器，自動發現 tests/test_*.py
```

## 下一步

要把每分鐘的快照存進資料庫/CSV 做後續分析、回測，或加技術指標、API，
照 ARCHITECTURE.md 的擴充路徑新增模組即可，爬蟲程式不用改。
