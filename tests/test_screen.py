"""選股篩選器 BreakoutScreen (7 規則) 的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.analysis import BreakoutScreen, limit_up_price, tick_size
from stock_quant.analysis.screen import _find_swings
from stock_quant.domain import DailyQuote, Market

# 核心 6 條規則的測試: 關閉多頭趨勢閘門 (短歷史即可，聚焦突破型態本身)
_CORE = BreakoutScreen(require_uptrend=False)


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


def _tri(i):
    return abs((i % 6) - 3)


def _trend_hist(up=True, n=64, prev_vol=1000):
    """造 n 根『頭頭高底底高』(up=True) 或空頭(down) 歷史，最後一根為淺回檔。

    線性趨勢 + 週期三角波 -> 每 6 根一個擺動高/低點，因線性項而逐波墊高(或墊低)。
    high=low=close 方便擺動偵測；最後一根(昨日)落在波谷 -> 昨收<MA5 (滿足規則5)。
    """
    bars = []
    start = date(2026, 6, 4) - timedelta(days=n - 1)
    for i in range(n):
        c = (30 + 0.8 * i + 1.5 * _tri(i)) if up else (90 - 0.8 * i + 1.5 * _tri(i))
        c = round(c, 2)
        bars.append(DailyQuote.normalize(symbol="T", name="T", market=Market.TWSE,
                    trade_date=start + timedelta(days=i),
                    open=c, high=c, low=c, close=c, volume=prev_vol))
    return bars


# ---- 升降單位 / 漲停價 --------------------------------------------------
def test_tick_size():
    assert tick_size(5) == 0.01 and tick_size(80) == 0.1 and tick_size(300) == 0.5
    assert tick_size(700) == 1.0 and tick_size(1500) == 5.0


def test_limit_up_price():
    assert limit_up_price(100) == 110.0
    assert limit_up_price(50) == 55.0


# ---- 核心 6 條全符合 (關閉趨勢閘門) -------------------------------------
def test_pass_core_rules():
    hist = _hist(_HIST_OK, prev_high=100.5, prev_vol=1000)
    today = _q(100, 105.2, 100, 105, 1500)   # +5%, 上影0.19%, 量1.5x, 收105>昨高100.5
    r = _CORE.check(today, hist)
    assert r.passed
    assert r.ma5 == 101.2 and r.prev_high == 100.5
    assert r.uptrend is None                  # 趨勢閘門關閉 -> 不評估


# ---- 原 4 條各自不符 ----------------------------------------------------
def test_fail_not_red():
    assert not _CORE.check(_q(106, 106.2, 104, 105, 1500), _hist(_HIST_OK)).passed


def test_fail_change_too_small():
    assert not _CORE.check(_q(100, 102.1, 100, 102, 1500), _hist(_HIST_OK)).passed


def test_fail_locked_limit_up():
    assert not _CORE.check(_q(108, 110, 108, 110, 1500), _hist(_HIST_OK)).passed


def test_fail_upper_shadow():
    assert not _CORE.check(_q(100, 107, 100, 105, 1500), _hist(_HIST_OK)).passed


def test_fail_volume():
    assert not _CORE.check(_q(100, 105.2, 100, 105, 1100), _hist(_HIST_OK, prev_vol=1000)).passed


# ---- 規則5: 前一日收盤 < MA5 -------------------------------------------
def test_fail_prevclose_not_below_ma5():
    hist = _hist([98, 98, 99, 99, 100], prev_high=100.5)   # MA5=98.8, 昨收100 (昨收>MA5)
    r = _CORE.check(_q(100, 105.2, 100, 105, 1500), hist)
    assert not r.passed and r.ma5 == 98.8


# ---- 規則6: 今日現價 > 昨日最高 ----------------------------------------
def test_fail_close_not_above_prev_high():
    hist = _hist(_HIST_OK, prev_high=106)                   # 昨高106
    assert not _CORE.check(_q(100, 105.2, 100, 105, 1500), hist).passed   # 收105 < 昨高106


# ---- 歷史不足 5 日 ------------------------------------------------------
def test_insufficient_history():
    r = _CORE.check(_q(100, 105.2, 100, 105, 1500), _hist([100, 101]))
    assert not r.passed and "MA5" in r.note


# ---- 規則4: 成交量依時段比例換算 (盤中公平比較) -------------------------
def test_volume_pace_early_session_passes():
    hist = _hist(_HIST_OK, prev_high=100.5, prev_vol=1000)
    today = _q(100, 105.2, 100, 105, 500)
    r = _CORE.check(today, hist, session_fraction=1 / 3)
    assert r.passed
    assert r.vol_ratio == 0.5 and r.vol_pace_ratio == 1.5


def test_volume_pace_full_session_unchanged():
    hist = _hist(_HIST_OK, prev_high=100.5, prev_vol=1000)
    r = _CORE.check(_q(100, 105.2, 100, 105, 1100), hist, session_fraction=1.0)
    assert not r.passed and r.vol_pace_ratio == 1.1   # 1100/1000=1.1 <1.2


# ---- 規則7: 多頭趨勢閘門 -----------------------------------------------
def test_find_swings():
    sh, sl = _find_swings([1, 3, 2, 5, 4], [9, 7, 8, 6, 7], k=1)
    assert sh == [3, 5]      # 擺動高點墊高 -> 頭頭高
    assert sl == [7, 6]      # 擺動低點 (此例為頭頭低)


def test_uptrend_passes_gate():
    hist = _trend_hist(up=True)                       # 多頭: 頭頭高底底高, MA20≥MA60
    prev_high = float(hist[-1].high)                  # 昨高 (=昨收)
    today = _q(prev_high + 0.6, prev_high + 2.8, prev_high, prev_high + 2.6, 1500)  # 突破+3%
    r = BreakoutScreen().check(today, hist)           # 預設啟用趨勢閘門
    assert r.passed and r.uptrend is True
    assert r.ma20 is not None and r.ma60 is not None and r.ma20 >= r.ma60


def test_downtrend_rejected_by_gate():
    # 空頭股 (如 6257 型態): 核心 6 條都成立，但因不是多頭 -> 趨勢閘門擋下
    hist = _trend_hist(up=False)
    prev_high = float(hist[-1].high)
    today = _q(prev_high + 0.2, prev_high + 1.5, prev_high, prev_high + 1.4, 1500)  # 單日反彈+3%
    r_core = _CORE.check(today, hist)                 # 關閉閘門 -> 核心 6 條過關
    assert r_core.passed
    r = BreakoutScreen().check(today, hist)           # 啟用閘門 -> 被擋
    assert not r.passed and r.uptrend is False


def test_trend_insufficient_history():
    # 歷史 < 季線天數 (60) -> 無法判多頭趨勢 -> 不入選並註記
    hist = _trend_hist(up=True, n=40)
    prev_high = float(hist[-1].high)
    today = _q(prev_high + 0.6, prev_high + 2.8, prev_high, prev_high + 2.6, 1500)
    r = BreakoutScreen().check(today, hist)
    assert not r.passed and "多頭趨勢" in r.note


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
