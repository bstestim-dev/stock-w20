"""TWSE（臺灣證券交易所）抓取器 — 端點與欄位僅限 spec_v1 第 3.2 節。"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.fetchers.http import HttpClient
from src.fetchers.parsing import (
    TWSE_NO_DATA_MARKER,
    ApiStatusError,
    CorporateEvent,
    DailyMarket,
    DailyQuote,
    ParseReport,
    SchemaMismatch,
    check_response_date,
    code_prefilter,
    date_chunks,
    find_table,
    parse_documented_date,
    parse_number,
    parse_number_logged,
    require_keys,
    table_rows,
)
from src.timezone import format_twse_query_date

OPENAPI = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"

INDEX_TABLE_KEYWORD = "價格指數(臺灣證券交易所)"
QUOTES_TABLE_KEYWORD = "每日收盤行情"
TAIEX_NAME = "發行量加權股價指數"

MI_INDEX_QUOTE_FIELDS = ("證券代號", "證券名稱", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價")
MI_INDEX_INDEX_FIELDS = ("指數", "收盤指數")


# ---------------------------------------------------------------- 共用

def _rwd_get(client: HttpClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = client.get_json(f"{RWD}{path}", params={**params, "response": "json"})
    if not isinstance(payload, dict):
        raise SchemaMismatch(f"TWSE {path}", "回應不是 JSON 物件", type(payload).__name__)
    return payload


def _is_no_data(payload: dict[str, Any]) -> bool:
    return TWSE_NO_DATA_MARKER in str(payload.get("stat", ""))


def _check_ok(payload: dict[str, Any], endpoint: str) -> None:
    stat = payload.get("stat")
    if stat != "OK":
        raise ApiStatusError(f"[{endpoint}] stat={stat!r}")


def _openapi(client: HttpClient, path: str, required: tuple[str, ...]) -> list[dict[str, Any]]:
    data = client.get_json(f"{OPENAPI}{path}")
    if not isinstance(data, list):
        raise SchemaMismatch(f"TWSE {path}", "回應不是 JSON 陣列", type(data).__name__)
    return require_keys(data, f"TWSE {path}", required)


# ---------------------------------------------------------------- 主歷史來源：MI_INDEX

def parse_mi_index(payload: dict[str, Any], trade_date: date, prefilter: bool = True) -> DailyMarket:
    endpoint = "TWSE MI_INDEX"
    if payload.get("stat") != "OK":
        if _is_no_data(payload):
            return DailyMarket("twse", trade_date, is_trading=False)
        raise ApiStatusError(f"[{endpoint}] {trade_date} stat={payload.get('stat')!r}")

    check_response_date(payload, endpoint, trade_date)
    report = ParseReport()
    idx_rows = table_rows(find_table(payload, endpoint, INDEX_TABLE_KEYWORD), endpoint, MI_INDEX_INDEX_FIELDS)
    taiex = [r for r in idx_rows if str(r["指數"]).strip() == TAIEX_NAME]
    if len(taiex) != 1:
        raise SchemaMismatch(endpoint, f"找不到唯一的「{TAIEX_NAME}」列", [r["指數"] for r in idx_rows][:30])
    index_close = parse_number_logged(taiex[0]["收盤指數"], report, TAIEX_NAME, "收盤指數")

    quotes = []
    for r in table_rows(find_table(payload, endpoint, QUOTES_TABLE_KEYWORD), endpoint, MI_INDEX_QUOTE_FIELDS):
        code = str(r["證券代號"]).strip()
        if prefilter and not code_prefilter(code):
            continue
        quotes.append(DailyQuote(
            market="twse", code=code, name=str(r["證券名稱"]).strip(),
            open=parse_number_logged(r["開盤價"], report, code, "開盤價"),
            high=parse_number_logged(r["最高價"], report, code, "最高價"),
            low=parse_number_logged(r["最低價"], report, code, "最低價"),
            close=parse_number_logged(r["收盤價"], report, code, "收盤價"),
            volume_shares=parse_number_logged(r["成交股數"], report, code, "成交股數"),
            value_ntd=parse_number_logged(r["成交金額"], report, code, "成交金額"),
        ))
    if not quotes:
        raise SchemaMismatch(endpoint, f"{trade_date} stat=OK 但收盤行情為空")
    return DailyMarket("twse", trade_date, True, quotes, index_close, report)


def fetch_mi_index(client: HttpClient, trade_date: date, prefilter: bool = True) -> DailyMarket:
    payload = _rwd_get(client, "/afterTrading/MI_INDEX",
                       {"date": format_twse_query_date(trade_date), "type": "ALLBUT0999"})
    return parse_mi_index(payload, trade_date, prefilter)


# ---------------------------------------------------------------- 補缺備援

def fetch_stock_day(client: HttpClient, any_day_in_month: date, code: str) -> list[dict[str, Any]]:
    """個股單月日 K（STOCK_DAY）；成交股數單位為股。"""
    endpoint = "TWSE STOCK_DAY"
    payload = _rwd_get(client, "/afterTrading/STOCK_DAY",
                       {"date": format_twse_query_date(any_day_in_month), "stockNo": code})
    if _is_no_data(payload):
        return []
    _check_ok(payload, endpoint)
    rows = table_rows(find_table(payload, endpoint), endpoint,
                      ("日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "註記"))
    out = []
    for r in rows:
        out.append({
            "date": parse_documented_date(r["日期"]),
            "open": parse_number(r["開盤價"]), "high": parse_number(r["最高價"]),
            "low": parse_number(r["最低價"]), "close": parse_number(r["收盤價"]),
            "volume_shares": parse_number(r["成交股數"]), "value_ntd": parse_number(r["成交金額"]),
            "note": str(r["註記"] or "").strip(),
        })
    return out


def fetch_taiex_month(client: HttpClient, any_day_in_month: date) -> list[dict[str, Any]]:
    """加權指數單月 OHLC（MI_5MINS_HIST）。日期格式規格未載明，以已記載格式解析，待 API 驗證確認。"""
    endpoint = "TWSE MI_5MINS_HIST"
    payload = _rwd_get(client, "/TAIEX/MI_5MINS_HIST", {"date": format_twse_query_date(any_day_in_month)})
    if _is_no_data(payload):
        return []
    _check_ok(payload, endpoint)
    rows = table_rows(find_table(payload, endpoint), endpoint, ("日期", "開盤指數", "最高指數", "最低指數", "收盤指數"))
    return [{
        "date": parse_documented_date(r["日期"]),
        "open": parse_number(r["開盤指數"]), "high": parse_number(r["最高指數"]),
        "low": parse_number(r["最低指數"]), "close": parse_number(r["收盤指數"]),
    } for r in rows]


# ---------------------------------------------------------------- 公司事件（按季分段）

_EVENT_SPECS = {
    # kind: (path, date 欄, 代號欄, 名稱欄, P_before 欄, P_ref 欄, 額外保留欄)
    "dividend": ("/exRight/TWT49U", "資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價", ("權值+息值", "權/息")),
    "capital_reduction": ("/reducation/TWTAUU", "恢復買賣日期", "股票代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價", ("除權參考價", "減資原因")),
    "par_value_change": ("/change/TWTB8U", "恢復買賣日期", "股票代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價", ()),
    "etf_split": ("/split/TWTCAU", "恢復買賣日期", "ETF代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價", ("分割(反分割)",)),
}


def parse_events(payload: dict[str, Any], kind: str) -> list[CorporateEvent]:
    path, f_date, f_code, f_name, f_before, f_ref, extras = _EVENT_SPECS[kind]
    endpoint = f"TWSE {path}"
    if _is_no_data(payload):
        return []
    _check_ok(payload, endpoint)
    required = (f_date, f_code, f_name, f_before, f_ref) + extras
    events = []
    for r in table_rows(find_table(payload, endpoint), endpoint, required):
        events.append(CorporateEvent(
            market="twse", code=str(r[f_code]).strip(), name=str(r[f_name]).strip(),
            event_date=parse_documented_date(r[f_date]), kind=kind,
            p_before=parse_number(r[f_before]), p_ref=parse_number(r[f_ref]),
            source=path, extra={k: r[k] for k in extras},
        ))
    return events


def fetch_events(client: HttpClient, kind: str, start: date, end: date) -> list[CorporateEvent]:
    path = _EVENT_SPECS[kind][0]
    out: list[CorporateEvent] = []
    for s, e in date_chunks(start, end, 90):
        payload = _rwd_get(client, path, {"startDate": format_twse_query_date(s), "endDate": format_twse_query_date(e)})
        out.extend(parse_events(payload, kind))
    return out


def fetch_all_events(client: HttpClient, start: date, end: date) -> list[CorporateEvent]:
    events: list[CorporateEvent] = []
    for kind in _EVENT_SPECS:
        events.extend(fetch_events(client, kind, start, end))
    return events


# ---------------------------------------------------------------- 三大法人（參考欄）

T86_FIELDS = ("證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數")


def fetch_t86(client: HttpClient, trade_date: date) -> dict[str, float]:
    """回傳 {證券代號: 三大法人買賣超股數}（單位：股）；非交易日回空 dict。"""
    endpoint = "TWSE T86"
    payload = _rwd_get(client, "/fund/T86", {"date": format_twse_query_date(trade_date), "selectType": "ALL"})
    if _is_no_data(payload):
        return {}
    _check_ok(payload, endpoint)
    out = {}
    for r in table_rows(find_table(payload, endpoint), endpoint, T86_FIELDS):
        value = parse_number(r["三大法人買賣超股數"])
        if value is not None:
            out[str(r["證券代號"]).strip()] = value
    return out


# ---------------------------------------------------------------- OpenAPI v1

def fetch_company_basics(client: HttpClient) -> list[dict[str, Any]]:
    """上市公司基本資料與股本（實收資本額單位：元）。"""
    return _openapi(client, "/opendata/t187ap03_L", (
        "公司代號", "公司簡稱", "產業別", "普通股每股面額", "實收資本額", "特別股", "已發行普通股數或TDR原股發行股數"))


def fetch_disposition(client: HttpClient) -> list[dict[str, Any]]:
    return _openapi(client, "/announcement/punish", ("Date", "Code", "Name", "DispositionPeriod"))


def fetch_notice(client: HttpClient) -> list[dict[str, Any]]:
    """注意股；無資料時官方回 1 筆空殼（Code == ""），在此濾除。"""
    rows = _openapi(client, "/announcement/notice", ("Date", "Code", "Name"))
    return [r for r in rows if str(r.get("Code", "")).strip()]


def fetch_holiday_schedule(client: HttpClient) -> list[dict[str, Any]]:
    return _openapi(client, "/holidaySchedule/holidaySchedule", ("Name", "Date", "Weekday", "Description"))


def fetch_monthly_revenue(client: HttpClient) -> list[dict[str, Any]]:
    return _openapi(client, "/opendata/t187ap05_L", ("資料年月", "公司代號", "營業收入-當月營收", "營業收入-去年同月增減(%)"))


def fetch_quarterly_eps(client: HttpClient) -> list[dict[str, Any]]:
    return _openapi(client, "/opendata/t187ap14_L", ("年度", "季別", "公司代號", "基本每股盈餘(元)"))
