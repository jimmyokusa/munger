"""Unit tests for material_events.py (Design v2.2 §3.8 Tier 1, M42-M43).

fetch_recent_8k_filings is tested against a real, trimmed SEC EDGAR
submissions-index fixture (tests/fixtures/sec_submissions_aapl_sample.json
-- real AAPL data fetched live 2026-09-03), matching this project's own
precedent (test_journal.py's real bot-state fixture, test_xbrl.py's real
companyfacts fixture). classify_items is tested against synthetic
per-item-type fixtures, per Design v2.2 §3.8's own stated acceptance
criteria ("a synthetic/injected 8-K fixture per item type").
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
import journal
import material_events

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(config, "PNL_DATA_PATH", tmp_path / "pnl.json")
    monkeypatch.setattr(config, "DISCORD_MATERIAL_EVENT_WEBHOOK_URL", "https://discord.example/m")
    monkeypatch.setattr(config, "SEC_EDGAR_MAX_REQUESTS_PER_SECOND", 1_000_000)


def _real_submissions() -> dict[str, object]:
    loaded = json.loads((_FIXTURES / "sec_submissions_aapl_sample.json").read_text())
    assert isinstance(loaded, dict)
    return loaded


# --- classify_items (synthetic fixture per item type, per §3.8's own bar) ---


def test_classify_items_critical_1_03_bankruptcy() -> None:
    assert material_events.classify_items("1.03") == [
        ("1.03", "Bankruptcy or receivership", "Critical")
    ]


def test_classify_items_critical_4_02_non_reliance() -> None:
    assert material_events.classify_items("4.02") == [
        ("4.02", "Non-reliance on previously issued financials", "Critical")
    ]


def test_classify_items_high_4_01_auditor_change() -> None:
    assert material_events.classify_items("4.01") == [("4.01", "Auditor change", "High")]


def test_classify_items_medium_5_02_officer_departure() -> None:
    assert material_events.classify_items("5.02") == [
        ("5.02", "Departure of principal officers", "Medium")
    ]


def test_classify_items_medium_2_01_asset_disposition() -> None:
    assert material_events.classify_items("2.01") == [("2.01", "Asset disposition", "Medium")]


def test_classify_items_medium_2_05_exit_costs() -> None:
    assert material_events.classify_items("2.05") == [("2.05", "Exit or disposal costs", "Medium")]


def test_classify_items_medium_2_06_material_impairment() -> None:
    assert material_events.classify_items("2.06") == [("2.06", "Material impairment", "Medium")]


def test_classify_items_low_2_02_results_of_operations() -> None:
    # Still classified (Low), even though poll_ticker suppresses alerting
    # on it -- classify_items itself doesn't know about suppression.
    assert material_events.classify_items("2.02") == [("2.02", "Results of operations", "Low")]


def test_classify_items_unrecognized_item_is_silently_skipped() -> None:
    # 7.01 (Regulation FD Disclosure), 9.01 (Financial Statements and
    # Exhibits) and 5.07 (submission of matters to a vote) are all real
    # SEC item numbers this taxonomy deliberately doesn't classify.
    assert material_events.classify_items("7.01,9.01") == []
    assert material_events.classify_items("5.07") == []


def test_classify_items_multiple_items_returns_every_match() -> None:
    assert material_events.classify_items("2.02,5.02,9.01") == [
        ("2.02", "Results of operations", "Low"),
        ("5.02", "Departure of principal officers", "Medium"),
    ]


def test_classify_items_empty_string_returns_empty_list() -> None:
    assert material_events.classify_items("") == []


def test_classify_items_strips_whitespace_around_items() -> None:
    assert material_events.classify_items("1.03, 5.02") == [
        ("1.03", "Bankruptcy or receivership", "Critical"),
        ("5.02", "Departure of principal officers", "Medium"),
    ]


# --- fetch_recent_8k_filings (real SEC EDGAR fixture) ---


def test_fetch_recent_8k_filings_returns_only_8k_and_8ka_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        material_events.xbrl,
        "throttled_get",
        lambda url: json.dumps(_real_submissions()).encode(),
    )
    filings = material_events.fetch_recent_8k_filings("0000320193")
    # Real fixture: 60 rows total, 7 are 8-K and 1 is 8-K/A (confirmed
    # live 2026-09-03) -- staff-engineer-reviewer finding: an earlier
    # version of this function silently dropped the 8-K/A.
    assert len(filings) == 8
    assert all("accession_number" in f and "filing_date" in f and "items" in f for f in filings)


def test_fetch_recent_8k_filings_includes_the_real_pinned_filing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        material_events.xbrl,
        "throttled_get",
        lambda url: json.dumps(_real_submissions()).encode(),
    )
    filings = material_events.fetch_recent_8k_filings("0000320193")
    by_accession = {f["accession_number"]: f for f in filings}
    assert by_accession["0001140361-26-015711"]["filing_date"] == "2026-04-20"
    assert by_accession["0001140361-26-015711"]["items"] == "5.02"


def test_fetch_recent_8k_filings_includes_a_real_8ka_amendment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real AAPL 8-K/A in the fixture, carrying a taxonomy-matching item
    # (5.02) -- the exact case a naive form == "8-K" filter would
    # silently drop.
    monkeypatch.setattr(
        material_events.xbrl,
        "throttled_get",
        lambda url: json.dumps(_real_submissions()).encode(),
    )
    filings = material_events.fetch_recent_8k_filings("0000320193")
    by_accession = {f["accession_number"]: f for f in filings}
    assert by_accession["0001140361-26-035325"]["filing_date"] == "2026-09-01"
    assert by_accession["0001140361-26-035325"]["items"] == "5.02"


def test_fetch_recent_8k_filings_returns_empty_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    def _raise(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(material_events.xbrl, "throttled_get", _raise)
    assert material_events.fetch_recent_8k_filings("0000320193") == []


def test_fetch_recent_8k_filings_returns_empty_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(material_events.xbrl, "throttled_get", lambda url: b"not valid json{{{")
    assert material_events.fetch_recent_8k_filings("0000320193") == []


# --- poll_ticker: idempotency, suppression, alerting ---


def _fake_urlopen_factory() -> MagicMock:
    context_manager = MagicMock()
    context_manager.__enter__ = MagicMock(return_value=MagicMock())
    context_manager.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=context_manager)


def test_poll_ticker_alerts_and_records_a_new_critical_filing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        material_events,
        "fetch_recent_8k_filings",
        lambda cik: [{"accession_number": "acc-1", "filing_date": "2026-02-20", "items": "1.03"}],
    )
    fake_urlopen = _fake_urlopen_factory()
    monkeypatch.setattr(material_events.urllib.request, "urlopen", fake_urlopen)

    events = material_events.poll_ticker("HRMY", "0000320193")

    assert len(events) == 1
    assert events[0]["severity"] == "Critical"
    assert journal.has_alerted_on_filing("acc-1") is True
    fake_urlopen.assert_called_once()  # Discord alert was actually sent


def test_poll_ticker_does_not_realert_an_already_seen_accession_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal.record_material_event("acc-1", "HRMY", "2026-02-20", "1.03", "Critical")
    monkeypatch.setattr(
        material_events,
        "fetch_recent_8k_filings",
        lambda cik: [{"accession_number": "acc-1", "filing_date": "2026-02-20", "items": "1.03"}],
    )
    fake_urlopen = _fake_urlopen_factory()
    monkeypatch.setattr(material_events.urllib.request, "urlopen", fake_urlopen)

    events = material_events.poll_ticker("HRMY", "0000320193")

    assert events == []
    fake_urlopen.assert_not_called()


def test_poll_ticker_suppresses_a_2_02_only_filing_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2.02 alone (a routine earnings 8-K, filed every quarter) must
    # neither alert nor be recorded in the journal -- otherwise every
    # holding's routine quarterly earnings 8-K would flood Discord.
    monkeypatch.setattr(
        material_events,
        "fetch_recent_8k_filings",
        lambda cik: [
            {"accession_number": "acc-1", "filing_date": "2026-01-29", "items": "2.02,9.01"}
        ],
    )
    fake_urlopen = _fake_urlopen_factory()
    monkeypatch.setattr(material_events.urllib.request, "urlopen", fake_urlopen)

    events = material_events.poll_ticker("AAPL", "0000320193")

    assert events == []
    assert journal.has_alerted_on_filing("acc-1") is False
    fake_urlopen.assert_not_called()


def test_poll_ticker_alerts_on_the_non_suppressed_item_in_a_mixed_filing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single 8-K can carry both a suppressed item (2.02) and a real one
    # (5.02) -- must still alert on the real one.
    monkeypatch.setattr(
        material_events,
        "fetch_recent_8k_filings",
        lambda cik: [
            {"accession_number": "acc-1", "filing_date": "2026-01-29", "items": "2.02,5.02"}
        ],
    )
    fake_urlopen = _fake_urlopen_factory()
    monkeypatch.setattr(material_events.urllib.request, "urlopen", fake_urlopen)

    events = material_events.poll_ticker("AAPL", "0000320193")

    assert len(events) == 1
    assert events[0]["severity"] == "Medium"
    fake_urlopen.assert_called_once()


def test_poll_ticker_no_taxonomy_match_neither_alerts_nor_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        material_events,
        "fetch_recent_8k_filings",
        lambda cik: [
            {"accession_number": "acc-1", "filing_date": "2026-01-29", "items": "7.01,9.01"}
        ],
    )
    fake_urlopen = _fake_urlopen_factory()
    monkeypatch.setattr(material_events.urllib.request, "urlopen", fake_urlopen)

    events = material_events.poll_ticker("AAPL", "0000320193")

    assert events == []
    assert journal.has_alerted_on_filing("acc-1") is False
    fake_urlopen.assert_not_called()


def test_poll_ticker_picks_the_highest_severity_among_multiple_real_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1.03 (Critical) and 5.02 (Medium) in one filing -- Critical wins.
    monkeypatch.setattr(
        material_events,
        "fetch_recent_8k_filings",
        lambda cik: [
            {"accession_number": "acc-1", "filing_date": "2026-01-29", "items": "5.02,1.03"}
        ],
    )
    fake_urlopen = _fake_urlopen_factory()
    monkeypatch.setattr(material_events.urllib.request, "urlopen", fake_urlopen)

    events = material_events.poll_ticker("AAPL", "0000320193")

    assert events[0]["severity"] == "Critical"


# --- poll_holdings: per-ticker fault tolerance ---


def test_poll_holdings_skips_a_ticker_with_no_cik_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(material_events.xbrl, "load_cik_lookup", lambda: {"AAPL": "0000320193"})
    monkeypatch.setattr(
        material_events,
        "poll_ticker",
        lambda ticker, cik: [{"ticker": ticker, "accession_number": "acc-x"}],
    )

    events = material_events.poll_holdings(["UNKNOWN_TICKER", "AAPL"])

    assert len(events) == 1
    assert events[0]["ticker"] == "AAPL"


# --- Discord alert config-gating ---


def test_send_discord_alert_is_a_noop_when_webhook_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DISCORD_MATERIAL_EVENT_WEBHOOK_URL", "")
    fake_urlopen = _fake_urlopen_factory()
    monkeypatch.setattr(material_events.urllib.request, "urlopen", fake_urlopen)

    material_events._send_discord_alert("test message")

    fake_urlopen.assert_not_called()


def test_send_discord_alert_swallows_a_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    monkeypatch.setattr(
        material_events.urllib.request,
        "urlopen",
        MagicMock(side_effect=urllib.error.URLError("no route")),
    )
    material_events._send_discord_alert("test message")  # must not raise


# --- _held_symbols_from_pnl_snapshot ---


def test_held_symbols_from_pnl_snapshot_reads_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pnl_path = tmp_path / "pnl.json"
    pnl_path.write_text(json.dumps({"positions": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}))
    monkeypatch.setattr(config, "PNL_DATA_PATH", pnl_path)

    assert material_events._held_symbols_from_pnl_snapshot() == ["AAPL", "MSFT"]


def test_held_symbols_from_pnl_snapshot_returns_empty_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PNL_DATA_PATH", tmp_path / "does_not_exist.json")
    assert material_events._held_symbols_from_pnl_snapshot() == []


def test_held_symbols_from_pnl_snapshot_returns_empty_on_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pnl_path = tmp_path / "pnl.json"
    pnl_path.write_text("not valid json{{{")
    monkeypatch.setattr(config, "PNL_DATA_PATH", pnl_path)
    assert material_events._held_symbols_from_pnl_snapshot() == []


# --- run(): end-to-end, no held tickers ---


def test_run_is_a_noop_when_no_tickers_are_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PNL_DATA_PATH", tmp_path / "does_not_exist.json")
    poll_holdings_mock = MagicMock()
    monkeypatch.setattr(material_events, "poll_holdings", poll_holdings_mock)

    material_events.run()

    poll_holdings_mock.assert_not_called()


def test_run_polls_the_held_tickers_from_pnl_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pnl_path = tmp_path / "pnl.json"
    pnl_path.write_text(json.dumps({"positions": [{"symbol": "AAPL"}]}))
    monkeypatch.setattr(config, "PNL_DATA_PATH", pnl_path)
    poll_holdings_mock = MagicMock(return_value=[])
    monkeypatch.setattr(material_events, "poll_holdings", poll_holdings_mock)

    material_events.run()

    poll_holdings_mock.assert_called_once_with(["AAPL"])


# --- Structural: alert-only, never trade-triggering (§3.8's hard constraint) ---


def test_module_never_imports_execution_or_portfolio() -> None:
    # The structural half of "alert-only, never trade-triggering": this
    # module must be architecturally incapable of placing an order or
    # touching the buy/sell decision path, the same guarantee
    # daily_screen.py's own test asserts for itself (never imports
    # execution.py). Parses the source with ast rather than checking
    # sys.modules after import, since a transitive import through some
    # other module wouldn't be caught by a naive "is execution imported
    # anywhere in the process" check -- this instead asserts material_events.py's
    # OWN import statements never name either module directly.
    source = Path(material_events.__file__).read_text()
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    assert "execution" not in imported_names
    assert "portfolio" not in imported_names


def test_poll_ticker_return_value_carries_no_order_shaped_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A softer, behavioral companion to the import-graph check above:
    # nothing poll_ticker actually returns carries a field named like an
    # order/side/qty/notional -- the shape a caller would need to
    # accidentally wire this into execution.py's market_buy/liquidate.
    # Exercises the real function, not a hand-built stand-in dict.
    monkeypatch.setattr(
        material_events,
        "fetch_recent_8k_filings",
        lambda cik: [{"accession_number": "acc-1", "filing_date": "2026-02-20", "items": "1.03"}],
    )
    monkeypatch.setattr(material_events.urllib.request, "urlopen", _fake_urlopen_factory())

    events = material_events.poll_ticker("HRMY", "0000320193")

    assert len(events) == 1
    forbidden_keys = {"side", "qty", "notional", "shares", "order", "action"}
    assert forbidden_keys.isdisjoint(events[0].keys())
