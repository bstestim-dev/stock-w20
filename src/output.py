"""輸出 ``site/data/``（spec_v1 第 6.3 節）：``latest.json`` 與 4 份 UTF-8 BOM CSV。

- ``latest.json``：第 6.3 節核心 Schema（schema_version "1.0"）。另加兩個頂層欄位：
  ``notes``（報告頂部提示，例：第 9 節「退回共同交易日」原因、FinMind 降級）與 ``errors``（第 9 節「記錄於 errors 列表」的缺漏交易日）。
  名單元素除第 6.3 節表列欄位外，保留 ``has_share_split_or_reduction``（第 3.5 節要求）與 M4 已裁定的附加欄位。
  ``weekly_charts`` 每檔保留最近 ``CHART_WEEKS`` 根週 K（第 7 節「近 40～45 週」）。
- CSV：欄位依第 6.3 節「名單陣列單一標的元素欄位定義」順序；巢狀欄位攤平為 ``signals.xxx``、``fundamentals_ref.xxx``，
  ``warnings`` 以「、」連接。編碼 UTF-8 with BOM。
- ``latest.js``：與 ``latest.json`` 同內容的 ``window.__SW20_LATEST__ = …``，供直接以 file:// 開啟 ``index.html``
  時讀取（瀏覽器在 file:// 下會擋 fetch）。
- 數值：價格、均線、倍數等保留 4 位小數；成交張數於 JSON 保留小數，CSV 與網頁顯示時四捨五入到整張（M3-Q5）。
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
CHART_WEEKS = 45
FLOAT_DIGITS = 4
TAB_FILES = ("new_cross", "above_and_spike", "etf", "exit_alerts")

SIGNAL_KEYS = ("w20_slope_up", "d20_cross_w20", "breakout_13w", "trendline_break", "w_bottom", "box_consolidation",
               "sector_peers_count", "sector_leader")
FUND_KEYS = ("eps_quarter", "eps", "rev_month", "rev_yoy_pct", "insti_5d_net_lots")
LOT_FIELDS = {"vol_week_lots", "vol_avg_20w_lots", "avg_daily_lots_20w", "fundamentals_ref.insti_5d_net_lots"}

# 第 6.3 節表列順序；之後接附加欄位
CSV_COLUMNS = (
    ["stock_id", "stock_name", "market", "asset_type", "is_tib", "etf_subtype", "industry", "capital_billion_ntd",
     "market_cap_billion_ntd", "close_raw", "close_adj", "w20ma", "d100ma", "d20ma", "ma_diff", "bias_20w_pct",
     "vol_week_lots", "vol_avg_20w_lots", "vol_ratio_20w", "vol_ratio_20w_projected", "vol_ratio_prev_week",
     "is_volume_spike", "score"]
    + [f"signals.{k}" for k in SIGNAL_KEYS]
    + ["exit_status", "exit_action_note", "warnings"]
    + [f"fundamentals_ref.{k}" for k in FUND_KEYS]
    + ["avg_daily_lots_20w", "etf_zone", "has_share_split_or_reduction", "unadjusted_jump_dates"]
)


def clean(value: Any) -> Any:
    """轉為 JSON 可序列化：日期 → ISO 字串、NaN／inf → None、numpy 型別 → Python、浮點數四捨五入。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (list, dict)):
        value = value.item()                 # numpy 純量
        if isinstance(value, bool):
            return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, FLOAT_DIGITS)
    return str(value)


def build_latest(res: Mapping[str, Any], generated_at: str, fundamentals: Mapping[str, dict] | None = None,
                 notes: list[str] | None = None, errors: list[str] | None = None) -> dict[str, Any]:
    """由 ``screener.screen`` 結果組成 latest.json 內容。"""
    fundamentals = fundamentals or {}

    def row(r: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in r.items() if not k.startswith("_")}
        if r.get("asset_type") == "common_stock" and r["stock_id"] in fundamentals:
            out["fundamentals_ref"] = fundamentals[r["stock_id"]]
        return out

    tabs = {k: [row(r) for r in res["tabs"][k]] for k in TAB_FILES}
    charts = {code: bars[-CHART_WEEKS:] for code, bars in res["weekly_charts"].items()}
    latest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_taipei": generated_at,
        "as_of_date": res["as_of_date"],
        "week_id": res["week_id"],
        "is_week_settled": res["is_week_settled"],
        "trading_days_in_current_week": res["trading_days_in_current_week"],
        "price_mode": res["price_mode"],
        "params": res["params"],
        "market": res["market"],
        "summary": res["summary"],
        "tabs": tabs,
        "weekly_charts": charts,
        "notes": list(res.get("notes", [])) + list(notes or []),
        "errors": list(errors or []),
    }
    return clean(latest)


def _csv_value(row: Mapping[str, Any], col: str) -> Any:
    if col.startswith("signals."):
        v = (row.get("signals") or {}).get(col.split(".", 1)[1])
    elif col.startswith("fundamentals_ref."):
        v = (row.get("fundamentals_ref") or {}).get(col.split(".", 1)[1])
    else:
        v = row.get(col)
    if col in LOT_FIELDS and isinstance(v, (int, float)):
        return int(round(v))
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "、".join(map(str, v))
    return "" if v is None else v


def csv_text(rows: list[Mapping[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([_csv_value(r, c) for c in CSV_COLUMNS])
    return buf.getvalue()


def write_site_data(latest: Mapping[str, Any], out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(latest, ensure_ascii=False, separators=(",", ":"))
    paths = [out / "latest.json", out / "latest.js"]
    paths[0].write_text(text, encoding="utf-8")
    paths[1].write_text(f"window.__SW20_LATEST__ = {text};\n", encoding="utf-8")
    for k in TAB_FILES:
        p = out / f"{k}.csv"
        p.write_text(csv_text(latest["tabs"][k]), encoding="utf-8-sig", newline="")
        paths.append(p)
    return paths
