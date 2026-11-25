from datetime import date, timedelta

import pandas as pd
import pytest

from src.adjustments import AdjustmentPlan, apply_plan, build_plan
from src.config import load_config
from src.fetchers.parsing import CorporateEvent
from src.indicators import compute_all, index_indicators, stock_indicators, weekly_bars
from src.trading_calendar import WeekStatus
from src.timezone import week_monday

CFG, _ = load_config(env={})
MON0 = date(2026, 1, 5)  # 週一


def rows_for_week(code, monday, closes, vols=None, skip=(), no_price=()):
    """closes：週一起每日收盤；skip：休市的星期索引；no_price：有列但開高低收為 None 的索引。"""
    out = []
    for i, c in enumerate(closes):
        if i in skip:
            continue
        d = monday + timedelta(days=i)
        px = None if i in no_price else c
        out.append({"date": d, "code": code, "name": "測試", "market": "twse", "managed": False,
                    "open": px if px is None else c - 1, "high": px if px is None else c + 2,
                    "low": px if px is None else c - 2, "close": px,
                    "volume_shares": (vols[i] if vols else 1_000_000.0)})
    return out


def frame(rows, plan=None):
    df = pd.DataFrame(rows)
    return apply_plan(df, plan or AdjustmentPlan(date(2025, 1, 1), date(2027, 1, 1)))


def weeks(code, n, close_fn=lambda w: 100.0 + w, vol_fn=lambda w: 1_000_000.0):
    rows = []
    for w in range(n):
        monday = MON0 + timedelta(weeks=w)
        rows += rows_for_week(code, monday, [close_fn(w)] * 5, [vol_fn(w)] * 5)
    return rows


def ws(as_of, settled=True, elapsed=5):
    return WeekStatus(as_of=as_of, week_id="", week_monday=week_monday(as_of), is_settled=settled,
                      trading_days_elapsed=elapsed)


# ---------- 週 K 聚合

def test_short_week_settles_on_last_trading_day():
    # 週五休市（如 2026-09-25 中秋）：4 天成 1 根週 K，結算於週四
    df = frame(rows_for_week("2330", date(2026, 9, 21), [10, 11, 12, 13, 99], skip=(4,)))
    bars, susp = weekly_bars(df)
    assert len(bars) == 1 and susp == []
    b = bars.iloc[0]
    assert b["week_end_date"] == date(2026, 9, 24) and b["valid_days"] == 4 and b["week_id"] == "2026-W39"
    assert (b["open"], b["high"], b["low"], b["close"]) == (9, 15, 8, 13)
    assert b["volume_lots"] == pytest.approx(4000.0)


def test_monday_holiday_week():
    df = frame(rows_for_week("2330", date(2026, 9, 28), [10, 11, 12, 13, 14], skip=(0,)))
    b = weekly_bars(df)[0].iloc[0]
    assert b["open"] == 10 and b["week_end_date"] == date(2026, 10, 2) and b["valid_days"] == 4


def test_no_price_day_excluded_from_ohlc_but_volume_counted():
    rows = rows_for_week("3632", MON0, [10, 11, 12, 13, 14], vols=[1000, 672, 1000, 1000, 1000], no_price=(1,))
    b = weekly_bars(frame(rows))[0].iloc[0]
    assert b["valid_days"] == 4 and b["volume_lots"] == pytest.approx(4.672)   # 零股 672 股計入（D2）
    assert b["high"] == 16


def test_fully_suspended_week_has_no_bar():
    rows = rows_for_week("1234", MON0, [10] * 5) + rows_for_week("1234", MON0 + timedelta(weeks=1), [10] * 5, no_price=range(5)) \
        + rows_for_week("1234", MON0 + timedelta(weeks=2), [11] * 5)
    bars, susp = weekly_bars(frame(rows))
    assert len(bars) == 2 and susp == ["2026-W03"]


def test_iso_week_across_year_end_is_one_bar():
    rows = rows_for_week("0050", date(2025, 12, 29), [10, 11, 12, 13, 14], skip=(3,))  # 2026-01-01 休市
    bars = weekly_bars(frame(rows))[0]
    assert list(bars["week_id"]) == ["2026-W01"] and bars.iloc[0]["week_end_date"] == date(2026, 1, 2)


def test_not_fixed_five_day_rolling():
    # 兩週：第一週 3 天、第二週 5 天 → 2 根週 K（不是每 5 日切一根）
    rows = rows_for_week("2330", MON0, [10, 11, 12, 0, 0], skip=(3, 4)) + rows_for_week("2330", MON0 + timedelta(weeks=1), [20] * 5)
    bars = weekly_bars(frame(rows))[0]
    assert list(bars["valid_days"]) == [3, 5] and list(bars["close"]) == [12, 20]


# ---------- 指標

def test_w20ma_requires_20_bars():
    df = frame(weeks("2330", 19))
    r = stock_indicators(df, ws(MON0 + timedelta(weeks=18, days=4)), CFG)
    assert r["bars"] == 19 and r["w20ma"] is None and r["vol_ratio_20w"] is None


def test_exactly_20_bars_uses_19_prior_weeks_and_notes_short_sample():
    df = frame(weeks("2330", 20))
    r = stock_indicators(df, ws(MON0 + timedelta(weeks=19, days=4)), CFG)
    assert r["w20ma"] == pytest.approx(sum(100 + w for w in range(20)) / 20)
    assert r["w20ma_prev"] is None                         # W20MA[t-1] 需 21 根
    assert r["vol_avg_sample_weeks"] == 19 and r["notes"] == ["掛牌未滿 21 週（均量樣本較少）"]


def test_volume_average_excludes_current_week_and_spike():
    vol = lambda w: 3_000_000.0 if w == 24 else 1_000_000.0
    df = frame(weeks("2330", 25, vol_fn=vol))
    r = stock_indicators(df, ws(MON0 + timedelta(weeks=24, days=4)), CFG)
    assert r["vol_avg_20w_lots"] == pytest.approx(5000.0)       # 1,000,000 股 × 5 日 ÷ 1000
    assert r["vol_week_lots"] == pytest.approx(15000.0)
    assert r["vol_ratio_20w"] == pytest.approx(3.0) and r["is_volume_spike"] is True
    assert r["vol_ratio_prev_week"] == pytest.approx(3.0)
    assert r["vol_avg_sample_weeks"] == 20 and r["notes"] == []
    assert r["vol_ratio_20w_projected"] is None                 # 已週結算


def test_spike_threshold_follows_config():
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["screening"]["volume_spike_ratio"] = 3.5
    df = frame(weeks("2330", 25, vol_fn=lambda w: 3_000_000.0 if w == 24 else 1_000_000.0))
    assert stock_indicators(df, ws(MON0 + timedelta(weeks=24, days=4)), cfg)["is_volume_spike"] is False


def test_midweek_uses_actual_volume_and_projects_with_five_over_x():
    rows = weeks("2330", 24)
    rows += rows_for_week("2330", MON0 + timedelta(weeks=24), [130, 131, 0, 0, 0], vols=[2_000_000.0] * 5, skip=(2, 3, 4))
    df = frame(rows)
    as_of = MON0 + timedelta(weeks=24, days=1)
    r = stock_indicators(df, ws(as_of, settled=False, elapsed=2), CFG)
    assert r["vol_week_lots"] == pytest.approx(4000.0)
    assert r["vol_ratio_20w"] == pytest.approx(4000 / 5000)                   # 實際累計量（3A）
    assert r["vol_ratio_20w_projected"] == pytest.approx(4000 * 5 / 2 / 5000)  # 以 5 日換算
    assert r["close_w"] == 131 and r["has_current_week_bar"] is True


def test_daily_mas_ma_diff_and_bias():
    px = lambda w, i: 100.0 + 2 * w + i * i          # 週內每日收盤不同
    rows = []
    for w in range(22):
        rows += rows_for_week("2330", MON0 + timedelta(weeks=w), [px(w, i) for i in range(5)])
    r = stock_indicators(frame(rows), ws(MON0 + timedelta(weeks=21, days=4)), CFG)
    daily_closes = [px(w, i) for w in range(22) for i in range(5)]
    assert r["d20ma"] == pytest.approx(sum(daily_closes[-20:]) / 20)
    assert r["d100ma"] == pytest.approx(sum(daily_closes[-100:]) / 100)
    w20 = sum(px(w, 4) for w in range(2, 22)) / 20     # 週收盤 = 週五收盤
    assert r["w20ma"] == pytest.approx(w20) and r["w20ma"] != pytest.approx(r["d100ma"])  # 分欄、不相同
    last = px(21, 4)
    assert r["close_raw"] == r["close_adj"] == last
    assert r["ma_diff"] == pytest.approx(last - w20)
    assert r["bias_20w_pct"] == pytest.approx((last - w20) / w20 * 100)


def test_w20ma_uses_adjusted_prices_and_close_raw_is_original():
    rows = weeks("7780", 22, close_fn=lambda w: 185.0 if w < 10 else 18.5)
    event_day = MON0 + timedelta(weeks=10)
    plan = build_plan([CorporateEvent("twse", "7780", "x", event_day, "par_value_change", 185.0, 18.5, "t")],
                      date(2025, 12, 1), MON0 + timedelta(weeks=22))
    r = stock_indicators(frame(rows, plan), ws(MON0 + timedelta(weeks=21, days=4)), CFG)
    assert r["w20ma"] == pytest.approx(18.5)          # 還原後無斷層
    assert r["close_raw"] == 18.5 and r["bias_20w_pct"] == pytest.approx(0.0)


def test_suspended_in_current_week_flagged():
    rows = weeks("2330", 21) + rows_for_week("2330", MON0 + timedelta(weeks=21), [1] * 5, no_price=range(5))
    r = stock_indicators(frame(rows), ws(MON0 + timedelta(weeks=21, days=4)), CFG)
    assert r["has_current_week_bar"] is False and r["suspended_weeks"] == ["2026-W23"]
    assert r["notes"] == ["2026-W23：全週停牌，本週不計入週K"]                 # M3-Q7 文字


def test_conflict_code_has_no_indicators():
    rows = weeks("1234", 22)
    d = MON0 + timedelta(weeks=5)
    plan = build_plan([CorporateEvent("twse", "1234", "x", d, "dividend", 10, 9, "t"),
                       CorporateEvent("twse", "1234", "x", d, "capital_reduction", 10, 12, "t")], date(2025, 12, 1), date(2027, 1, 1))
    out = compute_all(frame(rows, plan), ws(MON0 + timedelta(weeks=21, days=4)), CFG, plan.conflicts)
    r = out["1234"]
    assert r["bars"] == 0 and r["w20ma"] is None and r["adjustment_conflicts"]


def test_compute_all_by_code_and_as_of_cut():
    rows = weeks("2330", 22) + weeks("0050", 22, close_fn=lambda w: 50.0)
    out = compute_all(frame(rows), ws(MON0 + timedelta(weeks=20, days=4)), CFG)
    assert set(out) == {"0050", "2330"} and out["2330"]["bars"] == 21          # as_of 之後的資料不納入
    assert out["0050"]["w20ma"] == pytest.approx(50.0)


def test_index_indicators():
    d0, rows = date(2026, 1, 5), []
    for i in range(200):
        d = d0 + timedelta(days=i)
        if d.weekday() < 5:
            rows.append({"date": d, "close": 20000.0 + i})
    idx = pd.DataFrame(rows)
    r = index_indicators(idx, rows[-1]["date"], CFG)
    closes = [x["close"] for x in rows]
    assert r["close"] == closes[-1] and r["d20ma"] == pytest.approx(sum(closes[-20:]) / 20)
    assert r["d100ma"] == pytest.approx(sum(closes[-100:]) / 100)
    weekly = pd.Series(closes, index=[week_monday(x["date"]) for x in rows]).groupby(level=0).last()
    assert r["w20ma"] == pytest.approx(weekly.iloc[-20:].mean())
    assert r["bias_w20_pct"] == pytest.approx((closes[-1] - r["w20ma"]) / r["w20ma"] * 100)


def test_weekly_bars_all_matches_per_code():
    from src.indicators import weekly_bars_all
    rows = rows_for_week("1234", MON0, [10] * 5) + rows_for_week("1234", MON0 + timedelta(weeks=1), [10] * 5, no_price=range(5)) \
        + rows_for_week("1234", MON0 + timedelta(weeks=2), [11, 12, 13, 14, 15], skip=(4,)) + weeks("2330", 3)
    df = frame(rows)
    bars, susp = weekly_bars_all(df)
    for code in ("1234", "2330"):
        b, s_ = weekly_bars(df[df["code"] == code])
        pd.testing.assert_frame_equal(bars[code], b, check_dtype=False)
        assert susp.get(code, []) == s_


def test_partial_ohlc_day_is_invalid():
    """M3-Q6：只有部分開高低收（例：有收盤、無開盤）整天視為無效日，不納入週 K 價格；成交量照計。"""
    rows = rows_for_week("2330", MON0, [100, 101, 102, 103, 104])
    rows[4]["open"] = None
    rows[3]["high"] = 0
    bars, suspended = weekly_bars(frame(rows))
    b = bars.iloc[0]
    assert b["valid_days"] == 3 and b["week_end_date"] == MON0 + timedelta(days=2) and b["close"] == 102
    assert b["volume_lots"] == pytest.approx(5000.0) and suspended == []


def test_missing_week_flagged_in_compute_all():
    """M3-Q7：市場有交易、該代號整週無資料列 →「資料缺週」；首筆資料之前的週不算。"""
    rows = weeks("2330", 22)
    gap = MON0 + timedelta(weeks=10)
    rows = [r for r in rows if week_monday(r["date"]) != gap]
    rows += [r for r in weeks("1101", 22)]
    late = [r for r in weeks("6949", 22) if r["date"] >= MON0 + timedelta(weeks=15)]   # 第 15 週才開始有資料
    out = compute_all(frame(rows + late), ws(MON0 + timedelta(weeks=21, days=4)), CFG)
    assert out["2330"]["missing_weeks"] == ["2026-W12"] and "2026-W12：資料缺週" in out["2330"]["notes"]
    assert out["1101"]["missing_weeks"] == [] and out["6949"]["missing_weeks"] == []
    assert out["2330"]["suspended_weeks"] == []


def test_missing_week_includes_current_week():
    rows = weeks("2330", 21) + weeks("1101", 22)
    out = compute_all(frame(rows), ws(MON0 + timedelta(weeks=21, days=4)), CFG)
    assert out["2330"]["missing_weeks"] == ["2026-W23"] and out["2330"]["has_current_week_bar"] is False
