"""第 6.3 節：latest.json Schema、4 份 UTF-8 BOM CSV、latest.js；執行入口 run()。"""

import json
from datetime import date

import numpy as np
import pytest

from src import screener as sc
from src.config import load_config
from src.output import CHART_WEEKS, CSV_COLUMNS, build_latest, clean, csv_text, write_site_data
from src.screener import screen
from tests.test_screener_rules import make_inputs

CFG, _ = load_config(env={})
TOP_KEYS = ["schema_version", "generated_at_taipei", "as_of_date", "week_id", "is_week_settled",
            "trading_days_in_current_week", "price_mode", "params", "market", "summary", "tabs", "weekly_charts"]
ROW_KEYS = ["stock_id", "stock_name", "market", "asset_type", "is_tib", "etf_subtype", "industry", "capital_billion_ntd",
            "market_cap_billion_ntd", "close_raw", "close_adj", "w20ma", "d100ma", "d20ma", "ma_diff", "bias_20w_pct",
            "vol_week_lots", "vol_avg_20w_lots", "vol_ratio_20w", "vol_ratio_20w_projected", "vol_ratio_prev_week",
            "is_volume_spike", "score", "signals", "exit_status", "exit_action_note", "warnings", "fundamentals_ref"]
SIGNAL_KEYS = ["w20_slope_up", "d20_cross_w20", "breakout_13w", "trendline_break", "w_bottom", "box_consolidation",
               "sector_peers_count", "sector_leader"]
FUND = {"eps_quarter": "115Q3", "eps": 0.63, "rev_month": "11510", "rev_yoy_pct": 17.83, "insti_5d_net_lots": 4520.4}


@pytest.fixture(scope="module")
def latest():
    res = screen(make_inputs(settled=False), CFG)
    return build_latest(res, "2026-07-31T18:05:12+08:00", {"2330": FUND}, notes=["n1"], errors=["e1"])


def test_top_level_schema(latest):
    assert list(latest)[:len(TOP_KEYS)] == TOP_KEYS
    assert latest["schema_version"] == "1.0" and latest["price_mode"] == "backward_adjusted_official"
    assert latest["as_of_date"] == "2026-07-31" and latest["is_week_settled"] is False
    assert set(latest["params"]) == {"min_capital_ntd", "min_avg_daily_lots_20w", "volume_spike_ratio", "ma_weeks",
                                     "require_volume_spike_on_new_cross", "include_tib"}
    assert set(latest["market"]) >= {"index_name", "date", "close", "d20ma", "d100ma", "w20ma", "bias_w20_pct", "light",
                                      "light_label_zh", "suggested_exposure", "hard_filtered"}
    assert set(latest["summary"]) == {"total_common_stocks_scanned", "passed_capital_gt_20b", "passed_liquidity",
                                      "new_cross_count", "above_and_spike_count", "etf_signal_count",
                                      "exit_confirmed_count", "midweek_warning_count"}
    assert set(latest["tabs"]) == {"new_cross", "above_and_spike", "etf", "exit_alerts"}
    assert "n1" in latest["notes"] and latest["errors"] == ["e1"]
    json.dumps(latest, allow_nan=False)                         # 不得含 NaN／Infinity


def test_row_schema_and_types(latest):
    for tab, rows in latest["tabs"].items():
        for r in rows:
            assert all(k in r for k in ROW_KEYS), (tab, r["stock_id"])
            assert list(r["signals"]) == SIGNAL_KEYS
            assert isinstance(r["score"], int) and isinstance(r["warnings"], list)
            assert r["market"] in ("twse", "tpex") and r["asset_type"] in ("common_stock", "etf")
            assert r["exit_status"] in ("HOLD_ABOVE", "EXIT_CONFIRMED", "MIDWEEK_WARNING", "BELOW_W20", None)
            if r["asset_type"] == "etf":
                assert r["capital_billion_ntd"] is None and r["market_cap_billion_ntd"] is None
                assert r["fundamentals_ref"] is None and r["is_tib"] is False and r["etf_subtype"]
            else:
                assert r["etf_subtype"] is None
    r2330 = next(r for r in latest["tabs"]["new_cross"] if r["stock_id"] == "2330")
    assert r2330["fundamentals_ref"] == FUND
    assert next(r for r in latest["tabs"]["new_cross"] if r["stock_id"] == "6488")["fundamentals_ref"] is None
    assert r2330["vol_ratio_20w_projected"] is not None                       # 週中才有推算倍數


def test_weekly_charts(latest):
    bar = latest["weekly_charts"]["2330"][-1]
    assert list(bar) == ["week_end_date", "open", "high", "low", "close", "volume_lots", "w20ma", "vavg_20w_lots"]
    assert bar["week_end_date"] == "2026-07-31" and bar["w20ma"] == 101.0
    res = screen(make_inputs(), CFG)
    res["weekly_charts"]["2330"] = res["weekly_charts"]["2330"] * 3          # 90 根 → 只留最近 45 根
    assert len(build_latest(res, "x")["weekly_charts"]["2330"]) == CHART_WEEKS


def test_clean_values():
    assert clean({"a": np.float64("nan"), "b": np.int64(3), "c": np.bool_(True), "d": date(2026, 1, 2),
                  "e": float("inf"), "f": 1.234567}) == {"a": None, "b": 3, "c": True, "d": "2026-01-02", "e": None, "f": 1.2346}


def test_csv_bom_header_and_values(tmp_path, latest):
    paths = write_site_data(latest, tmp_path)
    assert sorted(p.name for p in paths) == sorted(["latest.json", "latest.js", "new_cross.csv", "above_and_spike.csv",
                                                    "etf.csv", "exit_alerts.csv"])
    raw = (tmp_path / "new_cross.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                                   # UTF-8 BOM
    lines = raw.decode("utf-8-sig").split("\r\n")
    assert lines[0].split(",") == CSV_COLUMNS
    assert len([x for x in lines[1:] if x]) == len(latest["tabs"]["new_cross"])
    import csv as _csv
    rows = list(_csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    r = next(x for x in rows if x["stock_id"] == "2330")
    assert r["is_volume_spike"] == "false" and r["signals.trendline_break"] in ("true", "false")
    assert r["fundamentals_ref.insti_5d_net_lots"] == "4520"                # 顯示時四捨五入到整張
    assert r["vol_week_lots"] == str(round(next(x for x in latest["tabs"]["new_cross"] if x["stock_id"] == "2330")["vol_week_lots"]))
    assert "處置股" in r["warnings"] and "、" in r["warnings"]
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8")) == latest
    js = (tmp_path / "latest.js").read_text(encoding="utf-8")
    assert js.startswith("window.__SW20_LATEST__ = {") and js.rstrip().endswith("};")
    for k in ("above_and_spike", "etf", "exit_alerts"):
        assert (tmp_path / f"{k}.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def test_csv_empty_tab_has_header_only():
    assert csv_text([]).split("\r\n")[0].split(",") == CSV_COLUMNS


class FakeClient:
    logs: list = []


def test_run_end_to_end(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(sc, "collect_inputs", lambda *a, **k: make_inputs())

    def fake_fund(client, codes, days):
        calls["codes"], calls["days"] = codes, days
        return {c: dict(FUND) for c in codes}, ["fund note"]
    import src.fundamentals as fm
    monkeypatch.setattr(fm, "collect_fundamentals", fake_fund)
    assert sc.run(str(tmp_path), env={}, client=FakeClient(), today=date(2026, 7, 31)) == 0
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert "fund note" in latest["notes"]
    assert set(calls["codes"]) == {r["stock_id"] for t in latest["tabs"].values() for r in t if r["asset_type"] == "common_stock"}
    assert len(calls["days"]) >= 5 and calls["days"][-1] == date(2026, 7, 31)
    assert latest["tabs"]["etf"][0]["fundamentals_ref"] is None


def test_run_skip_fundamentals_and_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "collect_inputs", lambda *a, **k: make_inputs())
    import src.fundamentals as fm
    monkeypatch.setattr(fm, "collect_fundamentals", lambda *a, **k: pytest.fail("不應抓參考欄"))
    assert sc.run(str(tmp_path), env={"SW20_SKIP_FUNDAMENTALS": "true"}, client=FakeClient(), today=date(2026, 7, 31)) == 0
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert any("略過" in n for n in latest["notes"])
    assert all(r["fundamentals_ref"] is None for t in latest["tabs"].values() for r in t)

    from src.fetchers.history import HistoryIncomplete

    def boom(*a, **k):
        raise HistoryIncomplete("缺漏 6%")
    monkeypatch.setattr(sc, "collect_inputs", boom)
    out = tmp_path / "x"
    assert sc.run(str(out), env={}, client=FakeClient(), today=date(2026, 7, 31)) == 2
    assert not (out / "latest.json").exists()                               # 缺漏過多不發佈
