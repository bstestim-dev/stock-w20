"""FinMind API 客戶端（spec_v1 第 3.4 節）。

- 第一階段僅用免 Token 的 ``TaiwanStockInfo``（分類輔助）；斷線時由呼叫端降級（第 9 節）。
- 選配 ``FINMIND_TOKEN``（Backer 以上）只從環境變數取得，以 ``Authorization: Bearer`` 標頭送出，
  不放網址、不寫入日誌。付費資料集（如 ``TaiwanStockPriceAdj``）於本里程碑僅保留 ``fetch_dataset`` 介面。
- 回應外層結構（資料放在 ``data`` 鍵）規格未明載，待 API 驗證確認；不符即拋 SchemaMismatch。
"""

from __future__ import annotations

from typing import Any

from src.fetchers.http import FetchError, HttpClient
from src.fetchers.parsing import SchemaMismatch, require_keys
from src.timezone import parse_iso_date

BASE = "https://api.finmindtrade.com/api/v4/data"
STOCK_INFO_KEYS = ("industry_category", "stock_id", "stock_name", "type", "date")


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
        return payload["data"]

    def fetch_stock_info(self) -> list[dict[str, Any]]:
        return require_keys(self.fetch_dataset("TaiwanStockInfo"), "FinMind TaiwanStockInfo", STOCK_INFO_KEYS)


def latest_info_by_stock(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """同一 stock_id 取 ``date`` 最新的一列（避免誤取已轉上市櫃的舊 emerging 列；spec 3.4B）。"""
    latest: dict[str, dict[str, Any]] = {}
    for rec in records:
        sid = str(rec["stock_id"]).strip()
        d = parse_iso_date(str(rec["date"]))
        cur = latest.get(sid)
        if cur is None or d > cur["_date"]:
            latest[sid] = {**rec, "_date": d}
    return latest


def try_fetch_stock_info(client: FinMindClient) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """取 TaiwanStockInfo；失敗回 (None, 錯誤訊息)，由呼叫端降級為規則過濾（第 9 節）。"""
    try:
        return latest_info_by_stock(client.fetch_stock_info()), None
    except (FetchError, SchemaMismatch, ValueError) as exc:
        return None, f"FinMind TaiwanStockInfo 不可用，改用規則過濾：{exc}"
