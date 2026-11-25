from datetime import date, timedelta

import pytest

from src.fetchers import history, tpex, twse
from src.fetchers.http import FetchError
from src.fetchers.parsing import DailyMarket, DailyQuote

TODAY = date(2026, 11, 25)  # 週三


def market(name, d, trading=True):
    quotes = [DailyQuote(name, "2330" if name == "twse" else "6488", "x", 1.0, 2.0, 0.5, 1.5, 1000.0, 1500.0)] if trading else []
    return DailyMarket(name, d, trading, quotes, 23000.0 if (name == "twse" and trading) else None)


def install(monkeypatch, twse_rule, tpex_rule):
    """rule(d) 回傳 True（交易）、False（非交易）或 Exception（失敗）。"""
    calls = []

    def mk(name, rule):
        def fn(client, d):
            calls.append((name, d))
            r = rule(d)
            if isinstance(r, Exception):
                raise r
            return market(name, d, r)
        return fn

    monkeypatch.setattr(twse, "fetch_mi_index", mk("twse", twse_rule))
    monkeypatch.setattr(tpex, "fetch_daily_quotes", mk("tpex", tpex_rule))
    return calls


HOLIDAYS = {date(2026, 10, 9)}


def normal(d):
    return d not in HOLIDAYS


def test_weekends_not_requested_and_holidays_skipped(monkeypatch):
    calls = install(monkeypatch, normal, normal)
    res = history.fetch_daily_history(None, date(2026, 10, 16), lookback_weeks=2, today=TODAY)
    assert res.start == date(2026, 10, 5)
    assert all(d.weekday() < 5 for _, d in calls)
    assert len(calls) == 20                                   # 10 平日 × 2 市場
    assert date(2026, 10, 9) not in [d.trade_date for d in res.trading_days]
    assert res.as_of_date == date(2026, 10, 16) and res.missing == [] and res.notes == []


def test_as_of_falls_back_to_latest_common_day(monkeypatch):
    # 今日上市已更新、上櫃尚未更新 → 退回昨日，並提示
    install(monkeypatch, normal, lambda d: d < TODAY)
    res = history.fetch_daily_history(None, TODAY, lookback_weeks=2, today=TODAY)
    assert res.as_of_date == TODAY - timedelta(days=1)
    assert len(res.notes) == 1 and str(TODAY) in res.notes[0]
    assert res.missing == []                                  # 基準日後不計缺漏


def test_end_after_today_is_clamped(monkeypatch):
    install(monkeypatch, normal, normal)
    res = history.fetch_daily_history(None, TODAY + timedelta(days=7), lookback_weeks=1, today=TODAY)
    assert res.as_of_date == TODAY


def test_isolated_failures_are_tolerated_and_listed(monkeypatch):
    bad = date(2026, 11, 11)
    install(monkeypatch, lambda d: FetchError("逾時") if d == bad else normal(d), normal)
    res = history.fetch_daily_history(None, date(2026, 11, 24), lookback_weeks=30, today=TODAY)
    assert [d for d, _ in res.missing] == [bad]
    assert bad not in [d.trade_date for d in res.trading_days]


def test_more_than_three_consecutive_missing_fails(monkeypatch):
    bad = {date(2026, 11, 16) + timedelta(days=i) for i in range(4)}   # 週一～週四
    install(monkeypatch, lambda d: FetchError("x") if d in bad else normal(d), normal)
    with pytest.raises(history.HistoryIncomplete, match="連續缺漏"):
        history.fetch_daily_history(None, date(2026, 11, 24), lookback_weeks=30, today=TODAY)


def test_three_consecutive_missing_allowed(monkeypatch):
    bad = {date(2026, 11, 16) + timedelta(days=i) for i in range(3)}
    install(monkeypatch, lambda d: FetchError("x") if d in bad else normal(d), normal)
    res = history.fetch_daily_history(None, date(2026, 11, 24), lookback_weeks=30, today=TODAY)
    assert len(res.missing) == 3


def test_holiday_between_failures_does_not_reset_or_extend_run(monkeypatch):
    # 失敗、休市、失敗、失敗、失敗 → 連續 4 個交易日缺漏
    days = [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 12), date(2026, 10, 13)]
    install(monkeypatch, lambda d: FetchError("x") if d in days else normal(d), normal)
    with pytest.raises(history.HistoryIncomplete):
        history.fetch_daily_history(None, date(2026, 10, 20), lookback_weeks=4, today=TODAY)


def test_missing_ratio_over_five_percent_fails(monkeypatch):
    # 4 週 20 個交易日，間隔失敗 2 日 = 10%
    bad = {date(2026, 11, 3), date(2026, 11, 10)}
    install(monkeypatch, lambda d: FetchError("x") if d in bad else normal(d), normal)
    with pytest.raises(history.HistoryIncomplete, match="超過 5%"):
        history.fetch_daily_history(None, date(2026, 11, 20), lookback_weeks=4, today=TODAY)


def test_partial_day_in_history_counts_as_missing(monkeypatch):
    odd = date(2026, 11, 12)
    install(monkeypatch, normal, lambda d: d != odd and normal(d))
    res = history.fetch_daily_history(None, date(2026, 11, 24), lookback_weeks=30, today=TODAY)
    assert [d for d, _ in res.missing] == [odd] and "僅一方" in res.missing[0][1]


def test_no_trading_day_raises(monkeypatch):
    install(monkeypatch, lambda d: False, lambda d: False)
    with pytest.raises(history.HistoryIncomplete):
        history.fetch_daily_history(None, TODAY, lookback_weeks=1, today=TODAY)


def test_cache_roundtrip_and_skips_today(monkeypatch, tmp_path):
    calls = install(monkeypatch, normal, normal)
    cache = history.DayCache(tmp_path)
    history.fetch_daily_history(None, TODAY, lookback_weeks=1, cache=cache, today=TODAY)
    first = len(calls)
    res = history.fetch_daily_history(None, TODAY, lookback_weeks=1, cache=cache, today=TODAY)
    assert len(calls) - first == 2                            # 只有「今日」重抓
    cached = [d for d in res.days if d.from_cache]
    assert len(cached) == 2 and cached[0].twse.quotes[0].code == "2330"
    assert not (tmp_path / f"{TODAY.isoformat()}.json.gz").exists()


def test_cache_does_not_store_errors(tmp_path):
    cache = history.DayCache(tmp_path)
    r = history.DayResult(date(2026, 11, 2), None, None, ["fail"])
    assert cache.put(r, TODAY) is False


def test_history_to_frames(monkeypatch):
    install(monkeypatch, normal, normal)
    res = history.fetch_daily_history(None, date(2026, 11, 20), lookback_weeks=1, today=TODAY)
    quotes, idx = history.history_to_frames(res)
    assert len(quotes) == 10 and set(quotes["market"]) == {"twse", "tpex"}
    assert list(idx["close"].unique()) == [23000.0] and len(idx) == 5



def test_cache_backward_compatible_without_managed_field(tmp_path):
    import gzip, json
    old = {"market": "tpex", "trade_date": "2026-11-02", "is_trading": True, "index_close": None,
           "quotes": [{"market": "tpex", "code": "6488", "name": "x", "open": 1.0, "high": 1.0, "low": 1.0,
                       "close": 1.0, "volume_shares": 1.0, "value_ntd": 1.0}]}
    tw = {**old, "market": "twse", "index_close": 1.0}
    (tmp_path / "2026-11-02.json.gz").write_bytes(gzip.compress(json.dumps({"twse": tw, "tpex": old}).encode()))
    r = history.DayCache(tmp_path).get(date(2026, 11, 2))
    assert r is not None and r.tpex.quotes[0].managed is False


def test_managed_flag_survives_cache_roundtrip(tmp_path):
    d = date(2026, 11, 2)
    q = DailyQuote("tpex", "5555", "x", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, managed=True)
    r = history.DayResult(d, market("twse", d), DailyMarket("tpex", d, True, [q]))
    cache = history.DayCache(tmp_path)
    assert cache.put(r, TODAY)
    assert cache.get(d).tpex.quotes[0].managed is True
