"""TPEx（證券櫃檯買賣中心）抓取器 — 端點與欄位僅限 spec_v1 第 3.3 節。

2026-11-25 verify-api run1 實測後補齊：
- 三大法人 ``/insti/dailyTrade``：24 欄中「買進股數／賣出股數／買賣超股數」各重複 7 次，不依位置猜測；
  只取名稱唯一的「三大法人買賣超股數合計」（單位：股）。
- 櫃買指數 ``/indexInfo/inx``：fields 為 日期、開市、最高、最低、收市、漲/跌；日期為西元 ``YYYY/MM/DD``。
- dailyQuotes 另有第二張表「管理股票」（run1 兩個樣本日皆 0 列）；是否併入待 Hector 裁定，目前只讀第一張表。
- 上櫃變更股票面額專用端點：規格標「待確認」，依備援方式（跳空異常警示）處理，不另尋端點。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.fetchers.http import HttpClient
from src.fetchers.parsing import (
    ApiStatusError,
    CorporateEvent,
    DailyMarket,
    DailyQuote,
    ParseReport,
    SchemaMismatch,
    check_response_date,
    code_prefilter,
    date_chunks,
    extract_tables,
    find_table,
    parse_documented_date,
    parse_number,
    parse_number_logged,
    require_keys,
    table_rows,
)
from src.timezone import format_tpex_query_date

OPENAPI = "https://www.tpex.org.tw/openapi/v1"
WWW = "https://www.tpex.org.tw/www/zh-tw"

DAILY_QUOTE_FIELDS = ("代號", "名稱", "收盤", "開盤", "最高", "最低", "成交股數", "成交金額(元)")


# ---------------------------------------------------------------- 共用

def _www_get(client: HttpClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = client.get_json(f"{WWW}{path}", params={**params, "response": "json"})
    if not isinstance(payload, dict):
        raise SchemaMismatch(f"TPEx {path}", "回應不是 JSON 物件", type(payload).__name__)
    return payload


def _check_ok(payload: dict[str, Any], endpoint: str) -> None:
    # 規格實測值為小寫 "ok"；比對時不分大小寫
    if str(payload.get("stat", "")).lower() != "ok":
        raise ApiStatusError(f"[{endpoint}] stat={payload.get('stat')!r}")


def _first_table_is_empty(payload: dict[str, Any]) -> bool:
    tables = extract_tables(payload)
    return not tables or not (tables[0].get("data") or [])


def _openapi(client: HttpClient, path: str, required: tuple[str, ...]) -> list[dict[str, Any]]:
    data = client.get_json(f"{OPENAPI}{path}")
    if not isinstance(data, list):
        raise SchemaMismatch(f"TPEx {path}", "回應不是 JSON 陣列", type(data).__name__)
    return require_keys(data, f"TPEx {path}", required)


# ---------------------------------------------------------------- 主歷史來源：dailyQuotes

def parse_daily_quotes(payload: dict[str, Any], trade_date: date, prefilter: bool = True) -> DailyMarket:
    endpoint = "TPEx dailyQuotes"
    _check_ok(payload, endpoint)
    check_response_date(payload, endpoint, trade_date)  # run1：交易日與非交易日皆回傳 date
    if _first_table_is_empty(payload):  # 規格：非交易日 tables[0].data == []
        return DailyMarket("tpex", trade_date, is_trading=False)
    report = ParseReport()
    quotes = []
    for r in table_rows(find_table(payload, endpoint), endpoint, DAILY_QUOTE_FIELDS):
        code = str(r["代號"]).strip()
        if prefilter and not code_prefilter(code):  # 含權證約 1.2～1.4 萬筆，載入後立即過濾
            continue
        quotes.append(DailyQuote(
            market="tpex", code=code, name=str(r["名稱"]).strip(),
            open=parse_number_logged(r["開盤"], report, code, "開盤"),
            high=parse_number_logged(r["最高"], report, code, "最高"),
            low=parse_number_logged(r["最低"], report, code, "最低"),
            close=parse_number_logged(r["收盤"], report, code, "收盤"),
            volume_shares=parse_number_logged(r["成交股數"], report, code, "成交股數"),
            value_ntd=parse_number_logged(r["成交金額(元)"], report, code, "成交金額(元)"),
        ))
    return DailyMarket("tpex", trade_date, True, quotes, None, report)


def fetch_daily_quotes(client: HttpClient, trade_date: date, prefilter: bool = True) -> DailyMarket:
    payload = _www_get(client, "/afterTrading/dailyQuotes", {"date": format_tpex_query_date(trade_date)})
    return parse_daily_quotes(payload, trade_date, prefilter)


# ---------------------------------------------------------------- 補缺備援

def fetch_trading_stock(client: HttpClient, any_day_in_month: date, code: str) -> list[dict[str, Any]]:
    """上櫃個股單月日 K。注意單位：成交張數、成交仟元（spec 3.3B），在此換算為股與元。"""
    endpoint = "TPEx tradingStock"
    payload = _www_get(client, "/afterTrading/tradingStock",
                       {"date": format_tpex_query_date(any_day_in_month), "code": code})
    _check_ok(payload, endpoint)
    if _first_table_is_empty(payload):
        return []
    rows = table_rows(find_table(payload, endpoint), endpoint,
                      ("日 期", "成交張數", "成交仟元", "開盤", "最高", "最低", "收盤"))
    out = []
    for r in rows:
        lots, kilo_ntd = parse_number(r["成交張數"]), parse_number(r["成交仟元"])
        out.append({
            "date": parse_documented_date(r["日 期"]),
            "open": parse_number(r["開盤"]), "high": parse_number(r["最高"]),
            "low": parse_number(r["最低"]), "close": parse_number(r["收盤"]),
            "volume_shares": None if lots is None else lots * 1000,
            "value_ntd": None if kilo_ntd is None else kilo_ntd * 1000,
        })
    return out


# ---------------------------------------------------------------- 公司事件（按季分段）

_EVENT_SPECS = {
    "dividend": ("/bulletin/exDailyQ", "除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價", ("權值+息值", "權/息")),
    "capital_reduction": ("/bulletin/revivt", "恢復買賣日期", "股票代號", "名稱", "最後交易日之收盤價格", "減資恢復買賣開始日參考價格", ("除權參考價", "減資原因")),
    "etf_split": ("/bulletin/etfRvsRslt", "恢復買賣日期", "證券代號", "證券名稱", "最後交易日之收盤價格", "恢復買賣開始參考價", ()),
}


def parse_events(payload: dict[str, Any], kind: str) -> list[CorporateEvent]:
    path, f_date, f_code, f_name, f_before, f_ref, extras = _EVENT_SPECS[kind]
    endpoint = f"TPEx {path}"
    _check_ok(payload, endpoint)
    if _first_table_is_empty(payload):
        return []
    required = (f_date, f_code, f_name, f_before, f_ref) + extras
    events = []
    for r in table_rows(find_table(payload, endpoint), endpoint, required):
        events.append(CorporateEvent(
            market="tpex", code=str(r[f_code]).strip(), name=str(r[f_name]).strip(),
            event_date=parse_documented_date(r[f_date]), kind=kind,
            p_before=parse_number(r[f_before]), p_ref=parse_number(r[f_ref]),
            source=path, extra={k: r[k] for k in extras},
        ))
    return events


def fetch_events(client: HttpClient, kind: str, start: date, end: date) -> list[CorporateEvent]:
    path = _EVENT_SPECS[kind][0]
    out: list[CorporateEvent] = []
    for s, e in date_chunks(start, end, 90):
        payload = _www_get(client, path, {"startDate": format_tpex_query_date(s), "endDate": format_tpex_query_date(e)})
        out.extend(parse_events(payload, kind))
    return out


def fetch_all_events(client: HttpClient, start: date, end: date) -> list[CorporateEvent]:
    events: list[CorporateEvent] = []
    for kind in _EVENT_SPECS:
        events.extend(fetch_events(client, kind, start, end))
    return events


# ---------------------------------------------------------------- 櫃買指數（第二階段用）

def fetch_otc_index_month(client: HttpClient, any_day_in_month: date) -> list[dict[str, Any]]:
    endpoint = "TPEx indexInfo/inx"
    payload = _www_get(client, "/indexInfo/inx", {"date": format_tpex_query_date(any_day_in_month)})
    _check_ok(payload, endpoint)
    if _first_table_is_empty(payload):
        return []
    rows = table_rows(find_table(payload, endpoint), endpoint, ("日期", "開市", "最高", "最低", "收市"))
    return [{
        "date": parse_documented_date(r["日期"]),
        "open": parse_number(r["開市"]), "high": parse_number(r["最高"]),
        "low": parse_number(r["最低"]), "close": parse_number(r["收市"]),
    } for r in rows]


# ---------------------------------------------------------------- 三大法人（參考欄）

INSTI_TOTAL_FIELD = "三大法人買賣超股數合計"


def fetch_insti_daily_raw(client: HttpClient, trade_date: date) -> dict[str, Any]:
    return _www_get(client, "/insti/dailyTrade",
                    {"type": "Daily", "sect": "EW", "date": format_tpex_query_date(trade_date)})


def parse_insti_net(payload: dict[str, Any], trade_date: date) -> dict[str, float]:
    """回傳 {代號: 三大法人買賣超股數合計}（單位：股）；非交易日回空 dict。"""
    endpoint = "TPEx insti/dailyTrade"
    _check_ok(payload, endpoint)
    if _first_table_is_empty(payload):
        return {}
    check_response_date(payload, endpoint, trade_date)
    out = {}
    for r in table_rows(find_table(payload, endpoint, "三大法人買賣明細"), endpoint, ("代號", "名稱", INSTI_TOTAL_FIELD)):
        value = parse_number(r[INSTI_TOTAL_FIELD])
        if value is not None:
            out[str(r["代號"]).strip()] = value
    return out


def fetch_insti_net(client: HttpClient, trade_date: date) -> dict[str, float]:
    return parse_insti_net(fetch_insti_daily_raw(client, trade_date), trade_date)


# ---------------------------------------------------------------- OpenAPI v1

def fetch_company_basics(client: HttpClient) -> list[dict[str, Any]]:
    """上櫃公司基本資料與股本（Paidin.Capital.NTDollars 單位：元；IssueShares 單位：股）。"""
    return _openapi(client, "/mopsfin_t187ap03_O", (
        "SecuritiesCompanyCode", "CompanyAbbreviation", "SecuritiesIndustryCode",
        "ParValueOfCommonStock", "Paidin.Capital.NTDollars", "PreferredStock.shares", "IssueShares"))


def fetch_disposition(client: HttpClient) -> list[dict[str, Any]]:
    return _openapi(client, "/tpex_disposal_information", ("Date", "SecuritiesCompanyCode", "CompanyName", "DispositionPeriod"))


def fetch_trading_warning(client: HttpClient) -> list[dict[str, Any]]:
    return _openapi(client, "/tpex_trading_warning_information", ("Date", "SecuritiesCompanyCode", "CompanyName"))


def fetch_monthly_revenue(client: HttpClient) -> list[dict[str, Any]]:
    return _openapi(client, "/mopsfin_t187ap05_O", ("資料年月", "公司代號", "營業收入-當月營收", "營業收入-去年同月增減(%)"))


def fetch_quarterly_eps(client: HttpClient) -> list[dict[str, Any]]:
    # 鍵名中英混用（spec 3.3A 註記）
    return _openapi(client, "/mopsfin_t187ap14_O", ("Year", "季別", "SecuritiesCompanyCode", "基本每股盈餘"))
