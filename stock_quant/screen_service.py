"""篩選快照持有者 + 純 dict 序列化 (不依賴 web 框架)。

把「最新一次篩選結果」存成一份執行緒安全的快照，供 stock_quant/api.py 的 HTTP
端點讀取。結果由外部 (run_intraday.py --serve 的每分鐘迴圈) 算好後用 publish() 發佈
—— API 自己不抓資料、不跑迴圈，因此不會和 CLI 重複爬取即時價。

只用標準函式庫 + 既有 stock_quant 模型，沒裝 fastapi 也能 import/測試。
⚠️ 篩選為資訊參考，非投資建議。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .breakout_screen import ScoredRow
from .intraday import IntradayRanker, RankRow


# ============================================================
# 設定 (環境變數)
# ============================================================

@dataclass
class ApiConfig:
    # 前端在別網域時設 ALLOWED_ORIGINS (逗號分隔)；預設 * 方便開發
    allowed_origins: tuple[str, ...] = ("*",)

    @classmethod
    def from_env(cls) -> "ApiConfig":
        origins = os.environ.get("ALLOWED_ORIGINS", "*")
        return cls(allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip())
                   or ("*",))


# ============================================================
# 快照
# ============================================================

@dataclass
class Snapshot:
    """某一次 tick 的完整結果，HTTP handler 只讀這份。"""
    generated_at: datetime
    source: str                       # "live" 盤中即時 / "eod" 最後交易日
    universe: int                     # 全市場掃描檔數
    quotable: int                     # 實際可算漲幅檔數
    pool_size: int                    # 通過漲幅池的檔數
    warning: Optional[str]            # 即時報價失敗摘要 (若有)
    pool: list = field(default_factory=list)        # list[RankRow]
    breakout: list = field(default_factory=list)    # list[ScoredRow]


# ============================================================
# 服務 (純快照持有者；不自行抓資料/跑迴圈)
# ============================================================

class ScreenService:
    """執行緒安全的篩選快照持有者。

    由外部 (run_intraday --serve 迴圈) attach_ranker() 後，每次 tick 算完用 publish()
    發佈最新結果；HTTP 端點用 snapshot() 讀。本身不 bootstrap、不抓即時價。
    """

    def __init__(self, config: Optional[ApiConfig] = None):
        self.config = config or ApiConfig()
        self._lock = threading.Lock()
        self._snapshot: Optional[Snapshot] = None
        self.ranker: Optional[IntradayRanker] = None
        self.ready = False
        self.last_error: Optional[str] = None

    def attach_ranker(self, ranker: IntradayRanker) -> None:
        """沿用外部已建好的 ranker (用來讀 universe/quotable/source/warning 狀態)。"""
        self.ranker = ranker
        self.ready = True

    def snapshot(self) -> Optional[Snapshot]:
        with self._lock:
            return self._snapshot

    def set_snapshot(self, snap: Snapshot) -> None:
        """直接設一份快照 (測試 / 外部組好時用)。"""
        with self._lock:
            self._snapshot = snap

    def publish(self, now: datetime, rows: list, scored: Optional[list]) -> Snapshot:
        """把『外部已算好』的漲幅池 rows + 起漲 scored 包成快照發佈。

        run_intraday 的每分鐘迴圈 tick 一次後，直接把同一份結果 publish 給 API，
        避免 API 自己再抓一次即時價 (零重複爬取)。
        """
        if self.ranker is None:
            raise RuntimeError("publish 需先設定 service.ranker (attach_ranker)")
        r = self.ranker
        snap = Snapshot(
            generated_at=now,
            source=getattr(r, "last_source", "live"),
            universe=len(r.pairs),
            quotable=getattr(r, "last_quoted", len(rows)),
            pool_size=len(rows),
            warning=getattr(r, "last_warning", None),
            pool=list(rows),
            breakout=list(scored or []),
        )
        self.set_snapshot(snap)
        return snap


# ============================================================
# 純 dict 序列化 (web 層套 pydantic 用)
# ============================================================

def stock_to_dict(r: RankRow) -> dict:
    market = getattr(r, "market", None)
    return dict(
        symbol=r.symbol,
        name=r.name or "",
        market=getattr(market, "zh", "") or "",
        market_code=getattr(market, "name", "") or "",
        close=r.close, prev_close=r.prev_close, change=r.change,
        change_pct=r.change_pct, volume=r.volume, lots=r.lots,
        open=r.open, high=r.high, low=r.low,
    )


def breakout_to_dict(sr: ScoredRow) -> dict:
    d = stock_to_dict(sr.row)
    d.update(
        prev_high=sr.prev_high, vol_ratio=sr.vol_ratio,
        ma5=sr.ma5, ma20=sr.ma20, ma20_up=sr.ma20_up, reasons=list(sr.reasons),
    )
    return d


def meta_to_dict(snap: Optional[Snapshot], service: "ScreenService", count: int) -> dict:
    if snap is None:
        return dict(generated_at=None, age_seconds=None, source=None, universe=0,
                    quotable=0, pool_size=0, count=0, warning=None,
                    last_error=service.last_error)
    now = datetime.now()
    return dict(
        generated_at=snap.generated_at.isoformat(timespec="seconds"),
        age_seconds=round((now - snap.generated_at).total_seconds(), 1),
        source=snap.source, universe=snap.universe, quotable=snap.quotable,
        pool_size=snap.pool_size, count=count, warning=snap.warning,
        last_error=service.last_error,
    )
