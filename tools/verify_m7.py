"""M7a：第二階段歷史資料可用性抽查（verify-api，mode = m7_probe）。

只抽查、不建立快取、不做回測。第一階段只驗證過 2025～2026 年的回應；本工具抽查 2020～2024 年
（含回測資料起日 2020-03-02，M7-Q1）同一批端點的結構，確認多年回測可以沿用第一階段解析器（IR-1）：

1. TWSE MI_INDEX（type=ALLBUT0999）：解析器、個股表欄位、TAIEX、**報酬指數表的「發行量加權股價報酬指數」**（M7-Q14）、0050。
2. TPEx dailyQuotes：解析器、欄位、管理股票表。
3. TPEx 櫃買指數（indexInfo/inx 月查詢）。
4. 事件端點：2020～2024 每年 7/1～9/28 一段（90 日）：TWTAUU、TWTB8U、TPEx exDailyQ／revivt／etfRvsRslt；
   0050（M7-Q14 基準）：TWT49U 與 TWTCAU 自 2020-03-02 起至 2026-11-20 全段，列出 0050 全部事件。
5. FinMind TaiwanStockDelisting：**不帶 token** 試讀（M7-Q19）。

請求約 100 次（不含非交易日順延），間隔沿用 HttpClient 預設。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.fetchers import tpex, twse
from src.fetchers.finmind import BASE as FINMIND_BASE
from src.fetchers.http import HttpClient
from src.fetchers.parsing import date_chunks, extract_tables, parse_number
from src.timezone import format_tpex_query_date, format_twse_query_date
from tools.verify_api import Verifier, render

# 抽樣日：回測資料起日、各年度一日；遇非交易日往後順延最多 4 個平日
SAMPLE_DATES = (date(2020, 3, 2), date(2020, 9, 15), date(2021, 1, 4), date(2022, 6, 15),
                date(2023, 9, 13), date(2024, 12, 31))
MAX_SHIFT = 4
EVENT_YEARS = (2020, 2021, 2022, 2023, 2024)
FULL_START, FULL_END = date(2020, 3, 2), date(2026, 11, 20)
TR_INDEX_NAME = "發行量加權股價報酬指數"
BENCH_ETF = "0050"

# 欄位比對基準：第一階段正式解析器所需欄位（src/fetchers/twse.py、tpex.py）；完整欄位清單照實列出不做相等比對
MI_REQUIRED = twse.MI_INDEX_QUOTE_FIELDS
DQ_REQUIRED = tpex.DAILY_QUOTE_FIELDS


# ---------------------------------------------------------------- 輔助（只讀原始回應，不改正式解析器）

def tr_index_close(payload: Any) -> tuple[float | None, str | None]:
    """從 MI_INDEX 回應找「發行量加權股價報酬指數」收盤值；回傳（值, 所在表標題）。"""
    for t in extract_tables(payload) if isinstance(payload, dict) else []:
        for row in t.get("data") or []:
            if isinstance(row, list) and len(row) > 1 and str(row[0]).strip() == TR_INDEX_NAME:
                try:
                    return parse_number(row[1]), str(t.get("title"))
                except ValueError:
                    return None, str(t.get("title"))
    return None, None


def quote_fields(payload: Any, title_keyword: str | None) -> list[str] | None:
    """MI_INDEX 以標題關鍵字找個股表（同 twse.find_table）；dailyQuotes 取第一張表（同 tpex 解析器）。"""
    tables = extract_tables(payload) if isinstance(payload, dict) else []
    if title_keyword is None:
        return list(tables[0].get("fields") or []) if tables else None
    for t in tables:
        if title_keyword in str(t.get("title") or "") and t.get("fields"):
            return list(t["fields"])
    return None


def fields_check(fields: list[str] | None, required: tuple[str, ...]) -> tuple[bool, str]:
    missing = [f for f in required if f not in (fields or [])]
    return fields is not None and not missing, (f"缺 {missing}；" if missing else "") + f"完整欄位 {fields}"


def delisting_summary(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return False, f"回應非預期格式：{type(payload).__name__} {str(payload)[:200]}"
    data = payload["data"]
    keys = list(data[0]) if data and isinstance(data[0], dict) else []
    dates = sorted(str(r.get("date")) for r in data if isinstance(r, dict) and r.get("date"))
    by_year = Counter(d[:4] for d in dates if d[:4].isdigit() and "2020" <= d[:4] <= "2026")
    four = sum(1 for r in data if isinstance(r, dict) and len(str(r.get("stock_id", ""))) == 4
               and str(r.get("stock_id", ""))[:1] in "123456789")
    ok = bool(data) and {"date", "stock_id", "stock_name"} <= set(keys)
    return ok, (f"msg={payload.get('msg')}；status={payload.get('status')}；{len(data)} 筆；欄位 {keys}；"
                f"日期 {dates[0] if dates else None}～{dates[-1] if dates else None}；2020～2026 各年 {dict(sorted(by_year.items()))}；"
                f"4 碼數字代號 {four} 筆")


# ---------------------------------------------------------------- 抽查

def _try_days(v: Verifier, d0: date, fn_probe, is_trading) -> tuple[date | None, Any]:
    d, n = d0, 0
    while n <= MAX_SHIFT:
        if d.weekday() < 5:
            p = fn_probe(d)
            if p is not None and is_trading(p):
                return d, p
            n += 1
        d += timedelta(days=1)
    return None, None


def probe_daily(v: Verifier) -> None:
    for d0 in SAMPLE_DATES:
        # ---- TWSE MI_INDEX
        def mi(d):
            k = format_twse_query_date(d)
            return v.probe(f"M7a：TWSE MI_INDEX {k}", f"{twse.RWD}/afterTrading/MI_INDEX",
                           {"date": k, "type": "ALLBUT0999", "response": "json"})
        d, p = _try_days(v, d0, mi, lambda p: isinstance(p, dict) and p.get("stat") == "OK")
        if d is None:
            v.check(f"M7a：MI_INDEX {d0} 起 {MAX_SHIFT + 1} 個平日", False, "皆無資料或請求失敗")
        else:
            k = format_twse_query_date(d)
            m = v.parse_check(f"M7a：MI_INDEX {k}（正式解析器）", lambda p=p, d=d: twse.parse_mi_index(p, d, prefilter=False),
                              lambda m: f"交易日={m.is_trading}；全部 {len(m.quotes)} 檔；TAIEX={m.index_close}；"
                                        f"無法解析值 {len(m.report.anomalies)}"
                                        + (f" {Counter((a.field, a.raw) for a in m.report.anomalies).most_common(5)}"
                                           if m.report.anomalies else ""))
            v.check(f"M7a：MI_INDEX {k} 個股表含解析器所需欄位", *fields_check(quote_fields(p, twse.QUOTES_TABLE_KEYWORD), MI_REQUIRED))
            tr, title = tr_index_close(p)
            v.check(f"M7a：MI_INDEX {k} 含「{TR_INDEX_NAME}」", tr is not None, f"收盤 {tr}；表「{title}」")
            if m is not None:
                q = next((x for x in m.quotes if x.code == BENCH_ETF), None)
                commons = sum(1 for x in m.quotes if len(x.code) == 4 and x.code[0] in "123456789")
                v.check(f"M7a：MI_INDEX {k} 含 {BENCH_ETF} 與普通股", q is not None and q.close is not None and commons > 0,
                        f"{BENCH_ETF} 開 {q.open if q else None} 收 {q.close if q else None} 股數 {q.volume_shares if q else None}；"
                        f"4 碼數字代號 {commons} 檔")

        # ---- TPEx dailyQuotes（與 MI_INDEX 同一日；MI_INDEX 全失敗時用原抽樣日）
        day = d or d0
        k = format_tpex_query_date(day)
        dp = v.probe(f"M7a：TPEx dailyQuotes {k}", f"{tpex.WWW}/afterTrading/dailyQuotes", {"date": k, "response": "json"})
        if dp is None:
            v.check(f"M7a：dailyQuotes {k}", False, "請求失敗")
        else:
            m2 = v.parse_check(f"M7a：dailyQuotes {k}（正式解析器）",
                               lambda p=dp, day=day: tpex.parse_daily_quotes(p, day, prefilter=False),
                               lambda m: f"交易日={m.is_trading}；全部 {len(m.quotes)} 檔；無法解析值 {len(m.report.anomalies)}"
                                         + (f" {Counter((a.field, a.raw) for a in m.report.anomalies).most_common(5)}"
                                            if m.report.anomalies else ""))
            v.check(f"M7a：dailyQuotes {k} 首表含解析器所需欄位", *fields_check(quote_fields(dp, None), DQ_REQUIRED))
            v.check(f"M7a：dailyQuotes {k} 表格清單（資訊）", None, f"{[str(t.get('title')) for t in extract_tables(dp)]}")
            if m2 is not None and d is not None:
                v.check(f"M7a：{day} 兩市皆有資料", m2.is_trading, f"MI_INDEX 有資料；dailyQuotes 交易日={m2.is_trading}")

        # ---- TPEx 櫃買指數（月）
        v.probe(f"M7a：TPEx indexInfo/inx {k}", f"{tpex.WWW}/indexInfo/inx", {"date": k, "response": "json"})
        r = v.parse_check(f"M7a：櫃買指數月表 {day:%Y-%m}（正式解析器）", lambda day=day: tpex.fetch_otc_index_month(v.client, day),
                          lambda r: f"{len(r)} 日；首筆 {r[0] if r else None}；末筆 {r[-1] if r else None}")
        if r is not None:
            off = [str(x["date"]) for x in r if (x["date"].year, x["date"].month) != (day.year, day.month)]
            v.check(f"M7a：櫃買指數月表 {day:%Y-%m} 日期皆屬查詢月份", bool(r) and not off,
                    f"{len(r)} 日；不屬該月 {off[:5]}" + ("；回應為空" if not r else ""))


def probe_events(v: Verifier) -> None:
    # 各年度 7/1～9/28（單一 90 日區段）
    twse_kinds = {k: s for k, s in twse._EVENT_SPECS.items() if k not in ("dividend", "etf_split")}
    for y in EVENT_YEARS:
        s, e = date(y, 7, 1), date(y, 9, 28)
        for kind, spec in twse_kinds.items():
            label = f"M7a：TWSE {spec[0]} {format_twse_query_date(s)}-{format_twse_query_date(e)}"
            p = v.probe(label, f"{twse.RWD}{spec[0]}", {"startDate": format_twse_query_date(s),
                                                       "endDate": format_twse_query_date(e), "response": "json"})
            if p is not None:
                v.parse_check(label, lambda p=p, kind=kind: twse.parse_events(p, kind), lambda r: _events_desc(r))
        for kind, spec in tpex._EVENT_SPECS.items():
            label = f"M7a：TPEx {spec[0]} {format_tpex_query_date(s)}-{format_tpex_query_date(e)}"
            p = v.probe(label, f"{tpex.WWW}{spec[0]}", {"startDate": format_tpex_query_date(s),
                                                       "endDate": format_tpex_query_date(e), "response": "json"})
            if p is not None:
                v.parse_check(label, lambda p=p, kind=kind: tpex.parse_events(p, kind), lambda r: _events_desc(r))

    # 0050：TWT49U（除權息）與 TWTCAU（ETF 分割）全段
    bench: list[Any] = []
    per_year: Counter = Counter()
    failed = 0
    for kind in ("dividend", "etf_split"):
        path = twse._EVENT_SPECS[kind][0]
        for s, e in date_chunks(FULL_START, FULL_END, 90):
            label = f"M7a：TWSE {path} {format_twse_query_date(s)}-{format_twse_query_date(e)}"
            p = v.probe(label, f"{twse.RWD}{path}", {"startDate": format_twse_query_date(s),
                                                    "endDate": format_twse_query_date(e), "response": "json"})
            got = v.parse_check(label, lambda p=p, kind=kind: twse.parse_events(p, kind), lambda r: f"{len(r)} 筆") if p is not None else None
            if got is None:
                failed += 1
                continue
            if kind == "dividend":
                per_year.update(str(x.event_date.year) for x in got)
            bench += [x for x in got if x.code == BENCH_ETF]
    bench.sort(key=lambda x: x.event_date)
    v.check("M7a：TWSE 除權息事件逐年筆數（TWT49U，2020-03-02～2026-11-20）", failed == 0,
            f"{dict(sorted(per_year.items()))}；失敗區段 {failed}")
    # 查無 0050 事件時無法判定（不自行假設應有幾筆），交人工比對官方公告
    v.check(f"M7a：{BENCH_ETF} 事件（除權息＋ETF 分割，2020-03-02～2026-11-20）", (failed == 0) if bench else None,
            "；".join(f"{x.event_date} {x.kind} 前收 {x.p_before} 參考 {x.p_ref}" for x in bench) or "查無")


def _events_desc(events: list[Any]) -> str:
    bad = [x for x in events if not (x.p_before and x.p_ref and x.p_before > 0 and x.p_ref > 0)]
    return f"{len(events)} 筆；價格非正數 {len(bad)} 筆" + (f" {[(b.code, str(b.event_date)) for b in bad[:5]]}" if bad else "")


def probe_finmind(v: Verifier) -> None:
    p = v.probe("M7a：FinMind TaiwanStockDelisting（無 token）", FINMIND_BASE, {"dataset": "TaiwanStockDelisting"})
    if p is None:
        last = v.results[-1]
        v.check("M7a：FinMind 下市清單無 token 可讀", False, f"請求失敗：{last.get('error')}")
        return
    ok, detail = delisting_summary(p)
    v.check("M7a：FinMind 下市清單無 token 可讀", ok, detail)


def run_m7(v: Verifier) -> None:
    for step in (probe_daily, probe_events, probe_finmind):
        try:
            step(v)
        except Exception:  # noqa: BLE001
            v.check(f"M7a：{step.__name__} 未預期錯誤", False, traceback.format_exc()[-1500:])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="verify_m7_report")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    v = Verifier(HttpClient(), out)
    run_m7(v)
    report = render(v, SAMPLE_DATES[0], SAMPLE_DATES[-1], title="M7a 歷史資料可用性抽查報告",
                    scope=f"抽樣日 {', '.join(map(str, SAMPLE_DATES))}；事件年度 {EVENT_YEARS[0]}～{EVENT_YEARS[-1]}；"
                          f"0050 事件全段 {FULL_START}～{FULL_END}")
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report.encode("utf-8")[:1_000_000].decode("utf-8", errors="ignore"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
