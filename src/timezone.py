"""Asia/Taipei（UTC+8）時間工具與民國年日期轉換。

支援規格書第 3 節實測到的官方日期格式：

- ``"1151124"``            7 碼民國年月日（TWSE OpenAPI ``Date``、TPEx ``revivt``）
- ``"115/11/02"``          民國年斜線（``STOCK_DAY`` ``日期``、``TWTAUU`` 等）
- ``"115年01月02日"``       民國年中文（``TWT49U`` ``資料日期``）
- ``"11510"``              民國年月（月營收 ``資料年月``）
- ``"115"``                民國年（季 EPS ``年度``）
- ``"1151125~1151201"``    TPEx 處置期間
- ``"115/11/25～115/12/01"`` TWSE 處置期間（全形波浪號）
- ``"2026/11/02"``         西元斜線（TPEx ``indexInfo/inx``）
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
ROC_OFFSET = 1911

_RE_ROC_COMPACT = re.compile(r"^(\d{2,3})(\d{2})(\d{2})$")
_RE_ROC_SLASH = re.compile(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$")
_RE_ROC_CJK = re.compile(r"^(\d{2,3})年(\d{1,2})月(\d{1,2})日$")
_RE_ROC_YM = re.compile(r"^(\d{2,3})(\d{2})$")
_RE_ROC_Y = re.compile(r"^(\d{2,3})$")
_RE_WEST_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_PERIOD_SEPARATORS = ("～", "~")


# ---------------------------------------------------------------- 現在時間

def now_taipei() -> datetime:
    """目前 Asia/Taipei 時間（含時區）。"""
    return datetime.now(TAIPEI)


def today_taipei() -> date:
    """目前 Asia/Taipei 日期。"""
    return now_taipei().date()


def to_taipei(dt: datetime) -> datetime:
    """將含時區的 datetime 轉為 Asia/Taipei；不接受無時區（naive）datetime，避免誤判。"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime 不可直接轉換，請先標明時區")
    return dt.astimezone(TAIPEI)


def format_taipei_iso(dt: datetime) -> str:
    """輸出秒精度 ISO 8601，例如 ``2026-11-25T18:05:12+08:00``（供 ``generated_at_taipei``）。"""
    return to_taipei(dt).replace(microsecond=0).isoformat()


def format_taipei_display(dt: datetime) -> str:
    """網頁顯示用：``YYYY-MM-DD HH:mm Asia/Taipei``。"""
    return to_taipei(dt).strftime("%Y-%m-%d %H:%M") + " Asia/Taipei"


# ---------------------------------------------------------------- 民國年解析

def roc_year_to_western(roc_year: int) -> int:
    if roc_year <= 0:
        raise ValueError(f"民國年需為正整數：{roc_year}")
    return roc_year + ROC_OFFSET


def western_year_to_roc(year: int) -> int:
    roc = year - ROC_OFFSET
    if roc <= 0:
        raise ValueError(f"西元 {year} 年早於民國元年")
    return roc


def _make_date(y: int, m: int, d: int, raw: str) -> date:
    try:
        return date(y, m, d)
    except ValueError as exc:
        raise ValueError(f"無效日期：{raw!r}") from exc


def parse_roc_date(value: str) -> date:
    """解析民國年日期，支援 ``1151124``、``115/11/02``、``115年01月02日``。"""
    if not isinstance(value, str):
        raise ValueError(f"日期需為字串：{value!r}")
    s = value.strip()
    for pattern in (_RE_ROC_COMPACT, _RE_ROC_SLASH, _RE_ROC_CJK):
        m = pattern.match(s)
        if m:
            roc_y, mo, d = (int(g) for g in m.groups())
            return _make_date(roc_year_to_western(roc_y), mo, d, value)
    raise ValueError(f"無法辨識的民國年日期格式：{value!r}")


def parse_roc_year_month(value: str) -> tuple[int, int]:
    """解析民國年月 ``11510`` → ``(2026, 10)``。"""
    s = str(value).strip()
    m = _RE_ROC_YM.match(s)
    if not m:
        raise ValueError(f"無法辨識的民國年月格式：{value!r}")
    roc_y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        raise ValueError(f"無效月份：{value!r}")
    return roc_year_to_western(roc_y), mo


def parse_roc_year(value: str) -> int:
    """解析民國年 ``115`` → ``2026``。"""
    s = str(value).strip()
    if not _RE_ROC_Y.match(s):
        raise ValueError(f"無法辨識的民國年格式：{value!r}")
    return roc_year_to_western(int(s))


def parse_roc_period(value: str) -> tuple[date, date]:
    """解析處置期間 ``1151125~1151201`` 或 ``115/11/25～115/12/01``。"""
    s = str(value).strip()
    for sep in _PERIOD_SEPARATORS:
        if sep in s:
            parts = s.split(sep)
            if len(parts) != 2:
                break
            start, end = parse_roc_date(parts[0]), parse_roc_date(parts[1])
            if end < start:
                raise ValueError(f"期間起訖顛倒：{value!r}")
            return start, end
    raise ValueError(f"無法辨識的期間格式：{value!r}")


def parse_western_slash_date(value: str) -> date:
    """解析西元斜線日期 ``2026/11/02``。"""
    s = str(value).strip()
    m = _RE_WEST_SLASH.match(s)
    if not m:
        raise ValueError(f"無法辨識的西元日期格式：{value!r}")
    y, mo, d = (int(g) for g in m.groups())
    return _make_date(y, mo, d, value)


def parse_iso_date(value: str) -> date:
    """解析 ``YYYY-MM-DD``（workflow 輸入 ``as_of_date`` 用）。"""
    s = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"日期需為 YYYY-MM-DD：{value!r}")
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"無效日期：{value!r}") from exc


# ---------------------------------------------------------------- 查詢參數格式

def format_twse_query_date(d: date) -> str:
    """TWSE RWD 查詢參數 ``YYYYMMDD``。"""
    return d.strftime("%Y%m%d")


def format_tpex_query_date(d: date) -> str:
    """TPEx 網站查詢參數 ``YYYY/MM/DD``（西元）。"""
    return d.strftime("%Y/%m/%d")


def format_roc_compact(d: date) -> str:
    """西元日期轉 7 碼民國年 ``1151124``（民國 100 年以前為 6 碼）。"""
    return f"{western_year_to_roc(d.year)}{d.month:02d}{d.day:02d}"


# ---------------------------------------------------------------- 交易週

def iso_week_id(d: date) -> str:
    """ISO 年週 ``YYYY-Www``；跨年週依 ISO 年（例：2027-01-01 → ``2026-W53``）。"""
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_monday(d: date) -> date:
    """d 所在自然週（週一至週日）的週一。"""
    return d - timedelta(days=d.weekday())


def week_weekdays(d: date) -> list[date]:
    """d 所在自然週的週一至週五日期。"""
    monday = week_monday(d)
    return [monday + timedelta(days=i) for i in range(5)]
