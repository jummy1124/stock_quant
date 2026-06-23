"""HTTP API (FastAPI): 把盤中起漲篩選結果以 JSON 提供給「另一個 repo」的前端。

由 run_intraday.py --serve 在同一進程內以背景 thread 啟動: run_intraday 的每分鐘迴圈
tick 一次後 publish 給這裡的 ScreenService，前端輪詢 GET /api/screen 直接讀快照
(毫秒回應，且不會重複爬即時價)。本檔只負責 web 層 (FastAPI / pydantic / CORS / 端點)。

歷史端點 GET /api/history/{symbol} 例外: 它即時向交易所逐月抓歷史日K (含 TTL 快取)，
不讀快照 — 詳見 history_api.py 與 API.md。

設定見 screen_service.ApiConfig / API.md。
⚠️ 篩選為資訊參考，非投資建議。
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .breakout_screen import BreakoutConfig
from .history_api import get_history_candles, get_intraday_quote
from .screen_service import (ApiConfig, ScreenService, breakout_to_dict,
                             meta_to_dict, stock_to_dict)

# 篩選參數預設值 (與 run_intraday CLI / BreakoutConfig 原始設定一致)。
# 前端 GET /api/screen-defaults 讀這份，當作「恢復預設」依據 (單一真實來源)。
_DEFAULTS = {
    # 第一層 漲幅池
    "min_change": 3.0,            # 漲幅下限 %
    "exclude_limit_up": True,     # 排除已鎖漲停 (收盤 ≤ 漲停前一檔)
    # 第二層 起漲點 6 條件
    "vol_ratio": BreakoutConfig.vol_ratio_min,            # 1.2 當日量/昨量下限
    "ma_short": BreakoutConfig.ma_short,                  # 5  5MA 天數
    "ma_mid": BreakoutConfig.ma_mid,                      # 20 月均線天數
    "ma_slope_lookback": BreakoutConfig.ma_mid_slope_lookback,  # 5 月線上彎回看天數
    "vol_projection": BreakoutConfig.use_volume_projection,     # False 是否用全日預估量
}


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
    prev_high: Optional[float] = Field(None, description="昨日最高 (突破基準)")
    vol_ratio: Optional[float] = Field(None, description="當日量/昨量")
    ma5: Optional[float] = None
    ma20: Optional[float] = Field(None, description="月均線")
    ma20_up: bool = Field(False, description="月均線是否上彎")


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


class ScreenDefaults(BaseModel):
    """篩選參數預設值 (前端「恢復預設」用)。"""
    min_change: float = Field(..., description="漲幅下限 %")
    exclude_limit_up: bool = Field(..., description="排除已鎖漲停")
    vol_ratio: float = Field(..., description="條件3 當日量/昨量下限")
    ma_short: int = Field(..., description="5MA 天數")
    ma_mid: int = Field(..., description="月均線天數")
    ma_slope_lookback: int = Field(..., description="月線上彎回看天數")
    vol_projection: bool = Field(..., description="條件3 是否用全日預估量")


class ScreenResponse(BaseModel):
    meta: Meta
    results: list[BreakoutRow]


class PoolResponse(BaseModel):
    meta: Meta
    results: list[StockRow]


class Candle(BaseModel):
    date: str = Field(..., description="交易日 YYYY-MM-DD")
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[int] = Field(None, description="成交股數")
    lots: Optional[float] = Field(None, description="成交量(張)")
    change: Optional[float] = Field(None, description="漲跌價")
    ma5: Optional[float] = Field(None, description="5 日均線")
    ma20: Optional[float] = Field(None, description="20 日均線 (月線)")
    ma60: Optional[float] = Field(None, description="60 日均線 (季線)")


class HistoryResponse(BaseModel):
    symbol: str = Field(..., description="股票代號")
    market: str = Field("", description="市場 (上市/上櫃)")
    market_code: str = Field("", description="市場代碼 (TWSE/TPEX)")
    months: int = Field(6, description="抓取月數")
    count: int = Field(0, description="K 棒數量")
    cached: bool = Field(False, description="是否命中後端快取")
    intraday: bool = Field(False, description="末根是否為今日盤中即時K")
    source: Optional[str] = Field(None, description="盤中即時=live / 非交易時間=eod")
    as_of: Optional[str] = Field(None, description="盤中K取得時間 ISO8601 (intraday 時才有)")
    candles: list[Candle] = Field(default_factory=list, description="時間升冪日K")


class QuoteResponse(BaseModel):
    symbol: str = Field(..., description="股票代號")
    market: str = Field("", description="市場 (上市/上櫃)")
    market_code: str = Field("", description="市場代碼 (TWSE/TPEX)")
    trading: bool = Field(False, description="目前是否為交易時間 (09:00-13:30 週一至五)")
    source: Optional[str] = Field(None, description="live=盤中即時 / eod=最後成交價")
    as_of: Optional[str] = Field(None, description="報價取得時間 ISO8601")
    prev_close: Optional[float] = Field(None, description="昨收")
    close: Optional[float] = Field(None, description="現價 / 最新成交價")
    change: Optional[float] = Field(None, description="漲跌價")
    change_pct: Optional[float] = Field(None, description="漲幅%")
    candle: Optional[Candle] = Field(None, description="今日(或最後交易日)一根，可接到日K尾端")


# ============================================================
# FastAPI app
# ============================================================

def create_app(service: Optional[ScreenService] = None) -> FastAPI:
    """建立 app。service 由 run_intraday --serve 傳入 (已 attach_ranker)。"""
    config = (service.config if service else None) or ApiConfig.from_env()
    svc = service or ScreenService(config)

    app = FastAPI(
        title="台股盤中起漲篩選 API",
        version="1.1.0",
        description="GET /api/screen 取最新起漲個股 (由 run_intraday --serve 每分鐘更新)；"
                    "GET /api/history/{symbol} 取個股歷史日K (含 MA5/20/60)。"
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

    @app.get("/api/screen-defaults", response_model=ScreenDefaults, tags=["screen"],
             summary="篩選參數預設值 (前端『恢復預設』用)")
    def screen_defaults() -> ScreenDefaults:
        return ScreenDefaults(**_DEFAULTS)

    @app.get("/api/screen", response_model=ScreenResponse, tags=["screen"],
             summary="最新起漲個股 (6 條件，參數可調)")
    def screen(
        top: int = Query(0, ge=0, description="只取前 N 名 (0=全部)"),
        # --- 第一層 漲幅池 ---
        min_change: float = Query(_DEFAULTS["min_change"], description="漲幅下限 %"),
        exclude_limit_up: bool = Query(_DEFAULTS["exclude_limit_up"],
                                       description="排除已鎖漲停 (收盤 ≤ 漲停前一檔)"),
        # --- 第二層 起漲點 6 條件 ---
        vol_ratio: float = Query(_DEFAULTS["vol_ratio"], gt=0,
                                 description="條件3 當日量/昨量下限 (預設 1.2)"),
        ma_short: int = Query(_DEFAULTS["ma_short"], ge=1,
                              description="5MA 天數 (預設 5)"),
        ma_mid: int = Query(_DEFAULTS["ma_mid"], ge=1,
                            description="月均線天數 (預設 20)"),
        ma_slope_lookback: int = Query(_DEFAULTS["ma_slope_lookback"], ge=0,
                                       description="月線上彎回看天數 (預設 5)"),
        vol_projection: bool = Query(_DEFAULTS["vol_projection"],
                                     description="條件3 改用全日預估量比昨量"),
    ):
        """依使用者參數即時重算起漲篩選。

        讀本進程每分鐘 publish 的快照原始列 (snapshot.raw)，套用漲幅池條件後再跑起漲
        6 條件 — 不重抓即時價，故各使用者參數互不影響、也不增加爬取。參數省略時等同
        原專案預設 (見 /api/screen-defaults)。
        """
        nr = _not_ready()
        if nr:
            return nr
        snap = svc.snapshot()
        pool_rows = svc.compute_pool(snap, min_change_pct=min_change,
                                     exclude_limit_up=exclude_limit_up)
        cfg = BreakoutConfig(
            vol_ratio_min=vol_ratio, ma_short=ma_short, ma_mid=ma_mid,
            ma_mid_slope_lookback=ma_slope_lookback, use_volume_projection=vol_projection,
        )
        scored = svc.compute_breakout(snap, pool_rows, cfg)
        rows = scored[:top] if top > 0 else scored
        m = meta_to_dict(snap, svc, len(rows))
        m["pool_size"] = len(pool_rows)
        return ScreenResponse(
            meta=Meta(**m),
            results=[BreakoutRow(**breakout_to_dict(sr)) for sr in rows],
        )

    @app.get("/api/pool", response_model=PoolResponse, tags=["screen"],
             summary="第一層漲幅池 (參數可調，預設 3%~漲停前一檔)")
    def pool(
        top: int = Query(0, ge=0, description="只取前 N 名 (0=全部)"),
        min_change: float = Query(_DEFAULTS["min_change"], description="漲幅下限 %"),
        exclude_limit_up: bool = Query(_DEFAULTS["exclude_limit_up"],
                                       description="排除已鎖漲停 (收盤 ≤ 漲停前一檔)"),
    ):
        nr = _not_ready()
        if nr:
            return nr
        snap = svc.snapshot()
        pool_rows = svc.compute_pool(snap, min_change_pct=min_change,
                                     exclude_limit_up=exclude_limit_up)
        rows = pool_rows[:top] if top > 0 else pool_rows
        m = meta_to_dict(snap, svc, len(rows))
        m["pool_size"] = len(pool_rows)
        return PoolResponse(
            meta=Meta(**m),
            results=[StockRow(**stock_to_dict(r)) for r in rows],
        )

    @app.get("/api/history/{symbol}", response_model=HistoryResponse,
             tags=["history"], summary="個股歷史日K (含 MA5/20/60)")
    def history(
        symbol: str = Path(..., description="股票代號，例如 2330"),
        months: int = Query(6, ge=1, le=24, description="抓取月數 (1~24)"),
        market: Optional[str] = Query(
            None, description="市場代碼 TWSE/TPEX，省略則自動判別 (先試上市再試上櫃)"),
        intraday: bool = Query(
            False, description="是否把今日盤中即時價接到日K尾端 (交易時間有效)"),
    ):
        """逐月抓取歷史日K (TWSE STOCK_DAY / TPEx)，在後端算好均線後回傳。

        資料即時向交易所抓取並做 TTL 快取 (盤後一天才變一次)。查無資料時回 200 +
        candles=[]，前端可顯示「無歷史資料」。intraday=true 時，於回應前接上今日盤中
        即時K (這根不進快取)；前端可再輪詢 /api/quote/{symbol} 持續更新。
        """
        try:
            data = get_history_candles(
                symbol, market_code=market, months=months, intraday=intraday)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"symbol": symbol, "market": "", "market_code": "",
                         "months": months, "count": 0, "cached": False,
                         "intraday": False, "source": None, "as_of": None,
                         "candles": [], "detail": f"歷史資料抓取失敗: {exc}"},
            )
        return HistoryResponse(**data)

    @app.get("/api/quote/{symbol}", response_model=QuoteResponse,
             tags=["history"], summary="個股盤中最新價 (單檔即時)")
    def quote(
        symbol: str = Path(..., description="股票代號，例如 2330"),
        market: Optional[str] = Query(
            None, description="市場代碼 TWSE/TPEX，省略則自動判別 (先試上市再試上櫃)"),
    ):
        """單檔盤中即時報價，給歷史圖表輪詢更新「今日K + 現價線」用。

        交易時間回 MIS 盤中即時價 (source=live)；非交易時間回最後成交價 (source=eod)。
        查無資料時回 200 + candle=null。⚠️ 資訊參考，非投資建議。
        """
        try:
            data = get_intraday_quote(symbol, market_code=market)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"symbol": symbol, "market": "", "market_code": "",
                         "trading": False, "source": None, "as_of": None,
                         "prev_close": None, "close": None, "change": None,
                         "change_pct": None, "candle": None,
                         "detail": f"即時報價抓取失敗: {exc}"},
            )
        return QuoteResponse(**data)

    return app
