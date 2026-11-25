"""FR-1.9 參考欄：最新季 EPS、最新月營收 YoY、近 5 日三大法人（張）。"""

from datetime import date, timedelta

import pytest

import src.fundamentals as fm
from src.fetchers import tpex, twse

DAYS = [date(2026, 11, 16) + timedelta(days=i) for i in range(6)]   # 6 個日期，只取最後 5 個


@pytest.fixture
def fake(monkeypatch):
    calls = []
    monkeypatch.setattr(twse, "fetch_quarterly_eps", lambda c: [
        {"公司代號": "2330", "年度": "115", "季別": "2", "基本每股盈餘(元)": "10.00"},
        {"公司代號": "2330", "年度": "115", "季別": "3", "基本每股盈餘(元)": "12.34"},
        {"公司代號": "1101", "年度": "115", "季別": "3", "基本每股盈餘(元)": "0.63"}])
    monkeypatch.setattr(twse, "fetch_monthly_revenue", lambda c: [
        {"公司代號": "2330", "資料年月": "11509", "營業收入-去年同月增減(%)": "5.0"},
        {"公司代號": "2330", "資料年月": "11510", "營業收入-去年同月增減(%)": "17.828005954078908"}])
    monkeypatch.setattr(tpex, "fetch_quarterly_eps", lambda c: [
        {"SecuritiesCompanyCode": "6488", "Year": "115", "季別": "3", "基本每股盈餘": "3.63"}])
    monkeypatch.setattr(tpex, "fetch_monthly_revenue", lambda c: [
        {"公司代號": "6488", "資料年月": "11510", "營業收入-去年同月增減(%)": "-2.99"}])

    def t86(c, d):
        calls.append(("twse", d))
        return {"2330": 1_000_500.0, "1101": -2000.0} if d != DAYS[-1] else {"2330": 500.0}
    monkeypatch.setattr(twse, "fetch_t86", t86)
    monkeypatch.setattr(tpex, "fetch_insti_net", lambda c, d: calls.append(("tpex", d)) or {"6488": 1000.0})
    return calls


def test_values(fake):
    out, notes = fm.collect_fundamentals(None, {"2330": "twse", "1101": "twse", "6488": "tpex", "9999": "twse"}, DAYS)
    assert out["2330"] == {"eps_quarter": "115Q3", "eps": 12.34, "rev_month": "11510",
                           "rev_yoy_pct": pytest.approx(17.828005954078908), "insti_5d_net_lots": pytest.approx(4002.5)}
    assert out["1101"]["insti_5d_net_lots"] == pytest.approx(-8.0)          # 最後一日查無 → 不計（M5-Q1 待確認）
    assert out["1101"]["rev_month"] is None
    assert out["6488"] == {"eps_quarter": "115Q3", "eps": 3.63, "rev_month": "11510", "rev_yoy_pct": -2.99,
                           "insti_5d_net_lots": 5.0}
    assert out["9999"] == {k: None for k in ("eps_quarter", "eps", "rev_month", "rev_yoy_pct", "insti_5d_net_lots")}
    assert sorted({d for _m, d in fake}) == DAYS[-5:] and notes == []


def test_failures_degrade(fake, monkeypatch):
    def boom(c, d=None):
        raise RuntimeError("down")
    monkeypatch.setattr(twse, "fetch_quarterly_eps", boom)
    monkeypatch.setattr(tpex, "fetch_insti_net", boom)
    out, notes = fm.collect_fundamentals(None, {"2330": "twse", "6488": "tpex"}, DAYS)
    assert out["2330"]["eps"] is None and out["2330"]["rev_yoy_pct"] is not None
    assert out["6488"]["insti_5d_net_lots"] is None
    assert any("TWSE 季 EPS" in n for n in notes) and any("TPEX 三大法人僅取得 0／5" in n for n in notes)


def test_only_needed_markets(fake):
    fm.collect_fundamentals(None, {"2330": "twse"}, DAYS)
    assert all(m == "twse" for m, _d in fake)
