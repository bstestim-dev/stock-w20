"""M7b：第二階段回測資料——多年官方行情抓取（可分次續抓）與 Parquet 分段快取（spec_v1 第 5.1 節、M7 計畫）。

只抓資料、建快取，不做回測（回測引擎為 M7c）。資料路徑只用 TWSE／TPEx 官方端點與免費 FinMind 資料集，
**不讀取、不傳送任何 token**（M7-Q18：FinMind 付費路徑不實作；M7-Q19：下市清單以無 token 試讀）。

依已核准裁定：
- M7-Q1：資料區間自 2020-03-02（交易窗 2021-01-04 前 45 週暖機）至最近一個已結算的週五。
- M7-Q2：快取只放 actions/cache（run-backtest.yml）；超過 7 日未使用被清除時接受重抓。
- M7-Q4：股本用目前快照（本模組每次抓取時存一份官方基本資料快照，記錄取得時間）。
- M7-Q14：加權報酬指數取 MI_INDEX「報酬指數(臺灣證券交易所)」表的「發行量加權股價報酬指數」列（M7a run 45771753603 實測 6 個抽樣日皆有）。

快取目錄結構（``root``）：
- ``days/YYYY-MM-DD.json.gz``：每個平日一檔（兩市解析後行情＋加權指數＋報酬指數），由舊到新逐日寫入、原子替換；
  只存「早於今日且狀態確定（交易日／兩市皆休市）」的日子，因此中斷後下次執行可接續（同第一階段 D5）。
- ``events/{market}_{kind}_{start}_{end}.json.gz``：事件表，自 2020-03-02 起固定 90 日分段；資料終點往後延伸時只重抓最後一段。
- ``snapshots/basics.json.gz``、``snapshots/delisting.json.gz``：基本資料（股本）與 FinMind 下市清單快照。
- ``parquet/``：由上述檔案彙整的 Parquet 分段快取（行情按年分檔）與 ``manifest.json``，供 M7c 回測讀取。

行情、事件數值全部來自官方回應，經第一階段正式解析器解析；本模組不補值、不推算。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.fetchers import finmind, tpex, twse
from src.fetchers.history import DayResult, _market_from_dict, _market_to_dict, weekdays_between
from src.fetchers.http import FetchError, HttpClient
from src.fetchers.parsing import (
    ApiStatusError, CorporateEvent, DailyMarket, SchemaMismatch, date_chunks, extract_tables, parse_number,
)
from src.timezone import format_taipei_iso, format_tpex_query_date, format_twse_query_date, now_taipei, today_taipei

DATA_START = date(2020, 3, 2)          # M7-Q1
SCHEMA_VERSION = 1
EVENT_CHUNK_DAYS = 90
TR_TABLE_KEYWORD = "報酬指數(臺灣證券交易所)"
TR_INDEX_NAME = "發行量加權股價報酬指數"
BENCH_ETF = "0050"
MAX_CONSECUTIVE_ERRORS = 5             # 連續 5 個平日抓取失敗即停止（疑似限流或官方異常），已抓資料保留
UNIT_RATIO_RANGE = (0.8, 1.25)         # 成交金額 ÷（成交股數 × 收盤價）每日中位數的合理範圍（單位檢查）
EXIT_OK, EXIT_INCOMPLETE, EXIT_BUDGET = 0, 2, 3

TWSE_EVENT_KINDS = tuple(twse._EVENT_SPECS)   # dividend, capital_reduction, par_value_change, etf_split
TPEX_EVENT_KINDS = tuple(tpex._EVENT_SPECS)   # dividend, capital_reduction, etf_split

_RECOVERABLE = (FetchError, ApiStatusError, SchemaMismatch, ValueError)

PRICE_COLUMNS = ("date", "market", "code", "name", "open", "high", "low", "close", "volume_shares", "value_ntd", "managed")
EVENT_COLUMNS = ("market", "code", "name", "event_date", "kind", "p_before", "p_ref", "source", "extra_json")


# ---------------------------------------------------------------- 日期

def default_end(today: date) -> date:
    """最近一個「早於今日」的週五：該週所有平日都早於今日，符合 D4 已結算條件（休市日需兩市皆無資料，於彙整時檢查）。"""
    d = today - timedelta(days=1)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d


def resolve_end(raw: str | None, today: date) -> date:
    latest = default_end(today)
    if raw is None or not raw.strip():
        return latest
    end = date.fromisoformat(raw.strip())
    if end.weekday() != 4:
        raise ValueError(f"end_date 需為週五（M7-Q1 交易窗止於已結算週五）：{end}")
    if end > latest:
        raise ValueError(f"end_date {end} 晚於最近一個已結算週五 {latest}")
    if end < DATA_START + timedelta(weeks=45):
        raise ValueError(f"end_date {end} 過早（資料自 {DATA_START} 起需至少 45 週暖機）")
    return end


# ---------------------------------------------------------------- 解析（只讀官方回應）

def parse_tr_index(payload: Any) -> float | None:
    """MI_INDEX「報酬指數(臺灣證券交易所)」表中「發行量加權股價報酬指數」的收盤指數；找不到回 None（不推算）。"""
    for t in extract_tables(payload) if isinstance(payload, dict) else []:
        if TR_TABLE_KEYWORD not in str(t.get("title") or ""):
            continue
        fields = list(t.get("fields") or [])
        if "收盤指數" not in fields or not fields:
            continue
        col = fields.index("收盤指數")
        for row in t.get("data") or []:
            if isinstance(row, list) and len(row) > col and str(row[0]).strip() == TR_INDEX_NAME:
                try:
                    return parse_number(row[col])
                except ValueError:
                    return None
    return None


# ---------------------------------------------------------------- 快取

def _write_gz_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8")))
    tmp.replace(path)


def _read_gz_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError):
        return None   # 損毀即視同沒有，下次重抓


@dataclass
class BTDay:
    result: DayResult
    tr_close: float | None = None


def _event_to_dict(ev: CorporateEvent) -> dict[str, Any]:
    d = asdict(ev)
    d["event_date"] = ev.event_date.isoformat()
    return d


def _event_from_dict(d: dict[str, Any]) -> CorporateEvent:
    return CorporateEvent(**{**d, "event_date": date.fromisoformat(d["event_date"])})


class BacktestStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.days_dir = self.root / "days"
        self.events_dir = self.root / "events"
        self.snap_dir = self.root / "snapshots"
        self.parquet_dir = self.root / "parquet"
        for p in (self.days_dir, self.events_dir, self.snap_dir, self.parquet_dir):
            p.mkdir(parents=True, exist_ok=True)

    # ---- 日資料
    def day_path(self, d: date) -> Path:
        return self.days_dir / f"{d.isoformat()}.json.gz"

    def get_day(self, d: date) -> BTDay | None:
        raw = _read_gz_json(self.day_path(d))
        if raw is None:
            return None
        try:
            res = DayResult(d, _market_from_dict(raw["twse"]), _market_from_dict(raw["tpex"]), [], True)
            return BTDay(res, raw.get("taiex_tr_close"))
        except (KeyError, TypeError, ValueError):
            return None

    def put_day(self, day: BTDay, today: date) -> bool:
        r = day.result
        if r.trade_date >= today or r.status not in ("trading", "non_trading"):
            return False
        _write_gz_json(self.day_path(r.trade_date), {
            "twse": _market_to_dict(r.twse), "tpex": _market_to_dict(r.tpex), "taiex_tr_close": day.tr_close})
        return True

    # ---- 事件
    def event_path(self, market: str, kind: str, s: date, e: date) -> Path:
        return self.events_dir / f"{market}_{kind}_{s:%Y%m%d}_{e:%Y%m%d}.json.gz"

    def get_events(self, market: str, kind: str, s: date, e: date) -> list[CorporateEvent] | None:
        raw = _read_gz_json(self.event_path(market, kind, s, e))
        if raw is None:
            return None
        try:
            return [_event_from_dict(x) for x in raw["events"]]
        except (KeyError, TypeError, ValueError):
            return None

    def put_events(self, market: str, kind: str, s: date, e: date, events: list[CorporateEvent], today: date) -> bool:
        if e >= today:
            return False
        _write_gz_json(self.event_path(market, kind, s, e), {"events": [_event_to_dict(x) for x in events]})
        return True

    def prune_events(self, grid: list[tuple[str, str, date, date]]) -> list[str]:
        """刪除不在目前分段格線上的舊事件檔（例如資料終點延伸前的最後一段）。"""
        keep = {self.event_path(*g).name for g in grid}
        removed = []
        for p in self.events_dir.glob("*.json.gz"):
            if p.name not in keep:
                p.unlink()
                removed.append(p.name)
        return removed

    # ---- 快照
    def put_snapshot(self, name: str, obj: Any) -> None:
        _write_gz_json(self.snap_dir / f"{name}.json.gz", obj)

    def get_snapshot(self, name: str) -> Any | None:
        return _read_gz_json(self.snap_dir / f"{name}.json.gz")


# ---------------------------------------------------------------- 抓取

def fetch_bt_day(client: HttpClient, d: date) -> BTDay:
    """與第一階段 history.fetch_day 相同的兩次請求；另從同一份 MI_INDEX 回應讀取報酬指數。"""
    result = DayResult(d, None, None)
    tr = None
    try:
        # twse._rwd_get／parse_mi_index 為第一階段正式函式（fetch_mi_index 的同一組參數），此處需保留原始回應以讀報酬指數
        payload = twse._rwd_get(client, "/afterTrading/MI_INDEX", {"date": format_twse_query_date(d), "type": "ALLBUT0999"})
        result.twse = twse.parse_mi_index(payload, d, prefilter=True)
        if result.twse.is_trading:
            tr = parse_tr_index(payload)
    except _RECOVERABLE as exc:
        result.errors.append(f"TWSE {d}：{exc}")
    try:
        result.tpex = tpex.fetch_daily_quotes(client, d)
    except _RECOVERABLE as exc:
        result.errors.append(f"TPEx {d}：{exc}")
    return BTDay(result, tr)


def event_grid(end: date) -> list[tuple[str, str, date, date]]:
    chunks = date_chunks(DATA_START, end, EVENT_CHUNK_DAYS)
    return ([("twse", k, s, e) for k in TWSE_EVENT_KINDS for s, e in chunks]
            + [("tpex", k, s, e) for k in TPEX_EVENT_KINDS for s, e in chunks])


def fetch_event_chunk(client: HttpClient, market: str, kind: str, s: date, e: date) -> list[CorporateEvent]:
    if market == "twse":
        path = twse._EVENT_SPECS[kind][0]
        payload = twse._rwd_get(client, path, {"startDate": format_twse_query_date(s), "endDate": format_twse_query_date(e)})
        return twse.parse_events(payload, kind)
    path = tpex._EVENT_SPECS[kind][0]
    payload = tpex._www_get(client, path, {"startDate": format_tpex_query_date(s), "endDate": format_tpex_query_date(e)})
    return tpex.parse_events(payload, kind)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class FetchSummary:
    end: date
    weekdays_total: int = 0
    days_cached_before: int = 0
    days_fetched: int = 0
    day_errors: list[tuple[str, str]] = field(default_factory=list)
    events_total: int = 0
    events_cached_before: int = 0
    events_fetched: int = 0
    event_errors: list[tuple[str, str]] = field(default_factory=list)
    snapshot_notes: list[str] = field(default_factory=list)
    stopped: str | None = None          # None | "budget" | "consecutive_errors"
    requests: int = 0
    elapsed_sec: float = 0.0


def _check_deadline(deadline: float | None, clock: Callable[[], float]) -> None:
    if deadline is not None and clock() > deadline:
        raise BudgetExceeded()


def fetch_snapshots(client: HttpClient, store: BacktestStore, summary: FetchSummary, now_iso: str) -> None:
    """M7-Q4 股本快照與 M7-Q19 下市清單；失敗時保留前一份快照並記錄（不中止抓取）。FinMind 一律不帶 token。"""
    basics: dict[str, Any] = {"fetched_at": now_iso}
    errors = []
    for key, fn in (("twse_basics", twse.fetch_company_basics), ("tpex_basics", tpex.fetch_company_basics)):
        try:
            basics[key] = fn(client)
        except _RECOVERABLE as exc:
            errors.append(f"{key}：{exc}"[:300])
    fm = finmind.FinMindClient(client, None)     # 明確不帶 token（M7-Q18／Q19）
    try:
        # 存原始列（M7c 以第一階段 finmind.latest_rows_by_stock 取最新列，與 screener 相同）
        basics["finmind_stock_info"] = fm.fetch_stock_info()
    except _RECOVERABLE as exc:
        basics["finmind_stock_info"] = None
        basics["finmind_error"] = f"FinMind TaiwanStockInfo 不可用：{exc}"[:300]
    if errors:
        summary.snapshot_notes.append("基本資料快照未更新（沿用前一份，如有）：" + "；".join(errors))
    else:
        store.put_snapshot("basics", basics)

    delist: dict[str, Any] = {"fetched_at": now_iso, "dataset": "TaiwanStockDelisting", "token": False}
    try:
        delist["data"] = fm.fetch_dataset("TaiwanStockDelisting")
        store.put_snapshot("delisting", delist)
    except _RECOVERABLE as exc:
        summary.snapshot_notes.append(f"FinMind 下市清單（無 token）讀取失敗，沿用前一份（如有）：{exc}"[:300])


def fetch_all(client: HttpClient, store: BacktestStore, end: date, today: date,
              deadline: float | None = None, clock: Callable[[], float] = time.monotonic,
              log: Callable[[str], None] = print) -> FetchSummary:
    """事件 → 快照 → 日資料（由舊到新）。超過 ``deadline`` 即停止（已抓的都已寫入快取）。"""
    started = clock()
    n0 = len(getattr(client, "logs", []))
    s = FetchSummary(end=end)
    try:
        grid = event_grid(end)
        s.events_total = len(grid)
        removed = store.prune_events(grid)
        if removed:
            log(f"移除不在目前分段的舊事件檔 {len(removed)} 個")
        todo = [g for g in grid if store.get_events(*g) is None]
        s.events_cached_before = len(grid) - len(todo)
        for g in todo:
            _check_deadline(deadline, clock)
            try:
                evs = fetch_event_chunk(client, *g)
            except _RECOVERABLE as exc:
                s.event_errors.append((f"{g[0]} {g[1]} {g[2]}～{g[3]}", str(exc)[:300]))
                continue
            store.put_events(*g, evs, today)
            s.events_fetched += 1
        log(f"事件：共 {s.events_total} 段，本次抓 {s.events_fetched}，失敗 {len(s.event_errors)}")

        _check_deadline(deadline, clock)
        fetch_snapshots(client, store, s, format_taipei_iso(now_taipei()))

        weekdays = weekdays_between(DATA_START, end)
        s.weekdays_total = len(weekdays)
        todo_days = [d for d in weekdays if store.get_day(d) is None]
        s.days_cached_before = len(weekdays) - len(todo_days)
        log(f"日資料：共 {len(weekdays)} 個平日，快取已有 {s.days_cached_before}，待抓 {len(todo_days)}")
        consecutive = 0
        for i, d in enumerate(todo_days, 1):
            _check_deadline(deadline, clock)
            day = fetch_bt_day(client, d)
            if store.put_day(day, today):
                s.days_fetched += 1
            if day.result.status == "error":
                consecutive += 1
                s.day_errors.append((d.isoformat(), "；".join(day.result.errors)[:300]))
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    s.stopped = "consecutive_errors"
                    log(f"連續 {consecutive} 個平日抓取失敗，停止抓取（已抓資料已保存）")
                    break
            else:
                consecutive = 0
                if day.result.status == "partial":
                    s.day_errors.append((d.isoformat(), "上市／上櫃僅一方有資料"))
            if i % 50 == 0 or i == len(todo_days):
                log(f"  已處理 {i}/{len(todo_days)}（至 {d}），經過 {(clock() - started) / 60:.1f} 分")
    except BudgetExceeded:
        s.stopped = "budget"
        log("已達本次抓取時間上限，停止抓取（已抓資料已保存，請再執行一次接續）")
    s.requests = len(getattr(client, "logs", [])) - n0
    s.elapsed_sec = clock() - started
    return s


# ---------------------------------------------------------------- Parquet 彙整

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_parquet(df, path: Path) -> None:
    """pandas＋pyarrow 寫出（requirements.txt 已列 pyarrow）；先寫暫存檔再原子替換。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", index=False)
    tmp.replace(path)


def _unit_ratio(market: DailyMarket) -> float | None:
    ratios = [q.value_ntd / (q.volume_shares * q.close) for q in market.quotes
              if q.value_ntd and q.volume_shares and q.close and q.value_ntd > 0 and q.volume_shares > 0 and q.close > 0]
    return statistics.median(ratios) if ratios else None


def build_parquet(store: BacktestStore, end: date, summary: FetchSummary | None = None,
                  writer: Callable[[Any, Path], None] = write_parquet, suffix: str = ".parquet") -> dict[str, Any]:
    """由日檔與事件檔彙整 Parquet（行情按年分檔），回傳 manifest（同時寫入 parquet/manifest.json）。"""
    import pandas as pd

    weekdays = weekdays_between(DATA_START, end)
    status: dict[str, str] = {}
    trading_by_year: Counter = Counter()
    tr_missing, taiex_missing, bench_missing, unit_flags = [], [], [], []
    idx_rows: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    price_rows_total = 0

    def emit(name: str, df) -> None:
        path = store.parquet_dir / name
        writer(df, path)
        files.append({"file": name, "rows": int(len(df)), "bytes": path.stat().st_size, "sha256": _sha256(path)})

    for old in store.parquet_dir.glob(f"*{suffix}"):
        old.unlink()   # 依目前快取重新產生，避免殘留舊年度檔

    by_year: dict[int, list[date]] = {}
    for d in weekdays:
        by_year.setdefault(d.year, []).append(d)
    for year, days in sorted(by_year.items()):
        cols: dict[str, list[Any]] = {c: [] for c in PRICE_COLUMNS}
        for d in days:
            day = store.get_day(d)
            if day is None:
                continue
            r = day.result
            status[d.isoformat()] = r.status
            if r.status != "trading":
                continue
            trading_by_year[year] += 1
            idx_rows.append({"date": d, "taiex_close": r.twse.index_close, "taiex_tr_close": day.tr_close})
            if day.tr_close is None:
                tr_missing.append(d.isoformat())
            if r.twse.index_close is None:
                taiex_missing.append(d.isoformat())
            if not any(q.code == BENCH_ETF and q.close is not None for q in r.twse.quotes):
                bench_missing.append(d.isoformat())
            for m in (r.twse, r.tpex):
                ratio = _unit_ratio(m)
                if ratio is None or not (UNIT_RATIO_RANGE[0] <= ratio <= UNIT_RATIO_RANGE[1]):
                    unit_flags.append(f"{d} {m.market} {None if ratio is None else round(ratio, 4)}")
                for q in m.quotes:
                    cols["date"].append(d)
                    cols["market"].append(q.market)
                    cols["code"].append(q.code)
                    cols["name"].append(q.name)
                    cols["open"].append(q.open)
                    cols["high"].append(q.high)
                    cols["low"].append(q.low)
                    cols["close"].append(q.close)
                    cols["volume_shares"].append(q.volume_shares)
                    cols["value_ntd"].append(q.value_ntd)
                    cols["managed"].append(bool(q.managed))
        if cols["date"]:
            df = pd.DataFrame(cols)
            df["date"] = pd.to_datetime(df["date"])
            for c in ("open", "high", "low", "close", "volume_shares", "value_ntd"):
                df[c] = df[c].astype("float64")
            price_rows_total += len(df)
            emit(f"prices_{year}{suffix}", df)

    idx_df = pd.DataFrame(idx_rows, columns=["date", "taiex_close", "taiex_tr_close"])
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    idx_df[["taiex_close", "taiex_tr_close"]] = idx_df[["taiex_close", "taiex_tr_close"]].astype("float64")
    emit(f"index_daily{suffix}", idx_df)

    cal_df = pd.DataFrame(sorted(status.items()), columns=["date", "status"])
    cal_df["date"] = pd.to_datetime(cal_df["date"])
    emit(f"calendar{suffix}", cal_df)

    grid = event_grid(end)
    ev_rows, ev_missing = [], []
    for g in grid:
        evs = store.get_events(*g)
        if evs is None:
            ev_missing.append(f"{g[0]} {g[1]} {g[2]}～{g[3]}")
            continue
        for ev in evs:
            ev_rows.append({"market": ev.market, "code": ev.code, "name": ev.name, "event_date": ev.event_date,
                            "kind": ev.kind, "p_before": ev.p_before, "p_ref": ev.p_ref, "source": ev.source,
                            "extra_json": json.dumps(ev.extra, ensure_ascii=False)})
    ev_df = pd.DataFrame(ev_rows, columns=list(EVENT_COLUMNS)).sort_values(
        ["event_date", "market", "code", "kind"], kind="stable").reset_index(drop=True)
    ev_df["event_date"] = pd.to_datetime(ev_df["event_date"])
    ev_df[["p_before", "p_ref"]] = ev_df[["p_before", "p_ref"]].astype("float64")
    emit(f"events{suffix}", ev_df)

    missing_days = [d.isoformat() for d in weekdays if d.isoformat() not in status]
    errors = dict(summary.day_errors) if summary else {}
    basics = store.get_snapshot("basics")
    delist = store.get_snapshot("delisting")
    bench_events = [f"{r['event_date']:%Y-%m-%d} {r['kind']} 前收 {r['p_before']} 參考 {r['p_ref']}"
                    for _, r in ev_df[(ev_df["code"] == BENCH_ETF) & (ev_df["market"] == "twse")].iterrows()]
    counts = Counter(status.values())
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "built_at": format_taipei_iso(now_taipei()),
        "data_start": DATA_START.isoformat(),
        "data_end": end.isoformat(),
        "weekdays_total": len(weekdays),
        "trading_days": counts.get("trading", 0),
        "non_trading_days": counts.get("non_trading", 0),
        "trading_days_by_year": {str(k): v for k, v in sorted(trading_by_year.items())},
        "missing_days": [{"date": d, "reason": errors.get(d, "尚未抓取或抓取失敗")} for d in missing_days],
        "taiex_tr_missing_days": tr_missing,
        "taiex_missing_days": taiex_missing,
        "bench_0050_missing_days": bench_missing,
        "unit_check_flags": unit_flags,
        "price_rows": price_rows_total,
        "events": {
            "chunks_total": len(grid),
            "chunks_missing": ev_missing,
            "count_by_market_kind": {f"{m}/{k}": int(n) for (m, k), n in
                                     ev_df.groupby(["market", "kind"]).size().items()} if len(ev_df) else {},
            "bench_0050": bench_events,
        },
        "snapshots": {
            "basics": None if basics is None else {
                "fetched_at": basics.get("fetched_at"),
                "twse_rows": len(basics.get("twse_basics") or []),
                "tpex_rows": len(basics.get("tpex_basics") or []),
                "finmind_stock_info_rows": len(basics.get("finmind_stock_info") or []),
                "finmind_error": basics.get("finmind_error")},
            "delisting": None if delist is None else {
                "fetched_at": delist.get("fetched_at"), "rows": len(delist.get("data") or []), "token": False},
        },
        "files": files,
    }
    manifest["complete"] = (not manifest["missing_days"] and not ev_missing and not tr_missing
                            and not taiex_missing and basics is not None
                            and bool(basics.get("twse_basics")) and bool(basics.get("tpex_basics")))
    tmp = store.parquet_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(store.parquet_dir / "manifest.json")
    return manifest


# ---------------------------------------------------------------- 報告

def _check(ok: bool | None) -> str:
    return "通過" if ok else ("無法判定" if ok is None else "不符")


def _short(items: list[Any], n: int = 20) -> str:
    items = [str(x) for x in items]
    return "、".join(items[:n]) + (f"…（共 {len(items)} 項）" if len(items) > n else "") if items else "無"


def render_report(manifest: dict[str, Any], summary: FetchSummary) -> str:
    m, ev, sn = manifest, manifest["events"], manifest["snapshots"]
    stopped = {"budget": "已達本次抓取時間上限（請再執行一次接續）",
               "consecutive_errors": f"連續 {MAX_CONSECUTIVE_ERRORS} 個平日抓取失敗而停止（疑似限流或官方異常，請稍後再執行）",
               None: "本次待抓項目已全部處理"}[summary.stopped]
    checks = [
        ("平日皆已確認（交易日或兩市皆休市）", not m["missing_days"], f"缺 {len(m['missing_days'])} 日：{_short([x['date'] + ' ' + x['reason'] for x in m['missing_days']], 10)}"),
        ("事件表各段皆已取得", not ev["chunks_missing"], f"共 {ev['chunks_total']} 段；缺 {_short(ev['chunks_missing'], 10)}"),
        (f"每個交易日皆有「{TR_INDEX_NAME}」", not m["taiex_tr_missing_days"], f"缺 {_short(m['taiex_tr_missing_days'])}"),
        ("每個交易日皆有加權指數收盤", not m["taiex_missing_days"], f"缺 {_short(m['taiex_missing_days'])}"),
        (f"每個交易日皆有 {BENCH_ETF} 收盤價", not m["bench_0050_missing_days"], f"缺 {_short(m['bench_0050_missing_days'])}"),
        ("成交量單位檢查（成交金額 ÷ 成交股數 ÷ 收盤價，每日中位數介於 0.8～1.25）", not m["unit_check_flags"],
         f"異常 {_short(m['unit_check_flags'])}"),
        ("股本基本資料快照（M7-Q4；FinMind 分類資料失敗時第一階段改用規則過濾，記為無法判定）",
         False if not sn["basics"] or not (sn["basics"]["twse_rows"] and sn["basics"]["tpex_rows"])
         else (None if sn["basics"].get("finmind_error") else True),
         json.dumps(sn["basics"], ensure_ascii=False)),
        ("FinMind 下市清單快照（無 token，M7-Q19；讀不到時報告改用一般性警語）", True if sn["delisting"] else None, json.dumps(sn["delisting"], ensure_ascii=False)),
    ]
    lines = [
        "# M7b 回測資料抓取報告",
        "",
        f"- 產生時間：{m['built_at']}（Asia/Taipei）",
        f"- 資料區間：{m['data_start']}～{m['data_end']}（M7-Q1）；資料完整：**{'是' if m['complete'] else '否'}**",
        f"- 本次：{stopped}；請求 {summary.requests} 次；耗時 {summary.elapsed_sec / 60:.1f} 分",
        f"- 日資料：平日 {summary.weekdays_total}，執行前快取已有 {summary.days_cached_before}，本次寫入 {summary.days_fetched}，"
        f"本次失敗／不完整 {len(summary.day_errors)}",
        f"- 事件表：{summary.events_total} 段，執行前快取已有 {summary.events_cached_before}，本次抓 {summary.events_fetched}，失敗 {len(summary.event_errors)}",
        f"- 交易日 {m['trading_days']}、兩市皆休市 {m['non_trading_days']}；各年交易日 {m['trading_days_by_year']}；行情列數 {m['price_rows']}",
        "- 只抓資料、不做回測；未讀取或傳送任何 token。",
        "",
        "## 一、檢查結果",
        "",
        "| 項目 | 結果 | 說明 |",
        "| :--- | :--- | :--- |",
    ]
    lines += [f"| {item} | {_check(ok)} | {str(detail).replace('|', '／')} |" for item, ok, detail in checks]
    lines += ["", "## 二、事件筆數（市場／類型）", "", "| 市場／類型 | 筆數 |", "| :--- | ---: |"]
    lines += [f"| {k} | {v} |" for k, v in ev["count_by_market_kind"].items()]
    lines += ["", f"## 三、{BENCH_ETF} 事件（M7-Q14 基準還原用）", ""]
    lines += [f"- {x}" for x in ev["bench_0050"]] or ["- 查無"]
    lines += ["", "## 四、Parquet 檔案", "", "| 檔案 | 列數 | 位元組 | SHA-256 |", "| :--- | ---: | ---: | :--- |"]
    lines += [f"| {f['file']} | {f['rows']} | {f['bytes']} | `{f['sha256']}` |" for f in m["files"]]
    if summary.day_errors or summary.event_errors or summary.snapshot_notes:
        lines += ["", "## 五、本次錯誤與備註", ""]
        lines += [f"- 日 {d}：{r}" for d, r in summary.day_errors[:50]]
        lines += [f"- 事件 {k}：{r}" for k, r in summary.event_errors[:50]]
        lines += [f"- {x}" for x in summary.snapshot_notes]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 入口

def run(cache_dir: str, out_dir: str, end_raw: str | None = None, budget_min: float | None = None,
        client: HttpClient | None = None, today: date | None = None,
        writer: Callable[[Any, Path], None] = write_parquet, suffix: str = ".parquet",
        clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print) -> int:
    from src.config import load_config

    today = today or today_taipei()
    end = resolve_end(end_raw, today)
    if client is None:
        cfg, _ = load_config(env={})
        client = HttpClient.from_config(cfg)
    store = BacktestStore(cache_dir)
    deadline = clock() + budget_min * 60 if budget_min else None
    log(f"M7b 回測資料抓取：{DATA_START}～{end}；快取 {cache_dir}；時間上限 {budget_min or '不限'} 分")
    summary = fetch_all(client, store, end, today, deadline=deadline, clock=clock, log=log)
    manifest = build_parquet(store, end, summary, writer=writer, suffix=suffix)
    report = render_report(manifest, summary)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report.encode("utf-8")[:1_000_000].decode("utf-8", errors="ignore"))
    log(report)
    if summary.stopped == "budget":
        return EXIT_BUDGET
    return EXIT_OK if manifest["complete"] else EXIT_INCOMPLETE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M7b：抓取多年官方行情並建立 Parquet 回測快取（不做回測）")
    ap.add_argument("command", choices=["fetch"])
    ap.add_argument("--cache-dir", default=".cache/backtest")
    ap.add_argument("--out", default="backtest_fetch_report")
    args = ap.parse_args(argv)
    # 以環境變數接收 workflow 輸入，避免把輸入值展開在 shell 指令中
    budget_raw = os.environ.get("BT_FETCH_BUDGET_MIN", "").strip()
    budget = float(budget_raw) if budget_raw else None
    if budget is not None and budget <= 0:
        raise SystemExit("BT_FETCH_BUDGET_MIN 需為正數")
    return run(args.cache_dir, args.out, os.environ.get("BT_END_DATE"), budget)


if __name__ == "__main__":
    sys.exit(main())
