"""共用 HTTP 客戶端：禮貌間隔、重試與退避（spec_v1 第 9 節）。

- 每次請求間隔 >= ``delay_sec``（預設 0.7 秒）。
- 429、5xx、連線逾時、連線中途斷線（回應未讀完）、回應含「查詢太頻繁」或無法解析 JSON：指數退避重試，
  第 1 次等 3 秒、第 2 次 8 秒、第 3 次 20 秒（最多 ``max_retries`` 次）。
- 其他 4xx 不重試；HTTP 402（FinMind 額度用盡）拋 ``QuotaExceeded``。
- 錯誤訊息不含任何認證標頭內容（token 只放在 Authorization 標頭，不放網址）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import requests
import urllib3

# 傳輸層可重試錯誤：連線失敗、逾時，以及回應讀取中途斷線（M7b 實跑 run 45775896782 遇到
# ChunkedEncodingError「Response ended prematurely」）。重試用盡後與其他可重試錯誤一樣轉成 FetchError。
TRANSPORT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    urllib3.exceptions.ProtocolError,
)

# TWSE RWD 端點須帶正常 User-Agent（spec 3.2B）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36 stock-w20/0.2 (+https://github.com/bstestim-dev/stock-w20)"
)
BACKOFF_SECONDS = (3.0, 8.0, 20.0)
RATE_LIMIT_MARKERS = ("查詢太頻繁",)


class FetchError(RuntimeError):
    """抓取失敗（重試用盡或不可重試）。"""


class HttpStatusError(FetchError):
    def __init__(self, url: str, status: int, snippet: str):
        super().__init__(f"HTTP {status}：{url}｜{snippet}")
        self.url = url
        self.status = status


class QuotaExceeded(HttpStatusError):
    """FinMind 超額（HTTP 402）。"""


@dataclass
class RequestLog:
    url: str
    params: dict[str, Any]
    status: int | None
    elapsed_sec: float
    attempts: int
    error: str | None = None


@dataclass
class HttpClient:
    delay_sec: float = 0.7
    timeout_sec: float = 20.0
    max_retries: int = 3
    user_agent: str = DEFAULT_USER_AGENT
    session: Any = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    logs: list[RequestLog] = field(default_factory=list)
    _last_request_at: float | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], **kwargs: Any) -> "HttpClient":
        ds = cfg["data_source"]
        return cls(
            delay_sec=float(ds["request_delay_sec"]),
            timeout_sec=float(ds["timeout_sec"]),
            max_retries=int(ds["max_retries"]),
            **kwargs,
        )

    # ------------------------------------------------------------ 內部

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        wait = self.delay_sec - (self.clock() - self._last_request_at)
        if wait > 0:
            self.sleep(wait)

    def _backoff(self, attempt_index: int) -> float:
        return BACKOFF_SECONDS[min(attempt_index, len(BACKOFF_SECONDS) - 1)]

    # ------------------------------------------------------------ 對外

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET 並解析 JSON；可重試錯誤依第 9 節退避，用盡後拋 FetchError。"""
        req_headers = {"User-Agent": self.user_agent, "Accept": "application/json, text/plain, */*"}
        if headers:
            req_headers.update(headers)
        params = dict(params or {})
        last_error = "未知錯誤"
        started = self.clock()
        status: int | None = None

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                self.sleep(self._backoff(attempt - 1))
            self._throttle()
            self._last_request_at = self.clock()
            try:
                resp = self.session.get(url, params=params, headers=req_headers, timeout=self.timeout_sec)
                status = resp.status_code
                body = resp.content or b""
            except TRANSPORT_ERRORS as exc:
                last_error = f"連線失敗：{type(exc).__name__}"
                status = None
                continue

            text = body.decode("utf-8", errors="replace")

            if status == 402:
                self._log(url, params, status, started, attempt + 1, "quota")
                raise QuotaExceeded(url, status, text[:200])
            if status == 429 or status >= 500:
                last_error = f"HTTP {status}"
                continue
            if 400 <= status < 500:
                self._log(url, params, status, started, attempt + 1, f"HTTP {status}")
                raise HttpStatusError(url, status, text[:200])
            if any(marker in text for marker in RATE_LIMIT_MARKERS):
                last_error = "來源回應查詢太頻繁"
                continue
            try:
                data = json.loads(body)
            except ValueError:
                last_error = f"回應非 JSON：{text[:120]!r}"
                continue
            self._log(url, params, status, started, attempt + 1, None)
            return data

        self._log(url, params, status, started, self.max_retries + 1, last_error)
        raise FetchError(f"{url} 重試 {self.max_retries} 次仍失敗：{last_error}")

    def _log(self, url, params, status, started, attempts, error) -> None:
        self.logs.append(
            RequestLog(url=url, params=params, status=status,
                       elapsed_sec=round(self.clock() - started, 3), attempts=attempts, error=error)
        )
