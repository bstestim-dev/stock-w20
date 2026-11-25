"""可開關參考欄 ``fundamentals_ref``（FR-1.9；第 6.3 節）：最新季 EPS、最新月營收 YoY、近 5 日三大法人買賣超（張）。

資料來源（第 3.2A、3.3A、3.2B、3.3B 節已驗證端點與欄位）：
- EPS：TWSE ``/opendata/t187ap14_L``（年度、季別、基本每股盈餘(元)）；TPEx ``/mopsfin_t187ap14_O``（Year、季別、基本每股盈餘）。
  ``eps_quarter`` 以「年度＋Q＋季別」表示（例 115Q3，對齊第 6.3 節範例）。
- 月營收：TWSE ``/opendata/t187ap05_L``、TPEx ``/mopsfin_t187ap05_O``（資料年月、營業收入-去年同月增減(%)）。
- 三大法人：TWSE ``T86``「三大法人買賣超股數」、TPEx ``insti/dailyTrade``「三大法人買賣超股數合計」，
  取基準日（含）往前 5 個交易日加總，股 ÷ 1000 換算為張（計算保留小數，顯示時才四捨五入，M3-Q5）。

只對名單中的普通股填值；ETF 一律 ``null``（第 7 節：ETF 頁隱藏基本面欄）。
任何一個來源抓取失敗只影響對應欄位（填 ``null``），並記入 notes，不中止報告。
EPS／營收為 OpenAPI「執行當日」的最新快照，補跑歷史基準日時不回溯（與第 3 節端點性質相同）。

【待確認 M5-Q1】某代號在某日的三大法人表中查無資料列時：該日不計入加總；5 日皆查無則為 ``null``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from src.fetchers import tpex, twse
from src.fetchers.parsing import parse_number

INSTI_DAYS = 5
SHARES_PER_LOT = 1000


def _num(v: Any) -> float | None:
    try:
        return parse_number(v)
    except ValueError:
        return None


def _latest_eps(rows: Iterable[Mapping[str, Any]], k_code: str, k_year: str, k_eps: str) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for r in rows:
        code = str(r.get(k_code, "")).strip()
        try:
            key = (int(str(r.get(k_year, "")).strip()), int(str(r.get("季別", "")).strip()))
        except ValueError:
            continue
        if code and (code not in out or key > out[code][0]):
            out[code] = (key, _num(r.get(k_eps)))
    return out


def _latest_revenue(rows: Iterable[Mapping[str, Any]]) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for r in rows:
        code = str(r.get("公司代號", "")).strip()
        ym = str(r.get("資料年月", "")).strip()
        if code and ym and (code not in out or ym > out[code][0]):
            out[code] = (ym, _num(r.get("營業收入-去年同月增減(%)")))
    return out


def collect_fundamentals(client, codes: Mapping[str, str], trading_days: list[date]) -> tuple[dict[str, dict], list[str]]:
    """``codes``：{代號: 市場}；``trading_days``：基準日（含）以前的交易日（遞增）。回傳（{代號: fundamentals_ref}, notes）。"""
    notes: list[str] = []
    eps: dict[str, tuple] = {}
    rev: dict[str, tuple] = {}

    def attempt(label: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — 參考欄失敗不中止報告
            notes.append(f"參考欄 {label} 抓取失敗：{type(exc).__name__}: {exc}"[:300])
            return None

    markets = set(codes.values())
    if "twse" in markets:
        rows = attempt("TWSE 季 EPS", lambda: twse.fetch_quarterly_eps(client))
        if rows is not None:
            eps.update(_latest_eps(rows, "公司代號", "年度", "基本每股盈餘(元)"))
        rows = attempt("TWSE 月營收", lambda: twse.fetch_monthly_revenue(client))
        if rows is not None:
            rev.update(_latest_revenue(rows))
    if "tpex" in markets:
        rows = attempt("TPEx 季 EPS", lambda: tpex.fetch_quarterly_eps(client))
        if rows is not None:
            eps.update(_latest_eps(rows, "SecuritiesCompanyCode", "Year", "基本每股盈餘"))
        rows = attempt("TPEx 月營收", lambda: tpex.fetch_monthly_revenue(client))
        if rows is not None:
            rev.update(_latest_revenue(rows))

    days = trading_days[-INSTI_DAYS:]
    insti: dict[str, float] = {}
    seen: dict[str, int] = {}
    insti_ok = {"twse": 0, "tpex": 0}
    for d in days:
        for market, fn in (("twse", twse.fetch_t86), ("tpex", tpex.fetch_insti_net)):
            if market not in markets:
                continue
            table = attempt(f"{market.upper()} 三大法人 {d}", lambda fn=fn, d=d: fn(client, d))
            if table is None:
                continue
            insti_ok[market] += 1
            for code, shares in table.items():
                if code in codes and codes[code] == market:
                    insti[code] = insti.get(code, 0.0) + shares
                    seen[code] = seen.get(code, 0) + 1
    for market in markets:
        if insti_ok[market] < len(days):
            notes.append(f"{market.upper()} 三大法人僅取得 {insti_ok[market]}／{len(days)} 個交易日，近 5 日加總不完整")

    out = {}
    for code, market in codes.items():
        e = eps.get(code)
        r = rev.get(code)
        out[code] = {
            "eps_quarter": f"{e[0][0]}Q{e[0][1]}" if e else None,
            "eps": e[1] if e else None,
            "rev_month": r[0] if r else None,
            "rev_yoy_pct": r[1] if r else None,
            "insti_5d_net_lots": (insti[code] / SHARES_PER_LOT) if seen.get(code) else None,
        }
    return out, notes
