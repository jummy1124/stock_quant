"""日期工具: 民國年 / 西元解析 + 推估最後交易日。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def parse_roc_date(raw) -> Optional[date]:
    """解析民國日期字串，如 '1130607' 或 '113/06/07' -> date(2024,6,7)。"""
    if raw is None:
        return None
    s = str(raw).strip().replace("/", "").replace("-", "")
    if not s.isdigit() or len(s) < 7:
        return None
    yyy = int(s[:-4])
    mm = int(s[-4:-2])
    dd = int(s[-2:])
    try:
        return date(yyy + 1911, mm, dd)
    except ValueError:
        return None


def parse_ad_date(raw) -> Optional[date]:
    """解析西元日期字串，如 '20260605' -> date(2026,6,5)。MIS API 用此格式。"""
    if raw is None:
        return None
    s = str(raw).strip().replace("/", "").replace("-", "")
    if not s.isdigit() or len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def latest_trading_day(today: Optional[date] = None) -> date:
    """粗略推估『最後一個交易日』(只排除週末，未含國定假日)。"""
    d = today or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d
