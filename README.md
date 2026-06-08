# stock_quant — 台股個股趨勢分析 (盤後 + 盤中即時)

物件導向、分層、依賴反轉，零第三方依賴。用多指標綜合評分判斷個股 **多頭 / 空頭 / 盤整**，
多進程平行。只看普通個股（ETF/權證/特別股/期貨等濾掉）。兩種模式：

- **盤後**（`run.py`）：列全市場個股 → 逐檔抓歷史日K判斷趨勢 → print。
- **盤中即時**（`run_intraday.py`）：盤中(09:00–13:30)**每分鐘**用 MIS 即時價接成「今日即時K」重算趨勢 → print。

## 盤中即時趨勢監控 (你要的功能)

```bash
cd stock_market

python run_intraday.py                 # 盤中每分鐘監控全市場個股趨勢，非盤中自動休眠
python run_intraday.py --limit 50      # 只監控前 50 檔 (降載)
python run_intraday.py 2330 2317       # 只監控指定個股
python run_intraday.py --once          # 立刻跑一次就結束 (測試，忽略盤中時段)
python run_intraday.py --interval 60 --batch-size 40 --procs 8
```

**運作方式（為何能盤中即時 + 每分鐘 + 多進程）**

1. **啟動取得歷史日K**：
   - 全市場 → 用『**逐日整批**』官方端點（上市 MI_INDEX、上櫃每日收盤），一天一次請求就拿到
     當天全市場所有個股，抓 ~75 個交易日只要 ~75 次請求（不是逐檔上萬次），對限流友善；
     並**本地快取**（`.cache/history.pkl`），當天抓一次、之後重跑秒開。
   - 指定少數個股 → 逐檔抓歷史（檔數少，OK）。
2. **盤中每分鐘**：用 MIS 即時 API **批次 + 多進程**抓現價（一次帶多檔、各批平行），
   把現價當「今日即時K」接在歷史後面 → 重算趨勢 → 印出快照與統計。
3. 只在盤中（週一至五 09:00–13:30）執行，非盤中自動休眠。

> 註：逐檔抓全市場歷史會被 TWSE 限流而卡住，所以全市場改用逐日整批；這也是先前
> 「抓不到任何股票」的原因（卡在逐檔抓 1957 檔歷史）。

輸出範例：

```
===== 盤中趨勢快照 2026-06-05 10:00:00 — 共 3 檔 =====
代號     市場   趨勢        分數     信心        成交     ADX
2317   上市   空頭        -5    1.0    122.80  100.00
2330   上市   多頭        +5    1.0    177.20  100.00
6488   上櫃   盤整        +0    0.0    100.00    3.45
統計: 多頭 1、空頭 1、盤整 1、資料不足 0
```

> ⚠️ 全市場每分鐘輪詢 MIS 量很大，可能被**限流/暫時封鎖 IP**。可用 `--limit`／指定個股降載，
> 或調大 `--interval`。MIS 需從一般網路存取（程式已帶瀏覽器標頭）。
> 技術分析為機率性參考、會落後，**非投資建議**。

## 盤後趨勢掃描

```bash
python run.py                  # 全市場個股，用歷史日K判斷趨勢
python run.py 2330 6488 --months 6
```

## 趨勢判斷邏輯 (多指標綜合評分)

五個訊號各投 +1/0/−1 加總成分數，再用 ADX 當趨勢強度過濾：

1. 均線排列 MA5/MA20/MA60　2. 價格 vs MA20　3. MA20 斜率　4. MACD　5. DMI 方向

分數 ≥ +2 多頭、≤ −2 空頭、其餘盤整；**ADX < 20（趨勢不明）一律盤整**。

## 專案結構

```
stock_market/
├── run.py                    # 盤後: 逐檔抓歷史日K判斷趨勢
├── run_intraday.py           # 盤中即時: 每分鐘用即時價重算趨勢
├── stock_quant/
│   ├── domain/               # DailyQuote / Market + is_individual_stock
│   ├── datasource/           # 個股清單(EOD) + 歷史日K + MIS即時
│   ├── analysis/             # indicators + TrendClassifier
│   ├── universe.py           # 全市場個股清單
│   ├── scanner.py            # 盤後多進程逐檔掃描
│   ├── intraday.py           # 盤中監控 (快取歷史 + 每分鐘即時重算)
│   └── scheduler.py          # 盤中時段判斷 + 每分鐘迴圈
└── tests/                    # 不需網路的單元測試 (30/30 通過)
```

## 資料來源

- 個股清單：TWSE `STOCK_DAY_ALL` / TPEx `tpex_mainboard_daily_close_quotes`
- 歷史日K(全市場/逐日整批)：上市 TWSE `MI_INDEX`、上櫃改用 OTC清單+逐檔 `tradingStock`（新版端點）；指定個股上市用 `STOCK_DAY`
- 盤中即時：`https://mis.twse.com.tw/stock/api/getStockInfo.jsp`

## 執行測試

```bash
python tests/run_tests.py
```
