"""加分評分與型態標籤（spec_v1 第 4.4 節；不硬剔除，FR-1.6）。

以週 K 序列（``indicators.add_weekly_series`` 結果，最後一列為本週 t）計算：
- S1 爆量強度：VolRatio_20w ≥ 2.0 得 25；1.5 ≤ x < 2.0 得 15；否則 0（Q3：S1 固定 1.5／2.0）。
- S2 週 20MA 走平或翻揚：W20MA[t] > W20MA[t-1] 得 15；≥ W20MA[t-1] × 0.998 得 8；否則 0。
- S3 趨勢線／區間突破：13 週高點突破 10 分；下降切線突破（或突破 26 週高點，Q5）10 分。
  切線兩高點間隔 Δt ≥ ``trendline_min_pivot_gap_weeks``，不合格時依序改試更早的高點組合（S3 裁定）。
- S4 W 底：最近兩擺盪低點 L2 ≥ L1 × 0.99 且站上頸線 20 分；僅底底高 8 分（Q5）。
- S5 日 20MA 上穿週 20MA：10 分（穿越判定見 ``indicators._d20_cross_w20``）。
- S6 族群共振：由 ``screener`` 跨標的計算（同業 N 檔 ≥ 2 得 10 分）。
- 盤整警示：最近 12 週週收盤 (max − min) / min < 0.08 → box_consolidation（不扣分）。

【暫定，待裁定】（見 docs/decisions.md「待裁定（尚未生效）」）：
- M4-Q2 切線改試順序：由最近的高點往回當 H2，每個 H2 再由近而遠找 H1，取第一組 Δt 合格且 H2 < H1；價格線性外推。
- M4-Q3 擺盪點視窗：候選點落在 [t-26, t-1]；右側鄰週須 ≤ t-1（同 S4 裁定），左側鄰週可早於 t-26。
- M4-Q11 盤整警示「最近 12 週」含本週 t。
- M4-Q12 signals.w_bottom 僅在 S4 得 20 分時為 true（w20_slope_up 的對應在 screener）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

S1_HIGH, S1_LOW = 2.0, 1.5
S2_FLAT_RATIO = 0.998
BREAKOUT_SHORT_WEEKS = 13
PATTERN_WINDOW_WEEKS = 26          # [t-26, t-1]
PATTERN_MIN_BARS = PATTERN_WINDOW_WEEKS + 1   # Q1 裁定：S3／S4 需 27 根週 K
W_BOTTOM_TOLERANCE = 0.99
W_BOTTOM_MIN_GAP = 3
BOX_WEEKS = 12
BOX_RANGE = 0.08
NOTE_BOX = "箱型橫盤中，宜確認突破有效性"
NOTE_STEEP = "切線偏陡"


@dataclass
class Trendline:
    t_h1: int
    h1: float
    t_h2: int
    h2: float
    value_at_t: float
    log_rate: float


def s1_points(vol_ratio: float | None) -> int:
    if vol_ratio is None:
        return 0
    if vol_ratio >= S1_HIGH:
        return 25
    if vol_ratio >= S1_LOW:
        return 15
    return 0


def s2_points(w20ma: float | None, w20ma_prev: float | None) -> int:
    if w20ma is None or w20ma_prev is None:
        return 0
    if w20ma > w20ma_prev:
        return 15
    if w20ma >= w20ma_prev * S2_FLAT_RATIO:
        return 8
    return 0


def swing_points(values: list[float], t: int, k: int, window: int, kind: str) -> list[int]:
    """[t-window, t-1] 內的嚴格擺盪高（kind="high"）或低點索引；左右各 k 根須存在且右側 ≤ t-1。"""
    out = []
    for i in range(max(t - window, k), t - k):
        v = values[i]
        neigh = values[i - k:i] + values[i + 1:i + k + 1]
        if kind == "high" and all(v > x for x in neigh):
            out.append(i)
        elif kind == "low" and all(v < x for x in neigh):
            out.append(i)
    return out


def find_trendline(highs: list[float], t: int, k: int, min_gap: int) -> Trendline | None:
    piv = swing_points(highs, t, k, PATTERN_WINDOW_WEEKS, "high")
    for j in range(len(piv) - 1, 0, -1):          # H2：由最近往回
        i2 = piv[j]
        for m in range(j - 1, -1, -1):            # H1：由近而遠
            i1 = piv[m]
            if i2 - i1 >= min_gap and highs[i2] < highs[i1]:
                slope = (highs[i2] - highs[i1]) / (i2 - i1)
                value = highs[i2] + slope * (t - i2)
                rate = (math.log(highs[i1]) - math.log(highs[i2])) / (i2 - i1)
                return Trendline(i1, highs[i1], i2, highs[i2], value, rate)
    return None


def evaluate_patterns(series: pd.DataFrame, ind: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, Any]:
    """單一標的 S1～S5 與盤整／偏陡標記。``series`` 最後一列須為本週 t（呼叫端確認 has_current_week_bar）。"""
    scr = cfg["screening"]
    k = int(scr["pivot_k_weeks"])
    min_gap = int(scr["trendline_min_pivot_gap_weeks"])
    steep = scr.get("trendline_steep_warning_log_rate")

    out: dict[str, Any] = {
        "s1": s1_points(ind.get("vol_ratio_20w")),
        "s2": s2_points(ind.get("w20ma"), ind.get("w20ma_prev")),
        "s3": 0, "s4": 0, "s5": 10 if ind.get("d20_cross_w20") else 0,
        "breakout_13w": False, "trendline_break": False, "trendline": None,
        "w_bottom": False, "w_bottom_partial": False, "box_consolidation": False, "notes": [],
    }
    n = len(series)
    t = n - 1
    highs = series["high"].astype("float64").tolist()
    lows = series["low"].astype("float64").tolist()
    closes = series["close"].astype("float64").tolist()
    close_t = closes[t] if n else None

    if n >= BREAKOUT_SHORT_WEEKS + 1:
        out["breakout_13w"] = close_t > max(highs[t - BREAKOUT_SHORT_WEEKS:t])
    if n >= PATTERN_MIN_BARS:
        line = find_trendline(highs, t, k, min_gap)
        high26 = max(highs[t - PATTERN_WINDOW_WEEKS:t])
        out["trendline_break"] = (line is not None and close_t > line.value_at_t) or close_t > high26
        if line is not None:
            out["trendline"] = {"t_h1": line.t_h1 - t, "h1": line.h1, "t_h2": line.t_h2 - t, "h2": line.h2,
                                "value_at_t": line.value_at_t, "log_rate_per_week": line.log_rate}
            if steep is not None and line.log_rate > float(steep):
                out["notes"].append(NOTE_STEEP)

        piv = swing_points(lows, t, k, PATTERN_WINDOW_WEEKS, "low")
        if len(piv) >= 2:
            i1, i2 = piv[-2], piv[-1]
            if i2 - i1 >= W_BOTTOM_MIN_GAP and lows[i2] >= lows[i1] * W_BOTTOM_TOLERANCE:
                neckline = max(highs[i1:i2 + 1])
                if close_t > neckline:
                    out["s4"], out["w_bottom"] = 20, True
                else:
                    out["s4"], out["w_bottom_partial"] = 8, True
    out["s3"] = (10 if out["breakout_13w"] else 0) + (10 if out["trendline_break"] else 0)

    if n >= BOX_WEEKS:
        last = closes[t - BOX_WEEKS + 1:t + 1]
        lo = min(last)
        out["box_consolidation"] = lo > 0 and (max(last) - lo) / lo < BOX_RANGE
    return out
