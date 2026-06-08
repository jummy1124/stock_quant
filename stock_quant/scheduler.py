"""盤中時段判斷 + 每分鐘常駐迴圈。

MarketClock: 判斷現在是否台股盤中 (預設 09:00–13:30，週一至五)。
             只看星期與時間，未含國定假日；假設系統時區為台北時間。
run_market_loop: 盤中每 interval 秒呼叫 task(now)，非盤中休眠。
                 可注入 now_fn / sleep_fn / max_iterations 以便測試。
"""
from __future__ import annotations

import time as _time
from datetime import datetime, time
from typing import Callable, Optional


class MarketClock:
    def __init__(self, open_t: time = time(9, 0), close_t: time = time(13, 30)):
        self.open_t = open_t
        self.close_t = close_t

    def is_trading(self, now: datetime) -> bool:
        if now.weekday() >= 5:
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
    clock = clock or MarketClock()
    ran = it = 0
    while max_iterations is None or it < max_iterations:
        now = now_fn()
        if ignore_hours or clock.is_trading(now):
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
