"""S1～S5 評分、擺盪點、切線、W 底、盤整警示；S5 日 20MA 上穿；大盤燈號；ETF 子類型；稽核工具。"""

import copy
from datetime import date, timedelta

import pandas as pd
import pytest

from src.adjustments import AdjustmentPlan, apply_plan
from src.anomalies import close_jumps, reconcile_events
from src.config import load_config
from src.fetchers.finmind import latest_rows_by_stock
from src.fetchers.parsing import CorporateEvent
from src.indicators import stock_indicators
from src.market import classify_light
from src.patterns import evaluate_patterns, find_trendline, s1_points, s2_points, swing_points
from src.trading_calendar import WeekStatus
from src.timezone import week_monday
from src.universe import etf_subtype, etf_subtype_matches

CFG, _ = load_config(env={})


def series(highs, lows=None, closes=None):
    n = len(highs)
    lows = lows or [h - 5 for h in highs]
    closes = closes or [h - 2 for h in highs]
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def ind(**kw):
    return {"vol_ratio_20w": 1.0, "w20ma": 100.0, "w20ma_prev": 100.0, "d20_cross_w20": False, **kw}


# ---------------------------------------------------------------- S1／S2

@pytest.mark.parametrize("ratio, pts", [(None, 0), (1.49, 0), (1.5, 15), (1.99, 15), (2.0, 25), (5, 25)])
def test_s1(ratio, pts):
    assert s1_points(ratio) == pts


@pytest.mark.parametrize("w, wp, pts", [(100.1, 100, 15), (100, 100, 8), (99.8, 100, 8), (99.79, 100, 0), (None, 1, 0)])
def test_s2(w, wp, pts):
    assert s2_points(w, wp) == pts


# ---------------------------------------------------------------- 擺盪點與切線

def test_swing_points_strict_and_right_neighbours_before_t():
    vals = [1, 2, 5, 2, 1, 1, 3, 3, 1, 1, 9, 1]          # 5 為嚴格高點；3,3 不嚴格；9 在 t-2，右鄰到 t-1 只有 1 根
    t = len(vals) - 1
    assert swing_points(vals, t, 2, 26, "high") == [2]


def flat_with_peaks(n, peaks, base=100.0):
    highs = [base] * n
    for i, v in peaks.items():
        highs[i] = v
    return highs


def test_trendline_break_and_13w():
    n = 30
    t = n - 1
    highs = flat_with_peaks(n, {t - 20: 150.0, t - 10: 130.0})   # Δt = 10 ≥ 8，H2 < H1
    closes = [h - 2 for h in highs]
    closes[t] = 160.0
    highs[t] = 161.0
    out = evaluate_patterns(series(highs, closes=closes), ind(), CFG)
    assert out["breakout_13w"] and out["trendline_break"] and out["s3"] == 20
    assert out["trendline"]["t_h1"] == -20 and out["trendline"]["t_h2"] == -10
    assert out["trendline"]["value_at_t"] == pytest.approx(110.0)       # 130 + (−2)×10


def test_trendline_tries_earlier_pivots_when_gap_too_short():
    n = 30
    t = n - 1
    highs = flat_with_peaks(n, {t - 24: 160.0, t - 12: 140.0, t - 6: 120.0})
    line = find_trendline(highs, t, 2, 8)
    # 最近一組 (t-12, t-6) 間隔 6 不足 → 以 H2 = t-6 往回找 H1 = t-24（Δt = 18）
    assert (line.t_h1, line.t_h2) == (t - 24, t - 6)


def test_trendline_none_when_not_descending_but_26w_high_counts():
    n = 30
    t = n - 1
    highs = flat_with_peaks(n, {t - 20: 120.0, t - 10: 130.0})   # 上升高點不成切線
    closes = [h - 2 for h in highs]
    assert find_trendline(highs, t, 2, 8) is None
    closes[t] = 131.0
    out = evaluate_patterns(series(highs, closes=closes), ind(), CFG)
    assert out["trendline_break"] is True                         # 突破 26 週高點視同切線突破（Q5）


def test_pattern_requires_27_bars():
    highs = flat_with_peaks(26, {5: 150.0, 15: 130.0})
    closes = [h - 2 for h in highs]
    closes[-1] = 200.0
    out = evaluate_patterns(series(highs, closes=closes), ind(), CFG)
    assert out["trendline_break"] is False and out["s4"] == 0 and out["breakout_13w"] is True


def test_steep_warning_only_when_configured():
    n = 30
    t = n - 1
    highs = flat_with_peaks(n, {t - 20: 150.0, t - 10: 130.0})
    closes = [h - 2 for h in highs]
    assert evaluate_patterns(series(highs, closes=closes), ind(), CFG)["notes"] == []
    cfg = copy.deepcopy(CFG)
    cfg["screening"]["trendline_steep_warning_log_rate"] = 0.001
    assert evaluate_patterns(series(highs, closes=closes), ind(), cfg)["notes"] == ["切線偏陡"]


# ---------------------------------------------------------------- W 底

def w_series(l1, l2, close_t, neck=120.0):
    n = 30
    t = n - 1
    lows = [100.0] * n
    highs = [110.0] * n
    lows[t - 15], lows[t - 6] = l1, l2
    highs[t - 10] = neck
    closes = [105.0] * n
    closes[t] = close_t
    highs[t] = max(close_t, 110.0)
    return series(highs, lows=lows, closes=closes)


def test_w_bottom_full_and_partial():
    full = evaluate_patterns(w_series(80.0, 79.5, 121.0), ind(), CFG)
    assert full["s4"] == 20 and full["w_bottom"] is True
    part = evaluate_patterns(w_series(80.0, 79.5, 115.0), ind(), CFG)
    assert part["s4"] == 8 and part["w_bottom"] is False and part["w_bottom_partial"] is True
    assert evaluate_patterns(w_series(80.0, 79.0, 121.0), ind(), CFG)["s4"] == 0     # 79 < 80 × 0.99


def test_box_consolidation_includes_current_week():
    closes = [100.0] * 30
    s = series([101.0] * 30, lows=[99.0] * 30, closes=closes)
    assert evaluate_patterns(s, ind(), CFG)["box_consolidation"] is True
    closes2 = closes[:-1] + [110.0]
    s2 = series([101.0] * 29 + [111.0], lows=[99.0] * 30, closes=closes2)
    assert evaluate_patterns(s2, ind(), CFG)["box_consolidation"] is False


# ---------------------------------------------------------------- S5

def daily(code, closes_by_day):
    rows = [{"date": d, "code": code, "name": "x", "market": "twse", "managed": False, "open": c, "high": c + 1,
             "low": c - 1, "close": c, "volume_shares": 1e6} for d, c in closes_by_day]
    return apply_plan(pd.DataFrame(rows), AdjustmentPlan(date(2025, 1, 1), date(2027, 1, 1)))


def weekdays(start, n_weeks):
    return [start + timedelta(weeks=w, days=i) for w in range(n_weeks) for i in range(5)]


def ws(as_of):
    return WeekStatus(as_of=as_of, week_id="", week_monday=week_monday(as_of), is_settled=True, trading_days_elapsed=5)


def test_d20_cross_w20_happens_this_week():
    days = weekdays(date(2026, 1, 5), 22)
    # 前 21 週高檔 100、近 4 週下跌讓 D20 < W20，本週急漲使 D20 由下往上穿越暫定 W20
    closes = []
    for i, d in enumerate(days):
        w = i // 5
        closes.append(100.0 if w < 17 else (80.0 if w < 21 else 140.0 + (i % 5) * 10))
    r = stock_indicators(daily("2330", list(zip(days, closes))), ws(days[-1]), CFG)
    assert r["d20_cross_w20"] is True


def test_d20_cross_not_counted_when_already_above():
    days = weekdays(date(2026, 1, 5), 22)
    closes = [100.0 + i for i in range(len(days))]          # 一路上漲：D20 一直在暫定 W20 之上，本週沒有「穿越」
    r = stock_indicators(daily("2330", list(zip(days, closes))), ws(days[-1]), CFG)
    assert r["d20_cross_w20"] is False


# ---------------------------------------------------------------- 燈號、ETF 子類型、FinMind 多列

@pytest.mark.parametrize("close, d20, d100, light", [
    (110, 100, 105, "RED"), (90, 100, 95, "GREEN"), (100, 100, 90, "YELLOW"), (95, 100, 90, "YELLOW"),
    (95, 90, 100, "YELLOW"), (None, 1, 1, None)])
def test_classify_light(close, d20, d100, light):
    assert classify_light(close, d20, d100) == light


@pytest.mark.parametrize("code, name, sub", [
    ("0050", "元大台灣50", "equity_regular"), ("00631L", "元大台灣50正2", "leveraged_inverse"),
    ("00632R", "元大台灣50反1", "leveraged_inverse"), ("00679B", "元大美債20年", "bond"),
    ("00720", "元大投資級公司債", "bond"), ("00400A", "主動國泰動能高息", "active"), ("00878", "國泰永續高股息", "equity_regular")])
def test_etf_subtype(code, name, sub):
    assert etf_subtype(code, name) == sub


def test_etf_subtype_overlap_follows_listed_order():
    assert etf_subtype_matches("00999A", "主動某某債") == ["bond", "active"]
    assert etf_subtype("00999A", "主動某某債") == "bond"


def test_latest_rows_keeps_all_rows_of_latest_date():
    recs = [
        {"stock_id": "2330", "industry_category": "半導體業", "stock_name": "台積電", "type": "twse", "date": "2026-11-20"},
        {"stock_id": "2330", "industry_category": "電子工業", "stock_name": "台積電", "type": "twse", "date": "2026-11-20"},
        {"stock_id": "2330", "industry_category": "舊", "stock_name": "台積電", "type": "emerging", "date": "2020-01-01"},
        {"stock_id": "TAIEX", "industry_category": "大盤", "stock_name": "加權", "type": "twse", "date": "None"},
    ]
    out = latest_rows_by_stock(recs)
    assert [r["industry_category"] for r in out["2330"]] == ["半導體業", "電子工業"]
    assert out["TAIEX"][0]["_date_unknown"] is True


# ---------------------------------------------------------------- 稽核工具

def test_close_jumps_and_event_flag():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    rows = []
    for code, closes in (("1111", [100, 101, 50, 51, 52]), ("2222", [100, 100, 10, 10, 10])):
        for d, c in zip(days, closes):
            rows.append({"date": d, "code": code, "name": code, "market": "twse", "open": c, "high": c, "low": c,
                         "close": c, "volume_shares": 1.0})
    ev = [CorporateEvent("twse", "2222", "x", days[2], "par_value_change", 100, 10, "t")]
    j = close_jumps(pd.DataFrame(rows), ev)
    got = {r.code: r for r in j.itertuples()}
    assert set(got) == {"1111", "2222"}
    assert got["1111"].has_event is False and got["1111"].close_ratio == pytest.approx(50 / 101)
    assert got["2222"].has_event is True and got["2222"].nth_valid_day == 3


def test_reconcile_events():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(4)]
    rows = [{"date": d, "code": "2330", "name": "x", "market": "twse", "open": c, "high": c, "low": c, "close": c,
             "volume_shares": 1.0} for d, c in zip(days, [100, 101, None, 98])]
    ev = [CorporateEvent("twse", "2330", "x", days[3], "dividend", 101, 98, "t"),      # 前一有效日為 1/6（1/7 無價）
          CorporateEvent("twse", "2330", "x", days[1], "dividend", 99, 98, "t"),
          CorporateEvent("twse", "2330", "x", days[0], "dividend", 99, 98, "t"),
          CorporateEvent("twse", "9999", "x", days[1], "dividend", 99, 98, "t")]
    r = reconcile_events(pd.DataFrame(rows), ev, date(2026, 1, 1), days[-1])
    assert r["status"].tolist() == ["match", "mismatch", "no_prior_day", "code_not_in_data"]
