"""設定檔載入、驗證與環境變數覆寫。

- 欄位與型別依 spec_v1 第 6.1 節（另含 2026-11-25 裁定新增之欄位，見 docs/decisions.md）。
- 已定案規則以「鎖定值」驗證，改值即報錯（IR-2）。
- 設定檔不得含任何機密；``FINMIND_TOKEN`` 只從環境變數讀取（IR-6）。
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from src.timezone import parse_iso_date, today_taipei

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# S3／S4 需 t-26..t 共 27 根週 K（2026-11-25 Q1 計算）；低於此值無法完成評分。
MIN_LOOKBACK_WEEKS = 27

ENV_AS_OF_DATE = "SW20_AS_OF_DATE"
ENV_LOOKBACK_WEEKS = "SW20_LOOKBACK_WEEKS"
ENV_VOLUME_SPIKE_RATIO = "SW20_VOLUME_SPIKE_RATIO"
ENV_SKIP_FUNDAMENTALS = "SW20_SKIP_FUNDAMENTALS"
ENV_FINMIND_TOKEN = "FINMIND_TOKEN"

ETF_SUBTYPES = ("equity_regular", "bond", "leveraged_inverse", "active")


class ConfigError(ValueError):
    """設定檔不符規格。"""


# 型別標記
INT, FLOAT, BOOL, STR, LIST_STR, DATE_STR = "int", "float", "bool", "str", "list[str]", "date_str"
OPT_FLOAT = "float|null"

SCHEMA: dict[str, Any] = {
    "timezone": STR,
    "data_source": {
        "primary": STR,
        "lookback_weeks": INT,
        "request_delay_sec": FLOAT,
        "max_retries": INT,
        "timeout_sec": FLOAT,
        "enable_optional_fundamentals": BOOL,
    },
    "universe": {
        "min_paid_in_capital_ntd": INT,
        "min_avg_daily_lots_20w": FLOAT,
        "include_tib": BOOL,
        "exclude_dr": BOOL,
        "exclude_preferred": BOOL,
        "exclude_etn": BOOL,
        "exclude_warrants": BOOL,
        "etf_min_avg_daily_lots_20w": FLOAT,
    },
    "screening": {
        "ma_weeks": INT,
        "vol_lookback_weeks": INT,
        "volume_spike_ratio": FLOAT,
        "require_volume_spike_on_new_cross": BOOL,
        "midweek_volume_basis": STR,
        "midweek_show_projected_ratio": BOOL,
        "bias_warning_pct": FLOAT,
        "pivot_k_weeks": INT,
        "trendline_max_angle_deg": FLOAT,
        "trendline_min_pivot_gap_weeks": INT,
        "trendline_steep_warning_log_rate": OPT_FLOAT,
    },
    "ui": {
        "etf_default_hide_subtypes": LIST_STR,
    },
    "market_timing": {
        "d_short": INT,
        "d_long": INT,
        "w_ma": INT,
        "hard_filter_on_green": BOOL,
    },
    "backtest": {
        "primary_data": STR,
        "start_date": DATE_STR,
        "end_date": DATE_STR,
        "fee_rate": FLOAT,
        "fee_discount": FLOAT,
        "tax_rate_stock": FLOAT,
        "tax_rate_etf": FLOAT,
        "slippage": FLOAT,
        "max_positions": INT,
    },
}

# 已定案、不得於設定檔更動的值（IR-2）
LOCKED_VALUES: dict[tuple[str, ...], Any] = {
    ("timezone",): "Asia/Taipei",
    ("universe", "min_paid_in_capital_ntd"): 2_000_000_000,
    ("universe", "include_tib"): True,
    ("universe", "exclude_dr"): True,
    ("universe", "exclude_preferred"): True,
    ("universe", "exclude_etn"): True,
    ("universe", "exclude_warrants"): True,
    ("universe", "etf_min_avg_daily_lots_20w"): 0,
    ("screening", "require_volume_spike_on_new_cross"): False,
    ("screening", "midweek_volume_basis"): "actual",
    ("market_timing", "hard_filter_on_green"): False,
}

ALLOWED_CHOICES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("data_source", "primary"): ("official_twse_tpex", "finmind_adj"),
    ("backtest", "primary_data"): ("official_twse_tpex_local_cache", "finmind_adj"),
}

# 機密防護：鍵名或值疑似機密即拒絕
_SECRET_KEY_RE = re.compile(r"(token|secret|passw(or)?d|api[_-]?key|apikey|credential|private[_-]?key)", re.I)
_JWT_LIKE_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_LONG_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_\-+/=]{40,}$")


@dataclass(frozen=True)
class RunOptions:
    """單次執行參數（不屬於 config.yaml）。"""

    as_of_date: date | None  # None = 取最新可用交易日


# ---------------------------------------------------------------- 驗證

def _path_str(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _check_type(value: Any, kind: str, path: tuple[str, ...]) -> None:
    where = _path_str(path)
    if kind == INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} 需為整數，實得 {value!r}")
    elif kind == FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} 需為數值，實得 {value!r}")
    elif kind == OPT_FLOAT:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ConfigError(f"{where} 需為數值或 null，實得 {value!r}")
    elif kind == BOOL:
        if not isinstance(value, bool):
            raise ConfigError(f"{where} 需為 true/false，實得 {value!r}")
    elif kind == STR:
        if not isinstance(value, str):
            raise ConfigError(f"{where} 需為字串，實得 {value!r}")
    elif kind == LIST_STR:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where} 需為字串陣列，實得 {value!r}")
    elif kind == DATE_STR:
        if not isinstance(value, str):
            raise ConfigError(f"{where} 需為 \"YYYY-MM-DD\" 字串，實得 {value!r}")
        try:
            parse_iso_date(value)
        except ValueError as exc:
            raise ConfigError(f"{where}：{exc}") from exc
    else:  # pragma: no cover
        raise AssertionError(kind)


def _validate_schema(cfg: Mapping[str, Any], schema: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> None:
    if not isinstance(cfg, Mapping):
        raise ConfigError(f"{_path_str(prefix) or '設定檔'} 需為物件")
    missing = [k for k in schema if k not in cfg]
    if missing:
        raise ConfigError(f"{_path_str(prefix) or '設定檔'} 缺少欄位：{', '.join(missing)}")
    unknown = [k for k in cfg if k not in schema]
    if unknown:
        raise ConfigError(f"{_path_str(prefix) or '設定檔'} 含未定義欄位：{', '.join(map(str, unknown))}")
    for key, kind in schema.items():
        path = prefix + (key,)
        if isinstance(kind, Mapping):
            _validate_schema(cfg[key], kind, path)
        else:
            _check_type(cfg[key], kind, path)


def _get(cfg: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = cfg
    for key in path:
        node = node[key]
    return node


def assert_no_secrets(node: Any, prefix: tuple[str, ...] = ()) -> None:
    """遞迴檢查設定內容不含疑似機密的鍵名或值（IR-6）。"""
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = prefix + (str(key),)
            if _SECRET_KEY_RE.search(str(key)):
                raise ConfigError(
                    f"設定檔不得含機密欄位：{_path_str(path)}（token 只能放 GitHub Secrets／環境變數）"
                )
            assert_no_secrets(value, path)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            assert_no_secrets(value, prefix + (str(i),))
    elif isinstance(node, str):
        if _JWT_LIKE_RE.match(node) or _LONG_OPAQUE_RE.match(node):
            raise ConfigError(f"{_path_str(prefix)} 的值疑似 token 或金鑰，設定檔不得含機密")


def validate_config(cfg: Mapping[str, Any]) -> None:
    """完整驗證設定；不符即拋出 ConfigError。"""
    assert_no_secrets(cfg)
    _validate_schema(cfg, SCHEMA)

    for path, locked in LOCKED_VALUES.items():
        actual = _get(cfg, path)
        if actual != locked or type(actual) is bool and not isinstance(locked, bool):
            raise ConfigError(f"{_path_str(path)} 為規格定案值 {locked!r}，不得改為 {actual!r}")

    for path, choices in ALLOWED_CHOICES.items():
        actual = _get(cfg, path)
        if actual not in choices:
            raise ConfigError(f"{_path_str(path)} 只允許 {choices}，實得 {actual!r}")

    ds, uni, scr, mt, bt = (cfg[k] for k in ("data_source", "universe", "screening", "market_timing", "backtest"))

    if ds["lookback_weeks"] < MIN_LOOKBACK_WEEKS:
        raise ConfigError(f"data_source.lookback_weeks 至少需 {MIN_LOOKBACK_WEEKS} 週（S3／S4 需 27 根週 K）")
    if ds["request_delay_sec"] < 0 or ds["max_retries"] < 0 or ds["timeout_sec"] <= 0:
        raise ConfigError("data_source 的節流／重試／逾時參數不可為負，timeout_sec 需 > 0")
    if uni["min_avg_daily_lots_20w"] < 0:
        raise ConfigError("universe.min_avg_daily_lots_20w 不可為負（設 0 即關閉）")
    for key in ("ma_weeks", "vol_lookback_weeks", "pivot_k_weeks", "trendline_min_pivot_gap_weeks"):
        if scr[key] < 1:
            raise ConfigError(f"screening.{key} 需 >= 1")
    if scr["volume_spike_ratio"] <= 0:
        raise ConfigError("screening.volume_spike_ratio 需 > 0")
    if scr["bias_warning_pct"] < 0:
        raise ConfigError("screening.bias_warning_pct 不可為負")
    if not 0 < scr["trendline_max_angle_deg"] < 90:
        raise ConfigError("screening.trendline_max_angle_deg 需介於 0～90 度")
    steep = scr["trendline_steep_warning_log_rate"]
    if steep is not None and steep <= 0:
        raise ConfigError("screening.trendline_steep_warning_log_rate 需 > 0 或 null（關閉）")
    bad_subtypes = [s for s in cfg["ui"]["etf_default_hide_subtypes"] if s not in ETF_SUBTYPES]
    if bad_subtypes:
        raise ConfigError(f"ui.etf_default_hide_subtypes 含未知子類型：{bad_subtypes}")
    if not 1 <= mt["d_short"] < mt["d_long"] or mt["w_ma"] < 1:
        raise ConfigError("market_timing 需滿足 1 <= d_short < d_long 且 w_ma >= 1")
    if parse_iso_date(bt["start_date"]) >= parse_iso_date(bt["end_date"]):
        raise ConfigError("backtest.start_date 需早於 end_date")
    for key in ("fee_rate", "fee_discount", "tax_rate_stock", "tax_rate_etf", "slippage"):
        if not 0 <= bt[key] < 1:
            raise ConfigError(f"backtest.{key} 需介於 0 與 1 之間")
    if bt["max_positions"] < 1:
        raise ConfigError("backtest.max_positions 需 >= 1")


# ---------------------------------------------------------------- 環境變數覆寫

def _env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None  # workflow 未填時傳入空字串，視為不覆寫


def _parse_bool(value: str, name: str) -> bool:
    lowered = value.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ConfigError(f"{name} 需為 true/false，實得 {value!r}")


def apply_env_overrides(cfg: Mapping[str, Any], env: Mapping[str, str]) -> tuple[dict[str, Any], RunOptions]:
    """依 workflow 輸入（環境變數）覆寫設定，回傳新設定與執行參數；不修改原物件。"""
    new = copy.deepcopy(dict(cfg))

    as_of: date | None = None
    raw = _env_value(env, ENV_AS_OF_DATE)
    if raw is not None:
        try:
            as_of = parse_iso_date(raw)
        except ValueError as exc:
            raise ConfigError(f"{ENV_AS_OF_DATE}：{exc}") from exc
        if as_of > today_taipei():
            raise ConfigError(f"{ENV_AS_OF_DATE}={raw} 晚於今日（Asia/Taipei）")

    raw = _env_value(env, ENV_LOOKBACK_WEEKS)
    if raw is not None:
        if not re.fullmatch(r"\d+", raw):
            raise ConfigError(f"{ENV_LOOKBACK_WEEKS} 需為正整數，實得 {raw!r}")
        new["data_source"]["lookback_weeks"] = int(raw)

    raw = _env_value(env, ENV_VOLUME_SPIKE_RATIO)
    if raw is not None:
        try:
            new["screening"]["volume_spike_ratio"] = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{ENV_VOLUME_SPIKE_RATIO} 需為數值，實得 {raw!r}") from exc

    raw = _env_value(env, ENV_SKIP_FUNDAMENTALS)
    if raw is not None and _parse_bool(raw, ENV_SKIP_FUNDAMENTALS):
        new["data_source"]["enable_optional_fundamentals"] = False

    return new, RunOptions(as_of_date=as_of)


# ---------------------------------------------------------------- 對外介面

def load_config(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], RunOptions]:
    """載入 config.yaml、套用環境變數覆寫並驗證。"""
    env = os.environ if env is None else env
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        with cfg_path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到設定檔：{cfg_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"設定檔 YAML 格式錯誤：{exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError("設定檔最上層需為物件")

    validate_config(raw)  # 先驗證原始檔（含機密檢查）
    cfg, options = apply_env_overrides(raw, env)
    validate_config(cfg)  # 再驗證覆寫後的值
    return cfg, options


def get_finmind_token(env: Mapping[str, str] | None = None) -> str | None:
    """只從環境變數讀取 FINMIND_TOKEN（GitHub Secrets 注入）；未設定回傳 None，走免費官方路線。"""
    env = os.environ if env is None else env
    return _env_value(env, ENV_FINMIND_TOKEN)
