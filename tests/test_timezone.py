from datetime import date, datetime, timedelta, timezone

import pytest

from src import timezone as tz


# ---------- 現在時間與時區

def test_now_taipei_is_utc_plus_8():
    now = tz.now_taipei()
    assert now.utcoffset() == timedelta(hours=8)


def test_to_taipei_converts_utc():
    utc = datetime(2026, 11, 24, 16, 30, tzinfo=timezone.utc)
    assert tz.to_taipei(utc) == datetime(2026, 11, 25, 0, 30, tzinfo=tz.TAIPEI)
    assert tz.to_taipei(utc).date() == date(2026, 11, 25)  # UTC 前一日深夜 = 台北隔日


def test_to_taipei_rejects_naive():
    with pytest.raises(ValueError):
        tz.to_taipei(datetime(2026, 11, 25, 18, 0))


def test_format_taipei_iso_matches_schema_example():
    dt = datetime(2026, 11, 25, 10, 5, 12, 999, tzinfo=timezone.utc)
    assert tz.format_taipei_iso(dt) == "2026-11-25T18:05:12+08:00"


def test_format_taipei_display():
    dt = datetime(2026, 11, 25, 18, 5, tzinfo=tz.TAIPEI)
    assert tz.format_taipei_display(dt) == "2026-11-25 18:05 Asia/Taipei"


# ---------- 民國年日期（規格第 3 節實測格式）

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1151124", date(2026, 11, 24)),          # TWSE OpenAPI Date
        ("1150112", date(2026, 1, 12)),           # TPEx revivt 恢復買賣日期
        ("115/11/02", date(2026, 11, 2)),         # STOCK_DAY 日期
        ("115/01/12", date(2026, 1, 12)),         # TWTAUU 恢復買賣日期
        ("115年01月02日", date(2026, 1, 2)),       # TWT49U 資料日期
        (" 115/11/02 ", date(2026, 11, 2)),       # 前後空白
        ("991231", date(2010, 12, 31)),           # 民國 100 年前 6 碼
        ("99/1/5", date(2010, 1, 5)),
        ("1150229", None),                        # 2026 非閏年 → 錯誤
    ],
)
def test_parse_roc_date(raw, expected):
    if expected is None:
        with pytest.raises(ValueError):
            tz.parse_roc_date(raw)
    else:
        assert tz.parse_roc_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "2026-11-24", "115-11-24", "11511", "abc", "115/13/01"])
def test_parse_roc_date_rejects_invalid(raw):
    with pytest.raises(ValueError):
        tz.parse_roc_date(raw)


def test_parse_roc_date_rejects_non_string():
    with pytest.raises(ValueError):
        tz.parse_roc_date(1151124)


def test_parse_roc_year_month():
    assert tz.parse_roc_year_month("11510") == (2026, 10)
    assert tz.parse_roc_year_month("9912") == (2010, 12)
    with pytest.raises(ValueError):
        tz.parse_roc_year_month("11513")
    with pytest.raises(ValueError):
        tz.parse_roc_year_month("2026-10")


def test_parse_roc_year():
    assert tz.parse_roc_year("115") == 2026
    assert tz.parse_roc_year(" 99 ") == 2010
    with pytest.raises(ValueError):
        tz.parse_roc_year("2026")
    with pytest.raises(ValueError):
        tz.parse_roc_year("0")


def test_parse_roc_period_tpex_and_twse_formats():
    expected = (date(2026, 11, 25), date(2026, 12, 1))
    assert tz.parse_roc_period("1151125~1151201") == expected          # TPEx
    assert tz.parse_roc_period("115/11/25～115/12/01") == expected     # TWSE 全形～


@pytest.mark.parametrize("raw", ["1151201~1151125", "1151125", "1151125~1151201~1151202"])
def test_parse_roc_period_rejects_invalid(raw):
    with pytest.raises(ValueError):
        tz.parse_roc_period(raw)


def test_parse_western_slash_date():
    assert tz.parse_western_slash_date("2026/11/02") == date(2026, 11, 2)
    with pytest.raises(ValueError):
        tz.parse_western_slash_date("115/11/02")


def test_parse_iso_date():
    assert tz.parse_iso_date("2026-11-24") == date(2026, 11, 24)
    for bad in ("2026/11/24", "20261124", "2026-02-30", ""):
        with pytest.raises(ValueError):
            tz.parse_iso_date(bad)


def test_roc_conversions_roundtrip():
    assert tz.roc_year_to_western(115) == 2026
    assert tz.western_year_to_roc(2026) == 115
    assert tz.format_roc_compact(date(2026, 11, 24)) == "1151124"
    assert tz.parse_roc_date(tz.format_roc_compact(date(2010, 3, 4))) == date(2010, 3, 4)
    with pytest.raises(ValueError):
        tz.roc_year_to_western(0)
    with pytest.raises(ValueError):
        tz.western_year_to_roc(1911)


# ---------- 查詢參數格式

def test_query_date_formats():
    d = date(2026, 1, 15)
    assert tz.format_twse_query_date(d) == "20260115"     # MI_INDEX date=
    assert tz.format_tpex_query_date(d) == "2026/01/15"   # dailyQuotes date=


# ---------- 交易週

@pytest.mark.parametrize(
    "d, expected",
    [
        (date(2026, 11, 24), "2026-W48"),   # 規格 latest.json 範例
        (date(2026, 11, 20), "2026-W47"),
        (date(2026, 1, 1), "2026-W01"),
        (date(2025, 12, 29), "2026-W01"),   # 跨年週歸新年
        (date(2027, 1, 1), "2026-W53"),     # 跨年週歸舊年（2026 有 53 週）
    ],
)
def test_iso_week_id(d, expected):
    assert tz.iso_week_id(d) == expected


def test_week_helpers():
    assert tz.week_monday(date(2026, 11, 27)) == date(2026, 11, 23)  # 週五
    assert tz.week_monday(date(2026, 11, 29)) == date(2026, 11, 23)  # 週日
    days = tz.week_weekdays(date(2026, 11, 25))
    assert days[0] == date(2026, 11, 23) and days[-1] == date(2026, 11, 27) and len(days) == 5
