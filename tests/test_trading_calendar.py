from datetime import date, timedelta

import pytest

from src.fetchers.history import DayResult
from src.fetchers.parsing import DailyMarket
from src.trading_calendar import week_status

MON = date(2026, 12, 21)  # 該週 12/25（五）在 run3 FinMind 交易日表中未列入


def day(d, status):
    if status == "error":
        return DayResult(d, None, None, ["x"])
    trading = status == "trading"
    tw = DailyMarket("twse", d, trading)
    tp = DailyMarket("tpex", d, trading if status != "partial" else not trading)
    return DayResult(d, tw, tp)


def days(statuses):
    return [day(MON + timedelta(days=i), s) for i, s in enumerate(statuses)]


FINMIND = {MON + timedelta(days=i) for i in range(4)}  # 週一～週四交易、週五不交易


def test_friday_as_of_is_settled():
    ws = week_status(days(["trading"] * 5), MON + timedelta(days=4), today=MON + timedelta(days=5), finmind_trading_dates=None)
    assert ws.is_settled and ws.trading_days_elapsed == 5 and ws.remaining == [] and ws.week_id == "2026-W52"


def test_thursday_before_future_holiday_is_not_settled_even_if_finmind_says_closed():
    thu = MON + timedelta(days=3)
    ws = week_status(days(["trading"] * 4), thu, today=thu, finmind_trading_dates=FINMIND)
    assert not ws.is_settled and ws.remaining[0].reason.startswith("未來日期")
    assert ws.trading_days_elapsed == 4


def test_holiday_today_confirmed_by_finmind():
    fri = MON + timedelta(days=4)
    ws = week_status(days(["trading"] * 4 + ["non_trading"]), MON + timedelta(days=3), today=fri, finmind_trading_dates=FINMIND)
    assert ws.is_settled and ws.remaining[0].confirmed_closed


def test_holiday_today_without_finmind_not_settled():
    fri = MON + timedelta(days=4)
    ws = week_status(days(["trading"] * 4 + ["non_trading"]), MON + timedelta(days=3), today=fri, finmind_trading_dates=None)
    assert not ws.is_settled and "不可用" in ws.remaining[0].reason


def test_today_no_data_but_finmind_lists_trading_day_not_settled():
    # 例：週三 14:30 執行，當日盤後資料尚未入庫
    wed = MON + timedelta(days=2)
    ws = week_status(days(["trading", "trading", "non_trading"]), MON + timedelta(days=1), today=wed,
                     finmind_trading_dates=FINMIND)
    assert not ws.is_settled and "FinMind 列為交易日" in ws.remaining[0].reason
    assert ws.trading_days_elapsed == 2


def test_past_holiday_confirmed_without_finmind():
    sat = MON + timedelta(days=5)
    ws = week_status(days(["trading"] * 4 + ["non_trading"]), MON + timedelta(days=3), today=sat, finmind_trading_dates=None)
    assert ws.is_settled


@pytest.mark.parametrize("status", ["error", "partial"])
def test_error_or_partial_day_not_confirmed(status):
    sat = MON + timedelta(days=5)
    ws = week_status(days(["trading"] * 4 + [status]), MON + timedelta(days=3), today=sat, finmind_trading_dates=FINMIND)
    assert not ws.is_settled


def test_unfetched_past_day_not_confirmed():
    # 指定歷史基準日（as_of_date 補跑）時，基準日之後的日子不在抓取範圍
    ws = week_status(days(["trading"] * 3), MON + timedelta(days=2), today=date(2027, 1, 10), finmind_trading_dates=FINMIND)
    assert not ws.is_settled and all(r.reason == "未抓取此日資料" for r in ws.remaining)


def test_short_week_with_monday_holiday_elapsed_count():
    ws = week_status(days(["non_trading", "trading", "trading"]), MON + timedelta(days=2), today=MON + timedelta(days=2),
                     finmind_trading_dates=None)
    assert ws.trading_days_elapsed == 2
