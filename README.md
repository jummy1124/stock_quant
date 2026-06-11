# stock_quant — 台股個股選股 (盤中即時 + 盤後)

物件導向、分層、依賴反轉，零第三方依賴。**用 7 條規則篩出符合的個股**，多進程平行。
只看普通個股（ETF/權證/特別股/期貨等濾掉）。單一進入點 `run_intraday.py`，資料來源依時間自動切換：

- **交易時間（週一至五 09:00–13:30）**：**每分鐘**用 MIS 即時價當「今日K」篩選 → print，可選 LINE 推播。
- **非交易時間（盤後／開盤前／假日）**：改用**最後一個交易日的完成日K**篩選（用 `--no-screen-when-closed` 可關閉、非盤中就純休眠）。

## 篩選規則 (BreakoutScreen)

對「當日K」與「歷史日K（結束於前一交易日）」檢查，全部符合才入選：

1. **紅K**：收盤 > 開盤
2. **漲幅 3% ~ 漲停前一檔**：`(收-昨收)/昨收 ≥ 3%`，且收盤 ≤ 漲停前一檔（用台股升降單位算漲停，自然排除鎖漲停）
3. **上影線 ≤ 1%**：`(最高 - max(開,收)) / 收盤 ≤ 1%`
4. **量增 1.2 倍（同時段）**：盤中把今日累積量依「已過盤中時段比例」換算成同時段量比，早盤才不會天生篩不出量增股；收盤後等同 `今日量 ≥ 1.2 × 昨日量`
5. **前一日收盤 < MA5**：昨收 < 最近五日(含昨日)收盤均線（短線回檔）
6. **今日現價 > 昨日最高**：今日收盤(即時現價) > 昨日最高價（突破昨高）
7. **多頭趨勢閘門**：站上月線（今價 > MA20）+ 月線 ≥ 季線（MA20 ≥ MA60）+ 頭頭高、底底高（碎形偵測近期擺動高/低點皆墊高）。用來擋掉空頭股的單日反彈；可用 `--no-trend` 關閉

參數可在 `BreakoutScreen(min_change_pct=3.0, max_upper_shadow_pct=1.0, min_vol_ratio=1.2, require_uptrend=True, ma_month_period=20, ma_quarter_period=60)` 調整。多頭趨勢閘門需 **≥60 個交易日**歷史才能判定，不足者不入選並註記。

> ⚠️ 成交量單位已統一為「股」（MIS 即時與 TPEx 歷史的「張」會 ×1000 轉換），量比才正確。
> 技術面選股為機率性參考、會落後，**非投資建議**。

## 穩定層（盤中去雜訊）

即時價是「盤中瞬間值」，把它當完成K線套規則時，命中名單會跨分鐘跳動（尤其上影線/突破這類門檻型規則）。所以盤中加了一層：

- **連續確認**：個股需連續 N 次 tick（預設 2）都符合才「確認」入選，過濾單次雜訊。
- **寬限窗**：確認後即使某次瞬間不符，仍保留幾次（預設 1），避免小跳動就消失。
- 只有「已確認(stable)」的個股才會被印出/推播。可用 `--confirm-ticks` / `--grace-ticks` 調整（`--once` 自動退回單次）。
- 非交易時間用完成日K是定案資料、不會跳動，因此**不套穩定層、直接報出**。

即時報價抓取對 MIS 限流也做了**單批錯誤隔離**：整個市場切數十批平行抓，單批限流/逾時不會拖垮整次掃描，並回報「N/總批 失敗」的覆蓋率警告。

## LINE 推播

LINE Notify 已於 2025/3/31 終止，本專案改用 **LINE 官方帳號的 Messaging API push**。加 `--notify line` 即可，**預設每天 13:00 把當日累積到的確認個股彙整成一則推出**（一天 1 則，免費額度無壓力）。

設定（擇一；token 等同密碼，**勿寫進程式或 commit**）：

- **`.env`（建議）**：複製 `.env.example` 成 `.env`（已被 `.gitignore` 忽略）並填入：

  ```
  LINE_CHANNEL_TOKEN=你的_long_lived_channel_access_token
  LINE_USER_ID=U你的userId        # 可逗號分隔多人
  ```

- 或直接設環境變數 `LINE_CHANNEL_TOKEN` / `LINE_USER_ID`。

token / userId 取得：LINE Developers Console > 你的 channel > **Messaging API** 分頁發 *Channel access token (long-lived)*；**Basic settings** 分頁的 *Your user ID*。記得先用手機把官方帳號加為好友，否則推播會失敗。

推播模式：

- `--notify-mode daily`（預設）+ `--notify-time 13:00`：每日定時彙整一次。
- `--notify-mode realtime`：個股一確認就推（同一檔當天去重只推一次）。

> 注意：是程式自己跑到設定時間才觸發，所以**該時間點程式必須在迴圈裡運行中**。

## 用法

```bash
cd stock_market

python run_intraday.py --notify line              # 盤中每分鐘篩，每日 13:00 推 LINE (預設)
python run_intraday.py --notify line --notify-mode realtime   # 改成命中即時推
python run_intraday.py --limit 50                 # 只看前 50 檔 (降載)
python run_intraday.py 2330 2317                  # 只看指定個股
python run_intraday.py --no-trend                 # 關閉多頭趨勢閘門 (只跑原 6 條)
python run_intraday.py --once                     # 立刻跑一次 (盤中=即時 / 非盤中=最後交易日)
python run_intraday.py --no-screen-when-closed    # 非交易時間純休眠，不用 EOD 資料
```

快照輸出範例（標題標示資料來源；只印已確認個股，依漲幅排序）：

```
===== 選股快照 2026-06-05 10:00:00 [盤中即時] — 確認 1 檔 / 可篩 1900 檔 / 全市場 1955 檔 =====
代號     市場           收盤     漲幅%     上影線%      量比
2330   上市       105.00    5.00     0.19    1.50
```

LINE 日結訊息範例：

```
📈 選股日結 2026-06-11 13:00｜符合 3 檔
2414 上市　收 56.8　漲 9.65%
6173 上櫃　收 228　漲 8.83%
2836 上市　收 12.3　漲 3.36%
⚠️ 技術面參考，非投資建議
```

## 運作方式 (盤中即時 + 每分鐘 + 多進程)

1. **啟動取得歷史日K**：全市場用『逐日整批』（上市 MI_INDEX、上櫃 OTC清單+逐檔），
   對 TWSE 限流友善（單請求、退避、**逐日增量快取 `.cache/history.pkl` + 續抓**）；指定個股則逐檔抓。
2. **交易時間每分鐘**：MIS 即時 API **批次 + 多進程**抓現價，當「今日K」與歷史比對篩選，套穩定層。
3. **非交易時間**：用最後一個交易日的完成日K篩選（不抓即時）；可用 `--no-screen-when-closed` 關閉。
4. 視設定在指定時間 / 命中時把確認結果推到 LINE。

## 專案結構

```
stock_market/
├── run_intraday.py           # 唯一進入點: 盤中即時 + 盤後 EOD 自動切換 + LINE 推播
├── .env.example              # LINE 推播設定範本 (複製成 .env 填值)
├── stock_quant/
│   ├── domain/               # DailyQuote / Market + is_individual_stock
│   ├── datasource/           # 個股清單(EOD) + 歷史日K(增量快取) + MIS即時 (單批錯誤隔離)
│   ├── analysis/             # BreakoutScreen 選股篩選器 (7 規則 + 多頭趨勢閘門)
│   ├── universe.py           # 全市場個股清單
│   ├── intraday.py           # 選股引擎 (快取歷史 + 盤中即時/盤後EOD 自動切換 + 穩定層)
│   ├── notify.py             # LINE 推播 + 每日彙整 / 即時彙整 + .env 載入
│   └── scheduler.py          # 盤中時段判斷 + 常駐迴圈 + 已過時段比例
└── tests/                    # 不需網路的單元測試 (63/63 通過)
```

## 資料來源

- 個股清單：TWSE `STOCK_DAY_ALL` / TPEx `tpex_mainboard_daily_close_quotes`
- 歷史日K：上市 `MI_INDEX`(逐日整批，rwd 新端點+舊端點備援)、上櫃 OTC清單+`tradingStock`(逐檔)；指定個股上市用 `STOCK_DAY`
- 盤中即時：`https://mis.twse.com.tw/stock/api/getStockInfo.jsp`
- LINE 推播：`https://api.line.me/v2/bot/message/push`（Messaging API）

## 執行測試

```bash
python tests/run_tests.py
```
