"""向後還原價（spec_v1 第 3.5 節）。

- 事件：除權息（TWSE TWT49U／TPEx exDailyQ）、減資（TWTAUU／revivt）、面額變更（TWTB8U）、
  ETF 分割／反分割（TWTCAU／etfRvsRslt）。單一事件因子 factor = P_ref / P_before。
- 累積係數 cum_factor(d) = ∏ factor(t_event)，取 t_event > d 之事件；基準日（最新交易日）因子為 1.0。
- Open/High/Low/Close 乘以 cum_factor；成交量不調整。
- 以代號為鍵（證券代號於兩市不重複；上櫃轉上市者事件照樣適用）。

尚待 Hector 裁定、目前採保守處理（不輸出可能錯誤的還原價）：
- 同一代號同日有兩筆以上「不同」事件（規格 3.5 第 2 點僅原則性說明取最終參考價，未指明欄位）。
- 事件價格不符 P_before > 0 且 P_ref > 0 的防呆檢核（規格未說明不符時如何處理）。
以上兩種情況，該代號列入 ``conflicts``，其還原價與累積係數全部設為 NaN，下游指標因此無值、不進名單。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from src.fetchers.parsing import CorporateEvent

# 會造成流通股數結構性跳變、使爆量倍數可能失真的事件（spec 3.5 第 5 點）
SHARE_STRUCTURE_KINDS = frozenset({"capital_reduction", "par_value_change", "etf_split"})
PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass
class AdjustmentPlan:
    start: date
    end: date
    factors: dict[str, list[tuple[date, float, str]]] = field(default_factory=dict)  # 代號 → [(生效日, 因子, 類別)]，依日期遞增
    conflicts: dict[str, list[str]] = field(default_factory=dict)                     # 代號 → 原因
    events: dict[str, list[CorporateEvent]] = field(default_factory=dict)            # 代號 → 區間內事件（去重後）


def _event_key(ev: CorporateEvent) -> tuple:
    return (ev.kind, ev.p_before, ev.p_ref)


def build_plan(events: Iterable[CorporateEvent], start: date, end: date) -> AdjustmentPlan:
    """彙整 (start, end] 區間內的事件並計算因子。"""
    plan = AdjustmentPlan(start, end)
    grouped: dict[tuple[str, date], dict[tuple, CorporateEvent]] = defaultdict(dict)
    for ev in events:
        if start < ev.event_date <= end:
            grouped[(ev.code, ev.event_date)].setdefault(_event_key(ev), ev)  # 完全相同的事件只算一次

    for (code, d), unique in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        evs = list(unique.values())
        plan.events.setdefault(code, []).extend(evs)
        if len(evs) > 1:
            desc = "、".join(f"{e.kind}({e.p_before}→{e.p_ref})" for e in evs)
            plan.conflicts.setdefault(code, []).append(f"{d} 同日多筆事件：{desc}")
            continue
        ev = evs[0]
        if not (ev.p_before and ev.p_ref and ev.p_before > 0 and ev.p_ref > 0):
            plan.conflicts.setdefault(code, []).append(
                f"{d} {ev.kind} 價格未通過防呆檢核：P_before={ev.p_before}、P_ref={ev.p_ref}")
            continue
        plan.factors.setdefault(code, []).append((d, ev.p_ref / ev.p_before, ev.kind))
    return plan


def apply_plan(daily: pd.DataFrame, plan: AdjustmentPlan) -> pd.DataFrame:
    """回傳加上 ``cum_factor`` 與 ``open_adj``…``close_adj`` 的新表；不修改原表。

    daily 需有欄位：date（datetime.date）、code、open、high、low、close。
    """
    out = daily.copy()
    cum = np.ones(len(out), dtype="float64")
    if not out.empty:
        date_ord = np.array([d.toordinal() for d in out["date"]], dtype=np.int64)
        for code, positions in out.groupby("code").indices.items():
            if code in plan.conflicts:
                cum[positions] = np.nan
                continue
            items = plan.factors.get(code)
            if not items:
                continue
            ev_ord = np.array([d.toordinal() for d, _f, _k in items], dtype=np.int64)
            fac = np.array([f for _d, f, _k in items], dtype="float64")
            # suffix[j] = ∏_{i>=j} fac[i]；suffix[k] = 1
            suffix = np.append(np.cumprod(fac[::-1])[::-1], 1.0)
            idx = np.searchsorted(ev_ord, date_ord[positions], side="right")  # 第一個 t_event > d 的事件
            cum[positions] = suffix[idx]
    out["cum_factor"] = cum
    for col in PRICE_COLUMNS:
        out[f"{col}_adj"] = out[col].astype("float64") * out["cum_factor"]
    return out


def codes_with_share_structure_change(plan: AdjustmentPlan, since: date) -> set[str]:
    """``since`` 之後（不含）發生減資、面額變更或 ETF 分割／反分割的代號（has_share_split_or_reduction）。

    視窗起點由呼叫端決定：規格寫「近 45 週」，回溯期已改為 60 週（Q2），兩者是否一致待 Hector 裁定。
    """
    return {
        code for code, items in plan.factors.items()
        if any(kind in SHARE_STRUCTURE_KINDS and d > since for d, _f, kind in items)
    } | {
        code for code, evs in plan.events.items()
        if code in plan.conflicts and any(e.kind in SHARE_STRUCTURE_KINDS and e.event_date > since for e in evs)
    }
