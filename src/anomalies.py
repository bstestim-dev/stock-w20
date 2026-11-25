"""還原資料的稽核：跳空異常警示與事件對帳。

1. 「疑似未還原跳空異常」（2026-11-25 裁定 M4-Q1＝A，依第 9 節；第 3.3 節「跌幅 > 40%」不採用）：
   相鄰兩個有效交易日的**原始收盤價**比值 < 0.65（跌幅 > 35%）或 > 1.5（漲幅 > 50%），
   且兩日之間（不含前一日、含當日）查無該代號任何官方還原事件 → 在 warnings 標註
   「疑似未還原跳空異常，請人工看圖複核」。掃描範圍為本次抓取的整段回溯期間；停牌後恢復、新上市初期不另排除。
   ``close_jumps`` 另附開盤比、停牌日數、第幾個有效日等欄位，僅供試跑報告分析。
2. 事件對帳（2026-11-25 裁定 M4-Q1b，取代 M3 的「還原後漲跌 ≤ 10%」推定門檻）：
   每筆事件的 P_before（除權息前收盤價／停止買賣前收盤價格／最後交易日之收盤價格）應等於該檔
   事件日前最後一個有效交易日的原始收盤價，容差 0.005 元。用於 verify-api 與全量試跑的驗收核對。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import numpy as np
import pandas as pd

from src.fetchers.parsing import CorporateEvent

PRICE_TOLERANCE = 0.005      # 官方價格至小數 2 位（M4-Q1b）
JUMP_DROP = 0.35             # 單日收盤跌幅 > 35%（M4-Q1）
JUMP_RISE = 0.50             # 單日收盤漲幅 > 50%（M4-Q1）
NOTE_UNADJUSTED_JUMP = "疑似未還原跳空異常，請人工看圖複核"   # 第 9 節原文


def _valid(daily: pd.DataFrame) -> pd.DataFrame:
    ok = pd.Series(True, index=daily.index)
    for col in ("open", "high", "low", "close"):
        v = pd.to_numeric(daily[col], errors="coerce")
        ok &= v.notna() & (v > 0)
    return daily[ok]


def close_jumps(daily: pd.DataFrame, events: Iterable[CorporateEvent], min_move: float = 0.30) -> pd.DataFrame:
    """回傳 |收盤比 − 1| 或 |開盤比 − 1| ≥ ``min_move`` 的列（預先放寬，供比較多組門檻）。"""
    v = _valid(daily).sort_values(["code", "date"]).copy()
    trading = {m: sorted(g["date"].unique()) for m, g in daily.groupby("market")}
    pos_of = {m: {d: i for i, d in enumerate(ds)} for m, ds in trading.items()}
    ev_by_code: dict[str, list[date]] = {}
    for e in events:
        ev_by_code.setdefault(e.code, []).append(e.event_date)

    v["prev_date"] = v.groupby("code")["date"].shift(1)
    v["prev_close"] = v.groupby("code")["close"].shift(1)
    v["nth_valid_day"] = v.groupby("code").cumcount() + 1
    v = v[v["prev_close"].notna()].copy()
    v["close_ratio"] = v["close"].astype(float) / v["prev_close"].astype(float)
    v["open_ratio"] = v["open"].astype(float) / v["prev_close"].astype(float)
    hit = v[(np.abs(v["close_ratio"] - 1) >= min_move) | (np.abs(v["open_ratio"] - 1) >= min_move)].copy()

    def has_event(r) -> bool:
        return any(r.prev_date < d <= r.date for d in ev_by_code.get(r.code, []))

    def gap(r) -> int:
        p = pos_of.get(r.market, {})
        return (p.get(r.date, 0) - p.get(r.prev_date, 0) - 1) if r.date in p and r.prev_date in p else -1

    hit["has_event"] = [has_event(r) for r in hit.itertuples()]
    hit["halted_trading_days"] = [gap(r) for r in hit.itertuples()]
    cols = ["code", "name", "market", "date", "prev_date", "prev_close", "open", "close", "close_ratio", "open_ratio",
            "has_event", "halted_trading_days", "nth_valid_day"]
    return hit[cols].reset_index(drop=True)


def unadjusted_jumps(daily: pd.DataFrame, events: Iterable[CorporateEvent]) -> dict[str, list[date]]:
    """{代號: [觸發日…]}：收盤比 < 1 − 35% 或 > 1 + 50%，且兩日之間查無官方事件（見模組說明）。"""
    j = close_jumps(daily, events, min_move=min(JUMP_DROP, JUMP_RISE))
    hit = j[((j["close_ratio"] < 1 - JUMP_DROP) | (j["close_ratio"] > 1 + JUMP_RISE)) & ~j["has_event"]]
    out: dict[str, list[date]] = {}
    for r in hit.sort_values(["code", "date"]).itertuples():
        out.setdefault(r.code, []).append(r.date)
    return out


def reconcile_events(daily: pd.DataFrame, events: Iterable[CorporateEvent], start: date, end: date) -> pd.DataFrame:
    """每筆 (start, end] 內事件：P_before 與事件日前最後有效收盤比對。

    status：match／mismatch／no_prior_day（回溯期內事件日前無有效交易日）／code_not_in_data。
    """
    v = _valid(daily)
    by_code = {c: g.sort_values("date")[["date", "close"]] for c, g in v.groupby("code")}
    rows = []
    for e in events:
        if not (start < e.event_date <= end):
            continue
        g = by_code.get(e.code)
        rec = {"market": e.market, "code": e.code, "name": e.name, "kind": e.kind, "event_date": e.event_date,
               "p_before": e.p_before, "p_ref": e.p_ref, "prev_date": None, "prev_close": None, "diff": None}
        if g is None:
            rec["status"] = "code_not_in_data"
        else:
            before = g[g["date"] < e.event_date]
            if before.empty:
                rec["status"] = "no_prior_day"
            else:
                last = before.iloc[-1]
                rec["prev_date"], rec["prev_close"] = last["date"], float(last["close"])
                rec["diff"] = None if e.p_before is None else float(e.p_before) - rec["prev_close"]
                rec["status"] = ("match" if rec["diff"] is not None and abs(rec["diff"]) <= PRICE_TOLERANCE
                                 else "mismatch")
        rows.append(rec)
    return pd.DataFrame(rows, columns=["market", "code", "name", "kind", "event_date", "p_before", "p_ref",
                                       "prev_date", "prev_close", "diff", "status"])
