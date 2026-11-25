"""官方 JSON 回應的共用解析工具。

原則（IR-1）：只認規格第 3 節列出的欄位名；必要欄位缺漏即拋 ``SchemaMismatch``，
附上實際欄位清單，供回報 Hector，不自行猜測替代欄位。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from src.timezone import parse_roc_date, parse_western_slash_date

# 「無成交價」佔位字串：規格第 3 節記載 "--"、"----"；
# "---" 為 2026-11-25 verify-api run1 實測 TPEx dailyQuotes 所見（開高低收四欄同時出現，前後空白已先去除）。
NO_PRICE_TOKENS = frozenset({"", "--", "---", "----"})
# TWSE 無資料訊息（非交易日或查無事件），spec 3.2B 記載全文為「很抱歉，沒有符合條件的資料!」
TWSE_NO_DATA_MARKER = "沒有符合條件的資料"

# 抓取時的代碼預篩（最終分類於 M4）：4 碼非 0 開頭（普通股候選，含 91xx TDR）或 00 開頭（ETF 候選）
_CODE_PREFILTER = re.compile(r"^(?:[1-9]\d{3}|00[0-9A-Z]{2,4})$")


class SchemaMismatch(RuntimeError):
    """官方回應結構與規格第 3 節不符；依 IR-1 須停下回報。"""

    def __init__(self, endpoint: str, detail: str, actual: Any = None):
        msg = f"[{endpoint}] 回應結構與規格不符：{detail}"
        if actual is not None:
            msg += f"｜實際：{actual!r}"[:600]
        super().__init__(msg)
        self.endpoint = endpoint
        self.actual = actual


class ApiStatusError(RuntimeError):
    """官方回應 stat 非預期值（非「無資料」）。"""


@dataclass
class Anomaly:
    code: str
    field: str
    raw: str


@dataclass
class ParseReport:
    anomalies: list[Anomaly] = field(default_factory=list)

    def add(self, code: str, field_name: str, raw: Any) -> None:
        self.anomalies.append(Anomaly(code, field_name, str(raw)))


# ---------------------------------------------------------------- 數值

def parse_number(raw: Any) -> float | None:
    """``"2,310.00"`` → 2310.0；``"--"``／``"----"``／空字串 → None；其他非數值拋 ValueError。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    s = str(raw).strip()
    if s in NO_PRICE_TOKENS:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"無法解析數值：{raw!r}") from exc


def parse_number_logged(raw: Any, report: ParseReport, code: str, field_name: str) -> float | None:
    try:
        return parse_number(raw)
    except ValueError:
        report.add(code, field_name, raw)
        return None


def parse_documented_date(raw: Any) -> date:
    """依規格已記載之格式解析日期：民國年（7 碼、斜線、中文）或西元斜線。"""
    s = str(raw).strip()
    if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", s):
        return parse_western_slash_date(s)
    return parse_roc_date(s)


def code_prefilter(code: str) -> bool:
    return bool(_CODE_PREFILTER.match(code))


# ---------------------------------------------------------------- 表格

def extract_tables(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """取出回應中的表格：``tables[]``（MI_INDEX、TPEx www）或頂層 ``fields``／``data``（TWSE 單表端點）。"""
    if not isinstance(payload, Mapping):
        return []
    tables = payload.get("tables")
    if isinstance(tables, list):
        return [t for t in tables if isinstance(t, Mapping)]
    if "fields" in payload:
        return [{"title": payload.get("title", ""), "fields": payload.get("fields"), "data": payload.get("data", [])}]
    return []


def find_table(payload: Mapping[str, Any], endpoint: str, title_keyword: str | None = None) -> dict[str, Any]:
    tables = extract_tables(payload)
    if not tables:
        raise SchemaMismatch(endpoint, "找不到 tables 或 fields", list(payload)[:20] if isinstance(payload, Mapping) else payload)
    if title_keyword is None:
        return dict(tables[0])
    for t in tables:
        if title_keyword in str(t.get("title", "")):
            return dict(t)
    raise SchemaMismatch(endpoint, f"找不到標題含「{title_keyword}」的表格", [t.get("title") for t in tables])


def table_rows(table: Mapping[str, Any], endpoint: str, required: Sequence[str]) -> list[dict[str, Any]]:
    """將 fields／data 轉成 dict 列；必要欄位缺漏即拋 SchemaMismatch。

    欄名重複時（例：TPEx insti/dailyTrade 的「買進股數」出現 7 次），重複欄不放入 dict，
    以免後欄覆蓋前欄；必要欄位若屬重複欄名，同樣拋 SchemaMismatch（不以位置猜測）。
    """
    fields = table.get("fields")
    if not isinstance(fields, list):
        raise SchemaMismatch(endpoint, "fields 不是陣列", fields)
    names = [str(f).strip() for f in fields]
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    missing = [r for r in required if r not in counts]
    if missing:
        raise SchemaMismatch(endpoint, f"缺少必要欄位 {missing}", names)
    ambiguous = [r for r in required if counts[r] > 1]
    if ambiguous:
        raise SchemaMismatch(endpoint, f"必要欄位名稱重複，無法依名稱對應 {ambiguous}", names)
    unique = [(i, n) for i, n in enumerate(names) if counts[n] == 1]
    data = table.get("data") or []
    rows = []
    for row in data:
        if not isinstance(row, list):
            raise SchemaMismatch(endpoint, "data 列不是陣列", row)
        rows.append({n: (row[i] if i < len(row) else None) for i, n in unique})
    return rows


def check_response_date(payload: Mapping[str, Any], endpoint: str, expected: date) -> None:
    """確認回應頂層 ``date``（YYYYMMDD）等於查詢日，避免把別天資料當成查詢日（run1 實測兩端點皆回傳此鍵）。"""
    actual = payload.get("date")
    want = expected.strftime("%Y%m%d")
    if actual is None:
        raise SchemaMismatch(endpoint, f"回應缺少 date 鍵，無法確認是否為 {want} 的資料", list(payload)[:20])
    if str(actual).strip() != want:
        raise SchemaMismatch(endpoint, f"回應日期 {actual!r} 與查詢日 {want} 不一致")


def require_keys(records: Iterable[Mapping[str, Any]], endpoint: str, required: Sequence[str]) -> list[dict[str, Any]]:
    """OpenAPI（JSON 物件陣列）欄位檢查。"""
    out = []
    for rec in records:
        if not isinstance(rec, Mapping):
            raise SchemaMismatch(endpoint, "陣列元素不是物件", rec)
        missing = [k for k in required if k not in rec]
        if missing:
            raise SchemaMismatch(endpoint, f"缺少必要鍵 {missing}", list(rec))
        out.append(dict(rec))
    return out


def date_chunks(start: date, end: date, days: int = 90) -> list[tuple[date, date]]:
    """將 [start, end] 切成每段最多 ``days`` 天（第 9 節：事件表按季分段抓取）。"""
    if end < start:
        raise ValueError("end 早於 start")
    chunks = []
    cur = start
    while cur <= end:
        seg_end = min(cur + timedelta(days=days - 1), end)
        chunks.append((cur, seg_end))
        cur = seg_end + timedelta(days=1)
    return chunks


# ---------------------------------------------------------------- 正規化資料型別

@dataclass
class DailyQuote:
    market: str          # "twse" | "tpex"
    code: str
    name: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume_shares: float | None   # 成交股數（股）
    value_ntd: float | None       # 成交金額（元）


@dataclass
class DailyMarket:
    market: str
    trade_date: date
    is_trading: bool
    quotes: list[DailyQuote] = field(default_factory=list)
    index_close: float | None = None    # TWSE：發行量加權股價指數收盤
    report: ParseReport = field(default_factory=ParseReport)


@dataclass
class CorporateEvent:
    market: str
    code: str
    name: str
    event_date: date
    kind: str            # "dividend" | "capital_reduction" | "par_value_change" | "etf_split"
    p_before: float | None
    p_ref: float | None
    source: str
    extra: dict[str, Any] = field(default_factory=dict)
