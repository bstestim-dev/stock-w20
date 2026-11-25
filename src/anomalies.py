"""還原資料的稽核工具（供 M3-Q3「疑似未還原跳空異常」門檻裁定與還原檢驗；目前不輸出到名單警示）。

1. ``close_jumps``：逐檔相鄰兩個有效交易日的原始價比值（收盤／前收、開盤／前收），標註
   - 兩日之間（不含前一日、含當日）是否有官方還原事件；
   - 兩日之間隔了幾個「該市場有交易、但此檔無有效價」的交易日（停牌後恢復）；
   - 當日是此檔在回溯期間的第幾個有效交易日（新上市初期）。
   第 9 節規則為「單日收盤跳空跌幅 > 35% 或漲幅 > 50% 卻查無對應事件紀錄」；第 3.3 節另寫「單日跌幅 > 40%」。
2. ``reconcile_events``：事件對帳。每筆事件的 P_before（除權息前收盤價／停止買賣前收盤價格／
   最後交易日之收盤價格）應等於該檔事件日前最後一個有效交易日的原始收盤價。
   相符即代表事件與行情對得上、還原因子 P_ref／P_before 套在正確的日子；不需「還原後漲跌 ≤ 10%」這類推定門檻。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import numpy as np
import pandas as pd

from src.fetchers.parsing import CorporateEvent

PRICE_TOLERANCE = 0.005      # 官方價格至小數 2 位


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
