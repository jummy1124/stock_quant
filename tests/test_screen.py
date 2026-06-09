"""選股篩選器 BreakoutScreen (6 規則) 的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.analysis import BreakoutScreen, limit_up_price, tick_size
from stock_quant.domain import DailyQuote, Market


def _q(open_, high, low, close, volume):
    """當日K。"""
    return DailyQuote.normalize(symbol="T", name="T", market=Market.TWSE,
                                trade_date=date(2026, 6, 5), open=open_, high=high,
                                low=low, close=close, volume=volume)


def _hist(closes, prev_high=100.5, prev_vol=1000):
    """造一段升冪歷史(結束於昨日)。最後一根=昨日，可指定昨高/昨量；其餘只需收盤算 MA5。"""
    bars = []
    n = len(closes)
    for i, c in enumerate(closes):
        d = date(2026, 6, 4) - timedelta(days=n - 1 - i)
        if i == n - 1:   # 昨日: 指定昨高/昨量
            bars.append(DailyQuote.normalize(symbol="T", name="T", market=Market.TWSE,
                        trade_date=d, open=c, high=prev_high, low=c - 1, close=c, volume=prev_vol))
        else:
            bars.append(DailyQuote.normalize(symbol="T", name="T", market=Market.TWSE,
                        trade_date=d, open=c, high=c, low=c, close=c, volume=prev_vol))
    return bars


# MA5 範例: 昨收=100，前4日較高 -> MA5>100 (規則5: 昨收<MA5 成立)
_HIST_OK = [102, 102, 101, 101, 100]      # MA5=101.2, 昨收100


# ---- 升降單位 / 漲停價 --------------------------------------------------
def test_tick_size():
    assert tick_size(5) == 0.01 and tick_size(80) == 0.1 and tick_size(300) == 0.5
    assert tick_size(700) == 1.0 and tick_size(1500) == 5.0


def test_limit_up_price():
    assert limit_up_price(100) == 110.0
    assert limit_up_price(50) == 55.0


# ---- 全部 6 條符合 ------------------------------------------------------
def test_pass_all_rules():
    hist = _hist(_HIST_OK, prev_high=100.5, prev_vol=1000)
    today = _q(100, 105.2, 100, 105, 1500)   # +5%, 上影0.19%, 量1.5x, 收105>昨高100.5
    r = BreakoutScreen().check(today, hist)
    assert r.passed
    assert r.ma5 == 101.2 and r.prev_high == 100.5


# ---- 原 4 條各自不符 ----------------------------------------------------
def test_fail_not_red():
    r = BreakoutScreen().check(_q(106, 106.2, 104, 105, 1500), _hist(_HIST_OK))
    assert not r.passed


def test_fail_change_too_small():
    r = BreakoutScreen().check(_q(100, 102.1, 100, 102, 1500), _hist(_HIST_OK))
    assert not r.passed


def test_fail_locked_limit_up():
    r = BreakoutScreen().check(_q(108, 110, 108, 110, 1500), _hist(_HIST_OK))
    assert not r.passed


def test_fail_upper_shadow():
    r = BreakoutScreen().check(_q(100, 107, 100, 105, 1500), _hist(_HIST_OK))
    assert not r.passed


def test_fail_volume():
    r = BreakoutScreen().check(_q(100, 105.2, 100, 105, 1100), _hist(_HIST_OK, prev_vol=1000))
    assert not r.passed


# ---- 新規則5: 前一日收盤 < MA5 -----------------------------------------
def test_fail_prevclose_not_below_ma5():
    # 歷史遞增 -> 昨收為最高 -> 昨收 > MA5 -> 規則5 不符
    hist = _hist([98, 98, 99, 99, 100], prev_high=100.5)   # MA5=98.8, 昨收100
    r = BreakoutScreen().check(_q(100, 105.2, 100, 105, 1500), hist)
    assert not r.passed and r.ma5 == 98.8


# ---- 新規則6: 今日現價 > 昨日最高 --------------------------------------
def test_fail_close_not_above_prev_high():
    hist = _hist(_HIST_OK, prev_high=106)                   # 昨高106
    r = BreakoutScreen().check(_q(100, 105.2, 100, 105, 1500), hist)   # 收105 < 昨高106
    assert not r.passed


# ---- 歷史不足 5 日 ------------------------------------------------------
def test_insufficient_history():
    r = BreakoutScreen().check(_q(100, 105.2, 100, 105, 1500), _hist([100, 101]))
    assert not r.passed and "MA5" in r.note


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
