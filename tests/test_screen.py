"""選股篩選器 BreakoutScreen 的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.analysis import BreakoutScreen, limit_up_price, tick_size
from stock_quant.domain import DailyQuote, Market


def _q(open_, high, low, close, volume):
    return DailyQuote.normalize(symbol="T", name="T", market=Market.TWSE,
                                trade_date=date(2026, 6, 5), open=open_, high=high,
                                low=low, close=close, volume=volume)


def _prev(close, volume):
    return DailyQuote.normalize(symbol="T", name="T", market=Market.TWSE,
                                trade_date=date(2026, 6, 4), close=close, volume=volume)


# ---- 升降單位 / 漲停價 --------------------------------------------------
def test_tick_size():
    assert tick_size(5) == 0.01 and tick_size(30) == 0.05 and tick_size(80) == 0.1
    assert tick_size(300) == 0.5 and tick_size(700) == 1.0 and tick_size(1500) == 5.0


def test_limit_up_price():
    assert limit_up_price(100) == 110.0       # 100×1.1=110
    assert limit_up_price(50) == 55.0         # 50x1.1=55 本身即為合法升降單位


# ---- 全部符合 -----------------------------------------------------------
def test_pass_all_rules():
    prev = _prev(close=100, volume=1000)
    today = _q(open_=100, high=105.2, low=100, close=105, volume=1500)  # +5%, 上影0.19%, 量1.5x
    r = BreakoutScreen().check(today, prev)
    assert r.passed
    assert r.change_pct == 5.0 and r.vol_ratio == 1.5


# ---- 各規則各自不符 -----------------------------------------------------
def test_fail_not_red():
    r = BreakoutScreen().check(_q(106, 106.2, 104, 105, 1500), _prev(100, 1000))
    assert not r.passed and r.is_red is False


def test_fail_change_too_small():
    r = BreakoutScreen().check(_q(100, 102.1, 100, 102, 1500), _prev(100, 1000))  # +2%
    assert not r.passed


def test_fail_locked_limit_up():
    # 鎖漲停: 收盤=漲停價110 -> 應被排除 (只到漲停前一檔)
    r = BreakoutScreen().check(_q(108, 110, 108, 110, 1500), _prev(100, 1000))
    assert not r.passed


def test_fail_upper_shadow_too_long():
    r = BreakoutScreen().check(_q(100, 107, 100, 105, 1500), _prev(100, 1000))  # 上影1.9%
    assert not r.passed


def test_fail_volume_not_enough():
    r = BreakoutScreen().check(_q(100, 105.2, 100, 105, 1100), _prev(100, 1000))  # 量1.1x
    assert not r.passed


def test_missing_data():
    r = BreakoutScreen().check(_q(100, 105, 100, 105, None), _prev(100, 1000))
    assert not r.passed and "缺" in r.note


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
