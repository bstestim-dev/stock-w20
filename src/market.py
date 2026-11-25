"""大盤紅綠燈（spec_v1 第 4.5 節；FR-1.8）。燈號一律不過濾名單（hard_filter_on_green 定案為 false）。

- 紅燈：Index_Close > D20MA 且 > D100MA
- 綠燈：Index_Close < D20MA 且 < D100MA
- 黃燈：其餘（介於兩線之間，含任一剛等於均線之邊界）
文案取自第 4.5 節表格「頂部建議水位文案」欄；紅燈的 suggested_exposure 依第 6.3 節範例。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INDEX_NAME = "發行量加權股價指數"

LIGHTS = {
    "RED": ("紅燈（偏多）", "建議持股 5～7 成（保留至少 3 成現金）"),
    "YELLOW": ("黃燈（多空不明／轉折）", "多空交戰，建議減碼觀望、嚴守紀律，新倉宜謹慎並優先看型態完整者"),
    "GREEN": ("綠燈（偏空）", "空頭格局，教學建議總水位降至約 3 成、原則上不開新倉"),
}


def classify_light(close: float | None, d20ma: float | None, d100ma: float | None) -> str | None:
    if close is None or d20ma is None or d100ma is None:
        return None
    if close > d20ma and close > d100ma:
        return "RED"
    if close < d20ma and close < d100ma:
        return "GREEN"
    return "YELLOW"


def market_block(index_ind: Mapping[str, Any], force_light: str | None = None) -> dict[str, Any]:
    """``index_ind`` 為 ``indicators.index_indicators`` 結果。``force_light`` 僅供測試模擬（第 10.1 節第 3 項）。"""
    light = force_light or classify_light(index_ind.get("close"), index_ind.get("d20ma"), index_ind.get("d100ma"))
    label, exposure = LIGHTS.get(light, (None, None))
    return {
        "index_name": INDEX_NAME,
        "date": index_ind.get("date"),
        "close": index_ind.get("close"),
        "d20ma": index_ind.get("d20ma"),
        "d100ma": index_ind.get("d100ma"),
        "w20ma": index_ind.get("w20ma"),
        "bias_w20_pct": index_ind.get("bias_w20_pct"),
        "light": light,
        "light_label_zh": label,
        "suggested_exposure": exposure,
        "hard_filtered": False,
    }
