"""M2 API 實測驗證（spec_v1 第 0.3 節 M2；2026-11-25 裁定採 (c)：於 GitHub runner 執行）。

用途：對規格第 3 節端點發出真實請求，傾印實際結構（stat、表格標題、欄位名、筆數、樣本列），
並以本專案解析器重跑同一份回應，產出可貼回的 Markdown 報告。不使用任何 token。

    python -m tools.verify_api --out verify_report
    （樣本日期由環境變數 VERIFY_SAMPLE_DATE、VERIFY_WINDOW_END 傳入，格式 YYYY-MM-DD）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from src.fetchers import finmind, history, tpex, twse
from src.fetchers.http import HttpClient
from src.fetchers.parsing import date_chunks, extract_tables
from src.timezone import (
    format_taipei_iso,
    format_tpex_query_date,
    format_twse_query_date,
    now_taipei,
    parse_iso_date,
)

DEFAULT_SAMPLE_DATE = "2026-11-20"   # 規格 3.2B／3.3B 實測樣本日（週五）
DEFAULT_WINDOW_END = "2026-10-09"    # 短窗歷史測試終點（含中秋／國慶前後，實際休市日以 API 結果為準）
SPEC_MI_INDEX_COUNTS = {"20260115": 1334, "20261120": 1393}
CELL_MAX = 40
SAMPLE_ROWS = 2


# ---------------------------------------------------------------- 報告工具

def _cell(v: Any) -> Any:
    s = v if isinstance(v, (int, float)) or v is None else str(v)
    return s[:CELL_MAX] + "…" if isinstance(s, str) and len(s) > CELL_MAX else s


def summarize(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        first = payload[0] if payload else None
        return {
            "type": "array", "length": len(payload),
            "keys": list(first) if isinstance(first, dict) else None,
            "sample": [{k: _cell(v) for k, v in r.items()} if isinstance(r, dict) else _cell(r)
                       for r in payload[:SAMPLE_ROWS]],
        }
    if isinstance(payload, dict):
        out: dict[str, Any] = {
            "type": "object",
            "top_keys": list(payload)[:30],
            "stat": payload.get("stat"),
            "date_like": {k: _cell(payload[k]) for k in payload if "date" in k.lower() and not isinstance(payload[k], (list, dict))},
        }
        tables = []
        for t in extract_tables(payload):
            data = t.get("data") or []
            tables.append({
                "title": _cell(t.get("title")), "fields": t.get("fields"), "rows": len(data),
                "sample": [[_cell(c) for c in row] for row in data[:SAMPLE_ROWS] if isinstance(row, list)],
            })
        out["tables"] = tables
        if isinstance(payload.get("data"), list) and not tables:  # FinMind 外層
            out["data_len"] = len(payload["data"])
            first = payload["data"][0] if payload["data"] else None
            out["data_keys"] = list(first) if isinstance(first, dict) else None
            out["data_sample"] = [{k: _cell(v) for k, v in r.items()} for r in payload["data"][:SAMPLE_ROWS] if isinstance(r, dict)]
        for k in ("msg", "status"):
            if k in payload:
                out[k] = _cell(payload[k])
        return out
    return {"type": type(payload).__name__, "value": _cell(payload)}


class Verifier:
    def __init__(self, client: HttpClient, out_dir: Path, client_factory: Callable[[], Any] = HttpClient):
        self.client = client
        self.client_factory = client_factory  # 並行模式的第二個（TPEx 專用）client
        self.out = out_dir
        (self.out / "raw").mkdir(parents=True, exist_ok=True)
        self.results: list[dict[str, Any]] = []
        self.checks: list[tuple[str, str, str]] = []   # (項目, 結果, 說明)
        self.payloads: dict[str, Any] = {}

    def probe(self, label: str, url: str, params: dict[str, Any] | None = None) -> Any:
        entry: dict[str, Any] = {"label": label, "url": url, "params": params or {}}
        t0 = time.monotonic()
        try:
            payload = self.client.get_json(url, params=params)
            entry["ok"] = True
            entry["summary"] = summarize(payload)
            self.payloads[label] = payload
        except Exception as exc:  # 報告工具：任何錯誤都記錄，不中斷
            payload = None
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
        entry["elapsed_sec"] = round(time.monotonic() - t0, 2)
        log = self.client.logs[-1] if self.client.logs else None
        entry["http_status"] = log.status if log else None
        entry["attempts"] = log.attempts if log else None
        self.results.append(entry)
        (self.out / "raw" / f"{len(self.results):03d}.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return payload

    def parse_check(self, label: str, fn: Callable[[], Any], describe: Callable[[Any], str] = lambda r: "OK") -> Any:
        try:
            result = fn()
            self.checks.append((f"解析器：{label}", "通過", describe(result)))
            return result
        except Exception as exc:
            self.checks.append((f"解析器：{label}", "失敗", f"{type(exc).__name__}: {exc}"[:400]))
            return None

    def check(self, item: str, ok: bool | None, detail: str) -> None:
        self.checks.append((item, "通過" if ok else ("無法判定" if ok is None else "不符"), detail))


# ---------------------------------------------------------------- 驗證內容

def run(v: Verifier, sample: date, window_end: date) -> None:
    s_twse, s_tpex = format_twse_query_date(sample), format_tpex_query_date(sample)
    nontrading = sample + timedelta(days=(5 - sample.weekday()) % 7 or 7)  # 樣本日後第一個週六
    year_chunks = date_chunks(sample - timedelta(days=364), sample, 90)

    # ---- TWSE OpenAPI
    for path in ("/opendata/t187ap03_L", "/exchangeReport/STOCK_DAY_ALL", "/announcement/punish",
                 "/announcement/notice", "/holidaySchedule/holidaySchedule", "/opendata/t187ap05_L",
                 "/opendata/t187ap14_L"):
        v.probe(f"TWSE OpenAPI {path}", twse.OPENAPI + path)
    v.parse_check("TWSE t187ap03_L", lambda: twse.require_keys(v.payloads["TWSE OpenAPI /opendata/t187ap03_L"], "t187ap03_L",
        ("公司代號", "公司簡稱", "產業別", "普通股每股面額", "實收資本額", "特別股", "已發行普通股數或TDR原股發行股數")),
        lambda r: f"{len(r)} 筆；產業別=91 共 {sum(1 for x in r if str(x['產業別']).strip() == '91')} 筆")
    hol = v.payloads.get("TWSE OpenAPI /holidaySchedule/holidaySchedule")
    if isinstance(hol, list):
        years = Counter(str(r.get("Date", ""))[:3] for r in hol)
        v.check("run2：TWSE 休市表涵蓋年度（民國年前 3 碼）", None,
                f"{dict(years)}；全部：" + "、".join(f"{r.get('Date')} {r.get('Name')}" for r in hol))
    v.parse_check("TWSE notice（濾空殼）", lambda: [r for r in v.payloads["TWSE OpenAPI /announcement/notice"] if str(r.get("Code", "")).strip()],
        lambda r: f"{len(r)} 筆有效")

    # ---- TWSE RWD 主歷史來源
    mi = {}
    for d in (sample, date(2026, 1, 15), nontrading):
        key = format_twse_query_date(d)
        mi[d] = v.probe(f"TWSE MI_INDEX {key}", f"{twse.RWD}/afterTrading/MI_INDEX",
                        {"date": key, "type": "ALLBUT0999", "response": "json"})
    for d, payload in mi.items():
        if payload is None:
            continue
        key = format_twse_query_date(d)
        res = v.parse_check(f"MI_INDEX {key}", lambda p=payload, d=d: twse.parse_mi_index(p, d, prefilter=False),
            lambda m: f"交易日={m.is_trading}；全部 {len(m.quotes)} 檔；TAIEX={m.index_close}；無法解析值 {len(m.report.anomalies)}")
        if res is not None and key in SPEC_MI_INDEX_COUNTS:
            v.check(f"MI_INDEX {key} 筆數與規格實測一致", len(res.quotes) == SPEC_MI_INDEX_COUNTS[key],
                    f"實得 {len(res.quotes)}，規格記載 {SPEC_MI_INDEX_COUNTS[key]}")
        if res is not None and res.report.anomalies:
            v.check(f"MI_INDEX {key} 未記載的非數值字串", False,
                    str(Counter((a.field, a.raw) for a in res.report.anomalies).most_common(10)))

    stock_day = v.probe("TWSE STOCK_DAY 2330", f"{twse.RWD}/afterTrading/STOCK_DAY",
                        {"date": s_twse, "stockNo": "2330", "response": "json"})
    taiex_m = v.probe("TWSE MI_5MINS_HIST", f"{twse.RWD}/TAIEX/MI_5MINS_HIST", {"date": s_twse, "response": "json"})

    # 單位交叉核對：MI_INDEX 成交股數 vs STOCK_DAY 成交股數（皆應為「股」）
    if mi.get(sample) is not None and stock_day is not None:
        m = twse.parse_mi_index(mi[sample], sample, prefilter=False) if mi[sample].get("stat") == "OK" else None
        sd_rows = v.parse_check("STOCK_DAY 2330", lambda: _parse_stock_day(stock_day),
                                lambda r: f"{len(r)} 日；日期樣本 {r[0]['date'] if r else None}")
        if m and sd_rows:
            a = next((q.volume_shares for q in m.quotes if q.code == "2330"), None)
            b = next((r["volume_shares"] for r in sd_rows if r["date"] == sample), None)
            v.check("單位：TWSE MI_INDEX 與 STOCK_DAY 之 2330 成交股數相同", a is not None and a == b, f"MI_INDEX={a}，STOCK_DAY={b}")
    if taiex_m is not None:
        rows = v.parse_check("MI_5MINS_HIST", lambda: _parse_taiex(taiex_m), lambda r: f"{len(r)} 日；日期樣本 {r[0]['date'] if r else None}")
        if rows and mi.get(sample) is not None and mi[sample].get("stat") == "OK":
            close_a = twse.parse_mi_index(mi[sample], sample).index_close
            close_b = next((r["close"] for r in rows if r["date"] == sample), None)
            v.check("TAIEX：MI_INDEX 收盤指數與 MI_5MINS_HIST 一致", close_a is not None and close_a == close_b, f"{close_a} vs {close_b}")

    # ---- TWSE 公司事件（一年、按季）
    for kind, spec in twse._EVENT_SPECS.items():
        events = []
        for s, e in year_chunks:
            label = f"TWSE {spec[0]} {format_twse_query_date(s)}-{format_twse_query_date(e)}"
            p = v.probe(label, f"{twse.RWD}{spec[0]}", {"startDate": format_twse_query_date(s), "endDate": format_twse_query_date(e), "response": "json"})
            if p is not None:
                got = v.parse_check(label, lambda p=p: twse.parse_events(p, kind), lambda r: f"{len(r)} 筆")
                events.extend(got or [])
        codes = {ev.code for ev in events}
        watch = {"dividend": ["2330", "00939"]}.get(kind, [])  # 4763 事件日 2025-06-30（run2），不在一年窗內
        if watch:
            v.check(f"TWSE {spec[0]} 一年內含 {watch}", all(c in codes for c in watch), f"命中 {[c for c in watch if c in codes]}；共 {len(events)} 筆")
        bad = [ev for ev in events if not (ev.p_before and ev.p_ref and ev.p_before > 0 and ev.p_ref > 0)]
        if bad:
            v.check(f"TWSE {spec[0]} 價格欄非正數", False, str([(b.code, b.event_date.isoformat(), b.p_before, b.p_ref) for b in bad[:10]]))

    v.probe("TWSE T86", f"{twse.RWD}/fund/T86", {"date": s_twse, "selectType": "ALL", "response": "json"})
    if "TWSE T86" in v.payloads:
        v.parse_check("T86", lambda: _parse_t86(v.payloads["TWSE T86"]), lambda r: f"{len(r)} 檔")

    # ---- TPEx OpenAPI
    for path in ("/mopsfin_t187ap03_O", "/tpex_mainboard_quotes", "/tpex_disposal_information",
                 "/tpex_trading_warning_information", "/mopsfin_t187ap05_O", "/mopsfin_t187ap14_O",
                 "/tpex_3insti_daily_trading"):
        v.probe(f"TPEx OpenAPI {path}", tpex.OPENAPI + path)
    if "TPEx OpenAPI /mopsfin_t187ap03_O" in v.payloads:
        v.parse_check("TPEx mopsfin_t187ap03_O", lambda: tpex.require_keys(v.payloads["TPEx OpenAPI /mopsfin_t187ap03_O"], "t187ap03_O",
            ("SecuritiesCompanyCode", "CompanyAbbreviation", "SecuritiesIndustryCode", "ParValueOfCommonStock",
             "Paidin.Capital.NTDollars", "PreferredStock.shares", "IssueShares")), lambda r: f"{len(r)} 筆")

    # ---- TPEx www 主歷史來源
    dq = {}
    for d in (sample, date(2026, 1, 15), nontrading):
        key = format_tpex_query_date(d)
        dq[d] = v.probe(f"TPEx dailyQuotes {key}", f"{tpex.WWW}/afterTrading/dailyQuotes", {"date": key, "response": "json"})
        if dq[d] is not None:
            v.parse_check(f"dailyQuotes {key}", lambda p=dq[d], d=d: tpex.parse_daily_quotes(p, d, prefilter=True),
                lambda m: f"交易日={m.is_trading}；預篩後 {len(m.quotes)} 檔；無法解析值 {len(m.report.anomalies)} "
                          f"{Counter((a.field, a.raw) for a in m.report.anomalies).most_common(5) if m.report.anomalies else ''}")

    if dq.get(sample) is not None:
        try:
            m = tpex.parse_daily_quotes(dq[sample], sample, prefilter=True)
            no_px = [(q.code, q.name, q.volume_shares) for q in m.quotes if q.close is None]
            v.check("run3：TPEx 無收盤價列（含 '---'）成交股數皆不足 1,000 股（D2）", all((x[2] or 0) < 1000 for x in no_px),
                    f"{len(no_px)} 檔：{no_px[:20]}")
            mgmt = next((t for t in extract_tables(dq[sample]) if t.get("title") == "管理股票"), None)
            v.check("run2：TPEx dailyQuotes「管理股票」表列數", None, f"{len(mgmt.get('data') or []) if mgmt else '無此表'}")
        except Exception as exc:
            v.check("run2：TPEx 無收盤價列", False, f"{type(exc).__name__}: {exc}"[:300])

    ts = v.probe("TPEx tradingStock 6488", f"{tpex.WWW}/afterTrading/tradingStock", {"date": s_tpex, "code": "6488", "response": "json"})
    if ts is not None and dq.get(sample) is not None:
        ts_rows = v.parse_check("tradingStock 6488", lambda: _parse_trading_stock(ts), lambda r: f"{len(r)} 日；日期樣本 {r[0]['date'] if r else None}")
        try:
            m = tpex.parse_daily_quotes(dq[sample], sample, prefilter=False)
            a = next((q.volume_shares for q in m.quotes if q.code == "6488"), None)
        except Exception:
            a = None
        b = next((r["volume_shares"] for r in (ts_rows or []) if r["date"] == sample), None)
        ratio = (a / b) if a and b else None
        v.check("單位：dailyQuotes 成交股數 ≈ tradingStock 成交張數×1000（6488）",
                None if ratio is None else abs(ratio - 1) < 0.001, f"dailyQuotes={a} 股；tradingStock 換算={b} 股；比值={ratio}")

    v.probe("TPEx indexInfo/inx", f"{tpex.WWW}/indexInfo/inx", {"date": s_tpex, "response": "json"})
    v.parse_check("TPEx 櫃買指數 fetch_otc_index_month", lambda: tpex.fetch_otc_index_month(v.client, sample),
                  lambda r: f"{len(r)} 日；首筆 {r[0] if r else None}")

    for kind, spec in tpex._EVENT_SPECS.items():
        events = []
        for s, e in year_chunks:
            label = f"TPEx {spec[0]} {format_tpex_query_date(s)}-{format_tpex_query_date(e)}"
            p = v.probe(label, f"{tpex.WWW}{spec[0]}", {"startDate": format_tpex_query_date(s), "endDate": format_tpex_query_date(e), "response": "json"})
            if p is not None:
                got = v.parse_check(label, lambda p=p: tpex.parse_events(p, kind), lambda r: f"{len(r)} 筆")
                events.extend(got or [])
        if kind == "dividend":
            v.check("TPEx exDailyQ 一年內含債券 ETF（規格例 00950B）", "00950B" in {e.code for e in events}, f"共 {len(events)} 筆")

    # Q4：三大法人欄位傾印（不解析）
    insti = v.probe("TPEx insti/dailyTrade（Q4 欄位）", f"{tpex.WWW}/insti/dailyTrade",
                    {"type": "Daily", "sect": "EW", "date": s_tpex, "response": "json"})
    if insti is not None:
        net = v.parse_check("TPEx parse_insti_net（取「三大法人買賣超股數合計」）", lambda: tpex.parse_insti_net(insti, sample),
                            lambda r: f"{len(r)} 檔；6488={r.get('6488')}")
        t0 = next((t for t in extract_tables(insti) if t.get("fields")), None)
        if t0:
            from src.fetchers.parsing import parse_number
            ok = bad = 0
            bad_rows = []
            for row in t0.get("data") or []:
                try:
                    g = lambda k: parse_number(row[2 + 3 * (k - 1) + 2])
                    if g(3) + g(4) + g(7) == parse_number(row[-1]):
                        ok += 1
                    else:
                        bad += 1
                        bad_rows.append(row[0])
                except Exception:
                    bad += 1
                    bad_rows.append(row[0])
            v.check("run2：法人欄位關係（第3+4+7組買賣超＝合計，僅佐證不用於取值）", bad == 0,
                    f"符合 {ok} 列、不符 {bad} 列 {bad_rows[:10]}")

    # ---- FinMind（免 token）
    info = v.probe("FinMind TaiwanStockInfo", finmind.BASE, {"dataset": "TaiwanStockInfo"})
    if info is not None:
        st = finmind.undated_stats(info["data"])
        v.check("run3：FinMind TaiwanStockInfo 無日期列（D1）", None,
                f"無日期列 {st['undated_rows']}（原始值分佈 {st['undated_raw_values']}）；"
                f"僅有無日期列的代號 {len(st['stock_ids_only_undated'])} 檔 {st['stock_ids_only_undated'][:40]}；"
                f"其 type 分佈 {st['only_undated_by_type']}；其類別分佈 {st['only_undated_by_category']}；"
                f"其他非 ISO 日期列 {st['invalid_date_rows']} {json.dumps(st['invalid_samples'], ensure_ascii=False)[:500]}；"
                f"樣本 {json.dumps(st['undated_samples'], ensure_ascii=False)[:1200]}")
        recs = v.parse_check("FinMind TaiwanStockInfo", lambda: finmind.latest_info_by_stock(
            finmind.require_keys(info["data"], "TaiwanStockInfo", finmind.STOCK_INFO_KEYS)), lambda r: f"{len(r)} 檔（取最新 date 後）")
        if recs:
            cats = Counter(str(r["industry_category"]) for r in recs.values())
            types = Counter(str(r["type"]) for r in recs.values())
            keyed = {c: n for c, n in cats.items() if any(k in c for k in ("ETF", "ETN", "存託", "創新", "TIB", "受益"))}
            v.check("FinMind 分類（ETF／ETN／DR／創新板）", None, f"type={dict(types)}；相關類別={keyed}")
    tdays = v.probe("FinMind TaiwanStockTradingDate", finmind.BASE,
                    {"dataset": "TaiwanStockTradingDate", "start_date": "2024-01-01", "end_date": sample.isoformat()})
    if tdays is not None and isinstance(tdays.get("data"), list):
        weekend = [r.get("date") for r in tdays["data"] if _is_weekend(r.get("date"))]
        v.check("週末無交易日（不請求週末之依據）", not weekend, f"2024-01-01 起 {len(tdays['data'])} 個交易日，週末交易日 {weekend[:10]}")

    # ---- run3：FinMind TaiwanStockTradingDate 是否提供未來交易日（D4）
    fut = v.probe("run3：FinMind TaiwanStockTradingDate（未來 60 天）", finmind.BASE,
                  {"dataset": "TaiwanStockTradingDate", "start_date": sample.isoformat(),
                   "end_date": (sample + timedelta(days=60)).isoformat()})
    if isinstance(fut, dict) and isinstance(fut.get("data"), list):
        from src.timezone import today_taipei
        dates = [str(r.get("date")) for r in fut["data"]]
        future = [d for d in dates if d > today_taipei().isoformat()]
        v.check("run3：FinMind 交易日表含今日之後的日期（D4）", bool(future) or None,
                f"回傳 {len(dates)} 筆；今日之後 {len(future)} 筆 {future[:30]}；全部 {dates[:60]}")

    # ---- run2：4763 面額變更往前回溯（規格例；run1 一年內未見）
    hits = []
    for s_, e_ in date_chunks(date(2023, 1, 1), sample - timedelta(days=365), 90):
        try:
            hits += [ev for ev in twse.fetch_events(v.client, "par_value_change", s_, e_) if ev.code == "4763"]
        except Exception as exc:
            v.check(f"run2：TWTB8U {s_}～{e_}", False, f"{type(exc).__name__}: {exc}"[:300])
    v.check("run2：TWTB8U 2023-01-01 起是否含 4763", bool(hits) or None,
            str([(h.event_date.isoformat(), h.p_before, h.p_ref) for h in hits]) or "2023 年以來查無")

    # ---- 短窗歷史流程實測（含非交易日判定）：逐日依序 vs 分主機並行
    def timed(label, end_d, **kw):
        clients = [v.client] + ([kw["tpex_client"]] if kw.get("tpex_client") else [])
        marks = [len(c.logs) for c in clients]
        t0 = time.monotonic()
        try:
            res = history.fetch_daily_history(v.client, end_d, lookback_weeks=3, **kw)
            lines = [f"{d.trade_date}({'一二三四五'[d.trade_date.weekday()]})={d.status}" for d in res.days]
            v.check(label, True, f"as_of={res.as_of_date}；缺漏={res.missing}；notes={res.notes}；" + "、".join(lines))
        except Exception as exc:
            v.check(label, False, f"{type(exc).__name__}: {exc}"[:800])
        wall = time.monotonic() - t0
        logs = [l for c, m in zip(clients, marks) for l in c.logs[m:]]
        per = {}
        for host in ("twse.com.tw", "tpex.org.tw"):
            hl = [l.elapsed_sec for l in logs if host in l.url]
            per[host] = (len(hl), sum(hl) / len(hl) if hl else 0.0)
        days = max(per["twse.com.tw"][0], 1)
        return wall, days, per

    w1, n1, per1 = timed("短窗歷史流程（fetch_daily_history）", window_end)
    w2, n2, per2 = timed("run2：短窗歷史流程（分主機並行）", window_end - timedelta(days=21),
                         parallel_hosts=True, tpex_client=v.client_factory())
    fmt = lambda per: "；".join(f"{h} {n} 次平均 {a:.2f} 秒" for h, (n, a) in per.items())
    v.check("耗時估算（逐日依序）", None, f"{n1} 個平日實耗 {w1:.0f} 秒（{fmt(per1)}）；300 個平日 ≈ {w1 / n1 * 300 / 60:.1f} 分鐘")
    v.check("run2：耗時估算（分主機並行）", None, f"{n2} 個平日實耗 {w2:.0f} 秒（{fmt(per2)}）；300 個平日 ≈ {w2 / n2 * 300 / 60:.1f} 分鐘")


# ---- 解析輔助（重用正式解析器，避免重複請求）

def _parse_stock_day(payload):
    from src.fetchers.parsing import find_table, parse_documented_date, parse_number, table_rows
    if payload.get("stat") != "OK":
        raise RuntimeError(f"stat={payload.get('stat')!r}")
    rows = table_rows(find_table(payload, "STOCK_DAY"), "STOCK_DAY", ("日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "註記"))
    return [{"date": parse_documented_date(r["日期"]), "volume_shares": parse_number(r["成交股數"])} for r in rows]


def _parse_taiex(payload):
    from src.fetchers.parsing import find_table, parse_documented_date, parse_number, table_rows
    rows = table_rows(find_table(payload, "MI_5MINS_HIST"), "MI_5MINS_HIST", ("日期", "開盤指數", "最高指數", "最低指數", "收盤指數"))
    return [{"date": parse_documented_date(r["日期"]), "close": parse_number(r["收盤指數"])} for r in rows]


def _parse_trading_stock(payload):
    from src.fetchers.parsing import find_table, parse_documented_date, parse_number, table_rows
    rows = table_rows(find_table(payload, "tradingStock"), "tradingStock", ("日 期", "成交張數", "成交仟元", "開盤", "最高", "最低", "收盤"))
    return [{"date": parse_documented_date(r["日 期"]), "volume_shares": (parse_number(r["成交張數"]) or 0) * 1000} for r in rows]


def _parse_t86(payload):
    from src.fetchers.parsing import find_table, table_rows
    return table_rows(find_table(payload, "T86"), "T86", twse.T86_FIELDS)


def _is_weekend(value: Any) -> bool:
    try:
        return parse_iso_date(str(value)).weekday() >= 5
    except ValueError:
        return False


# ---------------------------------------------------------------- 輸出

def render(v: Verifier, sample: date, window_end: date) -> str:
    ok = sum(1 for r in v.results if r["ok"])
    lines = [
        "# M2 API 實測報告",
        "",
        f"- 產生時間：{format_taipei_iso(now_taipei())}（Asia/Taipei）",
        f"- 樣本交易日：{sample}；短窗終點：{window_end}",
        f"- 請求：{len(v.results)} 個探測，成功 {ok}，失敗 {len(v.results) - ok}",
        "",
        "## 一、檢查結果",
        "",
        "| 項目 | 結果 | 說明 |",
        "| :--- | :--- | :--- |",
    ]
    for item, result, detail in v.checks:
        lines.append(f"| {item} | {result} | {detail.replace('|', '／').replace(chr(10), ' ')} |")
    lines += ["", "## 二、端點實際結構", ""]
    for r in v.results:
        status = "OK" if r["ok"] else "FAIL"
        lines.append(f"### [{status}] {r['label']}")
        lines.append(f"- HTTP {r['http_status']}，嘗試 {r['attempts']} 次，{r['elapsed_sec']} 秒")
        if r["ok"]:
            lines.append("```json")
            lines.append(json.dumps(r["summary"], ensure_ascii=False, default=str))
            lines.append("```")
        else:
            lines.append(f"- 錯誤：{r['error']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="verify_report")
    args = ap.parse_args(argv)
    sample = parse_iso_date(os.environ.get("VERIFY_SAMPLE_DATE") or DEFAULT_SAMPLE_DATE)
    window_end = parse_iso_date(os.environ.get("VERIFY_WINDOW_END") or DEFAULT_WINDOW_END)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    v = Verifier(HttpClient(), out)
    try:
        run(v, sample, window_end)
    except Exception:
        v.check("驗證腳本未預期錯誤", False, traceback.format_exc()[-1500:])
    report = render(v, sample, window_end)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report[:900_000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
