"""真正週 K 聚合與指標（spec_v1 第 4.2、4.3 節；第 9 節邊界）。

- 交易週：自然週（週一～週五，以週一日期為鍵，週別採 ISO ``YYYY-Www``）；
  週 K 由該週「有效交易日」組成，結算於該週最後一個有效交易日（短週照樣 1 根）。
- 有效交易日：原始開高低收皆有值且 > 0（TWSE ``--``、TPEx ``---``／``----`` 視為無成交價；D2）。
- Open_w＝首個有效日 open_adj；High_w／Low_w＝有效日 max／min；Close_w＝最後有效日 close_adj；
  Vol_w＝該週所有列成交股數合計 ÷ 1000（含無成交價日的零股量；D2），內部保留小數。
- 全週無有效交易日 → 不產生週 K，記入 ``suspended_weeks``。
- W20MA：最近 ``ma_weeks`` 根有效週 K 之 Close_w 平均；不足即無值。
- 前 20 週均量：不含本週之前 ``vol_lookback_weeks`` 根；不足 20 根時以實際可用（至少 4 根）計算並標註
  （第 9 節，門檻依 Q1 裁定為 21 根週 K）。
- D20MA／D100MA：最近 20／100 個有效交易日 close_adj 平均（D100MA 僅作對照欄）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from src.timezone import iso_week_id, week_monday
from src.trading_calendar import WeekStatus

LOTS_PER_SHARE = 1 / 1000
MIN_VOL_SAMPLE_WEEKS = 4      # 第 9 節：均量至少以 4 週計算
D_SHORT, D_LONG = 20, 100     # 個股日 20MA、日 100MA（規格固定）
PROJECTION_DAYS = 5           # 推算滿週量「以 5 日換算」（第 4.3A 節）


# ---------------------------------------------------------------- 工具

def _f(x: Any) -> float | None:
    """NaN／None → None，其餘轉 float。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(v) else v


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def valid_day_mask(df: pd.DataFrame) -> pd.Series:
    raw_ok = pd.Series(True, index=df.index)
    for col in ("open", "high", "low", "close"):
        v = pd.to_numeric(df[col], errors="coerce")
        raw_ok &= v.notna() & (v > 0)
    return raw_ok & df["close_adj"].notna()


# ---------------------------------------------------------------- 週 K

WEEK_COLUMNS = ["week_monday", "week_id", "week_end_date", "open", "high", "low", "close",
                "volume_lots", "valid_days"]


def weekly_bars(df_code: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """單一代號的週 K（依週一日期遞增）與「有資料列但全週無有效成交」的週別清單。"""
    if df_code.empty:
        return pd.DataFrame(columns=WEEK_COLUMNS), []
    d = df_code.sort_values("date").copy()
    if "week_monday" not in d:                       # compute_all 會預先算好整表
        d["week_monday"] = [week_monday(x) for x in d["date"]]
    if "_valid" not in d:
        d["_valid"] = valid_day_mask(d)
    d["_lots"] = pd.to_numeric(d["volume_shares"], errors="coerce").fillna(0.0) * LOTS_PER_SHARE

    rows, suspended = [], []
    for wk, g in d.groupby("week_monday", sort=True):
        v = g[g["_valid"]]
        if v.empty:
            suspended.append(iso_week_id(wk))
            continue
        rows.append({
            "week_monday": wk, "week_id": iso_week_id(wk), "week_end_date": v["date"].iloc[-1],
            "open": float(v["open_adj"].iloc[0]), "high": float(v["high_adj"].max()),
            "low": float(v["low_adj"].min()), "close": float(v["close_adj"].iloc[-1]),
            "volume_lots": float(g["_lots"].sum()), "valid_days": int(len(v)),
        })
    return pd.DataFrame(rows, columns=WEEK_COLUMNS), suspended


def weekly_bars_all(df: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], dict[str, list[str]]]:
    """全表一次聚合週 K（結果與逐檔 weekly_bars 相同），回傳 {代號: 週 K}、{代號: 停牌週}。"""
    d = df.sort_values(["code", "date"]).copy()
    if "week_monday" not in d:
        d["week_monday"] = [x - timedelta(days=x.weekday()) for x in d["date"]]
    if "_valid" not in d:
        d["_valid"] = valid_day_mask(d)
    d["_lots"] = pd.to_numeric(d["volume_shares"], errors="coerce").fillna(0.0) * LOTS_PER_SHARE
    keys = ["code", "week_monday"]
    vol = d.groupby(keys, sort=True)["_lots"].sum()
    v = d[d["_valid"]]
    agg = v.groupby(keys, sort=True).agg(
        week_end_date=("date", "last"), open=("open_adj", "first"), high=("high_adj", "max"),
        low=("low_adj", "min"), close=("close_adj", "last"), valid_days=("date", "size"))
    agg["volume_lots"] = vol.reindex(agg.index)
    agg = agg.reset_index()
    agg["week_id"] = [iso_week_id(x) for x in agg["week_monday"]]
    bars = {code: g[WEEK_COLUMNS].reset_index(drop=True).astype({"valid_days": int})
            for code, g in agg.groupby("code", sort=True)}
    missing = vol.index.difference(agg.set_index(keys).index)
    suspended: dict[str, list[str]] = {}
    for code, wk in sorted(missing):
        suspended.setdefault(code, []).append(iso_week_id(wk))
    return bars, suspended


def add_weekly_series(bars: pd.DataFrame, ma_weeks: int, vol_lookback: int) -> pd.DataFrame:
    """加上 w20ma 與 vavg（不含本週之前 vol_lookback 根均量，至少 4 根）欄位，供圖表與指標共用。"""
    out = bars.copy()
    out["w20ma"] = out["close"].rolling(ma_weeks, min_periods=ma_weeks).mean()
    prior = out["volume_lots"].shift(1)
    out["vavg"] = prior.rolling(vol_lookback, min_periods=MIN_VOL_SAMPLE_WEEKS).mean()
    out["vavg_n"] = prior.rolling(vol_lookback, min_periods=1).count()
    return out


# ---------------------------------------------------------------- 個股指標

def stock_indicators(df_code: pd.DataFrame, week: WeekStatus, cfg: Mapping[str, Any],
                     conflicts: list[str] | None = None,
                     precomputed: tuple[pd.DataFrame, list[str]] | None = None) -> dict[str, Any]:
    scr = cfg["screening"]
    ma_weeks, vol_lookback = int(scr["ma_weeks"]), int(scr["vol_lookback_weeks"])
    spike_ratio = float(scr["volume_spike_ratio"])

    d = df_code[df_code["date"] <= week.as_of].sort_values("date")
    last = d.iloc[-1] if not d.empty else None
    res: dict[str, Any] = {
        "code": None if last is None else last["code"],
        "name": None if last is None else last["name"],
        "market": None if last is None else last["market"],
        "managed": bool(last["managed"]) if last is not None and "managed" in d else False,
        "adjustment_conflicts": list(conflicts or []),
        "suspended_weeks": [], "notes": [],
        "bars": 0, "has_current_week_bar": False,
        "last_trade_date": None, "close_raw": None, "close_adj": None,
        "w20ma": None, "w20ma_prev": None, "close_w": None, "close_w_prev": None,
        "d20ma": None, "d100ma": None, "ma_diff": None, "bias_20w_pct": None,
        "vol_week_lots": None, "vol_prev_week_lots": None, "vol_avg_20w_lots": None, "vol_avg_sample_weeks": 0,
        "vol_ratio_20w": None, "vol_ratio_20w_projected": None, "vol_ratio_prev_week": None,
        "is_volume_spike": False,
    }
    if d.empty:
        return res

    valid = d[d["_valid"]] if "_valid" in d else d[valid_day_mask(d)]
    if not valid.empty:
        lv = valid.iloc[-1]
        res.update(last_trade_date=lv["date"], close_raw=_f(lv["close"]), close_adj=_f(lv["close_adj"]))
        closes = valid["close_adj"].astype("float64")
        if len(closes) >= D_SHORT:
            res["d20ma"] = float(closes.iloc[-D_SHORT:].mean())
        if len(closes) >= D_LONG:
            res["d100ma"] = float(closes.iloc[-D_LONG:].mean())

    bars, suspended = precomputed if precomputed is not None else weekly_bars(d)
    res["suspended_weeks"] = list(suspended)
    res["bars"] = int(len(bars))
    if bars.empty:
        return res
    s = add_weekly_series(bars, ma_weeks, vol_lookback)
    cur, prev = s.iloc[-1], (s.iloc[-2] if len(s) >= 2 else None)
    res["has_current_week_bar"] = cur["week_monday"] == week.week_monday
    res["close_w"] = _f(cur["close"])
    res["close_w_prev"] = None if prev is None else _f(prev["close"])
    res["vol_week_lots"] = _f(cur["volume_lots"])
    res["vol_prev_week_lots"] = None if prev is None else _f(prev["volume_lots"])
    res["vol_ratio_prev_week"] = _div(res["vol_week_lots"], res["vol_prev_week_lots"])

    if len(s) < ma_weeks:  # 第 9 節：有效週 K 不足，無法計算 W20MA，自動跳過不報錯
        return res
    res["w20ma"] = _f(cur["w20ma"])
    res["w20ma_prev"] = None if prev is None else _f(prev["w20ma"])

    n = int(cur["vavg_n"]) if not np.isnan(cur["vavg_n"]) else 0
    res["vol_avg_sample_weeks"] = n
    res["vol_avg_20w_lots"] = _f(cur["vavg"])
    if res["vol_avg_20w_lots"] is not None and n < vol_lookback:
        res["notes"].append(f"掛牌未滿 {vol_lookback + 1} 週（均量樣本較少）")
    res["vol_ratio_20w"] = _div(res["vol_week_lots"], res["vol_avg_20w_lots"])
    res["is_volume_spike"] = res["vol_ratio_20w"] is not None and res["vol_ratio_20w"] >= spike_ratio
    if not week.is_settled and res["has_current_week_bar"] and week.trading_days_elapsed > 0:
        projected = res["vol_week_lots"] * PROJECTION_DAYS / week.trading_days_elapsed
        res["vol_ratio_20w_projected"] = _div(projected, res["vol_avg_20w_lots"])

    if res["w20ma"] is not None and res["close_raw"] is not None:
        res["ma_diff"] = res["close_raw"] - res["w20ma"]                         # 第 4.3C 節：原始收盤 − W20MA
    if res["w20ma"] is not None and res["close_adj"] is not None:
        res["bias_20w_pct"] = (res["close_adj"] - res["w20ma"]) / res["w20ma"] * 100
    return res


def compute_all(adj_daily: pd.DataFrame, week: WeekStatus, cfg: Mapping[str, Any],
                conflicts: Mapping[str, list[str]] | None = None) -> dict[str, dict[str, Any]]:
    conflicts = conflicts or {}
    df = adj_daily[adj_daily["date"] <= week.as_of].copy()
    df["week_monday"] = [x - timedelta(days=x.weekday()) for x in df["date"]]
    df["_valid"] = valid_day_mask(df)
    bars, suspended = weekly_bars_all(df)
    empty = pd.DataFrame(columns=WEEK_COLUMNS)
    return {code: stock_indicators(g, week, cfg, conflicts.get(code),
                                   precomputed=(bars.get(code, empty), suspended.get(code, [])))
            for code, g in df.groupby("code", sort=True)}


# ---------------------------------------------------------------- 大盤指數

def index_indicators(index_daily: pd.DataFrame, as_of: date, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """TAIEX 原始收盤（指數無除權息問題，規格 4.5）：D20MA、D100MA、真正週 K 之 W20MA 與乖離率。"""
    mt = cfg["market_timing"]
    d_short, d_long, w_ma = int(mt["d_short"]), int(mt["d_long"]), int(mt["w_ma"])
    d = index_daily[(index_daily["date"] <= as_of) & index_daily["close"].notna()].sort_values("date")
    res = {"date": None, "close": None, "d20ma": None, "d100ma": None, "w20ma": None, "bias_w20_pct": None}
    if d.empty:
        return res
    closes = d["close"].astype("float64")
    res["date"], res["close"] = d["date"].iloc[-1], float(closes.iloc[-1])
    if len(closes) >= d_short:
        res["d20ma"] = float(closes.iloc[-d_short:].mean())
    if len(closes) >= d_long:
        res["d100ma"] = float(closes.iloc[-d_long:].mean())
    weekly = d.assign(wk=[week_monday(x) for x in d["date"]]).groupby("wk", sort=True)["close"].last()
    if len(weekly) >= w_ma:
        res["w20ma"] = float(weekly.iloc[-w_ma:].mean())
        res["bias_w20_pct"] = (res["close"] - res["w20ma"]) / res["w20ma"] * 100
    return res
