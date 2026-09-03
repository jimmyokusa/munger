"""Full-universe XBRL-vs-yfinance shadow comparison run (M37's prerequisite, Design v2.2 §3.4).

"Both sources run in parallel for a full cycle, disagreements are logged
per field, and the report is inspected by hand before XBRL becomes
authoritative" (§3.4). `xbrl.py` (M36) built the client and the
comparison mechanism and proved both out against fixture data only --
nothing had run them against a real, full universe. This module is that
run: fetch every universe ticker's fundamentals from both yfinance
(already the production source) and SEC EDGAR's XBRL companyfacts, run
`xbrl.shadow_compare` per ticker, and write every disagreement found to
a CSV for a human to review by hand. That hand review -- not this
module -- is what actually gates M37's switchover; this module's job
ends at producing the report, not judging it.

Deliberately read-only and broker-free, same as daily_screen.py: never
imports execution.py, never touches state.json/journal.db. A full
universe pass here is a diagnostic comparison exercise, not a trading
decision or even a screen -- there is no "buyable" concept in this
module's output at all.

Not wired into any recurring cadence (daily_screen.py, evaluate.py,
execute_trades.py) on purpose: this is a one-time (or occasional,
re-run-on-demand) validation cycle §3.4 calls for before M37's
switchover, not a permanent daily addition to production's own EDGAR
request budget. Dispatched on its own workflow
(.github/workflows/xbrl-shadow-run.yml, workflow_dispatch-only, no
Alpaca secrets) instead.

Known limitation, accepted rather than engineered around (staff-
engineer-reviewer finding): the report is written once, after the full
sequential pass over every ticker completes -- a mid-run failure (a
workflow timeout, a stretch of slow/failing EDGAR responses eating far
more wall-clock than the nominal ~3-minute budget at
config.SEC_EDGAR_MAX_REQUESTS_PER_SECOND) loses all completed comparison
work with no incremental checkpoint, and the next attempt pays the full
EDGAR request budget again from scratch. Accepted for an occasional,
human-triggered diagnostic run rather than building incremental-
checkpoint persistence a one-off script doesn't otherwise need -- if
this becomes a recurring pain in practice, that's the signal to add it,
not a reason to build it preemptively here.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime
import logging
import sys

import config
import data
import journal
import universe
import xbrl

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ShadowRunSummary:
    """Coverage and outcome counts for one full shadow-comparison cycle.

    Kept separate from the CSV (which lists disagreements only, per
    §3.4's "per-field disagreement report" language) -- a hand reviewer
    also needs to know how much of the universe the comparison actually
    covered, not just what disagreed, to judge whether the cycle is
    complete enough to act on.

    `xbrl_not_found` and `xbrl_fetch_failed` are deliberately separate
    counts, not one combined "no facts" number (staff-engineer-reviewer
    finding): a 404 means EDGAR genuinely has no XBRL data for that
    filer (a foreign private issuer on 20-F, a very new listing) --
    expected, structural non-coverage a re-run won't fix. A fetch
    failure (network/timeout/malformed response) means this run itself
    had trouble reaching EDGAR for that ticker -- transient, and a
    re-run might well recover it. Collapsing the two into one gap
    number would leave a hand reviewer unable to tell "the universe
    structurally has this much XBRL coverage" from "this particular run
    was degraded," which is exactly the judgment this summary exists to
    support.

    `degraded` (True if yfinance_fetched, cik_matched, OR the fraction of
    CIK-matched tickers EDGAR actually *resolved* -- fetched facts or a
    confirmed 404, as opposed to xbrl_fetch_failed -- comes in under
    `config.MIN_UNIVERSE_FETCH_FRACTION`, the same floor daily_screen.py
    applies to its own yfinance fetch) flags a run whose *fetch
    mechanics* had a bad day -- not a run with a lot of genuine XBRL
    non-coverage, which `xbrl_not_found` already accounts for
    separately. `comparable`/`xbrl_not_found` themselves are NOT part of
    the degraded check, for exactly that reason: plenty of the universe
    (financials, insurers, filers that don't tag GrossProfit) is
    expected to have no comparable XBRL value on a perfectly healthy
    run, and gating on that fraction would flag every run as degraded
    regardless of whether anything actually went wrong. `xbrl_fetch_failed`
    IS part of the check, though, since a high fetch-failure rate (as
    opposed to a high not-found rate) means EDGAR itself had reachability
    trouble this run -- exactly the kind of "fetch mechanics had a bad
    day" this flag exists to catch, not expected non-coverage.
    """

    universe_size: int
    yfinance_fetched: int
    cik_matched: int
    xbrl_facts_fetched: int
    xbrl_not_found: int  # confirmed 404 -- EDGAR has no XBRL data for this filer
    xbrl_fetch_failed: int  # network/timeout/malformed-response failure, not a 404
    comparable: int  # both a yfinance and an XBRL value were available to compare
    disagreements: int
    degraded: bool


@dataclasses.dataclass(frozen=True)
class _TickerResult:
    cik_matched: bool
    facts_fetched: bool
    not_found: bool
    fetch_failed: bool
    compared: bool  # both a yfinance and an XBRL gross_margin value existed to compare
    disagreements: list[xbrl.FieldDisagreement]


def _run_one_ticker(
    ticker: str, yf_metrics: data.Metrics | None, cik_lookup: dict[str, str]
) -> _TickerResult:
    """Compare one ticker's fundamentals across both sources.

    A ticker with no yfinance metrics (fetch failed) still gets an XBRL
    lookup attempted -- cik_matched/facts_fetched are about EDGAR's own
    coverage of the universe, independent of whether yfinance happened
    to succeed for the same ticker this run.
    """
    cik = xbrl.get_cik(ticker, cik_lookup)
    if cik is None:
        return _TickerResult(False, False, False, False, False, [])
    result = xbrl.fetch_company_facts_detailed(cik)
    if result.facts is None:
        return _TickerResult(True, False, result.not_found, not result.not_found, False, [])
    xbrl_gross_margin = xbrl.gross_margin_from_xbrl(result.facts)
    yf_gross_margin = yf_metrics.gross_margin if yf_metrics is not None else None
    compared = xbrl_gross_margin is not None and yf_gross_margin is not None
    disagreements = (
        xbrl.shadow_compare(ticker, result.facts, yf_metrics) if yf_metrics is not None else []
    )
    return _TickerResult(True, True, False, False, compared, disagreements)


def run(run_date: str | None = None) -> ShadowRunSummary:
    """Run one full-universe shadow comparison cycle and write the disagreement report.

    Sequential over tickers (not thread-pooled like data.fetch_all_metrics)
    -- xbrl.throttled_get already serializes every EDGAR request behind
    one shared rate limiter (also shared with material_events.py's 8-K
    poller, per that module's own docstring), so concurrent callers here
    would only queue behind the same lock, not actually go faster, while
    adding needless thread-pool complexity to a run that -- at
    config.SEC_EDGAR_MAX_REQUESTS_PER_SECOND -- comfortably finishes a
    ~1500-ticker universe well within a scheduled workflow's timeout.

    That shared rate limiter is only shared *within one process*
    (staff-engineer-reviewer finding): material_events.py's own EDGAR
    polling runs inside daily-trade.yml/daily-trade-live.yml, separate
    GitHub Actions jobs from this module's own dispatch-only workflow.
    Manually dispatching this module while one of those crons is also
    running means two independently-throttled processes each pacing at
    config.SEC_EDGAR_MAX_REQUESTS_PER_SECOND, whose *combined* real rate
    against SEC EDGAR could exceed the fair-access ceiling this whole
    scheme exists to respect. See xbrl-shadow-run.yml's own comment for
    the mitigation (avoid dispatching during the daily-trade cron
    window) -- a documented operational constraint, not a structural
    guarantee, since a true cross-workflow lock isn't worth building for
    an occasional, human-triggered diagnostic run.
    """
    journal.configure_logging()
    run_date = run_date or datetime.date.today().isoformat()
    logger.info("Starting XBRL shadow-comparison run for %s", run_date)

    universe_result = universe.get_universe_with_diagnostics()
    tickers = universe_result.tickers
    for index in universe_result.fallback_indices:
        logger.warning("S&P %s universe fallback triggered", index)

    yf_metrics = data.fetch_all_metrics(tickers, phase="xbrl shadow")
    cik_lookup = xbrl.load_cik_lookup()

    cik_matched = 0
    facts_fetched = 0
    not_found = 0
    fetch_failed = 0
    comparable = 0
    all_disagreements: list[xbrl.FieldDisagreement] = []
    for ticker in tickers:
        result = _run_one_ticker(ticker, yf_metrics.get(ticker), cik_lookup)
        cik_matched += result.cik_matched
        facts_fetched += result.facts_fetched
        not_found += result.not_found
        fetch_failed += result.fetch_failed
        comparable += result.compared
        all_disagreements.extend(result.disagreements)

    universe_size = len(tickers)
    yfinance_fetched = sum(1 for m in yf_metrics.values() if m is not None)
    # Same floor daily_screen.py applies to its own yfinance fetch --
    # reused here rather than a new threshold invented for this module,
    # and deliberately checked only against counts that reflect this
    # run's own fetch mechanics (yfinance, CIK matching, EDGAR
    # reachability), not against xbrl_facts_fetched/comparable directly,
    # which are expected to be well under 100% on a perfectly healthy
    # run (see ShadowRunSummary's own docstring on why).
    #
    # `reachable` (staff-engineer-reviewer finding, second pass): a
    # confirmed 404 and a successful facts fetch are both a *resolved*
    # EDGAR request -- SEC answered, whether or not this filer has XBRL
    # data. `xbrl_fetch_failed` is the one outcome that means EDGAR
    # itself had trouble answering this run (a timeout, a 500, a
    # malformed response) -- exactly the "fetch mechanics had a bad day"
    # case this flag exists to catch, and the first version of this
    # check missed it entirely: a real EDGAR-side outage on the
    # companyfacts endpoint mid-run would have left cik_matched/
    # yfinance_fetched both near 100% (neither depends on this
    # endpoint) while comparable/xbrl_facts_fetched silently collapsed
    # to near zero, with degraded staying False and the run exiting 0.
    reachable = facts_fetched + not_found
    # A zero-length universe is degraded too (staff-engineer-reviewer
    # finding, push-gate review): the previous `universe_size > 0`
    # guard short-circuited straight to False on a totally empty
    # universe -- an "0 tickers, 0 comparable" run reported as clean
    # exactly like an empty-but-successful case. daily_screen.py's own
    # fetched_fraction (this module's cited precedent) treats a
    # zero-length result as 0.0, correctly failing its own floor check
    # rather than reading as trivially healthy -- this module's total-
    # failure scenario (every S&P index scrape AND the static fallback
    # failing simultaneously) is exactly the case a coverage gate exists
    # to catch, and the empty-universe case was the one input where it
    # was guaranteed to say nothing was wrong.
    degraded = universe_size == 0 or (
        yfinance_fetched / universe_size < config.MIN_UNIVERSE_FETCH_FRACTION
        or cik_matched / universe_size < config.MIN_UNIVERSE_FETCH_FRACTION
        or (cik_matched > 0 and reachable / cik_matched < config.MIN_UNIVERSE_FETCH_FRACTION)
    )

    summary = ShadowRunSummary(
        universe_size=universe_size,
        yfinance_fetched=yfinance_fetched,
        cik_matched=cik_matched,
        xbrl_facts_fetched=facts_fetched,
        xbrl_not_found=not_found,
        xbrl_fetch_failed=fetch_failed,
        comparable=comparable,
        disagreements=len(all_disagreements),
        degraded=degraded,
    )
    _write_report(all_disagreements)

    if degraded:
        logger.warning(
            "XBRL shadow run DEGRADED: yfinance fetched %d/%d (%.1f%%), CIK matched %d/%d "
            "(%.1f%%), EDGAR resolved %d/%d CIK-matched requests (%.1f%%) -- below the "
            "%.0f%% floor. This run's own fetch mechanics had trouble; the report below may "
            "understate real coverage. Consider re-running before relying on it for M37's "
            "hand review.",
            yfinance_fetched,
            universe_size,
            100 * yfinance_fetched / universe_size if universe_size else 0.0,
            cik_matched,
            universe_size,
            100 * cik_matched / universe_size if universe_size else 0.0,
            reachable,
            cik_matched,
            100 * reachable / cik_matched if cik_matched else 0.0,
            100 * config.MIN_UNIVERSE_FETCH_FRACTION,
        )

    logger.info(
        "XBRL shadow run complete: %d universe tickers, %d yfinance-fetched, "
        "%d CIK-matched, %d XBRL-facts-fetched (%d not-found-on-EDGAR, %d fetch-failed), "
        "%d comparable, %d disagreement(s) found. Report written to %s -- needs hand review "
        "before M37's switchover.",
        summary.universe_size,
        summary.yfinance_fetched,
        summary.cik_matched,
        summary.xbrl_facts_fetched,
        summary.xbrl_not_found,
        summary.xbrl_fetch_failed,
        summary.comparable,
        summary.disagreements,
        config.XBRL_SHADOW_REPORT_PATH,
    )
    return summary


def _write_report(disagreements: list[xbrl.FieldDisagreement]) -> None:
    """Write every disagreement found to a CSV, worst-first, for hand review.

    Atomic write (temp file + rename), matching this codebase's existing
    convention for every other persisted-report write (portfolio.py's
    state.json, screener.py's screen_results.csv).
    """
    config.XBRL_SHADOW_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config.XBRL_SHADOW_REPORT_PATH.with_suffix(
        config.XBRL_SHADOW_REPORT_PATH.suffix + ".tmp"
    )
    ordered = sorted(disagreements, key=lambda d: d.absolute_diff, reverse=True)
    with tmp_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["ticker", "field", "xbrl_value", "yfinance_value", "absolute_diff", "relative_diff"]
        )
        for d in ordered:
            writer.writerow(
                [
                    d.ticker,
                    d.field,
                    d.xbrl_value,
                    d.yfinance_value,
                    d.absolute_diff,
                    "" if d.relative_diff is None else d.relative_diff,
                ]
            )
    tmp_path.replace(config.XBRL_SHADOW_REPORT_PATH)


if __name__ == "__main__":
    try:
        # A degraded run still writes a (possibly-understated) report --
        # exiting non-zero here matches daily_screen.py's own convention
        # (process exit code as the alert channel) so a degraded run is
        # visible in the dispatched workflow's own run status, not just
        # buried in a log line a reviewer has to go looking for.
        sys.exit(1 if run().degraded else 0)
    except Exception:
        logger.exception("XBRL shadow run crashed with an unhandled exception")
        raise
