"""Unit tests for universe.py's pure functions and get_universe() branches
(DESIGN.md section 6, layer 1)."""

import itertools
import string
from collections.abc import Callable

import pandas as pd
import pytest

import config
import universe

# Real tickers from the static fallback file, so generated fake tickers can
# be guaranteed not to collide with them by chance (a real S&P constituent
# like "AAON" or "FSLR" can otherwise land inside any given 4-letter
# combination range).
_REAL_FALLBACK_TICKERS = set(universe._load_static_fallback("500")) | {
    t for index in ("400", "600") for t in universe._load_static_fallback(index)
}


def _fake_tickers(n: int, offset: int = 0) -> list[str]:
    """n distinct, letters-only, plausible-looking ticker strings, guaranteed
    not to collide with any real ticker in the static fallback file.

    ``offset`` skips ahead in the combination space so tickers generated
    for different indices in the same test don't accidentally collide with
    each other (every call otherwise starts at "AAAA").
    """
    combos = itertools.product(string.ascii_uppercase, repeat=4)
    candidates = ("".join(letters) for letters in itertools.islice(combos, offset, None))
    fake = (t for t in candidates if t not in _REAL_FALLBACK_TICKERS)
    return list(itertools.islice(fake, n))


def _fake_table(tickers: list[str], sector: str = "Industrials") -> pd.DataFrame:
    return pd.DataFrame({"symbol": tickers, "sector": [sector] * len(tickers)})


def test_normalize_ticker_replaces_dot_with_dash() -> None:
    assert universe.normalize_ticker("BRK.B") == "BRK-B"


def test_normalize_ticker_uppercases_and_strips() -> None:
    assert universe.normalize_ticker(" aapl ") == "AAPL"


def test_normalize_ticker_leaves_plain_ticker_unchanged() -> None:
    assert universe.normalize_ticker("MSFT") == "MSFT"


def test_canonicalize_sector_trims_whitespace() -> None:
    assert universe._canonicalize_sector("  Financials  ") == "Financials"


def test_canonicalize_sector_passes_through_unchanged() -> None:
    assert universe._canonicalize_sector("Financials") == "Financials"
    assert universe._canonicalize_sector("Some New Sector") == "Some New Sector"


def test_canonicalize_sector_handles_non_string_without_raising() -> None:
    # A blank/NaN sector cell in a live fetch arrives as a float, not a
    # str -- must not raise, or one bad cell would discard an entire
    # index's live fetch (caught by _fetch_and_validate_index's broad
    # except, but that's hundreds of good rows thrown away for one).
    assert universe._canonicalize_sector(float("nan")) == "nan"


def test_validate_universe_accepts_sane_band() -> None:
    assert universe.validate_universe(_fake_tickers(500), "500") is True


def test_validate_universe_rejects_too_few_tickers() -> None:
    assert universe.validate_universe(["AAPL", "MSFT"], "500") is False


def test_validate_universe_rejects_too_many_tickers() -> None:
    assert universe.validate_universe(_fake_tickers(600), "500") is False


def test_validate_universe_rejects_implausible_ticker_format() -> None:
    # Simulates a corrupted-but-well-formed scrape: right row count, wrong
    # column contents (e.g. a shifted-column page restructure).
    tickers = [f"Some Company {i} Inc." for i in range(500)]
    assert universe.validate_universe(tickers, "500") is False


def test_validate_universe_accepts_share_class_suffix() -> None:
    tickers = [*_fake_tickers(499), "BRK-B"]
    assert universe.validate_universe(tickers, "500") is True


def test_validate_universe_uses_per_index_band() -> None:
    # 500 tickers is within the "500" band but outside the "400" band --
    # regression test that the per-index band lookup is actually wired up,
    # not just a leftover single global band.
    tickers = _fake_tickers(500)
    assert universe.validate_universe(tickers, "500") is True
    assert universe.validate_universe(tickers, "400") is False


def test_apply_sector_exclusions_drops_matching_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "EXCLUDED_SECTORS", ("Financials",))
    table = pd.DataFrame(
        {"symbol": ["JPM", "AAPL"], "sector": ["Financials", "Information Technology"]}
    )
    result = universe._apply_sector_exclusions(table)
    assert list(result["symbol"]) == ["AAPL"]


def test_apply_sector_exclusions_canonicalizes_before_comparing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stray leading/trailing space in a sector cell must not let a
    # ticker slip past an exclusion that should have caught it.
    monkeypatch.setattr(config, "EXCLUDED_SECTORS", ("Financials",))
    table = pd.DataFrame({"symbol": ["JPM"], "sector": [" Financials "]})
    result = universe._apply_sector_exclusions(table)
    assert list(result["symbol"]) == []


def test_apply_sector_exclusions_noop_when_unconfigured() -> None:
    table = pd.DataFrame({"symbol": ["JPM"], "sector": ["Financials"]})
    result = universe._apply_sector_exclusions(table)
    assert list(result["symbol"]) == ["JPM"]


def test_static_fallback_loads_and_normalizes_each_index() -> None:
    for index in ("500", "400", "600"):
        tickers = universe._load_static_fallback(index)
        assert universe.validate_universe(tickers, index)
        assert all("." not in t for t in tickers)


def test_static_fallback_applies_sector_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    # The live-fetch path and the fallback path each apply exclusions
    # (DESIGN.md 3.1 requires this for whichever path is actually in use);
    # this proves the fallback path actually does, via the same
    # _apply_sector_exclusions function the live path uses -- not a
    # second, independently-drifting implementation.
    assert "JPM" in universe._load_static_fallback("500")
    monkeypatch.setattr(config, "EXCLUDED_SECTORS", ("Financials",))
    assert "JPM" not in universe._load_static_fallback("500")


# Non-overlapping offsets so each index's fixture tickers in a combined
# test are guaranteed distinct from the others (see _fake_tickers' offset
# param) -- 26**4 = 456,976 total combos, far more than needed here.
_OFFSET_500, _OFFSET_400, _OFFSET_600 = 0, 10_000, 20_000


def _wikipedia_fixture(
    tickers_500: list[str], tickers_400: list[str], tickers_600: list[str]
) -> Callable[[str], pd.DataFrame]:
    by_index = {"500": tickers_500, "400": tickers_400, "600": tickers_600}
    return lambda index: _fake_table(by_index[index])


def test_get_universe_combines_all_three_sources_live_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        universe,
        "_fetch_wikipedia_index",
        _wikipedia_fixture(
            _fake_tickers(500, _OFFSET_500),
            _fake_tickers(400, _OFFSET_400),
            _fake_tickers(600, _OFFSET_600),
        ),
    )
    result = universe.get_universe()
    assert len(result) == 500 + 400 + 600


def test_get_universe_falls_back_when_an_index_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    live_400 = _fake_tickers(400, _OFFSET_400)
    live_600 = _fake_tickers(600, _OFFSET_600)

    def _fetch(index: str) -> pd.DataFrame:
        if index == "500":
            raise ConnectionError("network down")
        return _fake_table(live_400 if index == "400" else live_600)

    monkeypatch.setattr(universe, "_fetch_wikipedia_index", _fetch)

    result = universe.get_universe()

    fallback_500 = universe._load_static_fallback("500")
    assert len(result) == len(fallback_500) + 400 + 600


def test_get_universe_with_diagnostics_reports_fallback_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_400 = _fake_tickers(400, _OFFSET_400)
    live_600 = _fake_tickers(600, _OFFSET_600)

    def _fetch(index: str) -> pd.DataFrame:
        if index == "500":
            raise ConnectionError("network down")
        return _fake_table(live_400 if index == "400" else live_600)

    monkeypatch.setattr(universe, "_fetch_wikipedia_index", _fetch)

    result = universe.get_universe_with_diagnostics()

    assert result.fallback_indices == ["500"]
    fallback_500 = universe._load_static_fallback("500")
    assert len(result.tickers) == len(fallback_500) + 400 + 600


def test_get_universe_with_diagnostics_reports_no_fallback_on_clean_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        universe,
        "_fetch_wikipedia_index",
        _wikipedia_fixture(
            _fake_tickers(500, _OFFSET_500),
            _fake_tickers(400, _OFFSET_400),
            _fake_tickers(600, _OFFSET_600),
        ),
    )
    result = universe.get_universe_with_diagnostics()
    assert result.fallback_indices == []


def test_get_universe_falls_back_when_an_index_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        universe,
        "_fetch_wikipedia_index",
        # Only 2 tickers for the 400 index -- well outside its band.
        _wikipedia_fixture(
            _fake_tickers(500, _OFFSET_500),
            _fake_tickers(2, _OFFSET_400),
            _fake_tickers(600, _OFFSET_600),
        ),
    )
    result = universe.get_universe()
    fallback_400 = universe._load_static_fallback("400")
    assert len(result) == 500 + len(fallback_400) + 600


def test_get_universe_falls_back_on_missing_sector_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: a missing/renamed sector column must still result
    # in a fallback, not an uncaught KeyError, once an exclusion is
    # configured (exclusions run inside the same try/except boundary as
    # the fetch itself).
    monkeypatch.setattr(config, "EXCLUDED_SECTORS", ("Financials",))
    broken_table = pd.DataFrame({"symbol": _fake_tickers(500, _OFFSET_500)})  # no "sector" column
    live_400 = _fake_tickers(400, _OFFSET_400)
    live_600 = _fake_tickers(600, _OFFSET_600)

    def _fetch(index: str) -> pd.DataFrame:
        if index == "500":
            return broken_table
        return _fake_table(live_400 if index == "400" else live_600)

    monkeypatch.setattr(universe, "_fetch_wikipedia_index", _fetch)

    result = universe.get_universe()

    fallback_500 = universe._load_static_fallback("500")
    assert len(result) == len(fallback_500) + 400 + 600


def test_get_universe_deduplicates_cross_index_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_ticker = "SHRD"
    tickers_500 = [*_fake_tickers(499, _OFFSET_500), shared_ticker]
    tickers_400 = [*_fake_tickers(399, _OFFSET_400), shared_ticker]
    tickers_600 = _fake_tickers(600, _OFFSET_600)

    monkeypatch.setattr(
        universe,
        "_fetch_wikipedia_index",
        _wikipedia_fixture(tickers_500, tickers_400, tickers_600),
    )
    result = universe.get_universe()
    assert result.count(shared_ticker) == 1
    assert len(result) == 500 + 400 + 600 - 1
