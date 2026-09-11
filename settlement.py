"""Settlement pass (Design v2.2 §3.3, M26b/c/d).

Reconciles submitted orders against what actually happened. RC3
(Design v2.2 §2) was that orders were submitted and immediately
journaled as though they'd succeeded -- state transitioned on intent,
not outcome, which is how FOX/LPG ended up sold in the journal while
still genuinely held at the broker (their DAY limit orders never
filled). This module is the fix: given a list of client_order_ids that
were submitted but not yet confirmed, query each one's real status and
write it to journal.fills. Callers (bot.py) key state transitions
(strike resets, position tracking) off `fills`, never off `orders`
alone.

Distinguishes two outcomes that look similar but require different
responses, per §3.3's own specification:

- A query failure (the broker is unreachable, times out, or 5xxs) is
  retried with backoff; if retries are exhausted, the order is reported
  as unsettled this pass -- the caller (bot.py) sets the kill-switch
  flag and blocks the next execution window rather than proceeding on a
  position picture it can't currently verify.
- A successful query that reports the order still open (`pending`) is
  not an error -- it's just not resolved yet. When the caller asks
  (`settle_order(..., wait_for_fill=True)` -- the synchronous check
  bot.py / execute_trades.py run right after submitting an order), a
  still-pending order is re-polled a few times (M46,
  `config.SETTLEMENT_FILL_WAIT_*`) to ride out Alpaca's brief
  pending_new/accepted window before a fast market-order fill registers;
  if it's still pending after that, it's journaled `pending` and left
  for the next settlement pass, exactly as an un-waited call would.

Idempotent by construction: `journal.record_fill` upserts on
`client_order_id`, so re-running this module against the same order set
(e.g. after a crash mid-pass) only ever converges toward the order's
current true status, never duplicates a row.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from alpaca.trading.enums import OrderStatus
from alpaca.trading.models import Order

import config
import execution
import journal

logger = logging.getLogger(__name__)

# Terminal statuses that mean "no fill happened and none ever will" for
# this order -- distinct from EXPIRED (a DAY order that ran out of time,
# §3.3's own named case) and distinct from PARTIALLY_FILLED (a real,
# partial outcome that must be journaled, not discarded as a plain
# cancellation).
_TERMINAL_NO_FILL = frozenset(
    {
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.SUSPENDED,
        OrderStatus.STOPPED,
    }
)


def _classify(order: Order) -> str:
    """Map an Alpaca order's status onto journal.record_fill's vocabulary.

    Every open/in-flight Alpaca status (NEW, ACCEPTED, PENDING_NEW,
    PENDING_REVIEW, CALCULATED, HELD, REPLACED, PENDING_CANCEL,
    PENDING_REPLACE, ACCEPTED_FOR_BIDDING, DONE_FOR_DAY-with-no-fill)
    collapses to "pending" -- §3.3 only distinguishes pending/partial/
    filled/expired/canceled, not Alpaca's much finer internal state
    machine, and a settlement pass re-polls "pending" orders again next
    time regardless of which specific in-flight status caused it.
    """
    if order.status == OrderStatus.FILLED:
        return "filled"
    if order.status == OrderStatus.PARTIALLY_FILLED:
        return "partially_filled"
    if order.status == OrderStatus.EXPIRED:
        return "expired"
    if order.status in _TERMINAL_NO_FILL:
        return "canceled"
    return "pending"


@dataclass
class SettlementResult:
    """Outcome of one settlement pass over a set of client_order_ids."""

    settled: list[str] = field(default_factory=list)
    query_failed: list[str] = field(default_factory=list)

    @property
    def all_settled(self) -> bool:
        """True if every order in this pass resolved to a real broker status."""
        return not self.query_failed


def _query_status_with_retry(
    exec_module: execution.ExecutionModule, client_order_id: str
) -> Order | None:
    """Fetch one order's status, retrying only a *failing* query.

    Returns the `Order` on the first successful query, or `None` if every
    attempt raised -- `None` means "couldn't find out," never "no fill."
    A successful query is returned as-is regardless of what status it
    reports; deciding whether a still-open order is worth waiting on is
    settle_order's job, not this one's.
    """
    # Read from config at call time, not bound as a module-level
    # constant at import time -- a test (or a future runtime retune)
    # monkeypatching config.SETTLEMENT_QUERY_RETRY_* must actually take
    # effect, the same discipline config.py's own module docstring
    # requires of every other read site in this codebase.
    retry_attempts = config.SETTLEMENT_QUERY_RETRY_ATTEMPTS
    retry_backoff_seconds = config.SETTLEMENT_QUERY_RETRY_BACKOFF_SECONDS

    for attempt in range(1, retry_attempts + 1):
        try:
            return exec_module.get_order_status(client_order_id)
        except Exception:
            logger.warning(
                "%s: settlement status query failed (attempt %d/%d)",
                client_order_id,
                attempt,
                retry_attempts,
                exc_info=True,
            )
            if attempt < retry_attempts:
                time.sleep(retry_backoff_seconds)
    return None


def settle_order(
    exec_module: execution.ExecutionModule,
    client_order_id: str,
    *,
    wait_for_fill: bool = False,
) -> str | None:
    """Query and journal one order's current status, with retry on query failure.

    Returns the classified status string (one of journal's
    `_VALID_FILL_STATUSES`) on a successful query, or `None` if the
    query itself failed on every retry attempt -- `None` here means
    "couldn't find out," never "no fill," which is why it's a distinct
    return value from the status strings rather than an exception the
    caller has to unpack.

    `wait_for_fill` (M46): for the synchronous check bot.py /
    execute_trades.py run milliseconds after submitting an order, re-poll
    a still-`pending` order up to `config.SETTLEMENT_FILL_WAIT_POLLS`
    times, `config.SETTLEMENT_FILL_WAIT_POLL_SECONDS` apart, before
    giving up and reporting it pending -- long enough to ride out
    Alpaca's brief pending_new/accepted window ahead of a fast
    market-order fill, short enough that a genuinely stuck order still
    resolves to `pending` (and alerts) within seconds, not minutes. Only
    a `pending` classification is waited on; `filled`, `partially_filled`,
    `expired`, and `canceled` are all resolved outcomes and return at
    once. Left `False`, this function behaves exactly as it did pre-M46
    (one query, no fill wait).

    Two staff-engineer-reviewer findings on the M46 wait shaped the loop
    below:

    - **A blip on a *later* re-poll does not erase a verified earlier
      status.** If the first poll successfully read the order as
      `pending` but a subsequent poll's query fails all its retries, the
      known `pending` stands (journaled and returned) -- it is *not*
      escalated to the `None` / query-failure path, which would trip the
      caller's kill switch. `None` is returned only when *no* poll ever
      got a successful read.
    - **One order's wait is wall-clock bounded**, not just poll-count
      bounded: each poll's own `_query_status_with_retry` can burn
      `SETTLEMENT_QUERY_RETRY_*`, so on a flaky endpoint the poll count
      alone wouldn't cap elapsed time. `wait_deadline` stops the loop
      once the allotted sleep budget has elapsed regardless of how slow
      the queries were, so a broker brownout on a non-filling day can't
      stack per-order retry budgets onto the job's timeout.
    """
    poll_budget = config.SETTLEMENT_FILL_WAIT_POLLS if wait_for_fill else 0
    poll_delay_seconds = config.SETTLEMENT_FILL_WAIT_POLL_SECONDS
    wait_deadline = time.monotonic() + poll_budget * poll_delay_seconds if poll_budget > 0 else None

    order: Order | None = None
    status: str | None = None
    polls_remaining = poll_budget
    while True:
        queried = _query_status_with_retry(exec_module, client_order_id)
        if queried is not None:
            order = queried
            status = _classify(order)
            if status != "pending":
                break
        elif order is None:
            # No successful read yet -- a genuine query failure the caller
            # must fail closed on.
            return None
        # else: this poll's query blipped, but an earlier poll already
        # verified this order as pending -- that stands; keep waiting.
        past_deadline = wait_deadline is not None and time.monotonic() >= wait_deadline
        if polls_remaining <= 0 or past_deadline:
            break
        polls_remaining -= 1
        logger.info(
            "%s: order still pending; re-checking for a fill in %.0fs (%d attempt(s) left)",
            client_order_id,
            poll_delay_seconds,
            polls_remaining,
        )
        time.sleep(poll_delay_seconds)

    if order is None or status is None:
        return None

    # staff-engineer-reviewer finding: these conversions ran outside the
    # retry loop's try/except, so a genuinely malformed numeric field in
    # an otherwise-successful query response wasn't a "query failure"
    # per §3.3 -- it was an unhandled exception propagating out of
    # bot.run()'s order loops, crashing the whole run instead of going
    # through the structured kill-switch path. A successful query with
    # bad data is a data-integrity problem, not a broker-communication
    # one; record the status with the numeric fields as unknown (None)
    # rather than either crashing the run or silently coercing garbage.
    try:
        filled_qty = float(order.filled_qty) if order.filled_qty is not None else None
    except (TypeError, ValueError):
        logger.error("%s: filled_qty %r is not numeric", client_order_id, order.filled_qty)
        filled_qty = None
    try:
        fill_price = float(order.filled_avg_price) if order.filled_avg_price is not None else None
    except (TypeError, ValueError):
        logger.error(
            "%s: filled_avg_price %r is not numeric", client_order_id, order.filled_avg_price
        )
        fill_price = None
    journal.record_fill(
        client_order_id=client_order_id,
        symbol=str(order.symbol),
        status=status,
        filled_qty=filled_qty,
        fill_price=fill_price,
    )
    return status


def settle_orders(
    exec_module: execution.ExecutionModule, client_order_ids: list[str]
) -> SettlementResult:
    """Run the settlement pass over every given client_order_id.

    Every order is attempted independently -- one query failure doesn't
    stop the pass from checking the rest, so a single flaky lookup
    doesn't block settlement of orders that would have resolved cleanly.
    The caller decides what `query_failed` non-empty means (M26d: sets
    the kill-switch flag and blocks the next execution window).
    """
    result = SettlementResult()
    for client_order_id in client_order_ids:
        status = settle_order(exec_module, client_order_id)
        if status is None:
            result.query_failed.append(client_order_id)
        else:
            result.settled.append(client_order_id)
    return result
