"""起漲篩選 (breakout_screen) 測試 —— 重點在『盤中站上均線』的均線計算。

回歸 bug: 盤中(live)時把今日現價 append 進去算月線，會把最舊一根擠掉，對「已從高點
回落」的個股算出偏低的月線，使現價其實在月線下卻誤判「站上月線」(例: 6742 澤米，
現價 57.90 < 月均價 58.24 卻入選)。修正後均線只用完成日K，現價再去比這條線。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from stock_quant.breakout_screen import (BreakoutConfig, _ma, _ma_closes,
                                          screen_breakout)
from stock_quant.domain import DailyQuote, Market
from stock_quant.intraday import IntradayRanker, RankRow

_NOW = datetime(2026, 6, 15, 11, 23, 0)   # 盤中時間 (不影響: 直接給 live ranker)


def _q(sym, d, close, *, o=None, h=None, low=None, vol=1_000_000):
    return DailyQuote.normalize(symbol=sym, name="x", market=Market.TWSE, trade_date=d,
                                open=o if o is not None else close,
                                high=h if h is not None else close,
                                low=low if low is not None else close,
                                close=close, volume=vol)


def _live_ranker(sym, completed_closes, *, yhigh, yvol):
    """建一個 live ranker，歷史=完成日K (到昨日)；最後一根的 high/vol 給昨高/昨量。"""
    base = date(2026, 5, 1)
    quotes = []
    n = len(completed_closes)
    for i, c in enumerate(completed_closes):
        d = base + timedelta(days=i)
        if i == n - 1:                       # 昨日: 設昨高/昨量
            quotes.append(_q(sym, d, c, h=yhigh, vol=yvol))
        else:
            quotes.append(_q(sym, d, c))
    r = IntradayRanker([(sym, Market.TWSE)])
    r.preload_history({sym: quotes})
    r.last_source = "live"                   # 盤中
    return r


def _today_row(sym, *, close, open_, high, prev_close, vol):
    return RankRow(symbol=sym, name="x", market=Market.TWSE, close=close,
                   prev_close=prev_close, change=round(close - prev_close, 2),
                   change_pct=round((close - prev_close) / prev_close * 100, 2),
                   volume=vol, open=open_, high=high, low=min(open_, prev_close))


# ---------------- 均線序列: 盤中不含今日現價 ----------------

def test_ma_closes_excludes_intraday_today_in_live():
    closes = [50.0 + i for i in range(25)]    # 完成日K (到昨日)
    r = _live_ranker("AAA", closes, yhigh=80.0, yvol=1_000_000)
    row = _today_row("AAA", close=999.0, open_=70.0, high=999.0, prev_close=74.0, vol=2_000_000)
    got = _ma_closes(r, row)
    assert got == closes                      # 不含今日 999
    assert len(got) == 25 and got[-1] == closes[-1]


# ---------------- 6742 情境: 現價在月線下 → 不該入選 ----------------

def test_below_monthly_line_rejected_in_live():
    # 已從高點(70)回落、近期約 57.x、昨日更低(56)。完成日K 的 SMA20 > 今日現價。
    completed = [70.0] + [57.6] * 18 + [56.0]      # 共 20 根... 需 >25 才算斜率
    completed = [70.0] * 6 + [57.6] * 18 + [56.0]  # 25 根 (含 6 根高 plateau)
    today_price = 57.90
    r = _live_ranker("6742", completed, yhigh=57.30, yvol=1_000_000)
    row = _today_row("6742", close=today_price, open_=53.0, high=58.0,
                     prev_close=56.0, vol=2_000_000)

    ma20_completed = _ma(completed, 20)            # 修正後用這條 (券商月均價)
    ma20_incl_today = _ma(completed[-19:] + [today_price], 20)  # 舊 bug 用這條
    assert ma20_completed > today_price            # 現價其實在月線下
    assert ma20_incl_today < today_price           # 舊算法會誤判在月線上 (bug 重現)

    scored = screen_breakout(r, [row], _NOW, BreakoutConfig())
    assert scored == []                            # 修正後: 不入選


# ---------------- 控制組: 現價確實站上 (上彎) 月線 → 入選 ----------------

def test_above_rising_monthly_line_selected_in_live():
    # 緩升趨勢 (月線上彎)，今日跳上、突破昨高、量增、昨日在5MA下。
    completed = [round(90 + 0.4 * i, 2) for i in range(28)] + [99.0]   # 到昨日(=99.0)
    r = _live_ranker("2330", completed, yhigh=100.0, yvol=1_000_000)
    row = _today_row("2330", close=103.95, open_=99.5, high=104.0,
                     prev_close=99.0, vol=2_000_000)

    assert _ma(completed, 20) < 103.95             # 現價在月線上
    scored = screen_breakout(r, [row], _NOW, BreakoutConfig())
    assert len(scored) == 1 and scored[0].row.symbol == "2330"
    assert scored[0].ma20_up is True
