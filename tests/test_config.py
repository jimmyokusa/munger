"""Sanity checks on config.py's threshold/toggle values."""

import importlib
import math
from pathlib import Path

import pytest

import config


@pytest.fixture
def _reload_config_after() -> None:
    # M20's PAPER_TRADING/LIVE_TRADING_ENABLED are computed at import time
    # from os.environ -- verifying the env-driven parsing means actually
    # reloading the module under a patched env, which mutates the shared
    # `config` module object every other test file also imports. Reload
    # again afterward (with the patched env vars already torn down by
    # monkeypatch) so no other test in the session sees anything but the
    # real default-env values.
    yield
    importlib.reload(config)


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


def test_paper_trading_env_override_parses_false(
    monkeypatch: pytest.MonkeyPatch, _reload_config_after: None
) -> None:
    # M20 (DESIGN_REAL_MONEY.md §3.1): daily-trade-live.yml sets this to
    # switch the exact same bot.py binary to the live account -- getting
    # this parse wrong (e.g. any falsy-looking string still parsing
    # truthy) would either silently trade live when paper was intended or
    # vice versa.
    monkeypatch.setenv("MUNGER_PAPER_TRADING", "false")
    importlib.reload(config)
    assert config.PAPER_TRADING is False


@pytest.mark.parametrize(
    "garbage_value",
    ["True", "1", "yes", "", " ", "flase"],
)
def test_paper_trading_fails_safe_toward_paper_on_ambiguous_input(
    garbage_value: str, monkeypatch: pytest.MonkeyPatch, _reload_config_after: None
) -> None:
    # staff-engineer-reviewer finding (code-push review): only an exact
    # "false" (trimmed, case-insensitive) should ever enable live trading
    # -- everything else, including a value that *looks* like it might
    # mean "off" ("flase", a typo) or "on" ("True"/"1"/"yes"), must fail
    # toward the safer paper default, not silently toward live.
    monkeypatch.setenv("MUNGER_PAPER_TRADING", garbage_value)
    importlib.reload(config)
    assert config.PAPER_TRADING is True


def test_paper_trading_tolerates_incidental_whitespace_around_false(
    monkeypatch: pytest.MonkeyPatch, _reload_config_after: None
) -> None:
    # The .strip() is deliberate, not incidental -- a value set through a
    # UI field (a repo variable, as opposed to a YAML literal) is
    # plausibly whitespace-padded without the setter intending anything
    # other than "false." This still flips to live, unlike the garbage
    # values above.
    monkeypatch.setenv("MUNGER_PAPER_TRADING", " false ")
    importlib.reload(config)
    assert config.PAPER_TRADING is False


def test_paper_trading_env_unset_matches_default_when_reloaded(
    monkeypatch: pytest.MonkeyPatch, _reload_config_after: None
) -> None:
    # daily-trade.yml's existing invocation never sets this var at all --
    # confirms the reload path itself (not just the live-workflow path)
    # reproduces today's exact default, not an artifact of import order.
    monkeypatch.delenv("MUNGER_PAPER_TRADING", raising=False)
    importlib.reload(config)
    assert config.PAPER_TRADING is True


def test_live_trading_enabled_defaults_to_false() -> None:
    assert config.LIVE_TRADING_ENABLED is False


def test_live_trading_enabled_never_reads_alpaca_live_credentials() -> None:
    # staff-engineer-reviewer-style boundary check: an earlier design
    # draft phrased this as "derived from whether the live credentials
    # are configured" -- report.py's own deployment must never hold or
    # even reference an Alpaca credential name at all (M14 screen-only
    # boundary), so this asserts config.py's *code* (not its comments,
    # which do name ALPACA_LIVE_API_KEY to explain what NOT to do) never
    # actually reads such a variable.
    assert config.__file__ is not None
    code_lines = [
        line.split("#", 1)[0]
        for line in Path(config.__file__).read_text().splitlines()
    ]
    assert not any("ALPACA_LIVE" in line for line in code_lines)


def test_kill_switch_defaults_to_false() -> None:
    assert config.KILL_SWITCH is False


def test_global_kill_switch_flag_file_path_is_anchored_at_base_dir_not_data_dir() -> None:
    # M20 (DESIGN_REAL_MONEY.md §3.2): must be visible to both the paper
    # and live workflows' separate checkouts via a single git commit --
    # anchoring it at DATA_DIR (which can be repointed per-deployment via
    # MUNGER_DATA_DIR) would defeat that, since the two workflows don't
    # share a DATA_DIR at all.
    assert config.GLOBAL_KILL_SWITCH_FLAG_FILE_PATH.parent == config.BASE_DIR


def test_position_sizing_is_internally_consistent() -> None:
    # A single position capped below 1/target_count would make the target
    # position count unreachable at equal weight.
    assert config.MAX_SINGLE_POSITION_WEIGHT >= 1.0 / config.TARGET_POSITION_COUNT


def test_universe_ticker_count_bands_are_sane() -> None:
    assert set(config.UNIVERSE_TICKER_COUNT_BANDS) == {"500", "400", "600"}
    for min_count, max_count in config.UNIVERSE_TICKER_COUNT_BANDS.values():
        assert min_count < max_count


def test_global_notional_budget_pct_is_valid_fraction() -> None:
    assert 0 < config.GLOBAL_NOTIONAL_BUDGET_PCT <= 1


def test_min_universe_fetch_fraction_is_valid_fraction() -> None:
    assert 0 < config.MIN_UNIVERSE_FETCH_FRACTION <= 1
