"""M4 選股引擎全量試跑（真實官方資料；不發佈 Pages、不寫入 site/）。

用途：
1. 以真實 60 週資料跑一次 ``collect_inputs`` → ``screen``，核對第 10.1 節第 3、4 項（分頁互斥、股本硬門檻、
   不含權證／ETN／特別股／DR、創新板納入、U2 設 0 可關閉、綠燈不過濾、出場標示）。
2. 實測統計：跳空異常（M4-Q1 裁定：收盤跌 > 35% 或漲 > 50% 且無事件）、事件對帳（M4-Q1b 裁定）、
   族群分組（M4-Q4）、ETF 子類型重疊（M4-Q6）、處置／注意公告日期分佈（M4-Q7）。

執行：``python -m tools.dry_run_m4 --out dry_run_report [--as-of YYYY-MM-DD]``（約 30～60 分鐘，視 TPEx 回應而定）。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


from src.anomalies import close_jumps, reconcile_events
from src.config import get_finmind_token, load_config
from src.fetchers.http import HttpClient
from src.screener import collect_inputs, screen
from src.timezone import format_taipei_iso, now_taipei, parse_iso_date, today_taipei
from src.universe import COMMON_CODE_RE, TWSE_DR_INDUSTRY

TAB_COLUMNS = ("stock_id", "stock_name", "market", "close_raw", "w20ma", "bias_20w_pct", "vol_ratio_20w",
               "is_volume_spike", "score", "exit_status", "warnings")


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.checks: list[tuple[str, str, str]] = []
        self.data: dict[str, Any] = {}

    def h(self, text: str) -> None:
        self.lines += ["", f"## {text}", ""]

    def p(self, text: str = "") -> None:
        self.lines.append(text)

    def check(self, item: str, ok: bool | None, detail: str) -> None:
        self.checks.append((item, "通過" if ok else ("無法判定" if ok is None else "不符"), detail))

    def table(self, header: list[str], rows: list[list[Any]]) -> None:
        self.lines.append("| " + " | ".join(header) + " |")
        self.lines.append("| " + " | ".join(":---" for _ in header) + " |")
        for r in rows:
            self.lines.append("| " + " | ".join(_cell(x) for x in r) + " |")

    def render(self, title_meta: list[str]) -> str:
        out = ["# M4 選股引擎全量試跑報告", ""] + title_meta + ["", "## 一、驗收核對", ""]
        out.append("| 項目 | 結果 | 說明 |")
        out.append("| :--- | :--- | :--- |")
        out += [f"| {i} | {r} | {_cell(d)} |" for i, r, d in self.checks]
        return "\n".join(out + self.lines) + "\n"


def _cell(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:.4g}" if abs(x) < 1e6 else f"{x:,.0f}"
    if isinstance(x, (list, tuple)):
        return "、".join(map(str, x)) if x else "—"
    s = "—" if x is None else str(x)
    return s.replace("|", "／").replace("\n", " ")[:400]


def _ids(tab: list[dict[str, Any]]) -> list[str]:
    return [r["stock_id"] for r in tab]


# ---------------------------------------------------------------- 各節

def acceptance(rep: Report, inp, res, cfg) -> None:
    tabs = res["tabs"]
    p1, p2 = tabs["new_cross"], tabs["above_and_spike"]
    dr = {str(r["公司代號"]).strip() for r in inp.twse_basics if str(r.get("產業別", "")).strip() == TWSE_DR_INDUSTRY}
    if inp.finmind_rows:
        dr |= {c for c, rows in inp.finmind_rows.items() if any(r.get("industry_category") == "存託憑證" for r in rows)}
    commons = p1 + p2
    bad_cap = [r["stock_id"] for r in commons if not (r["capital_billion_ntd"] and r["capital_billion_ntd"] * 1e8 > 2e9)]
    bad_code = [r["stock_id"] for r in commons if not COMMON_CODE_RE.match(r["stock_id"])]
    rep.check("頁 1／頁 2 普通股皆股本 > 20 億", not bad_cap, f"共 {len(commons)} 檔；不符 {bad_cap}")
    rep.check("頁 1／頁 2 不含權證／ETN／特別股（代號皆 4 碼普通股）", not bad_code, f"不符 {bad_code}")
    rep.check("頁 1／頁 2 不含 DR", not (set(_ids(commons)) & dr), f"DR 代號 {len(dr)} 個；交集 {sorted(set(_ids(commons)) & dr)}")
    inter = set(_ids(p1)) & set(_ids(p2))
    rep.check("頁 1 與頁 2 互斥", not inter, f"頁 1 {len(p1)} 檔、頁 2 {len(p2)} 檔；交集 {sorted(inter)}")
    ratio = float(cfg["screening"]["volume_spike_ratio"])
    low = [r["stock_id"] for r in p2 if not (r["vol_ratio_20w"] is not None and r["vol_ratio_20w"] >= ratio)]
    rep.check(f"頁 2 全部 vol_ratio_20w ≥ {ratio}", not low, f"不符 {low}")
    ns = [r["stock_id"] for r in p1 if not r["is_volume_spike"]]
    rep.check("頁 1 含未爆量的新站上標的（若當週存在）", True if ns else None,
              f"未爆量 {len(ns)} 檔、爆量 {len(p1) - len(ns)} 檔；勾選「僅看同時爆量」後剩 {len(p1) - len(ns)} 檔")
    tib_pool = [c for c, r in res["_all_rows"].items() if r["asset_type"] == "common_stock" and r["is_tib"]]
    rep.check("創新板股本 > 20 億者在普通股池", None if not inp.finmind_rows else True,
              f"普通股池中創新板 {len(tib_pool)} 檔：{sorted(tib_pool)}；名單內 {sorted(set(tib_pool) & set(_ids(commons)))}")

    cfg0 = copy.deepcopy(cfg)
    cfg0["universe"]["min_avg_daily_lots_20w"] = 0
    res0 = screen(inp, cfg0)
    add1 = sorted(set(_ids(res0["tabs"]["new_cross"])) - set(_ids(p1)))
    add2 = sorted(set(_ids(res0["tabs"]["above_and_spike"])) - set(_ids(p2)))
    rep.check("U2 設 0 即關閉（名單可出現原被 U2 排除者）", bool(add1 or add2) if res["summary"]["passed_liquidity"] <
              res["summary"]["passed_capital_gt_20b"] else None,
              f"通過 U2 {res['summary']['passed_liquidity']} → {res0['summary']['passed_liquidity']}；"
              f"頁 1 增 {len(add1)} 檔、頁 2 增 {len(add2)} 檔")
    green = screen(inp, cfg, force_light="GREEN")
    same = all(_ids(green["tabs"][k]) == _ids(tabs[k]) for k in tabs)
    rep.check("模擬綠燈時名單照常產出", same and green["market"]["light"] == "GREEN",
              f"燈號 {green['market']['light_label_zh']}；文案「{green['market']['suggested_exposure']}」；四分頁與實際相同={same}")
    ex = Counter(r["exit_status"] for r in res["_all_rows"].values())
    rep.check("出場狀態", None, f"週結算={res['is_week_settled']}；{dict(ex)}；出場分頁 {len(tabs['exit_alerts'])} 檔")
    rep.data["u2_off_added"] = {"new_cross": add1, "above_and_spike": add2}


def overview(rep: Report, inp, res, timings) -> None:
    rep.h("二、執行概況")
    w = inp.week
    rep.table(["項目", "值"], [
        ["基準日／週別", f"{res['as_of_date']}／{res['week_id']}"],
        ["週結算", f"{w.is_settled}；已交易 {w.trading_days_elapsed} 天；"
                  + "；".join(f"{r.day} {'確認' if r.confirmed_closed else '未確認'}（{r.reason}）" for r in w.remaining)],
        ["回溯起日", inp.history_start],
        ["日 K 列數／代號數", f"{len(inp.daily):,}／{inp.daily['code'].nunique():,}"],
        ["事件數", dict(Counter((e.market, e.kind) for e in inp.events))],
        ["大盤", res["market"]],
        ["摘要", res["summary"]],
        ["標的範圍統計", res["_universe_stats"]],
        ["notes", res["notes"]],
        ["耗時（秒）", timings],
    ])


def tab_samples(rep: Report, res) -> None:
    rep.h("三、各分頁前 20 檔（依 score 降冪）")
    for k, tab in res["tabs"].items():
        rep.p(f"### {k}（{len(tab)} 檔）")
        rep.p()
        cols = list(TAB_COLUMNS) + (["etf_subtype", "etf_zone"] if k == "etf" else [])
        shown = [r for r in tab if r.get("etf_zone")] if k == "etf" else tab
        if k == "etf":
            rep.p(f"兩區內 {len(shown)} 檔（以下列出兩區內前 20 檔）")
            rep.p()
        rep.table(cols, [[r.get(c) for c in cols] for r in shown[:20]])
        rep.p()


def sector_data(rep: Report, inp, res) -> None:
    rep.h("四、M4-Q4 族群（S6）分組依據實測")
    fm = inp.finmind_rows
    if not fm:
        rep.p("FinMind 不可用，無法比較。")
        return
    off = {}
    for market, rows, k_code, k_ind in (("twse", inp.twse_basics, "公司代號", "產業別"),
                                        ("tpex", inp.tpex_basics, "SecuritiesCompanyCode", "SecuritiesIndustryCode")):
        for r in rows:
            c = str(r.get(k_code, "")).strip()
            if COMMON_CODE_RE.match(c):
                off[c] = (market, str(r.get(k_ind, "")).strip())
    multi = {c: [x.get("industry_category") for x in fm[c]] for c in off if c in fm and len(fm[c]) > 1}
    rep.p(f"- 普通股候選 {len(off)} 檔；FinMind 最新日期有多列（多個類別）者 {len(multi)} 檔，例：" +
          "；".join(f"{c} {v}" for c, v in list(multi.items())[:15]))
    by_code: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for c, (m, code) in off.items():
        for x in fm.get(c, []):
            by_code[code][m][x.get("industry_category")] += 1
    rows = []
    for code in sorted(by_code):
        tw, tp = by_code[code].get("twse", Counter()), by_code[code].get("tpex", Counter())
        rows.append([code, tw.most_common(3), tp.most_common(3)])
    rep.p("- 官方產業別代碼 → 各市場 FinMind 類別（前 3 名與檔數）；用以判斷上市、上櫃代碼表是否同義：")
    rep.p()
    rep.table(["官方代碼", "上市 FinMind 類別", "上櫃 FinMind 類別"], rows)
    tib = {c: [x.get("industry_category") for x in fm.get(c, [])] for c, r in res["_all_rows"].items()
           if r["asset_type"] == "common_stock" and r["is_tib"]}
    rep.p()
    rep.p("- 創新板（股本 > 20 億）FinMind 類別與官方代碼：" +
          "；".join(f"{c} {v} 官方 {off.get(c, ('', ''))[1]}" for c, v in tib.items()))
    rep.data["sector_multi_category"] = multi


def etf_data(rep: Report, res) -> None:
    rep.h("五、M4-Q6 ETF 子類型重疊實測")
    etfs = [r for r in res["_all_rows"].values() if r["asset_type"] == "etf"]
    rep.p(f"- ETF 共 {len(etfs)} 檔；子類型 {dict(Counter(r['etf_subtype'] for r in etfs))}")
    st = res["_universe_stats"]
    rep.p(f"- FinMind ETF 代號 {st.get('finmind_etf_codes')}、其中回溯期間無行情 {st.get('finmind_etf_without_quotes')}；"
          f"行情中 00 開頭但 FinMind 未標 ETF {st.get('quote_00_codes_not_finmind_etf')} 個")
    from src.universe import etf_subtype_matches
    multi = [(r["stock_id"], r["stock_name"], etf_subtype_matches(r["stock_id"], r["stock_name"] or ""))
             for r in etfs if len(etf_subtype_matches(r["stock_id"], r["stock_name"] or "")) > 1]
    rep.p(f"- 同時符合多項子類型規則者 {len(multi)} 檔（目前依第 4.1B 節列示順序取第一項）：")
    rep.p()
    rep.table(["代號", "名稱", "符合規則"], [list(m) for m in multi])


def alerts_data(rep: Report, inp, res) -> None:
    rep.h("六、M4-Q7 處置／注意資料實測")
    as_of = res["as_of_date"]
    uni = set(res["_all_rows"])
    for label, rows, k_code, has_period in (("TWSE 處置", inp.twse_disposition, "Code", True),
                                            ("TPEx 處置", inp.tpex_disposition, "SecuritiesCompanyCode", True),
                                            ("TWSE 注意", inp.twse_notice, "Code", False),
                                            ("TPEx 注意", inp.tpex_warning, "SecuritiesCompanyCode", False)):
        if rows is None:
            rep.p(f"- {label}：不可用")
            continue
        dates = Counter(str(r.get("Date")) for r in rows)
        inside = [str(r.get(k_code, "")).strip() for r in rows if str(r.get(k_code, "")).strip() in uni]
        rep.p(f"- {label}：{len(rows)} 列；公告日分佈 {dict(dates)}；屬本系統標的 {len(inside)} 檔 {inside[:30]}")
        if has_period:
            rep.p(f"  - 處置期間樣本：{[r.get('DispositionPeriod') for r in rows[:5]]}")
    flagged = {k: sorted(c for c, r in res["_all_rows"].items() if k in r["warnings"]) for k in ("處置股", "注意股", "管理股票")}
    rep.p(f"- 依 M4-Q7 裁定（處置：公告日 ≤ 基準日 {as_of} ≤ 迄日；注意：公告日 = 基準日）標示：{flagged}")


def jump_data(rep: Report, inp, res) -> None:
    rep.h("七、M3-Q3 跳空異常門檻實測")
    members = set(res["_all_rows"])
    daily = inp.daily[inp.daily["code"].isin(members)]
    j = close_jumps(daily, inp.events, min_move=0.30)
    j["asset_type"] = [res["_all_rows"][c]["asset_type"] for c in j["code"]]
    rep.p(f"- 掃描 {daily['code'].nunique():,} 檔、{len(daily):,} 列；相鄰有效日收盤或開盤變動 ≥ 30% 者 {len(j)} 筆"
          f"（有官方事件 {int(j['has_event'].sum())}、無事件 {int((~j['has_event']).sum())}）")
    rep.p()
    cands = {
        "A｜第 9 節：收盤跌 > 35% 或漲 > 50%（M4-Q1 採用）": (j["close_ratio"] < 0.65) | (j["close_ratio"] > 1.5),
        "B｜第 3.3 節：收盤跌 > 40%": j["close_ratio"] < 0.60,
        "C｜開盤跌 > 35% 或漲 > 50%": (j["open_ratio"] < 0.65) | (j["open_ratio"] > 1.5),
        "D｜收盤跌 > 30% 或漲 > 30%": (j["close_ratio"] < 0.70) | (j["close_ratio"] > 1.30),
    }
    rows = []
    for name, m in cands.items():
        sub = j[m]
        ne = sub[~sub["has_event"]]
        rows.append([name, len(sub), int(sub["has_event"].sum()), len(ne),
                     int((ne["halted_trading_days"] > 0).sum()), int((ne["nth_valid_day"] <= 5).sum()),
                     dict(Counter(ne["market"])), dict(Counter(ne["asset_type"]))])
    rep.table(["候選門檻", "觸發", "有事件", "無事件（會發警示）", "其中停牌後恢復", "其中回溯期第 ≤5 個有效日",
               "無事件依市場", "無事件依類型"], rows)
    ne = j[~j["has_event"]].sort_values(["date", "code"])
    rep.p()
    rep.p("- 無事件且收盤或開盤變動 ≥ 30% 明細（最多 80 筆）：")
    rep.p()
    rep.table(list(ne.columns), ne.head(80).values.tolist())
    flagged = sorted(c for c, r in res["_all_rows"].items() if r["unadjusted_jump_dates"])
    rep.check("跳空異常警示（M4-Q1：收盤跌 > 35% 或漲 > 50% 且查無事件）", None,
              f"標示 {len(flagged)} 檔：" + "；".join(f"{c} {res['_all_rows'][c]['unadjusted_jump_dates']}" for c in flagged))
    rep.data["jumps"] = j.to_dict(orient="records")


def reconcile_data(rep: Report, inp, res) -> None:
    rep.h("八、事件對帳（P_before 對前一有效日收盤，取代 10% 推定門檻）")
    members = set(res["_all_rows"])
    daily = inp.daily[inp.daily["code"].isin(members)]
    evs = [e for e in inp.events if e.code in members]
    r = reconcile_events(daily, evs, inp.history_start, res["as_of_date"])
    if r.empty:
        rep.p("回溯期間無屬本系統標的之事件。")
        return
    piv = r.groupby(["market", "kind", "status"]).size().unstack(fill_value=0)
    rep.table(["市場／類型"] + list(piv.columns), [[f"{m}／{k}"] + list(v) for (m, k), v in piv.iterrows()])
    mm = r[r["status"] == "mismatch"]
    rep.p()
    rep.p(f"- 不符 {len(mm)} 筆（最多 60 筆）；每筆附前一有效日至事件日之間的原始日 K（含無效日）：")
    rep.p()
    rep.table(list(mm.columns), mm.head(60).values.tolist())
    for m in mm.head(20).itertuples():
        raw = inp.daily[(inp.daily["code"] == m.code) & (inp.daily["date"] >= m.prev_date)
                        & (inp.daily["date"] <= m.event_date)].sort_values("date")
        rep.p()
        rep.p(f"  - {m.code} {m.prev_date}～{m.event_date} 原始列：")
        rep.p()
        cols = ["date", "open", "high", "low", "close", "volume_shares"]
        rep.table(cols, raw[cols].values.tolist())
    total = (r["status"].isin(["match", "mismatch"])).sum()
    rep.check("事件對帳：P_before 等於事件日前最後有效收盤（M4-Q1b，容差 0.005）", len(mm) == 0,
              f"可比對 {int(total)} 筆，相符 {int((r['status'] == 'match').sum())}、不符 {len(mm)}；"
              f"無前一日 {int((r['status'] == 'no_prior_day').sum())}")
    rep.data["reconcile"] = r.to_dict(orient="records")


# ---------------------------------------------------------------- 主程式

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dry_run_report")
    ap.add_argument("--as-of", default=os.environ.get("DRY_RUN_AS_OF", ""))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cfg, _opts = load_config(env={})
    ds = cfg["data_source"]
    client = HttpClient(delay_sec=float(ds["request_delay_sec"]), timeout_sec=float(ds["timeout_sec"]),
                        max_retries=int(ds["max_retries"]))
    today = today_taipei()
    end = parse_iso_date(args.as_of) if args.as_of.strip() else today
    rep = Report()
    timings: dict[str, float] = {}
    t0 = time.monotonic()
    n = [0]

    def progress(day) -> None:
        n[0] += 1
        if n[0] % 25 == 0:
            print(f"[{time.monotonic() - t0:.0f}s] 已處理 {n[0]} 個平日（{day.trade_date} {day.status}）", flush=True)

    try:
        inp = collect_inputs(client, cfg, end, today, progress=progress, finmind_token=get_finmind_token())
    except Exception as exc:  # noqa: BLE001
        (out / "report.md").write_text(f"# M4 試跑失敗\n\n資料蒐集失敗：{type(exc).__name__}: {exc}\n", encoding="utf-8")
        print(f"資料蒐集失敗：{exc}", file=sys.stderr)
        return 1
    timings["collect"] = round(time.monotonic() - t0, 1)
    t1 = time.monotonic()
    res = screen(inp, cfg)
    timings["screen"] = round(time.monotonic() - t1, 1)
    timings["requests"] = len(client.logs)

    acceptance(rep, inp, res, cfg)
    overview(rep, inp, res, timings)
    tab_samples(rep, res)
    sector_data(rep, inp, res)
    etf_data(rep, res)
    alerts_data(rep, inp, res)
    jump_data(rep, inp, res)
    reconcile_data(rep, inp, res)

    meta = [f"- 產生時間：{format_taipei_iso(now_taipei())}（Asia/Taipei）",
            f"- 請求終點：{end}；基準日：{res['as_of_date']}；請求 {len(client.logs)} 次",
            "- 本報告為試跑結果，不構成任何買賣建議。"]
    (out / "report.md").write_text(rep.render(meta), encoding="utf-8")
    dump = {k: v for k, v in res.items() if not k.startswith("_")}
    dump["universe_stats"] = res["_universe_stats"]
    dump["extra"] = rep.data
    (out / "result.json").write_text(json.dumps(dump, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            # Step Summary 上限 1 MiB（位元組）；中文 3 位元組／字，依位元組截斷
            fh.write(rep.render(meta).encode("utf-8")[:1_000_000].decode("utf-8", errors="ignore"))
    print(f"完成：{out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
