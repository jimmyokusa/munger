"""Daily screen-only snapshot (M14, user request) -- no trading, ever.

Runs the universe fetch + screener and archives the result, once a day,
purely to build up the HTML report's calendar view (report.py) with
richer history than the quarterly trading cadence alone would produce.
Deliberately does not import execution.py or construct ExecutionModule
at all -- this script is architecturally incapable of placing an order,
not just configured not to. Trading decisions stay on bot.py's own
quarterly cadence (DESIGN.md 4); this is a read-only, informational
sibling, not a faster trading loop.
"""

from __future__ import annotations

import datetime
import logging
import sys

import config
import journal
import report
import screener
import universe

logger = logging.getLogger(__name__)


def run(run_date: str | None = None) -> bool:
    """Fetch, screen, and archive today's universe. Never touches a broker.

    Returns True if the run archived (fetch quality met the floor), False
    if it was skipped as degraded. The caller (__main__) turns a False into
    a non-zero exit -- see the module docstring's note on why this can't
    just log and return normally.
    """
    journal.configure_logging()
    run_date = run_date or datetime.date.today().isoformat()
    logger.info("Starting daily screen-only run for %s", run_date)

    universe_result = universe.get_universe_with_diagnostics()
    for index in universe_result.fallback_indices:
        logger.warning("S&P %s universe fallback triggered", index)

    results = screener.run_screen(universe_result.tickers)

    # Fetch-quality gate (staff-engineer-reviewer): the trading path aborts
    # a run that fetched too little of the universe (bot.py / DESIGN.md §5),
    # but the daily screen has no trade to abort -- its risk is instead
    # writing a throttled, mostly-fetch_failed snapshot into the permanent
    # archive, where the calendar would render it identically to a genuine
    # "screened, nothing passed" day. So on a degraded run we skip
    # archiving (don't pollute the history) while still regenerating the
    # live report, whose M13 UI already flags fetch_failed rows.
    fraction = screener.fetched_fraction(results)
    buyable_count = int(results["buyable"].sum())
    archived = fraction >= config.MIN_UNIVERSE_FETCH_FRACTION
    if not archived:
        logger.warning(
            "Daily screen fetched only %.1f%% of the universe (< %.1f%% floor) -- "
            "NOT archiving this run to avoid polluting the history with a throttled "
            "snapshot. %d tickers, %d buyable.",
            fraction * 100,
            config.MIN_UNIVERSE_FETCH_FRACTION * 100,
            len(results),
            buyable_count,
        )
    else:
        journal.archive_screen_results(run_date)
        logger.info("Daily screen complete: %d tickers, %d buyable.", len(results), buyable_count)

    report.generate_report()
    return archived


if __name__ == "__main__":
    try:
        # A degraded (not-archived) run must exit non-zero -- otherwise the
        # GitHub Actions job / k3s CronJob Job shows green every day even
        # while silently never archiving, defeating the whole point of this
        # script and paging no one (staff-engineer-reviewer finding). This
        # mirrors bot.py's use of process exit code as its alert channel.
        sys.exit(0 if run() else 1)
    except Exception:
        # Same pattern as bot.py's __main__: the default excepthook only
        # writes to stderr, which doesn't survive an ephemeral runner/pod --
        # log to munger.log (persisted via GH Actions / the k3s PVC) before
        # re-raising so the process still exits non-zero.
        logger.exception("Daily screen run crashed with an unhandled exception")
        raise
