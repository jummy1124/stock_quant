"""把每日篩選結果 POST 到 userdata 後端 (stock_quant_backend) 的 ingestion 端點。

設計同 notify.py 的 alerter：零第三方依賴 (只用標準函式庫 urllib)，失敗不可中斷選股主迴圈。

兩種快照 (對應後端 SESSIONS)：
  - intraday_1300 : 交易日盤中、到指定時間 (預設 13:00) 的第一個 tick，把當下起漲個股推一份。
  - eod           : 收盤後用「最後交易日完成日K」算出的起漲個股，每個交易日推一次
                    (只在該交易日確實收盤後才送；不會在盤前/收盤前就冒出當日的盤後快照)。

去重以 (session, 交易日) 為鍵：同一交易日同一 session 只推一次 (後端本身也是 idempotent
upsert，重推只會覆蓋，故行程重啟造成的重推無害)。

設定 (擇一；token 等同密碼，勿 commit)：
  A. 專案根目錄 .env：INGEST_URL=http://localhost:8100  INGEST_TOKEN=你的token
  B. 環境變數 INGEST_URL / INGEST_TOKEN
啟用：run_intraday.py --ingest

⚠️ 篩選為資訊參考，非投資建議。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable, Optional, Sequence

from .screen_service import breakout_to_dict, stock_to_dict

# 後端 SESSIONS (與 app/models.py 對齊)
SESSION_INTRADAY_1300 = "intraday_1300"
SESSION_EOD = "eod"

_INGEST_PATH = "/userapi/ingest/snapshot"


@dataclass
class IngestConfig:
    base_url: str = ""          # 例 http://localhost:8100 (結尾不帶 /)
    token: str = ""
    timeout: float = 10.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}{_INGEST_PATH}"

    @classmethod
    def from_env(cls) -> "IngestConfig":
        return cls(
            base_url=os.environ.get("INGEST_URL", "").strip(),
            token=os.environ.get("INGEST_TOKEN", "").strip(),
        )


# ---------------------------------------------------------------------------
# Payload 組裝 (ScoredRow=起漲 / RankRow=漲幅池 皆可)
# ---------------------------------------------------------------------------

def _item_from_scored(rank: int, sr) -> dict:
    d = breakout_to_dict(sr)
    return {
        "rank": rank,
        "symbol": d["symbol"], "name": d["name"], "market": d["market"],
        "market_code": d["market_code"], "close": d["close"],
        "prev_close": d["prev_close"], "change": d["change"],
        "change_pct": d["change_pct"], "volume": d["volume"], "lots": d["lots"],
        "open": d["open"], "high": d["high"], "low": d["low"],
        "prev_high": d["prev_high"], "vol_ratio": d["vol_ratio"],
        "ma5": d["ma5"], "ma20": d["ma20"], "ma20_up": d["ma20_up"],
    }


def _item_from_pool(rank: int, row) -> dict:
    d = stock_to_dict(row)
    return {
        "rank": rank,
        "symbol": d["symbol"], "name": d["name"], "market": d["market"],
        "market_code": d["market_code"], "close": d["close"],
        "prev_close": d["prev_close"], "change": d["change"],
        "change_pct": d["change_pct"], "volume": d["volume"], "lots": d["lots"],
        "open": d["open"], "high": d["high"], "low": d["low"],
        "prev_high": None, "vol_ratio": None, "ma5": None, "ma20": None,
        "ma20_up": False,
    }


def build_payload(now: datetime, trade_date: date, session: str, source: str,
                  scored: Optional[Sequence], pool_rows: Sequence,
                  ranker) -> dict:
    """組一份 ingestion payload。scored 有給就存起漲結果，否則退化存漲幅池。"""
    if scored is not None:
        items = [_item_from_scored(i, sr) for i, sr in enumerate(scored, start=1)]
    else:
        items = [_item_from_pool(i, r) for i, r in enumerate(pool_rows, start=1)]
    return {
        "trade_date": trade_date.isoformat(),
        "session": session,
        "generated_at": now.isoformat(timespec="seconds"),
        "source": source,
        "universe": len(getattr(ranker, "pairs", []) or []),
        "quotable": int(getattr(ranker, "last_quoted", len(pool_rows)) or 0),
        "pool_size": len(pool_rows),
        "warning": getattr(ranker, "last_warning", None),
        "items": items,
    }


# ---------------------------------------------------------------------------
# HTTP POST (best-effort)
# ---------------------------------------------------------------------------

def post_snapshot(cfg: IngestConfig, payload: dict) -> tuple[bool, str]:
    """POST 一份快照。回傳 (ok, 說明)。任何錯誤都吞掉、回 (False, msg)。"""
    if not cfg.configured:
        return False, "INGEST_URL / INGEST_TOKEN 未設定"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg.url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Token": cfg.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return True, body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        return False, f"HTTP {exc.code}: {detail[:200]}"
    except Exception as exc:  # noqa: BLE001 — 上傳失敗不可中斷選股主迴圈
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 觸發器 (每 tick 呼叫；自行判斷是否該推)
# ---------------------------------------------------------------------------

def _eod_trade_date(ranker) -> Optional[date]:
    """非交易時間: 用 ranker 任一檔歷史最後一根的交易日當『最後交易日』。"""
    for sym, _market in getattr(ranker, "pairs", []) or []:
        hist = ranker.history(sym)
        if hist:
            return hist[-1].trade_date
    return None


class SnapshotIngestor:
    """每 tick 呼叫 process()，到條件就把當下篩選結果 POST 給後端，同日同 session 去重。

    - intraday_1300: source=live 且 now>=fire_time 的第一個 tick → 推一次。
    - eod          : source=eod，每個不同『最後交易日』推一次。
    """

    def __init__(self, cfg: IngestConfig, fire_time: time = time(13, 0),
                 close_time: time = time(13, 30),
                 log: Callable[[str], None] = print):
        self.cfg = cfg
        self.fire_time = fire_time
        self.close_time = close_time   # 交易日收盤時間;盤後快照只在該交易日收盤後才送
        self.log = log
        self._sent: set[tuple[str, str]] = set()   # (session, trade_date.isoformat())

    def _send(self, now, trade_date, session, source, scored, pool_rows, ranker) -> None:
        key = (session, trade_date.isoformat())
        if key in self._sent:
            return
        payload = build_payload(now, trade_date, session, source, scored, pool_rows, ranker)
        ok, msg = post_snapshot(self.cfg, payload)
        if ok:
            self._sent.add(key)
            self.log(f"📤 已上傳 {session} 快照 ({trade_date} 共 {len(payload['items'])} 檔)")
        else:
            # 不標記 -> 下個 tick 重試
            self.log(f"⚠️ 上傳 {session} 快照失敗: {msg} (下個 tick 重試)")

    def process(self, now: datetime, scored: Optional[Sequence],
                pool_rows: Sequence, ranker) -> None:
        if not self.cfg.configured:
            return
        source = getattr(ranker, "last_source", "live")
        if source == "live":
            if now.time() < self.fire_time:
                return
            self._send(now, now.date(), SESSION_INTRADAY_1300, source,
                       scored, pool_rows, ranker)
        else:  # eod
            td = _eod_trade_date(ranker)
            if td is None:
                return   # 歷史未就緒 -> 無可靠的最後交易日,不送 (別用今天日期造出假的盤後快照)
            if td >= now.date() and now.time() < self.close_time:
                return   # 該交易日=今天且尚未收盤 -> 盤後快照還不該出現 (前端才不會提早顯示下載)
            self._send(now, td, SESSION_EOD, source, scored, pool_rows, ranker)
