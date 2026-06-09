# stock_quant — 台股個股選股 (盤後 + 盤中即時)

物件導向、分層、依賴反轉，零第三方依賴。**用 6 條規則篩出符合的個股**，多進程平行。
只看普通個股（ETF/權證/特別股/期貨等濾掉）。兩種模式：

- **盤後**（`run.py`）：用最近兩個交易日日K篩選 → print。
- **盤中即時**（`run_intraday.py`）：盤中(09:00–13:30)**每分鐘**用 MIS 即時價當「今日K」篩選 → print。

## 篩選規則 (BreakoutScreen)

對「當日K」與「前一交易日」檢查，全部符合才入選：

1. **紅K**：收盤 > 開盤
2. **漲幅 3% ~ 漲停前一檔**：`(收-昨收)/昨收 ≥ 3%`，且收盤 ≤ 漲停前一檔（用台股升降單位算漲停，自然排除鎖漲停）
3. **上影線 ≤ 1%**：`(最高 - max(開,收)) / 收盤 ≤ 1%`
4. **量增 1.2 倍**：今日量 ≥ 1.2 × 昨日量
5. **前一日收盤 < MA5**：昨收 < 最近五日(含昨日)收盤均線
6. **今日現價 > 昨日最高**：今日收盤(即時現價) > 昨日最高價

參數可在 `BreakoutScreen(min_change_pct=3.0, max_upper_shadow_pct=1.0, min_vol_ratio=1.2)` 調整。

> ⚠️ 成交量單位已統一為「股」（MIS 即時與 TPEx 歷史的「張」會 ×1000 轉換），量比才正確。
> 技術面選股為機率性參考、會落後，**非投資建議**。

## 用法

```bash
cd stock_market

python run_intraday.py                 # 盤中每分鐘篩全市場個股，非盤中自動休眠
python run_intraday.py --limit 50      # 只看前 50 檔 (降載)
python run_intraday.py 2330 2317       # 只看指定個股
python run_intraday.py --once          # 立刻跑一次就結束 (測試)

python run.py                          # 盤後全市場篩選
python run.py --market twse --limit 100
```

輸出範例（只印符合的個股，依漲幅排序）：

```
===== 選股快照 2026-06-05 10:00:00 — 符合 1 檔 / 掃描 1900 檔 =====
代號     市場           收盤     漲幅%     上影線%      量比
2330   上市       105.00    5.00     0.19    1.50
```

## 運作方式 (盤中即時 + 每分鐘 + 多進程)

1. **啟動取得歷史日K**：全市場用『逐日整批』（上市 MI_INDEX、上櫃 OTC清單+逐檔），
   對限流友善並**本地快取** `.cache/history.pkl`；指定個股則逐檔抓。
2. **盤中每分鐘**：MIS 即時 API **批次 + 多進程**抓現價，當「今日K」與昨日比對篩選。
3. 只在盤中(週一至五 09:00–13:30)執行，非盤中休眠；**選股只認今日即時資料**，這分鐘沒拿到即時價的個股當次跳過。

## 專案結構

```
stock_market/
├── run.py                    # 盤後選股
├── run_intraday.py           # 盤中即時選股 (每分鐘)
├── stock_quant/
│   ├── domain/               # DailyQuote / Market + is_individual_stock
│   ├── datasource/           # 個股清單(EOD) + 歷史日K + MIS即時
│   ├── analysis/             # BreakoutScreen 選股篩選器
│   ├── universe.py           # 全市場個股清單
│   ├── intraday.py           # 盤中選股 (快取歷史 + 每分鐘即時篩選)
│   └── scheduler.py          # 盤中時段判斷 + 每分鐘迴圈
└── tests/                    # 不需網路的單元測試 (32/32 通過)
```

## 資料來源

- 個股清單：TWSE `STOCK_DAY_ALL` / TPEx `tpex_mainboard_daily_close_quotes`
- 歷史日K：上市 `MI_INDEX`(逐日整批)、上櫃 OTC清單+`tradingStock`(逐檔)；指定個股上市用 `STOCK_DAY`
- 盤中即時：`https://mis.twse.com.tw/stock/api/getStockInfo.jsp`

## 執行測試

```bash
python tests/run_tests.py
```
