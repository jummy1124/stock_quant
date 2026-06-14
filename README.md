# stock_quant — 台股盤中漲幅篩選 (盤中即時 + 盤後)

物件導向、分層、依賴反轉。**篩出「漲幅 3% ~ 漲停前一檔」的個股**，依漲幅由大到小排序，
多進程平行抓即時價。只看普通個股（ETF/權證/特別股/期貨等濾掉）。單一進入點 `run_intraday.py`：

- **交易時間（週一至五 09:00–13:30）**：**每分鐘**用 MIS 即時價當「今日K」算漲幅 → 篩選 → print（含股票名稱）+ 寫 Excel，可選 LINE 推播。
- **非交易時間（盤後／開盤前／假日）**：改用**最後一個交易日的完成日K**算漲幅（`--no-screen-when-closed` 可關閉）。

## 漲幅與篩選

對每檔有「昨收」與「今日現價」的個股計算 `漲幅% = (今收 − 昨收) / 昨收 × 100`，並套用篩選：

1. **漲幅 ≥ 3%**（`--min-change` 可調）
2. **收盤 ≤ 漲停前一檔**：用台股升降單位算漲停價，再往下一檔；排除已鎖漲停（`--include-limit-up` 可納入）

通過的個股依漲幅由大到小排序。`--no-filter` 可關閉篩選、輸出全市場漲幅排行。

**股票名稱**：啟動時用 EOD OpenAPI（含中文名稱）建立 `{代號:名稱}` 對照，補上輸出的股票名稱
（即時/歷史端點常不附名稱）；`--no-names` 可略過。

> ⚠️ 成交量單位已統一為「股」（MIS 即時與 TPEx 歷史的「張」會 ×1000 轉換）；顯示時再換回「張」。
> 技術面/漲幅篩選為機率性參考、會落後，**非投資建議**。

即時報價抓取對 MIS 限流做了**單批錯誤隔離**：切數十批平行抓，單批限流/逾時不會拖垮整次掃描，並回報「N/總批 失敗」覆蓋率警告。

## Excel 輸出

每個 cycle 用 `openpyxl` 把符合的個股覆蓋寫入同一個 `ranking.xlsx`（排名、代號、名稱、市場、現價、漲跌、漲幅%、量、開高低、昨收）。
`--excel PATH` 改路徑、`--no-excel` 關閉。檔案被 Excel 開著鎖住時會略過該次、不中斷迴圈。
依賴：標準函式庫 + `openpyxl`（`pip install openpyxl`）。

## LINE 推播

改用 **LINE 官方帳號的 Messaging API push**。加 `--notify line`，**預設每天 13:00 把符合的個股彙整成一則推出**。
設定（擇一；token 等同密碼，勿 commit）：`.env`（複製 `.env.example`）填 `LINE_CHANNEL_TOKEN` / `LINE_USER_ID`，或設同名環境變數。
模式：`--notify-mode daily`（預設）+ `--notify-time 13:00`；或 `--notify-mode realtime`（每次更新就推、同檔當天去重）。`--notify-top N` 只推前 N 名。

## 用法

```bash
cd stock_market

python run_intraday.py                            # 盤中每分鐘篩 3%~漲停前一檔 + 寫 ranking.xlsx
python run_intraday.py --once                     # 立刻跑一次 (盤中=即時 / 非盤中=最後交易日)
python run_intraday.py --min-change 5             # 改成漲幅 ≥ 5%
python run_intraday.py --include-limit-up         # 連已鎖漲停的也納入
python run_intraday.py --no-filter                # 不篩選，輸出全市場漲幅排行
python run_intraday.py --top 50                   # 畫面/LINE 只看前 50 名 (Excel 仍存全部)
python run_intraday.py --excel out/today.xlsx     # 自訂 Excel 路徑
python run_intraday.py --notify line              # 每日 13:00 彙整推播
python run_intraday.py 2330 2317                  # 只看指定個股
```

畫面輸出範例：

```
===== 漲幅篩選 (3%~漲停前一檔) 2026-06-12 11:30:00 [盤中即時] — 符合 2 檔 / 可算漲幅 1900 檔 / 全市場 1955 檔 =====
   #  代號      名稱          市場            現價       漲跌      漲幅%        量(張)
--------------------------------------------------------------------------
   1  2330    台積電         上市        109.50     9.50     9.50          25
   2  2317    鴻海          上市        103.00     3.00     3.00           9
```

## 運作方式 (盤中即時 + 每分鐘 + 多進程)

1. **啟動取得歷史日K**：全市場逐日整批（上市 MI_INDEX、上櫃 OTC清單+逐檔），對 TWSE 限流友善
   （單請求、退避、**逐日增量快取 `.cache/history.pkl`**，由最新往回補缺，最後交易日永遠會補上）。同時建立股票名稱對照。
2. **交易時間每分鐘**：MIS 即時 API **批次 + 多進程**抓現價，當「今日K」與昨收算漲幅、篩選、排序。
3. **非交易時間**：用最後一個交易日的完成日K 與前一日算漲幅（不抓即時）。
4. 每次 print 符合個股 + 覆蓋寫 `ranking.xlsx`；視設定推 LINE。

## 專案結構

```
stock_market/
├── run_intraday.py           # 唯一進入點: 盤中即時 + 盤後 EOD 自動切換 + 篩選 + Excel + LINE
├── .env.example              # LINE 推播設定範本 (複製成 .env 填值)
├── stock_quant/
│   ├── domain/               # DailyQuote / Market + is_individual_stock
│   ├── datasource/           # 個股清單(EOD) + 歷史日K(增量快取) + MIS即時 (單批錯誤隔離)
│   ├── limits.py             # 台股升降單位 + 漲停價/漲停前一檔 (篩選用)
│   ├── universe.py           # 全市場個股清單 + {代號:名稱} 對照
│   ├── intraday.py           # 漲幅排行/篩選引擎 IntradayRanker (盤中即時/盤後EOD 自動切換)
│   ├── excel_export.py       # 把結果寫進 .xlsx (openpyxl)
│   ├── notify.py             # LINE 推播 + 每日彙整 / 即時彙整 + .env 載入
│   └── scheduler.py          # 盤中時段判斷 + 常駐迴圈
└── tests/                    # 不需網路的單元測試 (53/53 通過)
```

## 執行測試

```bash
python tests/run_tests.py
```
