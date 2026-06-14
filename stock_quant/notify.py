"""LINE 推播通知 (Messaging API push) + 命中彙整。

LINE Notify 已於 2025/3/31 終止，改用 LINE 官方帳號的 Messaging API push endpoint。
零第三方依賴，用標準函式庫 urllib。

兩種彙整推播模式:
  - DailyDigestAlerter : 盤中累積所有確認個股，每天指定時間 (預設 13:00) 一次推完 (預設)。
  - StableAlerter      : 即時模式，個股一確認就推 (同一檔當天去重只推一次)。

設定方式 (擇一即可；token 等同密碼，請勿寫進程式或 commit 進 git):
  A. 專案根目錄放一個 .env 檔 (建議)，內容:
        LINE_CHANNEL_TOKEN=你的_long_lived_channel_access_token
        LINE_USER_ID=U你的userId        # 可逗號分隔多人
  B. 直接設環境變數 LINE_CHANNEL_TOKEN / LINE_USER_ID

.env 會被 load_dotenv() 載入到環境變數 (不覆蓋已存在的環境變數)。

⚠️ 技術面選股為機率性參考，非投資建議。
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date, datetime, time
from typing import Callable, Optional, Sequence

PUSH_URL = "https://api.line.me/v2/bot/message/push"

# 專案根目錄 (notify.py 在 stock_quant/ 底下 -> 上兩層)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path: Optional[str] = None, override: bool = False) -> dict[str, str]:
    """讀取 .env 檔 (KEY=VALUE 一行一筆) 並寫進 os.environ。

    - path 未給時，依序找 ./.env 與 <專案根>/.env，用第一個存在的。
    - override=False 時不覆蓋已設定的環境變數 (環境變數優先於 .env)。
    - 支援 # 註解、'export KEY=...'、值兩側的引號。回傳實際解析到的鍵值。
    """
    candidates = [path] if path else [os.path.join(os.getcwd(), ".env"),
                                       os.path.join(_ROOT, ".env")]
    loaded: dict[str, str] = {}
    for p in candidates:
        if not p or not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                val = val.strip().strip('"').strip("'")
                loaded[key] = val
                if override or key not in os.environ:
                    os.environ[key] = val
        break        # 只用第一個存在的 .env
    return loaded


class LineNotifier:
    """把一段文字 push 給一個或多個 LINE userId。"""

    def __init__(self, token: Optional[str] = None, user_ids=None, timeout: float = 10.0):
        if token is None or user_ids is None:       # 走預設來源時，先嘗試載入 .env
            load_dotenv()
        self.token = token if token is not None else os.environ.get("LINE_CHANNEL_TOKEN", "")
        ids = user_ids if user_ids is not None else os.environ.get("LINE_USER_ID", "")
        if isinstance(ids, str):
            ids = [x.strip() for x in ids.split(",") if x.strip()]
        self.user_ids = list(ids)
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.user_ids)

    def push(self, text: str) -> None:
        """推一則純文字訊息給所有 user_ids。任何一位失敗就丟出例外。"""
        if not self.configured:
            raise RuntimeError("LINE 未設定: 缺 LINE_CHANNEL_TOKEN 或 LINE_USER_ID (請設 .env 或環境變數)")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        for uid in self.user_ids:
            body = json.dumps({"to": uid, "messages": [{"type": "text", "text": text}]}).encode("utf-8")
            req = urllib.request.Request(PUSH_URL, data=body, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()


def _fmt_rows(head: str, rows: Sequence, empty_note: str) -> str:
    """共用的訊息格式: 標題 + 各檔一行 (依漲幅排序) + 免責聲明。"""
    if not rows:
        return f"{head}\n{empty_note}\n⚠️ 技術面參考，非投資建議"
    lines = [head]
    for r in sorted(rows, key=lambda x: -(x.result.change_pct or 0)):
        res = r.result
        cp = "-" if res.change_pct is None else f"{res.change_pct:.2f}"
        cl = "-" if res.close is None else f"{res.close:g}"
        name = getattr(r, "name", "") or ""
        lines.append(f"{r.symbol} {name}　收 {cl}　漲 {cp}%")
    lines.append("⚠️ 技術面參考，非投資建議")
    return "\n".join(lines)


class DailyDigestAlerter:
    """每天到指定時間 (預設 13:00) 的第一個 tick，推『當下』通過篩選的個股快照 (非累積)。

    - 到 fire_time 前不推；到點的第一個 tick 把「當下這個 tick 符合」的個股一次推完，當天不再推。
    - 即使當下沒有任何符合，也會在 fire_time 推一則「今日尚無符合」(讓你確認系統有在跑)。
    - 跨交易日自動重置。推播失敗不標記已送 -> 下個 tick 重試。
    """

    def __init__(self, notifier: LineNotifier, fire_time: time = time(12, 0),
                 log: Callable[[str], None] = print):
        self.notifier = notifier
        self.fire_time = fire_time
        self.log = log
        self._day: Optional[date] = None
        self._sent = False

    def process(self, now: datetime, rows: Sequence) -> list[str]:
        """rows: 當下這個 tick 通過篩選的個股。回傳本次推播的代號清單 (未到時間或已送則空)。"""
        if now.date() != self._day:                      # 跨交易日 -> 重置
            self._day = now.date()
            self._sent = False
        if self._sent or now.time() < self.fire_time:    # 還沒到推播時間 / 今天已推過
            return []
        snapshot = [r for r in rows if getattr(r, "stable", False)]   # 只取「當下」符合的個股
        syms = [r.symbol for r in snapshot]
        head = f"📈 選股日結 {now:%Y-%m-%d} {now:%H:%M}｜符合 {len(syms)} 檔"
        try:
            self.notifier.push(_fmt_rows(head, snapshot, "(今日尚無符合條件的個股)"))
        except Exception as exc:                         # noqa: BLE001 — 推播失敗不可中斷選股
            self.log(f"⚠️ LINE 日結推播失敗: {exc} (下個 tick 重試)")
            return []                                     # 不標記 -> 重試
        self._sent = True
        self.log(f"📨 LINE 日結已推 ({len(syms)} 檔)")
        return syms


class StableAlerter:
    """即時模式: 把每次 tick 的『已確認(stable)』個股，去重後彙整成一則訊息推播。

    - 一檔個股同一交易日只推一次 (靠已通知集合去重)；跨日自動重置。
    - 推播失敗不標記為已通知 -> 下個 tick 會重試，且不會中斷主迴圈。
    """

    def __init__(self, notifier: LineNotifier, log: Callable[[str], None] = print):
        self.notifier = notifier
        self.log = log
        self._notified: set[str] = set()
        self._day: Optional[date] = None

    def process(self, now: datetime, rows: Sequence) -> list[str]:
        """rows: list[TickRow]。回傳本次新推播的代號清單 (沒有就空)。"""
        if now.date() != self._day:                      # 跨交易日 -> 重置去重集合
            self._notified.clear()
            self._day = now.date()
        stable = [r for r in rows if getattr(r, "stable", False)]
        new = [r for r in stable if r.symbol not in self._notified]
        if not new:
            return []
        head = f"📈 選股提醒 {now:%H:%M:%S}｜新確認 {len(new)} 檔"
        try:
            self.notifier.push(_fmt_rows(head, new, "(無)"))
        except Exception as exc:                         # noqa: BLE001 — 推播失敗不可中斷選股
            self.log(f"⚠️ LINE 推播失敗: {exc}")
            return []                                     # 不標記 -> 下次重試
        for r in new:
            self._notified.add(r.symbol)
        return [r.symbol for r in new]
