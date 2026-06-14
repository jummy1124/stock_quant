"""台股普通股升降單位 (tick) 與漲停價計算。

用於「漲幅 3% ~ 漲停前一檔」篩選: 排除已鎖漲停 (收盤 > 漲停前一檔) 的個股。
"""
from __future__ import annotations

# 價格帶上限 -> 升降單位 (tick)
_TICKS = [(10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)]


def tick_size(price: float) -> float:
    for hi, t in _TICKS:
        if price < hi:
            return t
    return 5.0


def limit_up_price(prev_close: float) -> float:
    """漲停價 = 昨收×1.1，無條件捨去到升降單位 (不超過 10%)。"""
    raw = prev_close * 1.1
    t = tick_size(raw)
    return round(int(raw / t + 1e-9) * t, 2)


def limit_up_prev_tick(prev_close: float) -> float:
    """漲停前一檔 = 漲停價往下一個升降單位 (篩選上限)。"""
    lu = limit_up_price(prev_close)
    return round(lu - tick_size(lu), 2)
