"""技術指標 (純函式，輸入時間升冪的數列)。

回傳序列與輸入等長，資料不足的位置為 None；另提供 *_last() 取最新值。
"""
from __future__ import annotations

from typing import Optional, Sequence


def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    window: list[float] = []
    for v in values:
        window.append(v)
        if len(window) > period:
            window.pop(0)
        out.append(sum(window) / period if len(window) == period else None)
    return out


def ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    k = 2 / (period + 1)
    prev: Optional[float] = None
    for v in values:
        prev = v if prev is None else (v - prev) * k + prev
        out.append(prev)
    return out


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """回傳 (dif, signal_line, hist)，皆為與輸入等長的序列。"""
    ef = ema(values, fast)
    es = ema(values, slow)
    dif = [(a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)]
    dif_valid = [d for d in dif if d is not None]
    sig_valid = ema(dif_valid, signal)
    # 將 signal 對齊回原長度
    sig: list[Optional[float]] = [None] * (len(dif) - len(dif_valid)) + sig_valid
    hist = [(d - s) if (d is not None and s is not None) else None for d, s in zip(dif, sig)]
    return dif, sig, hist


def slope(values: Sequence[float], window: int) -> Optional[float]:
    """最近 window 點的線性迴歸斜率 (每單位時間的價格變化)。不足回傳 None。"""
    if len(values) < window or window < 2:
        return None
    ys = list(values)[-window:]
    n = window
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14):
    """Wilder ADX/DMI。回傳最新的 (adx, plus_di, minus_di)，資料不足回傳 (None,None,None)。"""
    n = len(closes)
    if n < period * 2 + 1:
        return None, None, None

    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    # Wilder 平滑
    def _wilder(seq):
        sm = [sum(seq[:period])]
        for i in range(period, len(seq)):
            sm.append(sm[-1] - sm[-1] / period + seq[i])
        return sm

    str_ = _wilder(trs)
    sp = _wilder(plus_dm)
    sm = _wilder(minus_dm)

    dxs = []
    for i in range(len(str_)):
        if str_[i] == 0:
            dxs.append(0.0)
            continue
        pdi = 100 * sp[i] / str_[i]
        mdi = 100 * sm[i] / str_[i]
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom else 0.0)

    if len(dxs) < period:
        return None, None, None
    adx_val = sum(dxs[:period]) / period
    for i in range(period, len(dxs)):
        adx_val = (adx_val * (period - 1) + dxs[i]) / period

    last_pdi = 100 * sp[-1] / str_[-1] if str_[-1] else 0.0
    last_mdi = 100 * sm[-1] / str_[-1] if str_[-1] else 0.0
    return adx_val, last_pdi, last_mdi


def _last(seq):
    return seq[-1] if seq else None
