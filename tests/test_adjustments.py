from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.adjustments import apply_plan, build_plan, codes_with_share_structure_change
from src.fetchers.parsing import CorporateEvent

START, END = date(2026, 1, 1), date(2026, 11, 20)


def ev(code, d, kind, before, ref, market="twse"):
    return CorporateEvent(market, code, "x", d, kind, before, ref, "test")


def daily(code, closes, start=date(2026, 1, 5)):
    rows, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append({"date": d, "code": code, "open": c, "high": c, "low": c, "close": c, "volume_shares": 1000.0})
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def test_factor_is_ref_over_before():
    plan = build_plan([ev("7780", date(2026, 1, 19), "par_value_change", 185.0, 18.5)], START, END)
    assert plan.factors["7780"] == [(date(2026, 1, 19), pytest.approx(0.1), "par_value_change")]


def test_event_outside_window_ignored():
    plan = build_plan([ev("2330", START, "dividend", 100, 95), ev("2330", END + timedelta(days=1), "dividend", 100, 95)], START, END)
    assert plan.factors == {}                      # (start, end]：起點當日與終點之後皆不計


def test_backward_adjustment_par_value_change_is_continuous():
    # 7780 實測案例：停止買賣前 185.00、恢復參考價 18.50
    dates = [date(2026, 1, 14), date(2026, 1, 15), date(2026, 1, 19), date(2026, 1, 20)]  # 1/16 停止買賣
    df = pd.DataFrame([{"date": d, "code": "7780", "open": c, "high": c, "low": c, "close": c, "volume_shares": 1000.0}
                       for d, c in zip(dates, [180, 185, 18.6, 19.0])])
    plan = build_plan([ev("7780", date(2026, 1, 19), "par_value_change", 185.0, 18.5)], START, END)
    out = apply_plan(df, plan)
    assert list(out["date"]) == [date(2026, 1, 14), date(2026, 1, 15), date(2026, 1, 19), date(2026, 1, 20)]
    assert list(out["cum_factor"]) == pytest.approx([0.1, 0.1, 1.0, 1.0])     # 事件當日（含）之後不調整
    assert list(out["close_adj"]) == pytest.approx([18.0, 18.5, 18.6, 19.0])
    assert out["close_adj"].iloc[2] / out["close_adj"].iloc[1] == pytest.approx(18.6 / 18.5)
    assert list(out["volume_shares"]) == [1000.0] * 4                          # 量不調整


def test_multiple_events_compound_and_latest_factor_is_one():
    df = daily("2330", [100, 100, 100, 100, 100, 100])
    days = list(df["date"])
    plan = build_plan([ev("2330", days[2], "dividend", 100, 95), ev("2330", days[4], "dividend", 100, 98)], START, END)
    out = apply_plan(df, plan)
    assert list(out["cum_factor"]) == pytest.approx([0.95 * 0.98, 0.95 * 0.98, 0.98, 0.98, 1.0, 1.0])
    assert out["cum_factor"].iloc[-1] == 1.0
    assert out["close_adj"].iloc[-1] == out["close"].iloc[-1]                  # 最新日還原價 = 原始價


def test_other_codes_untouched_and_input_not_mutated():
    df = pd.concat([daily("2330", [100, 100]), daily("0050", [50, 50])], ignore_index=True)
    before = df.copy()
    out = apply_plan(df, build_plan([ev("2330", date(2026, 1, 6), "dividend", 100, 90)], START, END))
    assert list(out[out["code"] == "0050"]["cum_factor"]) == [1.0, 1.0]
    pd.testing.assert_frame_equal(df, before)


def test_identical_duplicate_event_counted_once():
    e = ev("00939", date(2026, 1, 2), "dividend", 14.0, 13.95)
    plan = build_plan([e, ev("00939", date(2026, 1, 2), "dividend", 14.0, 13.95)], START, END)
    assert len(plan.factors["00939"]) == 1 and plan.conflicts == {}


def test_same_day_different_events_is_conflict_and_outputs_nan():
    d = date(2026, 1, 6)
    plan = build_plan([ev("1234", d, "capital_reduction", 20, 25), ev("1234", d, "dividend", 20, 19)], START, END)
    assert "1234" in plan.conflicts and "同日多筆事件" in plan.conflicts["1234"][0]
    out = apply_plan(daily("1234", [20, 25]), plan)
    assert out["cum_factor"].isna().all() and out["close_adj"].isna().all()


@pytest.mark.parametrize("before, ref", [(0, 10), (10, 0), (None, 10), (10, None), (-1, 10)])
def test_invalid_event_prices_are_conflicts(before, ref):
    plan = build_plan([ev("5678", date(2026, 1, 6), "capital_reduction", before, ref)], START, END)
    assert "防呆檢核" in plan.conflicts["5678"][0] and "5678" not in plan.factors


def test_share_structure_change_codes():
    plan = build_plan([
        ev("7780", date(2026, 1, 19), "par_value_change", 185, 18.5),
        ev("2330", date(2026, 6, 12), "dividend", 1000, 995),
        ev("0052", date(2026, 3, 1), "etf_split", 245.3, 35.04),
        ev("1808", date(2026, 9, 1), "capital_reduction", 34.45, 37.16),
    ], START, END)
    assert codes_with_share_structure_change(plan, date(2026, 1, 1)) == {"7780", "0052", "1808"}
    assert codes_with_share_structure_change(plan, date(2026, 6, 1)) == {"1808"}   # 視窗起點由呼叫端決定
