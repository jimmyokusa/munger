"""Sanity checks on config.py's threshold/toggle values."""

import math

import config


def test_munger_score_weights_sum_to_one() -> None:
    weights = [
        config.SCORE_WEIGHT_ROE,
        config.SCORE_WEIGHT_GROSS_MARGIN,
        config.SCORE_WEIGHT_OPERATING_MARGIN,
        config.SCORE_WEIGHT_FCF_YIELD,
        config.SCORE_WEIGHT_LOW_DEBT,
    ]
    assert math.isclose(sum(weights), 1.0)


def test_paper_trading_defaults_to_true() -> None:
    assert config.PAPER_TRADING is True


def test_kill_switch_defaults_to_false() -> None:
    assert config.KILL_SWITCH is False


def test_position_sizing_is_internally_consistent() -> None:
    # A single position capped below 1/target_count would make the target
    # position count unreachable at equal weight.
    assert config.MAX_SINGLE_POSITION_WEIGHT >= 1.0 / config.TARGET_POSITION_COUNT


def test_universe_ticker_count_band_is_sane() -> None:
    assert config.UNIVERSE_MIN_TICKER_COUNT < config.UNIVERSE_MAX_TICKER_COUNT


def test_global_notional_budget_pct_is_valid_fraction() -> None:
    assert 0 < config.GLOBAL_NOTIONAL_BUDGET_PCT <= 1


def test_min_universe_fetch_fraction_is_valid_fraction() -> None:
    assert 0 < config.MIN_UNIVERSE_FETCH_FRACTION <= 1
