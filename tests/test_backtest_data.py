"""M7b：src/backtest/data 以假回應測試（不連網）。Parquet 實際讀寫測試需 pyarrow（CI 依 requirements.txt 安裝）。"""

import gzip
import json
import pickle
from datetime import date, timedelta

import pandas as pd
import pytest

from src.backtest import data as bt
from src.fetchers.http import FetchError, RequestLog
from tests import fixtures as F

ORIG_GRID = bt.event_grid
TODAY = date(2026, 11, 26)          # 週四
NON_TRADING = {date(2020, 4, 2), date(2020, 4, 3)}   # 假資料中的兩市休市日（僅測試用）
TR_TABLE = {"title": "報酬指數(臺灣證券交易所)", "fields": ["報酬指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
            "data": [["寶島股價報酬指數", "16,038.42", "-", "1", "-1", ""], ["發行量加權股價報酬指數", "20,832.38", "-", "1", "-1", ""]]}


def _d(params, key):
    raw = params.get(key, "")
    if "/" in raw:
        y, m, dd = raw.split("/")
        return date(int(y), int(m), int(dd))
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))


class FakeClient:
    """依路徑回假資料；``fail_mi`` 讓 MI_INDEX 失敗；``tr`` 控制是否附報酬指數表。"""

    def __init__(self, fail_mi=False, tr=True, tpex_shares_in_lots=False):
        self.logs, self.fail_mi, self.tr, self.lots = [], fail_mi, tr, tpex_shares_in_lots
        self.headers_seen = []

    def get_json(self, url, params=None, headers=None):
        params = dict(params or {})
        self.logs.append(RequestLog(url, params, 200, 0.0, 1))
        if "finmindtrade" in url:
            self.headers_seen.append((headers, params))
            if params["dataset"] == "TaiwanStockDelisting":
                return {"msg": "success", "status": 200, "data": [{"date": "2021-05-03", "stock_id": "1234", "stock_name": "某甲"}]}
            return {"msg": "success", "status": 200, "data": [
                {"industry_category": "半導體業", "stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2024-01-01"}]}
        if "MI_INDEX" in url:
            if self.fail_mi:
                raise FetchError("模擬失敗")
            d = _d(params, "date")
            if d in NON_TRADING:
                return F.TWSE_NO_DATA
            p = F.mi_index_payload(date=d.strftime("%Y%m%d"))
            if self.tr:
                p["tables"].insert(1, TR_TABLE)
            return p
        if "dailyQuotes" in url:
            d = _d(params, "date")
            if d in NON_TRADING:
                return {**F.TPEX_NO_DATA, "date": d.strftime("%Y%m%d")}
            rows = None
            if self.lots:
                rows = [F.dq_row("6488", "環球晶", "400.00", "395.00", "405.00", "390.00", "2,345", "938,000,000")]
            return F.daily_quotes_payload(rows=rows, date=d.strftime("%Y%m%d"))
        if "TWT49U" in url:
            s = _d(params, "startDate")
            if s == bt.DATA_START:
                return {**F.TWT49U_PAYLOAD, "data": [["109年03月04日", "0050", "元大台灣50", "88.00", "87.00", "1.00", "息", "", "", "", ""]]}
            return F.TWSE_NO_DATA
        if "twse.com.tw/rwd" in url:
            return F.TWSE_NO_DATA
        if "revivt" in url:
            return F.REVIVT_PAYLOAD
        if "tpex.org.tw/www" in url:
            return {"stat": "ok", "tables": [{"title": "", "fields": [], "data": []}]}
        if "t187ap03_L" in url:
            return [{"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24", "普通股每股面額": "新台幣 10.0000元",
                     "實收資本額": "259323700670", "特別股": "0", "已發行普通股數或TDR原股發行股數": "25932370067"}]
        if "mopsfin_t187ap03_O" in url:
            return [{"SecuritiesCompanyCode": "6488", "CompanyAbbreviation": "環球晶", "SecuritiesIndustryCode": "24",
                     "ParValueOfCommonStock": "10", "Paidin.Capital.NTDollars": "4780000000", "PreferredStock.shares": "0",
                     "IssueShares": "478000000"}]
        raise AssertionError(url)


def pkl_writer(df, path):
    df.to_pickle(path)


def short_end():
    return bt.DATA_START + timedelta(weeks=5) - timedelta(days=3)   # 2020-04-03（週五）


# ---------------------------------------------------------------- 日期與解析

def test_default_end_is_latest_friday_before_today():
    assert bt.default_end(date(2026, 11, 26)) == date(2026, 11, 20)   # 週四
    assert bt.default_end(date(2026, 11, 27)) == date(2026, 11, 20)   # 週五當天尚不算
    assert bt.default_end(date(2026, 11, 28)) == date(2026, 11, 27)   # 週六
    assert bt.resolve_end("", TODAY) == date(2026, 11, 20)
    assert bt.resolve_end("2025-12-26", TODAY) == date(2025, 12, 26)
    for bad in ("2025-12-25", "2026-11-27", "2020-06-05"):
        with pytest.raises(ValueError):
            bt.resolve_end(bad, TODAY)


def test_parse_tr_index():
    p = F.mi_index_payload()
    assert bt.parse_tr_index(p) is None
    p["tables"].append(TR_TABLE)
    assert bt.parse_tr_index(p) == 20832.38
    wrong = {"stat": "OK", "tables": [{**TR_TABLE, "title": "報酬指數(跨市場)"}]}
    assert bt.parse_tr_index(wrong) is None
    assert bt.parse_tr_index(None) is None


def test_event_grid_is_stable_when_end_extends():
    a, b = bt.event_grid(date(2026, 11, 20)), bt.event_grid(date(2026, 11, 27))
    assert len(a) == len(b) == 196 and a[0] == ("twse", "dividend", date(2020, 3, 2), date(2020, 5, 30))
    changed = [x for x, y in zip(a, b) if x != y]
    assert len(changed) == 7 and all(x[3] == date(2026, 11, 20) for x in changed)   # 只有各類最後一段改變


# ---------------------------------------------------------------- 快取

def test_store_day_roundtrip_and_refusals(tmp_path):
    store = bt.BacktestStore(tmp_path)
    d = date(2020, 3, 2)
    day = bt.fetch_bt_day(FakeClient(), d)
    assert day.result.status == "trading" and day.tr_close == 20832.38
    assert store.put_day(day, TODAY)
    got = store.get_day(d)
    assert got.tr_close == 20832.38 and got.result.twse.index_close == 23100.5
    assert {q.code for q in got.result.twse.quotes} == {"2330", "0050", "00679B", "9105"}   # 第一階段預篩
    assert not store.put_day(bt.fetch_bt_day(FakeClient(), TODAY), TODAY)                 # 今日不存
    assert not store.put_day(bt.fetch_bt_day(FakeClient(fail_mi=True), d + timedelta(1)), TODAY)  # 失敗日不存
    store.day_path(d).write_bytes(b"broken")
    assert store.get_day(d) is None                                                         # 損毀視同沒有


# ---------------------------------------------------------------- 抓取流程

def test_fetch_all_then_resume_without_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "event_grid", lambda end: [g for g in ORIG_GRID(end) if g[2] == bt.DATA_START][:7])
    store = bt.BacktestStore(tmp_path)
    c = FakeClient()
    s = bt.fetch_all(c, store, short_end(), TODAY, log=lambda m: None)
    assert s.stopped is None and s.weekdays_total == 25 and s.days_fetched == 25 and not s.day_errors
    assert s.events_fetched == 7 and not s.event_errors
    assert store.get_snapshot("basics")["tpex_basics"][0]["SecuritiesCompanyCode"] == "6488"
    assert store.get_snapshot("delisting")["data"][0]["stock_id"] == "1234"

    c2 = FakeClient()
    s2 = bt.fetch_all(c2, store, short_end(), TODAY, log=lambda m: None)
    assert s2.days_fetched == 0 and s2.events_fetched == 0 and s2.days_cached_before == 25
    assert all("finmindtrade" in x.url or "openapi" in x.url for x in c2.logs)   # 只重抓快照（4 次）
    assert len(c2.logs) == 4


def test_budget_stops_and_keeps_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "event_grid", lambda end: ORIG_GRID(end)[:2])
    store = bt.BacktestStore(tmp_path)
    ticks = iter(range(10_000))
    s = bt.fetch_all(FakeClient(), store, short_end(), TODAY, deadline=5, clock=lambda: next(ticks), log=lambda m: None)
    assert s.stopped == "budget" and s.events_fetched == 2 and 0 < s.days_fetched < 25
    s2 = bt.fetch_all(FakeClient(), store, short_end(), TODAY, log=lambda m: None)
    assert s2.stopped is None and s2.days_cached_before + s2.days_fetched == 25


def test_consecutive_errors_stop_early(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "event_grid", lambda end: [])
    s = bt.fetch_all(FakeClient(fail_mi=True), bt.BacktestStore(tmp_path), short_end(), TODAY, log=lambda m: None)
    assert s.stopped == "consecutive_errors" and len(s.day_errors) == bt.MAX_CONSECUTIVE_ERRORS and s.days_fetched == 0


def test_finmind_never_sends_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "should-not-be-used")
    monkeypatch.setattr(bt, "event_grid", lambda end: [])
    c = FakeClient()
    bt.fetch_snapshots(c, bt.BacktestStore(tmp_path), bt.FetchSummary(end=short_end()), "t")
    assert len(c.headers_seen) == 2
    assert all(not h and "token" not in p for h, p in c.headers_seen)


# ---------------------------------------------------------------- 彙整與報告

def _full_store(tmp_path, monkeypatch, **client_kw):
    monkeypatch.setattr(bt, "event_grid", lambda end: [g for g in ORIG_GRID(end) if g[2] == bt.DATA_START])
    store = bt.BacktestStore(tmp_path)
    s = bt.fetch_all(FakeClient(**client_kw), store, short_end(), TODAY, log=lambda m: None)
    return store, s


def test_build_manifest_and_partitions(tmp_path, monkeypatch):
    store, s = _full_store(tmp_path, monkeypatch)
    m = bt.build_parquet(store, short_end(), s, writer=pkl_writer, suffix=".pkl")
    assert m["complete"] and m["trading_days"] == 23 and m["non_trading_days"] == 2
    assert m["trading_days_by_year"] == {"2020": 23} and not m["unit_check_flags"] and not m["taiex_tr_missing_days"]
    assert [f["file"] for f in m["files"]] == ["prices_2020.pkl", "index_daily.pkl", "calendar.pkl", "events.pkl"]
    prices = pd.read_pickle(store.parquet_dir / "prices_2020.pkl")
    assert list(prices.columns) == list(bt.PRICE_COLUMNS) and len(prices) == m["price_rows"] == 23 * 7
    idx = pd.read_pickle(store.parquet_dir / "index_daily.pkl")
    assert idx["taiex_tr_close"].eq(20832.38).all() and len(idx) == 23
    assert m["events"]["bench_0050"] == ["2020-03-04 dividend 前收 88.0 參考 87.0"]
    assert m["events"]["count_by_market_kind"] == {"tpex/capital_reduction": 1, "twse/dividend": 1}
    assert json.loads((store.parquet_dir / "manifest.json").read_text(encoding="utf-8"))["data_end"] == "2020-04-03"
    rep = bt.render_report(m, s)
    assert rep.startswith("# M7b 回測資料抓取報告") and "資料完整：**是**" in rep and "| 不符 |" not in rep


def test_build_flags_missing_tr_and_unit(tmp_path, monkeypatch):
    store, s = _full_store(tmp_path, monkeypatch, tr=False, tpex_shares_in_lots=True)
    m = bt.build_parquet(store, short_end(), s, writer=pkl_writer, suffix=".pkl")
    assert not m["complete"] and len(m["taiex_tr_missing_days"]) == 23
    assert len(m["unit_check_flags"]) == 23 and all(" tpex " in x for x in m["unit_check_flags"])
    assert "| 不符 |" in bt.render_report(m, s)


def test_run_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "event_grid", lambda end: [])
    monkeypatch.setattr(bt, "DATA_START", date(2026, 10, 5))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    kw = dict(client=FakeClient(), today=TODAY, writer=pkl_writer, suffix=".pkl", log=lambda m: None)
    monkeypatch.setattr(bt, "resolve_end", lambda raw, today: date(2026, 10, 16))
    assert bt.run(str(tmp_path / "c"), str(tmp_path / "o"), **kw) == bt.EXIT_OK
    assert (tmp_path / "o" / "report.md").exists() and (tmp_path / "o" / "manifest.json").exists()
    ticks = iter(range(10_000))
    monkeypatch.setattr(bt, "resolve_end", lambda raw, today: date(2026, 11, 20))
    code = bt.run(str(tmp_path / "c"), str(tmp_path / "o"), budget_min=0.05, clock=lambda: next(ticks), **kw)
    assert code == bt.EXIT_BUDGET
    kw["client"] = FakeClient(fail_mi=True)
    assert bt.run(str(tmp_path / "c"), str(tmp_path / "o"), **kw) == bt.EXIT_INCOMPLETE


def test_parquet_roundtrip_with_pyarrow(tmp_path, monkeypatch):
    pytest.importorskip("pyarrow")
    store, s = _full_store(tmp_path, monkeypatch)
    m = bt.build_parquet(store, short_end(), s)
    assert m["complete"] and all(f["file"].endswith(".parquet") for f in m["files"])
    prices = pd.read_parquet(store.parquet_dir / "prices_2020.parquet")
    assert str(prices["date"].dtype).startswith("datetime64") and prices["managed"].dtype == bool
    assert prices.loc[prices["code"] == "0050", "close"].eq(180.5).all()
    ev = pd.read_parquet(store.parquet_dir / "events.parquet")
    assert set(ev.columns) == set(bt.EVENT_COLUMNS) and len(ev) == 2

