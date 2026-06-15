"""HTTP API (FastAPI): 把盤中起漲篩選結果以 JSON 提供給「另一個 repo」的前端。

由 run_intraday.py --serve 在同一進程內以背景 thread 啟動: run_intraday 的每分鐘迴圈
tick 一次後 publish 給這裡的 ScreenService，前端輪詢 GET /api/screen 直接讀快照
(毫秒回應，且不會重複爬即時價)。本檔只負責 web 層 (FastAPI / pydantic / CORS / 端點)。

設定見 screen_service.ApiConfig / API.md。
⚠️ 篩選為資訊參考，非投資建議。
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .screen_service import (ApiConfig, ScreenService, breakout_to_dict,
                             meta_to_dict, stock_to_dict)


# ============================================================
# JSON 回應模型 (對前端的穩定契約)
# ============================================================

class StockRow(BaseModel):
    symbol: str = Field(..., description="股票代號")
    name: str = Field("", description="股票名稱")
    market: str = Field("", description="市場 (上市/上櫃)")
    market_code: str = Field("", description="市場代碼 (TWSE/TPEX)")
    close: Optional[float] = Field(None, description="現價/收盤")
    prev_close: Optional[float] = Field(None, description="昨收")
    change: Optional[float] = Field(None, description="漲跌價")
    change_pct: Optional[float] = Field(None, description="漲幅%")
    volume: Optional[int] = Field(None, description="成交股數")
    lots: Optional[float] = Field(None, description="成交量(張)")
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None


class BreakoutRow(StockRow):
    score: float = Field(..., description="起漲強度分 0~100 (僅排序用)")
    prev_high: Optional[float] = Field(None, description="昨日最高 (突破基準)")
    vol_ratio: Optional[float] = Field(None, description="當日量/昨量")
    ma5: Optional[float] = None
    ma20: Optional[float] = Field(None, description="月均線")
    ma20_up: bool = Field(False, description="月均線是否上彎")
    reasons: list[str] = Field(default_factory=list, description="入選理由")


class Meta(BaseModel):
    generated_at: Optional[str] = Field(None, description="本份資料產生時間 ISO8601")
    age_seconds: Optional[float] = Field(None, description="資料距今幾秒")
    source: Optional[str] = Field(None, description="live=盤中即時 / eod=最後交易日")
    universe: int = 0
    quotable: int = 0
    pool_size: int = 0
    count: int = 0
    warning: Optional[str] = None
    last_error: Optional[str] = None


class ScreenResponse(BaseModel):
    meta: Meta
    results: list[BreakoutRow]


class PoolResponse(BaseModel):
    meta: Meta
    results: list[StockRow]


# ============================================================
# FastAPI app
# ============================================================

def create_app(service: Optional[ScreenService] = None) -> FastAPI:
    """建立 app。service 由 run_intraday --serve 傳入 (已 attach_ranker)。"""
    config = (service.config if service else None) or ApiConfig.from_env()
    svc = service or ScreenService(config)

    app = FastAPI(
        title="台股盤中起漲篩選 API",
        version="1.0.0",
        description="GET /api/screen 取最新起漲個股 (由 run_intraday --serve 每分鐘更新)。"
                    "⚠️ 資訊參考，非投資建議。",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.state.service = svc

    def _not_ready() -> Optional[JSONResponse]:
        if svc.snapshot() is None:
            return JSONResponse(
                status_code=503,
                content={"meta": meta_to_dict(None, svc, 0), "results": [],
                         "detail": "資料尚未備妥 (首次篩選尚未完成)，請稍後重試。"},
            )
        return None

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "ready": svc.ready,
                "has_snapshot": svc.snapshot() is not None}

    @app.get("/api/meta", response_model=Meta, tags=["meta"])
    def meta() -> Meta:
        snap = svc.snapshot()
        return Meta(**meta_to_dict(snap, svc, len(snap.breakout) if snap else 0))

    @app.get("/api/screen", response_model=ScreenResponse, tags=["screen"],
             summary="最新起漲個股 (6 條件)")
    def screen(
        top: int = Query(0, ge=0, description="只取前 N 名 (0=全部)"),
        min_score: float = Query(0.0, ge=0, description="強度分下限過濾"),
    ):
        nr = _not_ready()
        if nr:
            return nr
        snap = svc.snapshot()
        rows = [sr for sr in snap.breakout if sr.score >= min_score]
        if top > 0:
            rows = rows[:top]
        return ScreenResponse(
            meta=Meta(**meta_to_dict(snap, svc, len(rows))),
            results=[BreakoutRow(**breakout_to_dict(sr)) for sr in rows],
        )

    @app.get("/api/pool", response_model=PoolResponse, tags=["screen"],
             summary="第一層漲幅池 (3%~漲停前一檔)")
    def pool(top: int = Query(0, ge=0, description="只取前 N 名 (0=全部)")):
        nr = _not_ready()
        if nr:
            return nr
        snap = svc.snapshot()
        rows = snap.pool[:top] if top > 0 else snap.pool
        return PoolResponse(
            meta=Meta(**meta_to_dict(snap, svc, len(rows))),
            results=[StockRow(**stock_to_dict(r)) for r in rows],
        )

    return app
