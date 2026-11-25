import copy
from datetime import date

import pytest

from src.fetchers import finmind, tpex, twse
from src.fetchers.parsing import (
    ApiStatusError,
    SchemaMismatch,
    code_prefilter,
    date_chunks,
    parse_documented_date,
    parse_number,
)
from tests import fixtures as F

D = date(2026, 11, 20)


class StubClient:
    """依 URL 片段回傳預設回應，記錄請求參數。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), headers))
        for key, value in self.routes.items():
            if key in url:
                return value(params) if callable(value) else copy.deepcopy(value)
        raise AssertionError(f"未預期的 URL：{url}")


# ---------- 共用解析

@pytest.mark.parametrize("raw, expected", [
    ("2,310.00", 2310.0), ("1,045.00", 1045.0), ("0", 0.0), (12, 12.0),
    ("--", None), ("----", None), ("", None), (None, None), (" 3.5 ", 3.5),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_parse_number_rejects_undocumented_token():
    with pytest.raises(ValueError):
        parse_number("X")


def test_parse_documented_date():
    assert parse_documented_date("115/11/20") == D
    assert parse_documented_date("1151120") == D
    assert parse_documented_date("115年11月20日") == D
    assert parse_documented_date("2026/11/20") == D


@pytest.mark.parametrize("code, keep", [
    ("2330", True), ("9105", True), ("0050", True), ("00679B", True), ("006208", True), ("00400A", True),
    ("2881A", False), ("020000", False), ("703012", False), ("01001T", False), ("0", False),
])
def test_code_prefilter(code, keep):
    assert code_prefilter(code) is keep


def test_date_chunks_quarterly():
    chunks = date_chunks(date(2026, 1, 1), date(2026, 12, 31), 90)
    assert chunks[0] == (date(2026, 1, 1), date(2026, 3, 31))
    assert chunks[-1][1] == date(2026, 12, 31)
    assert all((e - s).days < 90 for s, e in chunks)
    assert all(chunks[i][1].toordinal() + 1 == chunks[i + 1][0].toordinal() for i in range(len(chunks) - 1))


# ---------- TWSE MI_INDEX

def test_mi_index_parses_index_and_quotes():
    m = twse.parse_mi_index(F.mi_index_payload(), D)
    assert m.is_trading and m.index_close == 23100.50
    codes = [q.code for q in m.quotes]
    assert codes == ["2330", "0050", "00679B", "9105"]      # 特別股、ETN 預篩剔除
    tsmc = m.quotes[0]
    assert (tsmc.open, tsmc.high, tsmc.low, tsmc.close) == (1020.0, 1055.0, 1005.0, 1045.0)
    assert tsmc.volume_shares == 30_123_456 and tsmc.value_ntd == 31_000_000_000
    dr = m.quotes[-1]
    assert dr.close is None and dr.volume_shares == 10_000   # "--" 無成交價


def test_mi_index_without_prefilter_keeps_all():
    assert len(twse.parse_mi_index(F.mi_index_payload(), D, prefilter=False).quotes) == 6


def test_mi_index_non_trading_day():
    m = twse.parse_mi_index(F.TWSE_NO_DATA, D)
    assert m.is_trading is False and m.quotes == []


def test_mi_index_unexpected_stat_raises():
    with pytest.raises(ApiStatusError):
        twse.parse_mi_index({"stat": "查詢日期大於今日"}, D)


def test_mi_index_missing_field_reports_actual_fields():
    p = F.mi_index_payload()
    p["tables"][2]["fields"][2] = "成交量"
    with pytest.raises(SchemaMismatch) as ei:
        twse.parse_mi_index(p, D)
    assert "成交股數" in str(ei.value) and "成交量" in str(ei.value)


def test_mi_index_table_located_by_title_not_index():
    p = F.mi_index_payload()
    p["tables"].reverse()
    assert twse.parse_mi_index(p, D).index_close == 23100.50


def test_mi_index_undocumented_value_logged_not_guessed():
    rows = [F.mi_row("2330", "台積電", "1,000", "1,000", "X1.00", "1.00", "1.00", "1.00")]
    m = twse.parse_mi_index(F.mi_index_payload(rows), D)
    assert m.quotes[0].open is None
    assert [(a.code, a.field, a.raw) for a in m.report.anomalies] == [("2330", "開盤價", "X1.00")]


def test_fetch_mi_index_query_params():
    c = StubClient({"MI_INDEX": F.mi_index_payload()})
    twse.fetch_mi_index(c, D)
    url, params, _ = c.calls[0]
    assert url == "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    assert params == {"date": "20261120", "type": "ALLBUT0999", "response": "json"}


# ---------- TWSE 事件與參考欄

def test_twse_dividend_events():
    ev = twse.parse_events(F.TWT49U_PAYLOAD, "dividend")
    assert [(e.code, e.event_date, e.p_before, e.p_ref) for e in ev] == [
        ("00939", date(2026, 1, 2), 14.0, 13.95), ("2330", date(2026, 6, 12), 1000.0, 995.0)]
    assert ev[0].kind == "dividend" and ev[0].market == "twse"


def test_twse_capital_reduction_uses_resume_reference_price():
    ev = twse.parse_events(F.TWTAUU_PAYLOAD, "capital_reduction")[0]
    assert (ev.event_date, ev.p_before, ev.p_ref) == (date(2026, 1, 12), 20.0, 25.0)
    assert ev.extra["除權參考價"] == "25.00"


def test_twse_events_no_data_is_empty():
    assert twse.parse_events(F.TWSE_NO_DATA, "par_value_change") == []


def test_twse_fetch_events_chunks_by_quarter():
    c = StubClient({"TWTCAU": F.TWSE_NO_DATA})
    twse.fetch_events(c, "etf_split", date(2025, 11, 26), date(2026, 11, 25))
    assert len(c.calls) == 5
    assert c.calls[0][1]["startDate"] == "20251126" and c.calls[-1][1]["endDate"] == "20261125"


def test_t86_net_shares():
    c = StubClient({"T86": F.T86_PAYLOAD})
    assert twse.fetch_t86(c, D) == {"2330": 1_150_000.0}


def test_twse_notice_filters_empty_shell():
    c = StubClient({"notice": [{"Date": "", "Code": "", "Name": "", "TradingInfoForAttention": "", "ClosingPrice": "", "PE": ""}]})
    assert twse.fetch_notice(c) == []


def test_openapi_missing_key_raises():
    c = StubClient({"t187ap03_L": [{"公司代號": "2330"}]})
    with pytest.raises(SchemaMismatch, match="實收資本額"):
        twse.fetch_company_basics(c)


# ---------- TPEx

def test_daily_quotes_parse_and_prefilter():
    m = tpex.parse_daily_quotes(F.daily_quotes_payload(), D)
    assert m.is_trading and [q.code for q in m.quotes] == ["6488", "00937B", "8069"]
    gw = m.quotes[0]
    assert (gw.close, gw.volume_shares, gw.value_ntd) == (400.0, 2_345_000, 938_000_000)
    assert m.quotes[-1].close is None   # "----"


def test_daily_quotes_non_trading_day():
    assert tpex.parse_daily_quotes(F.TPEX_NO_DATA, D).is_trading is False


def test_daily_quotes_query_uses_western_slash_date():
    c = StubClient({"dailyQuotes": F.daily_quotes_payload()})
    tpex.fetch_daily_quotes(c, D)
    assert c.calls[0][1] == {"date": "2026/11/20", "response": "json"}


def test_trading_stock_converts_lots_and_kilo_ntd():
    c = StubClient({"tradingStock": F.TRADING_STOCK_PAYLOAD})
    row = tpex.fetch_trading_stock(c, D, "6488")[0]
    assert row["date"] == D and row["volume_shares"] == 2_345_000 and row["value_ntd"] == 938_000_000


def test_tpex_capital_reduction_event():
    ev = tpex.parse_events(F.REVIVT_PAYLOAD, "capital_reduction")[0]
    assert (ev.market, ev.code, ev.event_date, ev.p_before, ev.p_ref) == ("tpex", "5678", date(2026, 1, 12), 30.0, 40.0)


def test_tpex_events_empty():
    empty = {"stat": "ok", "tables": [{"title": "", "fields": [], "data": []}]}
    assert tpex.parse_events(empty, "etf_split") == []


def test_tpex_unexpected_stat_raises():
    with pytest.raises(ApiStatusError):
        tpex.parse_daily_quotes({"stat": "error"}, D)


# ---------- FinMind

def test_finmind_latest_row_per_stock_and_token_header():
    data = [
        {"industry_category": "電子工業", "stock_id": "6488", "stock_name": "環球晶", "type": "emerging", "date": "2010-01-01"},
        {"industry_category": "半導體業", "stock_id": "6488", "stock_name": "環球晶", "type": "tpex", "date": "2024-06-01"},
        {"industry_category": "ETF", "stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "date": "2024-06-01"},
    ]
    c = StubClient({"finmindtrade": {"msg": "success", "status": 200, "data": data}})
    fm = finmind.FinMindClient(c, token="SECRET-TOKEN")
    latest = finmind.latest_info_by_stock(fm.fetch_stock_info())
    assert latest["6488"]["type"] == "tpex" and latest["6488"]["industry_category"] == "半導體業"
    url, params, headers = c.calls[0]
    assert params == {"dataset": "TaiwanStockInfo"} and "SECRET-TOKEN" not in url + str(params)
    assert headers == {"Authorization": "Bearer SECRET-TOKEN"}
    assert "SECRET" not in repr(fm)


def test_finmind_without_token_sends_no_auth():
    c = StubClient({"finmindtrade": {"msg": "success", "status": 200, "data": []}})
    finmind.FinMindClient(c).fetch_dataset("TaiwanStockInfo")
    assert c.calls[0][2] is None


def test_finmind_unexpected_envelope_degrades():
    c = StubClient({"finmindtrade": {"msg": "x", "status": 200}})
    info, err = finmind.try_fetch_stock_info(finmind.FinMindClient(c))
    assert info is None and "規則過濾" in err


# ---------- 2026-11-25 run1 實測後新增

def test_triple_dash_from_tpex_is_no_price():
    rows = [F.dq_row("8069", "元太", " ---", "---", "---", "---", "0", "0")]
    m = tpex.parse_daily_quotes(F.daily_quotes_payload(rows), D)
    q = m.quotes[0]
    assert (q.open, q.high, q.low, q.close) == (None, None, None, None)
    assert m.report.anomalies == []


def test_mi_index_rejects_other_day_payload():
    with pytest.raises(SchemaMismatch, match="不一致"):
        twse.parse_mi_index(F.mi_index_payload(date="20261119"), D)


def test_mi_index_requires_date_key_on_trading_day():
    p = F.mi_index_payload()
    del p["date"]
    with pytest.raises(SchemaMismatch, match="缺少 date"):
        twse.parse_mi_index(p, D)


def test_daily_quotes_rejects_other_day_payload():
    with pytest.raises(SchemaMismatch, match="不一致"):
        tpex.parse_daily_quotes(F.daily_quotes_payload(date="20261119"), D)


def test_duplicate_field_names_not_overwritten_and_not_required():
    from src.fetchers.parsing import table_rows
    table = F.INSTI_PAYLOAD["tables"][0]
    rows = table_rows(table, "x", ("代號", "三大法人買賣超股數合計"))
    assert "買進股數" not in rows[0]                     # 重複欄不放入 dict
    with pytest.raises(SchemaMismatch, match="重複"):
        table_rows(table, "x", ("買進股數",))


def test_tpex_insti_net_uses_unique_total_field():
    assert tpex.parse_insti_net(F.INSTI_PAYLOAD, D) == {
        "00411A": 9_406_835.0, "006201": -705_724.0, "00679B": 45_735_291.0}


def test_tpex_insti_total_equals_group_sums_in_run1_samples():
    # 僅供佐證欄位關係（位置 3、4、7 組買賣超相加 = 合計）；正式程式不依位置取值
    for row in F.INSTI_PAYLOAD["tables"][0]["data"]:
        diff = lambda g: parse_number(row[2 + 3 * (g - 1) + 2])
        assert diff(3) + diff(4) + diff(7) == parse_number(row[-1])


def test_otc_index_month():
    c = StubClient({"indexInfo/inx": F.INX_PAYLOAD})
    assert tpex.fetch_otc_index_month(c, D) == [
        {"date": date(2026, 11, 2), "open": 368.22, "high": 373.28, "low": 362.18, "close": 369.24}]


@pytest.mark.parametrize("raw, expected", [
    ("115/11/02", date(2026, 11, 2)),      # MI_5MINS_HIST 日期、tradingStock 日 期、exDailyQ 除權息日期
    ("1160101", date(2027, 1, 1)),         # holidaySchedule Date
    ("1141124", date(2025, 11, 24)),       # revivt 恢復買賣日期
    ("114年11月21日", date(2025, 11, 21)),  # TWT49U 資料日期
    ("2026/11/02", date(2026, 11, 2)),     # indexInfo/inx 日期
])
def test_run1_observed_date_formats(raw, expected):
    assert parse_documented_date(raw) == expected


def test_finmind_null_date_rows_provisional_rule():
    data = [
        {"industry_category": "半導體業", "stock_id": "6488", "stock_name": "環球晶", "type": "tpex", "date": "None"},
        {"industry_category": "半導體業", "stock_id": "6488", "stock_name": "環球晶", "type": "tpex", "date": "2024-06-01"},
        {"industry_category": "ETF", "stock_id": "00999A", "stock_name": "某ETF", "type": "twse", "date": None},
    ]
    latest = finmind.latest_info_by_stock(data)
    assert latest["6488"]["_date"] == date(2024, 6, 1) and latest["6488"]["_date_unknown"] is False
    assert latest["00999A"]["_date"] is None and latest["00999A"]["_date_unknown"] is True
    stats = finmind.undated_stats(data)
    assert stats["undated_rows"] == 2 and stats["stock_ids_only_undated"] == ["00999A"]
    assert stats["undated_raw_values"] == {"'None'": 1, "None": 1}
    assert stats["only_undated_by_type"] == {"twse": 1} and stats["invalid_date_rows"] == 0


def test_finmind_string_none_date_does_not_crash():
    data = [{"industry_category": "ETF", "stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "date": "None"}]
    assert finmind.latest_info_by_stock(data)["0050"]["_date_unknown"] is True


def test_finmind_other_invalid_date_still_raises():
    data = [{"industry_category": "ETF", "stock_id": "0050", "stock_name": "x", "type": "twse", "date": "2024/01/01"}]
    with pytest.raises(ValueError):
        finmind.latest_info_by_stock(data)
    assert finmind.undated_stats(data)["invalid_date_rows"] == 1


def test_finmind_non_200_status_raises():
    c = StubClient({"finmindtrade": {"msg": "Your level is free", "status": 400, "data": []}})
    with pytest.raises(SchemaMismatch, match="status=400"):
        finmind.FinMindClient(c).fetch_dataset("TaiwanStockPriceAdj")


# ---------- 2026-11-25 D3：管理股票表

def _with_managed(rows, title="管理股票"):
    p = F.daily_quotes_payload()
    p["tables"].append({"title": title, "fields": list(F.DAILY_QUOTES_FIELDS), "data": rows})
    return p


def test_managed_table_rows_parsed_and_flagged():
    p = _with_managed([F.dq_row("5555", "某管理股", "10.00", "10.00", "10.50", "9.80", "12,000", "120,000"),
                       F.dq_row("712345", "某權證", "1.00", "1.00", "1.00", "1.00", "1,000", "1,000")])
    m = tpex.parse_daily_quotes(p, D)
    managed = [q for q in m.quotes if q.managed]
    assert [(q.code, q.close, q.volume_shares) for q in managed] == [("5555", 10.0, 12_000)]   # 權證仍被預篩
    assert all(not q.managed for q in m.quotes if q.code != "5555")


def test_empty_managed_table_is_ignored_like_run1_to_run3():
    m = tpex.parse_daily_quotes(_with_managed([]), D)
    assert len(m.quotes) == 3 and not any(q.managed for q in m.quotes)


def test_managed_table_missing_is_fine():
    assert len(tpex.parse_daily_quotes(F.daily_quotes_payload(), D).quotes) == 3


def test_code_in_both_tables_raises():
    p = _with_managed([F.dq_row("6488", "環球晶", "400.00", "395.00", "405.00", "390.00", "1,000", "1,000")])
    with pytest.raises(SchemaMismatch, match="同時出現"):
        tpex.parse_daily_quotes(p, D)


def test_managed_table_schema_change_raises():
    p = _with_managed([["5555", "某管理股"]])
    p["tables"][-1]["fields"] = ["代號", "名稱"]
    with pytest.raises(SchemaMismatch, match="管理股票"):
        tpex.parse_daily_quotes(p, D)


def test_non_trading_day_with_empty_managed_table():
    nd = copy.deepcopy(F.TPEX_NO_DATA)
    nd["tables"].append({"title": "管理股票", "fields": list(F.DAILY_QUOTES_FIELDS), "data": []})
    assert tpex.parse_daily_quotes(nd, D).is_trading is False
