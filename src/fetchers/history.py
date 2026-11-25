"""按交易日抓取全市場單日收盤表（spec_v1 第 3.1 節主策略）與第 9 節邊界處理。

每個平日請求 TWSE ``MI_INDEX`` 與 TPEx ``dailyQuotes`` 各 1 次：
- 兩市皆有資料 → 交易日；兩市皆無資料 → 非交易日（自動跳過，不補 0）。
- 僅一市有資料、或任一市抓取失敗 → 視為缺漏。
- ``as_of_date`` 取兩市「皆有完整資料」之最近共同交易日；其後的缺漏（例如盤後資料尚未入庫）
  不計入缺漏比例，但記入 notes 於報告頂部提示。
- 缺漏連續超過 3 個平日，或缺漏比例 > 5%，拋 ``HistoryIncomplete``（workflow 以非 0 結束，不發佈錯誤報告）。
- 週末不請求（台股週末不開市）；此假設由 API 驗證 workflow 以 FinMind TaiwanStockTradingDate 交叉檢查。
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.fetchers import tpex, twse
from src.fetchers.http import FetchError, HttpClient
from src.fetchers.parsing import ApiStatusError, DailyMarket, DailyQuote, ParseReport, SchemaMismatch
from src.timezone import today_taipei, week_monday

MAX_CONSECUTIVE_MISSING = 3
MAX_MISSING_RATIO = 0.05


class HistoryIncomplete(RuntimeError):
    """歷史資料缺漏超過第 9 節容許範圍。"""


@dataclass
class DayResult:
    trade_date: date
    twse: DailyMarket | None
    tpex: DailyMarket | None
    errors: list[str] = field(default_factory=list)
    from_cache: bool = False

    @property
    def status(self) -> str:
        if self.errors or self.twse is None or self.tpex is None:
            return "error"
        if self.twse.is_trading and self.tpex.is_trading:
            return "trading"
        if not self.twse.is_trading and not self.tpex.is_trading:
            return "non_trading"
        return "partial"


@dataclass
class HistoryResult:
    requested_end: date
    start: date
    as_of_date: date
    days: list[DayResult]
    missing: list[tuple[date, str]]
    notes: list[str]

    @property
    def trading_days(self) -> list[DayResult]:
        return [d for d in self.days if d.status == "trading" and d.trade_date <= self.as_of_date]


# ---------------------------------------------------------------- 斷點快取

class DayCache:
    """只快取「今日以前」且狀態確定（交易日／非交易日）的結果，避免把盤中或未更新資料存起來。"""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.json.gz"

    def get(self, d: date) -> DayResult | None:
        p = self._path(d)
        if not p.exists():
            return None
        try:
            raw = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
            return DayResult(d, _market_from_dict(raw["twse"]), _market_from_dict(raw["tpex"]), [], True)
        except (OSError, ValueError, KeyError, TypeError):
            return None  # 快取損毀即重抓

    def put(self, result: DayResult, today: date) -> bool:
        if result.trade_date >= today or result.status not in ("trading", "non_trading"):
            return False
        payload = {"twse": _market_to_dict(result.twse), "tpex": _market_to_dict(result.tpex)}
        self._path(result.trade_date).write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        return True


def _market_to_dict(m: DailyMarket) -> dict[str, Any]:
    return {
        "market": m.market, "trade_date": m.trade_date.isoformat(), "is_trading": m.is_trading,
        "index_close": m.index_close, "quotes": [asdict(q) for q in m.quotes],
    }


def _market_from_dict(d: dict[str, Any]) -> DailyMarket:
    return DailyMarket(
        market=d["market"], trade_date=date.fromisoformat(d["trade_date"]), is_trading=d["is_trading"],
        quotes=[DailyQuote(**q) for q in d["quotes"]], index_close=d["index_close"], report=ParseReport(),
    )


# ---------------------------------------------------------------- 主流程

def weekdays_between(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def fetch_day(client: HttpClient, d: date) -> DayResult:
    result = DayResult(d, None, None)
    for name, fn in (("TWSE", twse.fetch_mi_index), ("TPEx", tpex.fetch_daily_quotes)):
        try:
            market = fn(client, d)
        except (FetchError, ApiStatusError, SchemaMismatch, ValueError) as exc:
            result.errors.append(f"{name} {d}：{exc}")
            continue
        if name == "TWSE":
            result.twse = market
        else:
            result.tpex = market
    return result


def evaluate_missing(days: list[DayResult], as_of: date) -> list[tuple[date, str]]:
    """檢查缺漏規則；超限拋 HistoryIncomplete，否則回傳缺漏清單。"""
    missing: list[tuple[date, str]] = []
    run = 0
    counted = 0
    for day in days:
        if day.trade_date > as_of:
            break
        status = day.status
        if status == "non_trading":
            continue  # 非交易日不打斷也不累計連續缺漏
        counted += 1
        if status == "trading":
            run = 0
            continue
        reason = "；".join(day.errors) if day.errors else "上市／上櫃僅一方有資料"
        missing.append((day.trade_date, reason))
        run += 1
        if run > MAX_CONSECUTIVE_MISSING:
            raise HistoryIncomplete(
                f"連續缺漏超過 {MAX_CONSECUTIVE_MISSING} 個交易日（至 {day.trade_date}）：" +
                "；".join(f"{d} {r}" for d, r in missing[-run:]))
    if counted and len(missing) / counted > MAX_MISSING_RATIO:
        raise HistoryIncomplete(
            f"缺漏 {len(missing)}/{counted} 日（{len(missing) / counted:.1%}）超過 {MAX_MISSING_RATIO:.0%}："
            + "；".join(f"{d} {r}" for d, r in missing))
    return missing


def fetch_daily_history(
    client: HttpClient,
    end: date,
    lookback_weeks: int,
    cache: DayCache | None = None,
    today: date | None = None,
    progress: Callable[[DayResult], None] | None = None,
) -> HistoryResult:
    today = today or today_taipei()
    end = min(end, today)
    start = week_monday(end) - timedelta(weeks=lookback_weeks - 1)

    days: list[DayResult] = []
    for d in weekdays_between(start, end):
        result = cache.get(d) if cache else None
        if result is None:
            result = fetch_day(client, d)
            if cache:
                cache.put(result, today)
        days.append(result)
        if progress:
            progress(result)

    trading = [d for d in days if d.status == "trading"]
    if not trading:
        raise HistoryIncomplete(f"{start}～{end} 期間找不到上市與上櫃皆有資料的交易日")
    as_of = trading[-1].trade_date

    notes = []
    for day in days:
        if day.trade_date > as_of and day.status in ("partial", "error"):
            detail = "；".join(day.errors) if day.errors else "上市／上櫃資料未同時更新"
            notes.append(f"{day.trade_date} 資料不完整（{detail}），基準日退回 {as_of}")

    missing = evaluate_missing(days, as_of)
    return HistoryResult(end, start, as_of, days, missing, notes)


def history_to_frames(result: HistoryResult):
    """轉為 pandas：(日 K 長表, TAIEX 日收盤)。僅含 as_of_date（含）以前的交易日。"""
    import pandas as pd

    rows, idx = [], []
    for day in result.trading_days:
        for market in (day.twse, day.tpex):
            for q in market.quotes:
                rows.append({"date": day.trade_date, **asdict(q)})
        idx.append({"date": day.trade_date, "close": day.twse.index_close})
    return pd.DataFrame(rows), pd.DataFrame(idx)
