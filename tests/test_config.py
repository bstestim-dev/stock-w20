import copy
from datetime import date, timedelta

import pytest
import yaml

from src import config as C
from src.timezone import today_taipei


@pytest.fixture()
def base_cfg():
    with C.DEFAULT_CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _write(tmp_path, cfg):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


# ---------- 預設設定

def test_default_config_loads_with_spec_values():
    cfg, opts = C.load_config(env={})
    assert opts.as_of_date is None
    assert cfg["timezone"] == "Asia/Taipei"
    assert cfg["universe"]["min_paid_in_capital_ntd"] == 2_000_000_000
    assert cfg["universe"]["min_avg_daily_lots_20w"] == 1000
    assert cfg["universe"]["include_tib"] is True
    assert cfg["screening"]["volume_spike_ratio"] == 1.5
    assert cfg["screening"]["require_volume_spike_on_new_cross"] is False
    assert cfg["screening"]["midweek_volume_basis"] == "actual"
    assert cfg["ui"]["etf_default_hide_subtypes"] == ["bond", "leveraged_inverse"]
    assert cfg["market_timing"]["hard_filter_on_green"] is False
    assert cfg["backtest"]["primary_data"] == "official_twse_tpex_local_cache"


def test_rulings_2026_11_25(base_cfg):
    assert base_cfg["data_source"]["lookback_weeks"] == 60                    # Q2
    assert "trendline_angle_norm_pct_per_week" not in base_cfg["screening"]       # S3 裁定：移除
    assert base_cfg["screening"]["trendline_min_pivot_gap_weeks"] == 8             # S3 裁定
    assert base_cfg["screening"]["trendline_steep_warning_log_rate"] is None      # 預設關閉


def test_steep_warning_can_be_enabled(base_cfg):
    base_cfg["screening"]["trendline_steep_warning_log_rate"] = 0.02
    C.validate_config(base_cfg)


def test_liquidity_threshold_can_be_disabled(base_cfg, tmp_path):
    base_cfg["universe"]["min_avg_daily_lots_20w"] = 0
    cfg, _ = C.load_config(_write(tmp_path, base_cfg), env={})
    assert cfg["universe"]["min_avg_daily_lots_20w"] == 0


# ---------- 鎖定值（IR-2）

@pytest.mark.parametrize(
    "path, value",
    [
        (("timezone",), "UTC"),
        (("universe", "min_paid_in_capital_ntd"), 1_000_000_000),
        (("universe", "include_tib"), False),
        (("universe", "exclude_dr"), False),
        (("universe", "exclude_warrants"), False),
        (("universe", "etf_min_avg_daily_lots_20w"), 1000),
        (("screening", "require_volume_spike_on_new_cross"), True),
        (("screening", "midweek_volume_basis"), "projected"),
        (("market_timing", "hard_filter_on_green"), True),
    ],
)
def test_locked_values_cannot_change(base_cfg, path, value):
    node = base_cfg
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(C.ConfigError):
        C.validate_config(base_cfg)


def test_bool_not_accepted_as_number(base_cfg):
    base_cfg["universe"]["etf_min_avg_daily_lots_20w"] = False
    with pytest.raises(C.ConfigError):
        C.validate_config(base_cfg)


# ---------- 結構與數值驗證

def test_missing_and_unknown_fields_rejected(base_cfg):
    bad = copy.deepcopy(base_cfg)
    del bad["screening"]["ma_weeks"]
    with pytest.raises(C.ConfigError, match="缺少"):
        C.validate_config(bad)
    bad = copy.deepcopy(base_cfg)
    bad["screening"]["extra_rule"] = 1
    with pytest.raises(C.ConfigError, match="未定義"):
        C.validate_config(bad)


@pytest.mark.parametrize(
    "path, value",
    [
        (("data_source", "lookback_weeks"), 26),
        (("data_source", "primary"), "yahoo"),
        (("screening", "volume_spike_ratio"), 0),
        (("screening", "trendline_steep_warning_log_rate"), -1),
        (("screening", "trendline_steep_warning_log_rate"), 0),
        (("screening", "trendline_min_pivot_gap_weeks"), 0),
        (("screening", "trendline_min_pivot_gap_weeks"), 8.5),
        (("ui", "etf_default_hide_subtypes"), ["bond", "unknown"]),
        (("backtest", "start_date"), "2027-01-01"),
        (("backtest", "fee_rate"), 1.5),
        (("market_timing", "d_short"), 100),
    ],
)
def test_invalid_values_rejected(base_cfg, path, value):
    base_cfg[path[0]][path[1]] = value
    with pytest.raises(C.ConfigError):
        C.validate_config(base_cfg)


def test_missing_config_file(tmp_path):
    with pytest.raises(C.ConfigError, match="找不到"):
        C.load_config(tmp_path / "nope.yaml", env={})


# ---------- 機密防護（IR-6）

def test_secret_key_in_config_rejected(base_cfg, tmp_path):
    base_cfg["data_source"]["finmind_token"] = "abc"
    with pytest.raises(C.ConfigError, match="機密"):
        C.load_config(_write(tmp_path, base_cfg), env={})


def test_jwt_like_value_rejected(base_cfg):
    # 測試用假 token：執行時組出，避免公開 repo 內出現 token 樣式字串
    import base64, json
    b64 = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    fake = ".".join([b64({"alg": "none"}), b64({"test": "dummy"}), b64("not-a-signature")])
    base_cfg["data_source"]["primary"] = fake
    with pytest.raises(C.ConfigError, match="機密"):
        C.validate_config(base_cfg)


def test_repo_config_has_no_secrets(base_cfg):
    C.assert_no_secrets(base_cfg)


def test_finmind_token_only_from_env():
    assert C.get_finmind_token({}) is None
    assert C.get_finmind_token({"FINMIND_TOKEN": "  "}) is None
    assert C.get_finmind_token({"FINMIND_TOKEN": "x123"}) == "x123"


# ---------- 環境變數覆寫（workflow 輸入）

def test_env_overrides_applied():
    env = {
        "SW20_AS_OF_DATE": "2026-11-20",
        "SW20_LOOKBACK_WEEKS": "50",
        "SW20_VOLUME_SPIKE_RATIO": "2.0",
        "SW20_SKIP_FUNDAMENTALS": "true",
    }
    cfg, opts = C.load_config(env=env)
    assert opts.as_of_date == date(2026, 11, 20)
    assert cfg["data_source"]["lookback_weeks"] == 50
    assert cfg["screening"]["volume_spike_ratio"] == 2.0
    assert cfg["data_source"]["enable_optional_fundamentals"] is False


def test_empty_env_values_do_not_override():
    env = {k: "" for k in ("SW20_AS_OF_DATE", "SW20_LOOKBACK_WEEKS", "SW20_VOLUME_SPIKE_RATIO", "SW20_SKIP_FUNDAMENTALS")}
    cfg, opts = C.load_config(env=env)
    assert opts.as_of_date is None
    assert cfg["data_source"]["lookback_weeks"] == 60
    assert cfg["data_source"]["enable_optional_fundamentals"] is True


def test_skip_fundamentals_false_keeps_setting():
    cfg, _ = C.load_config(env={"SW20_SKIP_FUNDAMENTALS": "false"})
    assert cfg["data_source"]["enable_optional_fundamentals"] is True


def test_env_override_does_not_mutate_input(base_cfg):
    snapshot = copy.deepcopy(base_cfg)
    C.apply_env_overrides(base_cfg, {"SW20_LOOKBACK_WEEKS": "40"})
    assert base_cfg == snapshot


@pytest.mark.parametrize(
    "env",
    [
        {"SW20_AS_OF_DATE": "2026/11/20"},
        {"SW20_AS_OF_DATE": "2026-02-30"},
        {"SW20_LOOKBACK_WEEKS": "abc"},
        {"SW20_LOOKBACK_WEEKS": "10"},           # 低於 27 週下限
        {"SW20_VOLUME_SPIKE_RATIO": "x"},
        {"SW20_VOLUME_SPIKE_RATIO": "-1"},
        {"SW20_SKIP_FUNDAMENTALS": "maybe"},
    ],
)
def test_invalid_env_overrides_rejected(env):
    with pytest.raises(C.ConfigError):
        C.load_config(env=env)


def test_future_as_of_date_rejected():
    future = (today_taipei() + timedelta(days=1)).isoformat()
    with pytest.raises(C.ConfigError, match="晚於今日"):
        C.load_config(env={"SW20_AS_OF_DATE": future})
