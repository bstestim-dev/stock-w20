"""FinMind API 客戶端（spec_v1 第 3.4 節）。

- 第一階段僅用免 Token 的 ``TaiwanStockInfo``（分類輔助）；斷線時由呼叫端降級（第 9 節）。
- 選配 ``FINMIND_TOKEN``（Backer 以上）只從環境變數取得，以 ``Authorization: Bearer`` 標頭送出，
  不放網址、不寫入日誌。付費資料集（如 ``TaiwanStockPriceAdj``）於本里程碑僅保留 ``fetch_dataset`` 介面。
- 回應外層結構經 2026-11-25 verify-api run1 確認為 ``{"msg", "status", "data"}``（status 200、msg "success"）。
- ``TaiwanStockInfo`` 部分列的 ``date`` 為字面字串 ``"None"``（run2 實測：JSON null 0 列，但仍有 'None' 值；
  run1 誤判為 null）。處理方式待 Hector 裁定（見 latest_info_by_stock）。
"""

from __future__ import annotations

from typing import Any

from src.fetchers.http import FetchError, HttpClient
from src.fetchers.parsing import SchemaMismatch, require_keys
from src.timezone import parse_iso_date

BASE = "https://api.finmindtrade.com/api/v4/data"
STOCK_INFO_KEYS = ("industry_category", "stock_id", "stock_name", "type", "date")
# 視為「無日期」的值：JSON null、空字串、字面字串 "None"（run2 實測）。其他非 YYYY-MM-DD 值仍拋錯。
UNDATED_VALUES = frozenset({"", "None"})


def is_undated(raw: Any) -> bool:
    return raw is None or str(raw).strip() in UNDATED_VALUES


class FinMindClient:
    def __init__(self, http: HttpClient, token: str | None = None):
        self.http = http
        self._token = token or None

    @property
    def has_token(self) -> bool:
        return self._token is not None

    def __repr__(self) -> str:  # 避免 token 出現在任何輸出
        return f"FinMindClient(has_token={self.has_token})"

    def fetch_dataset(self, dataset: str, **params: Any) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        payload = self.http.get_json(BASE, params={"dataset": dataset, **params}, headers=headers)
        endpoint = f"FinMind {dataset}"
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            actual = list(payload) if isinstance(payload, dict) else type(payload).__name__
            raise SchemaMismatch(endpoint, "外層未見 data 陣列", actual)
        if payload.get("status") != 200:
            raise SchemaMismatch(endpoint, f"status={payload.get('status')!r} msg={payload.get('msg')!r}")
        return payload["data"]

    def fetch_stock_info(self) -> list[dict[str, Any]]:
        return require_keys(self.fetch_dataset("TaiwanStockInfo"), "FinMind TaiwanStockInfo", STOCK_INFO_KEYS)


def latest_info_by_stock(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """同一 stock_id 取 ``date`` 最新的一列（避免誤取已轉上市櫃的舊 emerging 列；spec 3.4B）。

    【暫行處理，待 Hector 裁定 D1】無日期列（見 ``is_undated``）：同代號有具日期的列時不採用；
    只有無日期列時才採用，並標 ``_date = None``、``_date_unknown = True``。
    其他非 YYYY-MM-DD 的值照舊拋 ValueError（不猜測）。
    """
    dated: dict[str, dict[str, Any]] = {}
    undated: dict[str, dict[str, Any]] = {}
    for rec in records:
        sid = str(rec["stock_id"]).strip()
        raw = rec.get("date")
        if is_undated(raw):
            undated.setdefault(sid, {**rec, "_date": None, "_date_unknown": True})
            continue
        d = parse_iso_date(str(raw))
        cur = dated.get(sid)
        if cur is None or d > cur["_date"]:
            dated[sid] = {**rec, "_date": d, "_date_unknown": False}
    return {**{k: v for k, v in undated.items() if k not in dated}, **dated}


def undated_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """統計無日期列與其他非 ISO 日期值（供 verify-api 回報 D1 範圍）。"""
    from collections import Counter

    flags = [is_undated(r.get("date")) for r in records]
    undated_rows = [r for r, f in zip(records, flags) if f]
    dated_ids = {str(r["stock_id"]).strip() for r, f in zip(records, flags) if not f}
    only_undated = sorted({str(r["stock_id"]).strip() for r in undated_rows} - dated_ids)
    only_rows = [r for r in undated_rows if str(r["stock_id"]).strip() in set(only_undated)]
    invalid = []
    for r, f in zip(records, flags):
        if not f:
            try:
                parse_iso_date(str(r.get("date")))
            except ValueError:
                invalid.append(r)
    return {
        "undated_rows": len(undated_rows),
        "undated_raw_values": dict(Counter(repr(r.get("date")) for r in undated_rows)),
        "stock_ids_only_undated": only_undated,
        "only_undated_by_type": dict(Counter(str(r.get("type")) for r in only_rows)),
        "only_undated_by_category": dict(Counter(str(r.get("industry_category")) for r in only_rows)),
        "undated_samples": undated_rows[:10],
        "invalid_date_rows": len(invalid),
        "invalid_samples": invalid[:10],
    }


def try_fetch_stock_info(client: FinMindClient) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """取 TaiwanStockInfo；失敗回 (None, 錯誤訊息)，由呼叫端降級為規則過濾（第 9 節）。"""
    try:
        return latest_info_by_stock(client.fetch_stock_info()), None
    except (FetchError, SchemaMismatch, ValueError) as exc:
        return None, f"FinMind TaiwanStockInfo 不可用，改用規則過濾：{exc}"
