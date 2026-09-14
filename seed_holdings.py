"""CLI to seed journal.py with an account's pre-existing broker holdings (M49).

The one-off tool for a real gap M47 (adding the ira account) surfaced but
didn't fix: an account onboarded to this bot mid-life can already hold
real positions this bot never placed. journal.get_expected_holdings()
starts empty for a fresh journal, so the first run with real trading
enabled would find every pre-existing position "unexpected" and
journal.check_reconciliation()'s abort-on-mismatch (M27, applies to every
non-paper account) would block the run on all of them.

The one prior precedent for this exact failure mode (M20a's pre-existing
1-share `G` holding on the live account) was resolved by simply selling
the position at the broker -- not a viable fix for an account already
holding tens of thousands of dollars across several positions, which is
exactly the case this script exists for.

Mechanism: reads the account's current holdings straight from Alpaca
(execution.ExecutionModule.get_current_holdings(), already the exact
method portfolio.py itself uses every run) and, for every symbol not
already in journal.get_expected_holdings(account), inserts one "buy" row
via journal.record_order(..., account=account) with no notional/qty --
reconciliation is a pure symbol-set comparison (confirmed against
journal.py's own get_expected_holdings query), so no invented dollar
amount is ever recorded for a position this bot didn't size. Idempotent
by construction: a symbol already expected-held is skipped, so a re-run
never double-seeds.

--account must match config.ACCOUNT_LABEL (refuses otherwise, before
touching the broker or the journal at all) -- unlike record_override.py,
this script's --account isn't just a label on a row, it also selects
which broker's holdings get read, via whatever ALPACA_API_KEY/
ALPACA_SECRET_KEY this process's credentials point at. Without this
check, a mismatched --account would silently seed one real account's
holdings into a different account's journal partition (staff-engineer-
reviewer finding, push review) -- the existing reconciliation
abort-on-mismatch would eventually catch the resulting divergence, but
only after wrong rows already sat in a real account's append-only
`orders` table with no delete/undo path.

Usage (run manually against wherever the account's real journal.db
lives, e.g. via a one-off GitHub Actions dispatch that restores/persists
the real bot-state-ira branch -- see seed-holdings-ira.yml -- the same
"deliberate human action, not a scheduled job" shape record_override.py
already established):

    python seed_holdings.py --account ira
    python seed_holdings.py --account ira --dry-run   # preview only, writes nothing
"""

from __future__ import annotations

import argparse
import datetime
import sys

import config
import execution
import journal


def main(argv: list[str] | None = None) -> int:
    """Parse args, seed any missing holdings, print a summary. Returns an exit code."""
    parser = argparse.ArgumentParser(
        description="Seed journal.py with an account's pre-existing broker holdings (M49)."
    )
    parser.add_argument(
        "--account",
        required=True,
        choices=("paper", "live", "ira"),
        help="Which account's journal to seed -- must match the credentials this process "
        "is actually running against.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be seeded without writing anything.",
    )
    args = parser.parse_args(argv)

    # staff-engineer-reviewer finding (push review): unlike record_override.py
    # (which never touches the broker, so --account can't disagree with
    # reality), this script fetches real holdings from whichever credentials
    # config.ALPACA_API_KEY/ALPACA_SECRET_KEY currently point at -- nothing
    # previously stopped --account from silently disagreeing with those
    # credentials (e.g. --account ira run against live credentials would
    # seed the live account's real holdings into the ira journal). The
    # existing reconciliation abort-on-mismatch would eventually surface
    # this, but only after it already wrote wrong rows into a real
    # account's append-only orders table with no delete/undo path. Fail
    # loud here instead, before touching the broker or the journal at all.
    if args.account != config.ACCOUNT_LABEL:
        print(
            f"Refusing to seed account={args.account!r}: this process is running as "
            f"config.ACCOUNT_LABEL={config.ACCOUNT_LABEL!r} (set MUNGER_ACCOUNT_LABEL/"
            "MUNGER_PAPER_TRADING to match --account, or fix --account to match the "
            "credentials this process actually has).",
            file=sys.stderr,
        )
        return 1

    run_date = datetime.date.today().isoformat()
    exec_module = execution.ExecutionModule(run_date=run_date)
    current_holdings = exec_module.get_current_holdings()
    already_expected = journal.get_expected_holdings(args.account)

    to_seed = sorted(set(current_holdings) - already_expected)
    if not to_seed:
        print(
            f"Nothing to seed for account={args.account!r} -- every held symbol is already in "
            "the journal."
        )
        return 0

    for symbol in to_seed:
        reason = (
            f"SEEDED: pre-existing {args.account} holding as of {run_date}, not placed by this bot"
        )
        if args.dry_run:
            print(f"[dry-run] would seed {symbol} (account={args.account}): {reason}")
            continue
        journal.record_order(symbol, "buy", reason, account=args.account)
        print(f"Seeded {symbol} (account={args.account}): {reason}")

    already_seeded = sorted(set(current_holdings) & already_expected)
    if already_seeded:
        print(f"Already in the journal, skipped: {', '.join(already_seeded)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
