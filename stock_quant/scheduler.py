"""盤中時段判斷 + 每分鐘常駐迴圈。

MarketClock: 判斷『現在是否為台股盤中』(預設 09:00–13:30，週一至五)。
             注意: 只看星期與時間，未含國定假日；假設系統時區為台北時間。

run_market_loop: 常駐迴圈，盤中每 interval 秒呼叫一次 task(now)，
                 非盤中則休眠等待。可注入 now_fn / sleep_fn / max_iterations 方便測試。
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Callable, Optional
import time as _time


class MarketClock:
    def __init__(self, open_t: time = time(9, 0), close_t: time = time(13, 30)):
        self.open_t = open_t
        self.close_t = close_t

    def is_trading(self, now: datetime) -> bool:
        if now.weekday() >= 5:          # 週六/週日
            return False
        return self.open_t <= now.time() <= self.close_t


def run_market_loop(
    task: Callable[[datetime], None],
    clock: Optional[MarketClock] = None,
    interval: int = 60,
    ignore_hours: bool = False,
    idle_interval: int = 30,
    max_iterations: Optional[int] = None,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep_fn: Callable[[float], None] = _time.sleep,
    log_fn: Callable[[str], None] = print,
) -> int:
    """回傳實際執行 task 的次數。max_iterations=None 代表無限常駐。"""
    ran = 0
    it = 0
    while max_iterations is None or it < max_iterations:
        now = now_fn()
        if ignore_hours or clock_is_trading(clock, now):
            task(now)
            ran += 1
            wait = interval
        else:
            log_fn(f"[{now:%Y-%m-%d %H:%M:%S}] 非盤中，休眠 {idle_interval}s ...")
            wait = idle_interval
        it += 1
        if max_iterations is not None and it >= max_iterations:
            break
        sleep_fn(wait)
    return ran


def clock_is_trading(clock: Optional[MarketClock], now: datetime) -> bool:
    return (clock or MarketClock()).is_trading(now)
