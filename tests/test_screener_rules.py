"""第 10.1 節第 3、4 項：分頁互斥、股本硬門檻、排除 DR／特別股、創新板納入、U2 可關閉、綠燈不過濾、出場標示。"""

import copy
from datetime import date, timedelta

import pandas as pd
import pytest

from src.config import load_config
from src.screener import ScreenInputs, alert_codes, exit_status, page_of, screen
from src.timezone import format_roc_compact
from src.trading_calendar import WeekStatus

CFG, _ = load_config(env={})
MON0 = date(2026, 1, 5)
N_WEEKS = 30
LAST_MON = MON0 + timedelta(weeks=N_WEEKS - 1)
AS_OF = LAST_MON + timedelta(days=4)
ROC_AS_OF = format_roc_compact(AS_OF)


def crossing(w):          # 前 29 週 100，本週 120 → 本週新站上（t-1 收盤＝W20MA[t-1]，等號仍算未站上）
    return 120.0 if w == N_WEEKS - 1 else 100.0


def rising(w):
    return 100.0 + w


def falling_now(w):       # 一路走高，本週跌破
    return 80.0 if w == N_WEEKS - 1 else 100.0 + w


def below(w):
    return 200.0 - w


def daily_rows(code, market, name, close_fn, shares=2_000_000.0, spike=1.0, managed=False):
    rows = []
    for w in range(N_WEEKS):
        c = close_fn(w)
        vol = shares * (spike if w == N_WEEKS - 1 else 1.0)
        for i in range(5):
            d = MON0 + timedelta(weeks=w, days=i)
            rows.append({"date": d, "market": market, "code": code, "name": name, "open": c, "high": c + 1,
                         "low": c - 1, "close": c, "volume_shares": vol, "value_ntd": vol * c, "managed": managed})
    return rows


STOCKS = [
    # code, market, name, close_fn, kwargs, capital, industry, finmind category
    ("2330", "twse", "台積電", crossing, {}, 2.5e11, "24", "半導體業"),
    ("2317", "twse", "鴻海", rising, {"spike": 3.0}, 1.3e11, "31", "其他電子業"),
    ("2454", "twse", "聯發科", rising, {}, 1.6e10, "24", "半導體業"),
    ("1101", "twse", "台泥", crossing, {}, 1.9e9, "01", "水泥工業"),
    ("1102", "twse", "亞泥", crossing, {}, 2.0e9, "01", "水泥工業"),
    ("3008", "twse", "大立光", crossing, {"shares": 500_000.0}, 3.0e9, "31", "其他電子業"),
    ("9103", "twse", "美德醫療", crossing, {}, 5.0e9, "91", "存託憑證"),
    ("6949", "twse", "沛爾生醫-創", crossing, {}, 3.0e9, "22", "創新板股票"),
    ("2603", "twse", "長榮", falling_now, {}, 2.1e10, "15", "航運業"),
    ("2002", "twse", "中鋼", below, {}, 1.5e11, "10", "鋼鐵工業"),
    ("5347", "twse", "世界", crossing, {"managed": True}, 1.6e10, "24", "半導體業"),
    ("6488", "tpex", "環球晶", crossing, {}, 5.3e9, "24", "半導體業"),
]
ETFS = [
    ("0050", "twse", "元大台灣50", crossing, {}, "ETF"),
    ("00679B", "tpex", "元大美債20年", rising, {"spike": 3.0}, "上櫃ETF"),
    ("00631L", "twse", "元大台灣50正2", rising, {}, "ETF"),
]


def make_inputs(settled=True, finmind=True, disposition=None, notice=None):
    rows = []
    for code, market, name, fn, kw, *_ in STOCKS:
        rows += daily_rows(code, market, name, fn, **kw)
    for code, market, name, fn, kw, _cat in ETFS:
        rows += daily_rows(code, market, name, fn, **kw)
    daily = pd.DataFrame(rows)
    days = sorted(daily["date"].unique())
    index = pd.DataFrame({"date": days, "close": [10000.0 + i for i in range(len(days))]})
    twse_basics = [{"公司代號": c, "公司簡稱": n, "產業別": ind, "實收資本額": str(int(cap)),
                    "已發行普通股數或TDR原股發行股數": str(int(cap / 10))}
                   for c, m, n, _f, _k, cap, ind, _cat in STOCKS if m == "twse"]
    twse_basics.append({"公司代號": "2881A", "公司簡稱": "富邦特", "產業別": "17", "實收資本額": "9000000000",
                        "已發行普通股數或TDR原股發行股數": "1"})
    tpex_basics = [{"SecuritiesCompanyCode": c, "CompanyAbbreviation": n, "SecuritiesIndustryCode": ind,
                    "Paidin.Capital.NTDollars": str(int(cap)), "IssueShares": str(int(cap / 10))}
                   for c, m, n, _f, _k, cap, ind, _cat in STOCKS if m == "tpex"]
    fm = None
    if finmind:
        fm = {c: [{"industry_category": cat, "stock_id": c, "stock_name": n, "type": m, "date": "2026-11-20"}]
              for c, m, n, _f, _k, _cap, _ind, cat in STOCKS}
        fm.update({c: [{"industry_category": cat, "stock_id": c, "stock_name": n, "type": m, "date": "2026-11-20"}]
                   for c, m, n, _f, _k, cat in ETFS})
    week = WeekStatus(as_of=AS_OF, week_id="2026-W06", week_monday=LAST_MON, is_settled=settled,
                      trading_days_elapsed=5)
    return ScreenInputs(
        daily=daily, index_daily=index, events=[], week=week, history_start=MON0,
        twse_basics=twse_basics, tpex_basics=tpex_basics, finmind_rows=fm,
        twse_disposition=disposition if disposition is not None else [
            {"Date": ROC_AS_OF, "Code": "2330", "Name": "台積電",
             "DispositionPeriod": f"{format_roc_compact(AS_OF - timedelta(days=2))}~{format_roc_compact(AS_OF + timedelta(days=7))}"}],
        twse_notice=notice if notice is not None else [{"Date": ROC_AS_OF, "Code": "2317", "Name": "鴻海"}],
        tpex_disposition=[], tpex_warning=[],
    )


def roc_slash(d):
    r = format_roc_compact(d)
    return f"{r[:3]}/{r[3:5]}/{r[5:]}"


def ids(tab):
    return [r["stock_id"] for r in tab]


@pytest.fixture(scope="module")
def result():
    return screen(make_inputs(), CFG)


def test_pages_are_mutually_exclusive_and_expected(result):
    p1, p2 = set(ids(result["tabs"]["new_cross"])), set(ids(result["tabs"]["above_and_spike"]))
    assert p1 & p2 == set()
    assert p1 == {"2330", "6949", "6488", "5347"}
    assert p2 == {"2317"}
    assert all(r["vol_ratio_20w"] >= CFG["screening"]["volume_spike_ratio"] for r in result["tabs"]["above_and_spike"])


def test_new_cross_does_not_require_spike(result):
    row = next(r for r in result["tabs"]["new_cross"] if r["stock_id"] == "2330")
    assert row["is_volume_spike"] is False                 # 頁 1 不硬卡爆量，只標示
    assert [r for r in result["tabs"]["new_cross"] if r["is_volume_spike"]] == []   # 勾選「僅看同時爆量」後為空


def test_capital_threshold_is_strict_and_all_listed_pass(result):
    listed = result["tabs"]["new_cross"] + result["tabs"]["above_and_spike"]
    assert all(r["capital_billion_ntd"] > 20 for r in listed)
    assert "1101" not in ids(listed) and "1102" not in ids(listed)       # 19 億、恰 20 億皆排除
    assert result["summary"]["passed_capital_gt_20b"] == 9      # 12 檔扣 DR、19 億、恰 20 億


def test_excludes_dr_preferred_and_non_common_codes(result):
    every = result["_all_rows"]
    assert "9103" not in every and "2881A" not in every
    listed = result["tabs"]["new_cross"] + result["tabs"]["above_and_spike"]
    assert all(r["asset_type"] == "common_stock" and len(r["stock_id"]) == 4 for r in listed)
    assert result["summary"]["total_common_stocks_scanned"] == 11      # 12 檔扣 DR；特別股代號不符


def test_tib_included_when_capital_passes(result):
    row = next(r for r in result["tabs"]["new_cross"] if r["stock_id"] == "6949")
    assert row["is_tib"] is True


def test_liquidity_threshold_and_switch_off():
    assert "3008" not in ids(screen(make_inputs(), CFG)["tabs"]["new_cross"])
    cfg = copy.deepcopy(CFG)
    cfg["universe"]["min_avg_daily_lots_20w"] = 0
    res = screen(make_inputs(), cfg)
    assert "3008" in ids(res["tabs"]["new_cross"])
    assert res["summary"]["passed_liquidity"] == res["summary"]["passed_capital_gt_20b"]


def test_spike_ratio_follows_config():
    cfg = copy.deepcopy(CFG)
    cfg["screening"]["volume_spike_ratio"] = 3.5
    assert ids(screen(make_inputs(), cfg)["tabs"]["above_and_spike"]) == []


def test_green_light_does_not_filter(result):
    green = screen(make_inputs(), CFG, force_light="GREEN")
    assert green["market"]["light"] == "GREEN" and green["market"]["hard_filtered"] is False
    assert "3 成" in green["market"]["suggested_exposure"]
    for tab in ("new_cross", "above_and_spike", "etf", "exit_alerts"):
        assert ids(green["tabs"][tab]) == ids(result["tabs"][tab])


def test_market_light_red_from_index(result):
    assert result["market"]["light"] == "RED" and result["market"]["light_label_zh"] == "紅燈（偏多）"


def test_exit_confirmed_when_settled_and_midweek_when_not(result):
    row = next(r for r in result["tabs"]["exit_alerts"] if r["stock_id"] == "2603")
    assert row["exit_status"] == "EXIT_CONFIRMED" and row["exit_action_note"] == "跌破週 20MA：建議下週一開盤賣"
    mid = screen(make_inputs(settled=False), CFG)
    row = next(r for r in mid["tabs"]["exit_alerts"] if r["stock_id"] == "2603")
    assert row["exit_status"] == "MIDWEEK_WARNING" and "待週五收盤確認" in row["exit_action_note"]
    assert mid["summary"]["midweek_warning_count"] == 1 and mid["summary"]["exit_confirmed_count"] == 0
    assert result["_all_rows"]["2002"]["exit_status"] == "BELOW_W20"
    assert result["_all_rows"]["2002"]["stock_id"] not in ids(result["tabs"]["exit_alerts"])
    assert result["_all_rows"]["2330"]["exit_status"] == "HOLD_ABOVE"


def test_no_hard_stop_loss_logic():
    import inspect

    import src.screener as sc
    src_text = inspect.getsource(sc)
    assert "stop_loss" not in src_text and "0.07" not in src_text


def test_warnings(result):
    rows = result["_all_rows"]
    assert "處置股" in rows["2330"]["warnings"] and "注意股" in rows["2317"]["warnings"]
    assert "管理股票" in rows["5347"]["warnings"]
    assert "處置股" not in rows["2317"]["warnings"]


def test_sector_resonance_and_leader(result):
    r = result["_all_rows"]["2330"]
    assert r["signals"]["sector_peers_count"] == 4          # 2330、2454、5347、6488（同代碼 24，跨市場）
    assert r["score_detail"]["s6"] == 10 and r["sector_label"] == "族群共振（同業 4 檔）"
    assert r["signals"]["sector_leader"] is True
    assert result["_all_rows"]["6488"]["signals"]["sector_leader"] is False
    assert result["_all_rows"]["6949"]["score_detail"]["s6"] == 0   # 同業只有自身


def test_etf_tab_zones_and_no_capital(result):
    etf = {r["stock_id"]: r for r in result["tabs"]["etf"]}
    assert set(etf) == {"0050", "00679B", "00631L"}                 # 收錄全部 ETF（M4-Q5 暫定）
    assert etf["00631L"]["etf_zone"] is None and result["summary"]["etf_signal_count"] == 2
    assert etf["0050"]["etf_zone"] == "new_cross" and etf["00679B"]["etf_zone"] == "above_and_spike"
    assert etf["00679B"]["etf_subtype"] == "bond" and etf["0050"]["etf_subtype"] == "equity_regular"
    assert etf["0050"]["capital_billion_ntd"] is None and etf["0050"]["asset_type"] == "etf"
    assert result["_all_rows"]["00631L"]["etf_subtype"] == "leveraged_inverse"


def test_finmind_unavailable_falls_back():
    res = screen(make_inputs(finmind=False), CFG)
    assert {"0050", "00679B"} <= set(ids(res["tabs"]["etf"]))
    row = res["_all_rows"]["6949"]
    assert row["is_tib"] is False and row["industry"] == "22"           # Q6：顯示官方代碼
    assert "9103" not in res["_all_rows"]                               # 仍以產業別 91 排除
    assert any("FinMind" in n for n in res["notes"])


def test_score_sorted_desc(result):
    scores = [r["score"] for r in result["tabs"]["new_cross"]]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= r["score"] <= 100 for r in result["_all_rows"].values())


def test_weekly_charts_for_listed(result):
    listed = set(ids(result["tabs"]["new_cross"]) + [r["stock_id"] for r in result["tabs"]["etf"] if r["etf_zone"]])
    assert listed <= set(result["weekly_charts"])
    bar = result["weekly_charts"]["2330"][-1]
    assert bar["close"] == 120.0 and bar["w20ma"] == pytest.approx(101.0)


def test_empty_data_does_not_crash():
    inp = make_inputs()
    inp.daily = inp.daily.iloc[0:0]
    inp.index_daily = inp.index_daily.iloc[0:0]
    res = screen(inp, CFG)
    assert all(res["tabs"][k] == [] for k in res["tabs"])
    assert res["market"]["light"] is None


# ---------------------------------------------------------------- 單元判定

def test_page_of_equality_and_missing():
    base = {"has_current_week_bar": True, "close_w": 101, "w20ma": 100, "close_w_prev": 100, "w20ma_prev": 100,
            "vol_ratio_20w": 1.0}
    assert page_of(base, 1.5) == "new_cross"
    assert page_of({**base, "close_w": 100}, 1.5) is None                       # 須嚴格大於
    assert page_of({**base, "close_w_prev": 100.01}, 1.5) is None              # 已站上但未爆量
    assert page_of({**base, "close_w_prev": 100.01, "vol_ratio_20w": 1.5}, 1.5) == "above_and_spike"
    assert page_of({**base, "w20ma_prev": None}, 1.5) is None
    assert page_of({**base, "has_current_week_bar": False}, 1.5) is None       # 全週停牌不進買進名單


def test_exit_status_partition():
    b = {"has_current_week_bar": True, "w20ma": 100, "w20ma_prev": 100}
    assert exit_status({**b, "close_w": 100, "close_w_prev": 90}, True) == "HOLD_ABOVE"
    assert exit_status({**b, "close_w": 99, "close_w_prev": 100}, True) == "EXIT_CONFIRMED"
    assert exit_status({**b, "close_w": 99, "close_w_prev": 100}, False) == "MIDWEEK_WARNING"
    assert exit_status({**b, "close_w": 99, "close_w_prev": 99}, True) == "BELOW_W20"
    assert exit_status({**b, "close_w": 99, "close_w_prev": 100, "has_current_week_bar": False}, True) is None


def test_alert_codes_rules_and_bad_rows():
    inp = make_inputs(
        disposition=[
            {"Date": ROC_AS_OF, "Code": "1111",                                              # TWSE 格式，涵蓋基準日
             "DispositionPeriod": f"{roc_slash(AS_OF - timedelta(days=4))}～{roc_slash(AS_OF)}"},
            {"Date": format_roc_compact(AS_OF + timedelta(days=1)), "Code": "2222",           # 公告晚於基準日
             "DispositionPeriod": f"{format_roc_compact(AS_OF + timedelta(days=3))}~{format_roc_compact(AS_OF + timedelta(days=9))}"},
            {"Date": format_roc_compact(AS_OF - timedelta(days=20)), "Code": "3333",          # 已結束
             "DispositionPeriod": f"{format_roc_compact(AS_OF - timedelta(days=17))}~{format_roc_compact(AS_OF - timedelta(days=11))}"},
            {"Date": "bad", "Code": "4444", "DispositionPeriod": "x"},
        ],
        notice=[{"Date": ROC_AS_OF, "Code": "5555"}, {"Date": format_roc_compact(AS_OF - timedelta(days=1)), "Code": "6666"},
                {"Date": "", "Code": ""}])
    disp, notice, notes = alert_codes(inp, AS_OF)
    assert disp == {"1111"} and notice == {"5555"}
    assert any("無法解析" in n for n in notes)
    inp.twse_notice = None
    _d, _n, notes = alert_codes(inp, AS_OF)
    assert any("注意資料不可用" in n for n in notes)


def _halve_from(inp, code, day):
    m = (inp.daily["code"] == code) & (inp.daily["date"] >= day)
    for col in ("open", "high", "low", "close"):
        inp.daily.loc[m, col] = inp.daily.loc[m, col] * 0.5
    return inp


def test_unadjusted_jump_warning():
    """M4-Q1：收盤跌 > 35% 且查無事件 → warnings 標註第 9 節原文；有官方事件則不標。"""
    day = MON0 + timedelta(weeks=10, days=2)
    res = screen(_halve_from(make_inputs(), "2002", day), CFG)
    row = res["_all_rows"]["2002"]
    assert "疑似未還原跳空異常，請人工看圖複核" in row["warnings"] and row["unadjusted_jump_dates"] == [day]
    assert res["_all_rows"]["2330"]["unadjusted_jump_dates"] == []

    from src.fetchers.parsing import CorporateEvent
    inp = _halve_from(make_inputs(), "2002", day)
    prev_close = float(inp.daily[(inp.daily["code"] == "2002") & (inp.daily["date"] == day - timedelta(days=1))]["close"].iloc[0])
    inp.events = [CorporateEvent("twse", "2002", "中鋼", day, "par_value_change", prev_close, prev_close * 0.5, "t")]
    row = screen(inp, CFG)["_all_rows"]["2002"]
    assert "疑似未還原跳空異常，請人工看圖複核" not in row["warnings"] and row["unadjusted_jump_dates"] == []
    assert "近45週曾減資/分割" in row["warnings"]
