"""本週是否已結算（spec_v1 第 4.6 節第 1 點；2026-11-25 D4 裁定）。

基準日 as_of 所在自然週（週一～週五）中，as_of 之後的每個平日 d 須「確認休市」：
  兩市官方皆回無資料（DayResult.status == "non_trading"），且
  （d 早於今日〔Asia/Taipei〕，或 FinMind 交易日表未列入 d）。
今日之後的日子一律不算確認（不會被抓取，亦不採信預測）。
所有剩餘平日皆確認休市才算 ``is_week_settled = True``。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from src.fetchers.history import DayResult
from src.timezone import iso_week_id, week_monday, week_weekdays


@dataclass
class RemainingDay:
    day: date
    confirmed_closed: bool
    reason: str


@dataclass
class WeekStatus:
    as_of: date
    week_id: str
    week_monday: date
    is_settled: bool
    trading_days_elapsed: int          # 本週至 as_of（含）兩市皆有資料的交易日數（推算倍數之 X）
    remaining: list[RemainingDay] = field(default_factory=list)


def week_status(
    days: Iterable[DayResult],
    as_of: date,
    today: date,
    finmind_trading_dates: set[date] | None,
) -> WeekStatus:
    by_date = {d.trade_date: d for d in days}
    weekdays = week_weekdays(as_of)
    elapsed = sum(1 for d in weekdays if d <= as_of and d in by_date and by_date[d].status == "trading")

    remaining: list[RemainingDay] = []
    for d in weekdays:
        if d <= as_of:
            continue
        if d > today:
            remaining.append(RemainingDay(d, False, "未來日期，不算確認"))
            continue
        day = by_date.get(d)
        if day is None:
            remaining.append(RemainingDay(d, False, "未抓取此日資料"))
            continue
        if day.status != "non_trading":
            remaining.append(RemainingDay(d, False, f"官方資料狀態為 {day.status}"))
            continue
        if d < today:
            remaining.append(RemainingDay(d, True, "兩市皆無資料且早於今日"))
        elif finmind_trading_dates is not None and d not in finmind_trading_dates:
            remaining.append(RemainingDay(d, True, "兩市皆無資料且 FinMind 交易日表未列入"))
        elif finmind_trading_dates is None:
            remaining.append(RemainingDay(d, False, "今日兩市無資料，但 FinMind 交易日表不可用"))
        else:
            remaining.append(RemainingDay(d, False, "今日兩市無資料，但 FinMind 列為交易日（可能尚未收盤或資料未更新）"))

    return WeekStatus(
        as_of=as_of, week_id=iso_week_id(as_of), week_monday=week_monday(as_of),
        is_settled=all(r.confirmed_closed for r in remaining),
        trading_days_elapsed=elapsed, remaining=remaining,
    )
