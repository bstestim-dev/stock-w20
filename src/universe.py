"""標的範圍（spec_v1 第 4.1 節；第 9 節 FinMind 斷線降級）。

普通股（核心選股池）：
- 來源：TWSE ``/opendata/t187ap03_L``（上市）＋ TPEx ``/mopsfin_t187ap03_O``（上櫃），以代號識別；
  未列入兩表者（興櫃等）不納入。
- 代號須符合 ``^[1-9]\\d{3}$``（權證、ETN、特別股、REITs 代號皆不符而排除）。
- 排除存託憑證：TWSE ``產業別 == "91"``，或 FinMind ``TaiwanStockInfo`` 最新列標為「存託憑證」。
- 創新板（TIB）照樣納入；``is_tib`` 依 FinMind 類別含「創新板」「創新版」「TIB」判定。
- U1：實收資本額 > ``universe.min_paid_in_capital_ntd``（嚴格大於 20 億元）。
- 參考市值（億元）＝ 原始最新收盤價 × 已發行普通股數 ÷ 10^8（不設硬過濾）。

ETF（獨立分頁）：
- FinMind 可用：最新列 ``industry_category`` 含 "ETF" 且 ``type`` 為 twse／tpex；
  不可用：當期行情中代號以 "00" 開頭、長度 4～6 碼者。
- 子類型：末碼 L／R → leveraged_inverse；末碼 B 或名稱含「債」→ bond；末碼 A → active；其餘 equity_regular。
  同時符合多項時依上列順序取第一項（2026-11-25 裁定 M4-Q6；例：00680L 元大美債20正2 歸槓反）。
- FinMind 不可用時 ``is_tib`` 一律 false 並於 notes 註明（M4-Q9）。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.fetchers.parsing import parse_number

COMMON_CODE_RE = re.compile(r"^[1-9]\d{3}$")
TWSE_DR_INDUSTRY = "91"
FINMIND_DR_CATEGORY = "存託憑證"
TIB_KEYWORDS = ("創新板", "創新版", "TIB")
ETF_KEYWORD = "ETF"
LISTED_TYPES = ("twse", "tpex")

ETF_SUBTYPES = ("leveraged_inverse", "bond", "active", "equity_regular")


@dataclass
class Security:
    code: str
    name: str
    market: str                       # "twse" | "tpex"
    asset_type: str                   # "common_stock" | "etf"
    is_tib: bool = False
    etf_subtype: str | None = None
    industry: str | None = None       # 顯示用產業別（FinMind 類別；不可用時為官方代碼，Q6）
    official_industry_code: str | None = None
    finmind_categories: list[str] = field(default_factory=list)
    capital_ntd: float | None = None
    issued_shares: float | None = None
    etf_subtype_matches: list[str] = field(default_factory=list)   # 符合的所有子類型規則（M4-Q6 統計用）


@dataclass
class Universe:
    commons: dict[str, Security]
    etfs: dict[str, Security]
    finmind_available: bool
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 分類工具

def etf_subtype_matches(code: str, name: str) -> list[str]:
    c = code.strip().upper()
    out = []
    if c.endswith(("L", "R")):
        out.append("leveraged_inverse")
    if c.endswith("B") or "債" in name:
        out.append("bond")
    if c.endswith("A"):
        out.append("active")
    return out


def etf_subtype(code: str, name: str) -> str:
    m = etf_subtype_matches(code, name)
    return m[0] if m else "equity_regular"


def _categories(rows: Iterable[Mapping[str, Any]] | None) -> list[str]:
    seen: list[str] = []
    for r in rows or []:
        c = str(r.get("industry_category", "")).strip()
        if c and c not in seen:
            seen.append(c)
    return seen


def _is_tib(categories: Iterable[str]) -> bool:
    return any(k in c for c in categories for k in TIB_KEYWORDS)


def _finmind_is_etf(rows: Iterable[Mapping[str, Any]] | None) -> bool:
    return any(ETF_KEYWORD in str(r.get("industry_category", "")) and str(r.get("type", "")).strip() in LISTED_TYPES
               for r in rows or [])


def _num(raw: Any) -> float | None:
    try:
        return parse_number(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------- 主流程

def build_universe(
    twse_basics: list[Mapping[str, Any]],
    tpex_basics: list[Mapping[str, Any]],
    quotes_latest: Mapping[str, Mapping[str, Any]],
    finmind_rows: Mapping[str, list[Mapping[str, Any]]] | None,
) -> Universe:
    """``quotes_latest``：{代號: {"market", "name"}}，取自回溯期間行情（判定 ETF 與市場別）。
    ``finmind_rows``：``finmind.latest_rows_by_stock`` 結果；None 表示 FinMind 不可用（第 9 節降級）。"""
    fm = finmind_rows
    notes: list[str] = []
    stats: Counter = Counter()
    if fm is None:
        notes.append("FinMind TaiwanStockInfo 不可用：ETF 改以代號 00 開頭辨識、存託憑證僅以 TWSE 產業別 91 排除、"
                     "產業別顯示官方代碼、無法標示創新板（is_tib）")

    commons: dict[str, Security] = {}
    dup: list[str] = []
    for market, records, k_code, k_name, k_ind, k_cap, k_shares in (
        ("twse", twse_basics, "公司代號", "公司簡稱", "產業別", "實收資本額", "已發行普通股數或TDR原股發行股數"),
        ("tpex", tpex_basics, "SecuritiesCompanyCode", "CompanyAbbreviation", "SecuritiesIndustryCode",
         "Paidin.Capital.NTDollars", "IssueShares"),
    ):
        for rec in records:
            code = str(rec.get(k_code, "")).strip()
            stats[f"{market}_basics_rows"] += 1
            if not COMMON_CODE_RE.match(code):
                stats[f"{market}_excluded_code_pattern"] += 1
                continue
            ind = str(rec.get(k_ind, "")).strip()
            cats = _categories(fm.get(code)) if fm is not None else []
            if (market == "twse" and ind == TWSE_DR_INDUSTRY) or FINMIND_DR_CATEGORY in cats:
                stats[f"{market}_excluded_dr"] += 1
                continue
            sec = Security(
                code=code, name=str(rec.get(k_name, "")).strip(), market=market, asset_type="common_stock",
                is_tib=_is_tib(cats), industry=("／".join(cats) if cats else (ind or None)),
                official_industry_code=ind or None, finmind_categories=cats,
                capital_ntd=_num(rec.get(k_cap)), issued_shares=_num(rec.get(k_shares)),
            )
            if code in commons:
                dup.append(code)
                q = quotes_latest.get(code)
                if q is None or q.get("market") != market:
                    continue
            commons[code] = sec
    if dup:
        notes.append(f"上市、上櫃基本資料表重複出現的代號 {sorted(set(dup))}，以行情所屬市場為準")
    stats["commons"] = len(commons)
    stats["commons_tib"] = sum(1 for s in commons.values() if s.is_tib)
    stats["commons_without_quotes"] = sum(1 for c in commons if c not in quotes_latest)
    stats["commons_capital_missing"] = sum(1 for s in commons.values() if s.capital_ntd is None)

    etfs: dict[str, Security] = {}
    if fm is not None:
        etf_codes = {c for c, rows in fm.items() if _finmind_is_etf(rows)}
        stats["finmind_etf_codes"] = len(etf_codes)
        stats["finmind_etf_without_quotes"] = len(etf_codes - set(quotes_latest))
        stats["quote_00_codes_not_finmind_etf"] = sum(
            1 for c in quotes_latest if c.startswith("00") and c not in etf_codes)
    else:
        etf_codes = {c for c in quotes_latest if c.startswith("00") and 4 <= len(c) <= 6}
    for code in sorted(etf_codes):
        q = quotes_latest.get(code)
        if q is None:
            continue
        name = str(q.get("name", "")).strip()
        matches = etf_subtype_matches(code, name)
        etfs[code] = Security(code=code, name=name, market=str(q.get("market")), asset_type="etf",
                              etf_subtype=matches[0] if matches else "equity_regular",
                              etf_subtype_matches=matches,
                              finmind_categories=_categories(fm.get(code)) if fm is not None else [])
    stats["etfs"] = len(etfs)
    for sub in ETF_SUBTYPES:
        stats[f"etf_{sub}"] = sum(1 for e in etfs.values() if e.etf_subtype == sub)
    stats["etf_subtype_multi_match"] = sum(1 for e in etfs.values() if len(e.etf_subtype_matches) > 1)

    return Universe(commons=commons, etfs=etfs, finmind_available=fm is not None, notes=notes, stats=dict(stats))
