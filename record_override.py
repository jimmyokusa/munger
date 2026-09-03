"""CLI to journal a manual override (Design v2.2 §3.8, M43).

The one actual write path for journal.record_manual_override -- staff-
engineer-reviewer finding on M42/M43: the function existed and was
correctly guarded (never called by any automated code path) but had no
usable entry point for an actual human, so the manual_overrides table
would in practice have stayed permanently empty regardless of real
override behavior, silently defeating the "alerts per holding per
quarter" process metric §3.8 exists to establish.

Usage (run manually, the same operational shape as `touch KILL_SWITCH`
or a `gcloud`/`kubectl` command -- a deliberate human action, not a
scheduled job):

    python record_override.py HRMY "Litigation ruling alert reviewed, holding
        through the appeal window, thesis intact"

Reads/writes config.JOURNAL_DB_PATH directly -- run this from wherever
that database actually lives for the account being overridden (the
`bot-state`/`bot-state-live` git branch's checkout, or against a locally
pulled copy), same as any other one-off `python -c "import journal..."`
operational action this project already relies on for infrequent manual
interventions.
"""

from __future__ import annotations

import argparse
import sys

import config
import journal


def main(argv: list[str] | None = None) -> int:
    """Parse args, record the override, print a confirmation. Returns an exit code."""
    parser = argparse.ArgumentParser(
        description="Journal a manual override with a reason (Design v2.2 §3.8)."
    )
    parser.add_argument("ticker", help="The ticker this override concerns, e.g. HRMY")
    parser.add_argument("reason", help="Why -- required, freeform text")
    parser.add_argument(
        "--account",
        choices=("paper", "live"),
        default=None,
        help="Defaults to whichever account config.PAPER_TRADING currently says.",
    )
    args = parser.parse_args(argv)

    if not args.reason.strip():
        print("Refusing to record an override with an empty reason.", file=sys.stderr)
        return 1

    journal.record_manual_override(args.ticker.upper(), args.reason, account=args.account)
    account = args.account or ("paper" if config.PAPER_TRADING else "live")
    print(f"Recorded manual override for {args.ticker.upper()} ({account}): {args.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
