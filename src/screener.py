"""第一階段選股引擎（spec_v1 第 4.1～4.6 節；M4）。

``collect_inputs`` 抓取所需官方資料（僅用第 3 節已驗證端點），``screen`` 為純計算：
標的過濾（U1／U2）→ 還原價 → 真正週 K 指標 → 頁 1／頁 2（互斥）→ ETF 專頁 → 大盤紅綠燈（不過濾）
→ 出場與週中警戒 → S1～S6 評分（不硬剔除）。輸出為 Python 結構，欄位鍵名對齊第 6.3 節；
序列化（latest.json／CSV）於 M5 實作。

【暫定，待裁定】（見 docs/decisions.md「待裁定（尚未生效）」）：
- M4-Q4 族群（S6）分組依官方產業別代碼（上市 ``產業別``、上櫃 ``SecuritiesIndustryCode`` 視為同一代碼表）。
- M4-Q5 ETF 專頁收錄全部 ETF（第 4.1B 節「MUST 收錄全部上市櫃 ETF」），每檔以 ``etf_zone`` 標示
  "new_cross"／"above_and_spike"／None 供前端分區或篩選（第 4.3B 節「或透過狀態篩選切換」）；
  ``etf_signal_count`` 只計兩區內者。
- M4-Q7 處置股：公告日 ≤ 基準日 ≤ 處置期間迄日；注意股：公告日 = 基準日。
- M4-Q8 乖離警示文字採第 4.3C 節「距週 20MA 較遠」；出場說明採 FR-1.7 與第 4.6 節文字。
- M4-Q10 本週無週 K（全週停牌或缺週）者不給出場狀態（exit_status = None）；t-1 取前一根有效週 K。
- M4-Q9 FinMind 斷線時 is_tib 一律 false（見 universe）。
- M4-Q12 w20_slope_up 僅在 S2 得 15 分時為 true；同分依代號遞增。
- M4-Q1（原 M3-Q3）第 9 節「疑似未還原跳空異常」門檻待裁定，本里程碑不輸出該警示（稽核工具見 anomalies）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from src.adjustments import apply_plan, build_plan, codes_with_share_structure_change
from src.fetchers.parsing import CorporateEvent
from src.indicators import compute_all, index_indicators
from src.market import market_block
from src.patterns import NOTE_BOX, evaluate_patterns
from src.timezone import parse_roc_date, parse_roc_period
from src.trading_calendar import WeekStatus
from src.universe import Security, Universe, build_universe

LOTS_DAYS_PER_WEEK = 5                 # U2：前 20 週平均週量 ÷ 5
S6_MIN_PEERS = 2
S6_POINTS = 10
SECTOR_LEADER_TOP = 2

WARN_DISPOSITION = "處置股"
WARN_NOTICE = "注意股"
WARN_MANAGED = "管理股票"              # D3
WARN_SHARE_STRUCTURE = "近45週曾減資/分割"   # 第 6.3 節範例；M3-Q4 45 週
WARN_BIAS = "距週 20MA 較遠"            # 第 4.3C 節（M4-Q8 暫定）

EXIT_NOTES = {
    "EXIT_CONFIRMED": "跌破週 20MA：建議下週一開盤賣",
    "MIDWEEK_WARNING": "週中收盤低於暫定週 20MA，待週五收盤確認，第一階段不構成賣訊",
    "HOLD_ABOVE": "站穩週 20MA 之上",
    "BELOW_W20": "前週已跌破、持續位於週 20MA 之下",
}


@dataclass
class ScreenInputs:
    daily: pd.DataFrame                       # history_to_frames 日 K 長表（原始價）
    index_daily: pd.DataFrame                 # TAIEX 日收盤
    events: list[CorporateEvent]
    week: WeekStatus
    history_start: date
    twse_basics: list[dict[str, Any]]
    tpex_basics: list[dict[str, Any]]
    finmind_rows: dict[str, list[dict[str, Any]]] | None
    twse_disposition: list[dict[str, Any]] | None = None
    twse_notice: list[dict[str, Any]] | None = None
    tpex_disposition: list[dict[str, Any]] | None = None
    tpex_warning: list[dict[str, Any]] | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 處置／注意

def alert_codes(inp: ScreenInputs, as_of: date) -> tuple[set[str], set[str], list[str]]:
    """回傳（處置股代號、注意股代號、說明）。無法解析的列略過並記錄。"""
    notes: list[str] = []
    disp: set[str] = set()
    notice: set[str] = set()
    for label, rows, k_code in (("TWSE 處置", inp.twse_disposition, "Code"),
                                ("TPEx 處置", inp.tpex_disposition, "SecuritiesCompanyCode")):
        if rows is None:
            notes.append(f"{label}資料不可用，未標示處置股")
            continue
        bad = 0
        for r in rows:
            try:
                announced = parse_roc_date(str(r["Date"]))
                _start, end = parse_roc_period(str(r["DispositionPeriod"]))
            except (KeyError, ValueError):
                bad += 1
                continue
            if announced <= as_of <= end:
                disp.add(str(r[k_code]).strip())
        if bad:
            notes.append(f"{label}有 {bad} 列日期無法解析，已略過")
    for label, rows, k_code in (("TWSE 注意", inp.twse_notice, "Code"),
                                ("TPEx 注意", inp.tpex_warning, "SecuritiesCompanyCode")):
        if rows is None:
            notes.append(f"{label}資料不可用，未標示注意股")
            continue
        bad = 0
        for r in rows:
            code = str(r.get(k_code, "")).strip()
            if not code:
                continue
            try:
                announced = parse_roc_date(str(r["Date"]))
            except (KeyError, ValueError):
                bad += 1
                continue
            if announced == as_of:
                notice.add(code)
        if bad:
            notes.append(f"{label}有 {bad} 列日期無法解析，已略過")
    return disp, notice, notes


# ---------------------------------------------------------------- 單檔判定

def exit_status(ind: Mapping[str, Any], settled: bool) -> str | None:
    cw, w, cwp, wp = ind.get("close_w"), ind.get("w20ma"), ind.get("close_w_prev"), ind.get("w20ma_prev")
    if not ind.get("has_current_week_bar") or None in (cw, w, cwp, wp):
        return None
    if cw >= w:
        return "HOLD_ABOVE"
    if cwp >= wp:
        return "EXIT_CONFIRMED" if settled else "MIDWEEK_WARNING"
    return "BELOW_W20"


def page_of(ind: Mapping[str, Any], spike_ratio: float) -> str | None:
    """"new_cross"（頁 1）、"above_and_spike"（頁 2）或 None；兩者條件互斥（t-1 是否站上）。"""
    cw, w, cwp, wp = ind.get("close_w"), ind.get("w20ma"), ind.get("close_w_prev"), ind.get("w20ma_prev")
    if not ind.get("has_current_week_bar") or None in (cw, w, cwp, wp) or not cw > w:
        return None
    if cwp <= wp:
        return "new_cross"
    ratio = ind.get("vol_ratio_20w")
    if ratio is not None and ratio >= spike_ratio:
        return "above_and_spike"
    return None


def passes_liquidity(ind: Mapping[str, Any], min_avg_daily_lots: float) -> bool:
    if min_avg_daily_lots <= 0:
        return True
    v = ind.get("vol_avg_20w_lots")
    return v is not None and v / LOTS_DAYS_PER_WEEK >= min_avg_daily_lots


def _market_cap_billion(sec: Security, close_raw: float | None) -> float | None:
    if sec.issued_shares is None or close_raw is None:
        return None
    return close_raw * sec.issued_shares / 1e8


# ---------------------------------------------------------------- 主流程

def screen(inp: ScreenInputs, cfg: Mapping[str, Any], force_light: str | None = None) -> dict[str, Any]:
    uni_cfg, scr = cfg["universe"], cfg["screening"]
    min_capital = float(uni_cfg["min_paid_in_capital_ntd"])
    min_lots = float(uni_cfg["min_avg_daily_lots_20w"])
    spike_ratio = float(scr["volume_spike_ratio"])
    bias_warn = float(scr["bias_warning_pct"])
    week = inp.week
    as_of = week.as_of

    daily = inp.daily[inp.daily["date"] <= as_of]
    latest = daily.sort_values("date").groupby("code").tail(1)
    quotes_latest = {r.code: {"market": r.market, "name": r.name} for r in latest.itertuples()}
    uni: Universe = build_universe(inp.twse_basics, inp.tpex_basics, quotes_latest, inp.finmind_rows)
    members: dict[str, Security] = {**uni.commons, **uni.etfs}

    plan = build_plan(inp.events, inp.history_start, as_of)
    adj = apply_plan(daily[daily["code"].isin(members)].reset_index(drop=True), plan)
    inds = compute_all(adj, week, cfg, plan.conflicts)
    share_change = codes_with_share_structure_change(plan, as_of)
    disp, notice, alert_notes = alert_codes(inp, as_of)
    market = market_block(index_indicators(inp.index_daily, as_of, cfg), force_light=force_light)

    def ind_of(code: str) -> dict[str, Any]:
        return inds.get(code) or {"has_current_week_bar": False, "notes": [], "_series": None}

    # ---- U1／U2
    u1 = {c for c, s in uni.commons.items() if s.capital_ntd is not None and s.capital_ntd > min_capital}
    u2 = {c for c in u1 if passes_liquidity(ind_of(c), min_lots)}

    # ---- S6 族群（同業 N 檔含自身：股本 > 20 億、本週 Close_w > W20MA）
    peers: dict[str, int] = defaultdict(int)
    caps: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for c in u1:
        sec, ind = uni.commons[c], ind_of(c)
        key = sec.official_industry_code
        if key is None:
            continue
        if ind.get("has_current_week_bar") and ind.get("w20ma") is not None and ind["close_w"] > ind["w20ma"]:
            peers[key] += 1
        mc = _market_cap_billion(sec, ind.get("close_raw"))
        if mc is not None:
            caps[key].append((mc, c))
    leaders = {c for key, lst in caps.items() for _mc, c in sorted(lst, key=lambda x: (-x[0], x[1]))[:SECTOR_LEADER_TOP]}

    # ---- 逐檔
    def build_row(code: str) -> dict[str, Any]:
        sec, ind = members[code], ind_of(code)
        is_common = sec.asset_type == "common_stock"
        pat = (evaluate_patterns(ind["_series"], ind, cfg)
               if ind.get("has_current_week_bar") and ind.get("_series") is not None else None)
        key = sec.official_industry_code if is_common else None
        above = bool(ind.get("has_current_week_bar") and ind.get("w20ma") is not None and ind["close_w"] > ind["w20ma"])
        n_peers = peers.get(key, 0) if (is_common and key is not None and above) else 0
        s6 = S6_POINTS if n_peers >= S6_MIN_PEERS else 0
        scores = {k: (pat[k] if pat else 0) for k in ("s1", "s2", "s3", "s4", "s5")}
        scores["s6"] = s6
        status = exit_status(ind, week.is_settled)

        warnings: list[str] = []
        if code in disp:
            warnings.append(WARN_DISPOSITION)
        if code in notice:
            warnings.append(WARN_NOTICE)
        if ind.get("managed"):
            warnings.append(WARN_MANAGED)
        if code in share_change:
            warnings.append(WARN_SHARE_STRUCTURE)
        if ind.get("bias_20w_pct") is not None and ind["bias_20w_pct"] > bias_warn:
            warnings.append(WARN_BIAS)
        warnings += list(ind.get("notes", []))
        if pat:
            warnings += pat["notes"]

        vavg = ind.get("vol_avg_20w_lots")
        return {
            "stock_id": code,
            "stock_name": sec.name,
            "market": sec.market,
            "asset_type": sec.asset_type,
            "is_tib": bool(sec.is_tib) if is_common else False,
            "etf_subtype": sec.etf_subtype,
            "industry": sec.industry if is_common else ("／".join(sec.finmind_categories) or None),
            "capital_billion_ntd": (sec.capital_ntd / 1e8) if is_common and sec.capital_ntd is not None else None,
            "market_cap_billion_ntd": _market_cap_billion(sec, ind.get("close_raw")) if is_common else None,
            "close_raw": ind.get("close_raw"),
            "close_adj": ind.get("close_adj"),
            "w20ma": ind.get("w20ma"),
            "d100ma": ind.get("d100ma"),
            "d20ma": ind.get("d20ma"),
            "ma_diff": ind.get("ma_diff"),
            "bias_20w_pct": ind.get("bias_20w_pct"),
            "vol_week_lots": ind.get("vol_week_lots"),
            "vol_avg_20w_lots": vavg,
            "avg_daily_lots_20w": None if vavg is None else vavg / LOTS_DAYS_PER_WEEK,
            "vol_ratio_20w": ind.get("vol_ratio_20w"),
            "vol_ratio_20w_projected": ind.get("vol_ratio_20w_projected"),
            "vol_ratio_prev_week": ind.get("vol_ratio_prev_week"),
            "is_volume_spike": bool(ind.get("is_volume_spike")),
            "score": int(sum(scores.values())),
            "score_detail": scores,
            "signals": {
                "w20_slope_up": scores["s2"] == 15,
                "d20_cross_w20": bool(ind.get("d20_cross_w20")),
                "breakout_13w": bool(pat and pat["breakout_13w"]),
                "trendline_break": bool(pat and pat["trendline_break"]),
                "w_bottom": bool(pat and pat["w_bottom"]),
                "box_consolidation": bool(pat and pat["box_consolidation"]),
                "sector_peers_count": n_peers,
                "sector_leader": is_common and code in leaders,
            },
            "sector_label": f"族群共振（同業 {n_peers} 檔）" if s6 else None,
            "box_label": NOTE_BOX if pat and pat["box_consolidation"] else None,
            "trendline": pat["trendline"] if pat else None,
            "exit_status": status,
            "exit_action_note": EXIT_NOTES.get(status) if status else None,
            "warnings": warnings,
            "has_share_split_or_reduction": code in share_change,
            "adjustment_conflicts": list(ind.get("adjustment_conflicts", [])),
            "fundamentals_ref": None,           # M5 實作
            "_page": page_of(ind, spike_ratio),
        }

    def order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda r: (-r["score"], r["stock_id"]))

    common_rows = {c: build_row(c) for c in sorted(u1)}
    etf_rows = {c: build_row(c) for c in sorted(uni.etfs)}

    new_cross = order([r for c, r in common_rows.items() if c in u2 and r["_page"] == "new_cross"])
    above_spike = order([r for c, r in common_rows.items() if c in u2 and r["_page"] == "above_and_spike"])
    etf_tab = order([{**r, "etf_zone": r["_page"]} for r in etf_rows.values()])
    exit_rows = order([r for r in list(common_rows.values()) + list(etf_rows.values())
                       if r["exit_status"] in ("EXIT_CONFIRMED", "MIDWEEK_WARNING")])

    listed = {r["stock_id"] for tab in (new_cross, above_spike, etf_tab, exit_rows) for r in tab}  # ETF 全收錄
    charts = {}
    for code in sorted(listed):
        s = ind_of(code).get("_series")
        if s is None:
            continue
        charts[code] = [{
            "week_end_date": b.week_end_date, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume_lots": b.volume_lots,
            "w20ma": None if pd.isna(b.w20ma) else float(b.w20ma),
            "vavg_20w_lots": None if pd.isna(b.vavg) else float(b.vavg),
        } for b in s.itertuples()]

    strip = lambda rows: [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]  # noqa: E731
    return {
        "as_of_date": as_of,
        "week_id": week.week_id,
        "is_week_settled": week.is_settled,
        "trading_days_in_current_week": week.trading_days_elapsed,
        "price_mode": "backward_adjusted_official",
        "params": {
            "min_capital_ntd": int(min_capital),
            "min_avg_daily_lots_20w": min_lots,
            "volume_spike_ratio": spike_ratio,
            "ma_weeks": int(scr["ma_weeks"]),
            "require_volume_spike_on_new_cross": bool(scr["require_volume_spike_on_new_cross"]),
            "include_tib": bool(uni_cfg["include_tib"]),
        },
        "market": market,
        "summary": {
            "total_common_stocks_scanned": len(uni.commons),
            "passed_capital_gt_20b": len(u1),
            "passed_liquidity": len(u2),
            "new_cross_count": len(new_cross),
            "above_and_spike_count": len(above_spike),
            "etf_signal_count": sum(r["etf_zone"] is not None for r in etf_tab),
            "exit_confirmed_count": sum(r["exit_status"] == "EXIT_CONFIRMED" for r in exit_rows),
            "midweek_warning_count": sum(r["exit_status"] == "MIDWEEK_WARNING" for r in exit_rows),
        },
        "tabs": {
            "new_cross": strip(new_cross),
            "above_and_spike": strip(above_spike),
            "etf": strip(etf_tab),
            "exit_alerts": strip(exit_rows),
        },
        "weekly_charts": charts,
        "notes": list(inp.notes) + uni.notes + alert_notes,
        "_universe_stats": uni.stats,
        "_all_rows": {**common_rows, **etf_rows},
        "_plan": plan,
        "_adjusted": adj,
    }


# ---------------------------------------------------------------- 資料蒐集

def collect_inputs(client, cfg: Mapping[str, Any], requested_end: date, today: date,
                   cache=None, progress=None, finmind_token: str | None = None) -> ScreenInputs:
    """依第 3 節已驗證端點抓取 ``screen`` 所需資料。行情、事件、基本資料失敗即拋錯（不發佈錯誤報告）；
    FinMind、處置／注意資料失敗則降級並記入 notes。"""
    from src.fetchers import finmind, history, tpex, twse
    from src.trading_calendar import week_status
    from src.timezone import week_weekdays

    notes: list[str] = []
    hist = history.fetch_daily_history(client, requested_end, int(cfg["data_source"]["lookback_weeks"]),
                                       cache=cache, today=today, progress=progress)
    notes += hist.notes
    if hist.missing:
        notes.append("缺漏交易日：" + "；".join(f"{d} {r}" for d, r in hist.missing))
    daily, idx = history.history_to_frames(hist)
    as_of = hist.as_of_date

    events = twse.fetch_all_events(client, hist.start, as_of) + tpex.fetch_all_events(client, hist.start, as_of)
    twse_basics = twse.fetch_company_basics(client)
    tpex_basics = tpex.fetch_company_basics(client)

    fm = finmind.FinMindClient(client, finmind_token)
    rows, err = finmind.try_fetch_stock_info_rows(fm)
    if err:
        notes.append(err)
    wk = week_weekdays(as_of)
    fm_dates, err = finmind.try_fetch_trading_dates(fm, wk[0], wk[-1])
    if err:
        notes.append(err)
    week = week_status(hist.days, as_of, today, fm_dates)

    def attempt(label, fn):
        try:
            return fn(client)
        except Exception as exc:  # noqa: BLE001 — 警示資料失敗不中止選股
            notes.append(f"{label} 抓取失敗：{type(exc).__name__}: {exc}"[:300])
            return None

    return ScreenInputs(
        daily=daily, index_daily=idx, events=events, week=week, history_start=hist.start,
        twse_basics=twse_basics, tpex_basics=tpex_basics, finmind_rows=rows,
        twse_disposition=attempt("TWSE 處置股", twse.fetch_disposition),
        twse_notice=attempt("TWSE 注意股", twse.fetch_notice),
        tpex_disposition=attempt("TPEx 處置股", tpex.fetch_disposition),
        tpex_warning=attempt("TPEx 注意股", tpex.fetch_trading_warning),
        notes=notes,
    )
