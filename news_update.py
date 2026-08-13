"""Claude-written news/performance digest (M22; monthly cadence, M23).

Reads pnl.py's freshly-written pnl.json for the held-symbol list (same
pattern prices.py already uses, for the same reason: the two modules can
never disagree about which symbols are "currently held" on a given run),
pulls each symbol's last-month news from Alpaca, and asks Claude to write a
short, Buffett-disciplined digest -- does the past month's news change
anything about a held business's moat, management, or financial health, or
is it just price noise -- which is then posted to Discord.

Runs from the same daily-scheduled workflow as before (M22) but only
actually generates and posts a digest on config.NEWS_UPDATE_DAY_OF_MONTH
(see _should_run_today()) -- every other day's invocation is a fast no-op.
See config.NEWS_UPDATE_DAY_OF_MONTH's own comment for why this cadence
lives in Python rather than a second, monthly cron schedule.

This is a genuinely new kind of integration for this codebase: the first
call to a paid LLM API, with real per-call cost (trivial at this volume,
but non-zero, unlike everything else here). It runs where ALPACA_API_KEY
and ANTHROPIC_API_KEY live (daily-trade.yml / daily-trade-live.yml, GitHub
Actions), never in report.py's own deployment (DESIGN.md 3.5/M14's
screen-only boundary).

Deliberately fails soft, unlike pnl.py's core snapshot generation: this is
a bonus display feature layered on top of the trading run, not something
that should ever fail the job or block the P&L pipeline. A broken Alpaca
News call, a rate-limited/refused Anthropic call, or a Discord outage is
logged and swallowed, matching pnl.py's own _send_discord_alert posture.
"""

from __future__ import annotations

import datetime
import json
import logging
import urllib.error
import urllib.request

import anthropic
from alpaca.common.exceptions import APIError as AlpacaAPIError
from alpaca.data.historical.news import NewsClient
from alpaca.data.models.news import NewsSet
from alpaca.data.requests import NewsRequest

import config

logger = logging.getLogger(__name__)

# Discord hard-caps a single message at 2000 characters; leave headroom
# for the mode-label header line prepended below.
_DISCORD_MESSAGE_CHAR_LIMIT = 1900

_SYSTEM_PROMPT = """\
You are writing a brief monthly update for a long-term value investor who \
holds the positions described below, covering the past month's news. \
Apply Warren Buffett's discipline: judge whether this month's news changes \
anything about a held business's durable competitive moat, management \
execution, or financial health -- not whether the price moved. Treat a \
price swing on its own as "Mr. Market" being emotional, never a signal to \
act on, unless the news reveals an actual change in business fundamentals.

For each position, in 2-4 sentences: summarize what happened over the \
month (if anything notable) and, only if relevant, whether it plausibly \
affects the long-term investment thesis. If there is no notable news, say \
so briefly -- do not invent commentary to fill space. Do not add generic \
market commentary unrelated to a specific held company.

Write in plain text formatted for Discord: **bold** each ticker, no \
markdown headers, no preamble or sign-off, one paragraph per position. \
Keep the entire response under 1700 characters.\
"""


def _held_symbol_positions(pnl_snapshot: dict[str, object]) -> list[dict[str, object]]:
    """Held positions (symbol, unrealized_plpc, current_price) from pnl.json.

    Sorted by symbol for a deterministic prompt. Mirrors prices.py's
    held_symbols() skip-malformed-entry posture -- a position missing its
    symbol is dropped rather than raising -- and its de-dupe-by-symbol
    posture (staff-engineer-reviewer finding: an earlier draft here kept
    every entry, so a duplicate symbol in positions[] would have rendered
    that ticker's digest section twice; first occurrence wins, matching
    prices.py's own set-based de-dupe).

    Deliberately raises (not caught here) on a malformed positions[]
    shape -- the caller (run()) is responsible for the soft-fail posture,
    same contract as fetch_recent_news below.
    """
    positions = pnl_snapshot.get("positions")
    if positions is None:
        positions = []
    assert isinstance(positions, list)
    held: dict[str, dict[str, object]] = {}
    for p in positions:
        if isinstance(p, dict) and isinstance(p.get("symbol"), str) and p["symbol"]:
            symbol = str(p["symbol"])
            held.setdefault(symbol, p)
    return [held[symbol] for symbol in sorted(held)]


def fetch_recent_news(symbols: list[str]) -> dict[str, list[dict[str, object]]]:
    """Fetch last-config.NEWS_LOOKBACK_HOURS news for the given symbols.

    One Alpaca News API call for all symbols (not one per symbol) --
    cheaper and avoids N sequential rate-limited calls for what is a
    once-a-month, low-volume feed. include_content=False keeps the fetch to
    headlines/summaries only (config.NEWS_PER_SYMBOL_LIMIT's own docstring
    explains why full article text isn't worth the extra tokens here).

    Returns {} for a symbol with no news in the window -- callers should
    treat that as "nothing notable," not an error. Raises on a genuine API
    failure; the caller (main()) is responsible for the soft-fail posture
    described in this module's docstring.
    """
    if not symbols:
        return {}
    client = NewsClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    start = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=config.NEWS_LOOKBACK_HOURS
    )
    # A single combined request capped at a small fixed limit would let a
    # couple of unusually newsy held symbols crowd out the whole
    # recency-sorted feed, silently starving other genuinely-newsy symbols
    # of any articles at all -- indistinguishable, downstream, from those
    # symbols just having no news (staff-engineer-reviewer finding). Scale
    # the request's own limit with how many articles could actually be
    # used (NEWS_PER_SYMBOL_LIMIT per held symbol), with headroom for
    # articles that mention more than one held symbol and so count against
    # more than one per-symbol quota at once.
    limit = max(50, len(symbols) * config.NEWS_PER_SYMBOL_LIMIT * 2)
    request = NewsRequest(
        symbols=",".join(symbols),
        start=start,
        limit=limit,
        include_content=False,
        exclude_contentless=True,
        sort="desc",
    )
    news_set = client.get_news(request)
    if not isinstance(news_set, NewsSet):
        # Same fail-closed posture as pnl.py/prices.py's own response-type
        # checks: a schema drift or malformed response must abort this
        # fetch rather than silently producing an empty-looking digest.
        raise ValueError(f"unexpected get_news() response: {news_set!r}")
    by_symbol: dict[str, list[dict[str, object]]] = {symbol: [] for symbol in symbols}
    for article in news_set.data.get("news", []):
        for symbol in article.symbols:
            if symbol not in by_symbol or len(by_symbol[symbol]) >= config.NEWS_PER_SYMBOL_LIMIT:
                continue
            by_symbol[symbol].append(
                {
                    "headline": article.headline,
                    "summary": article.summary,
                    "source": article.source,
                }
            )
    return by_symbol


def _format_user_message(
    mode: str,
    positions: list[dict[str, object]],
    news_by_symbol: dict[str, list[dict[str, object]]],
) -> str:
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    lookback_days = config.NEWS_LOOKBACK_HOURS // 24
    lines = [f"Date: {today} ({mode} account, covering the last {lookback_days} days)", ""]
    for position in positions:
        symbol = str(position["symbol"])
        plpc = position.get("unrealized_plpc")
        price = position.get("current_price")
        plpc_str = f"{plpc:+.1%}" if isinstance(plpc, int | float) else "unknown"
        price_str = f"${price:,.2f}" if isinstance(price, int | float) else "unknown"
        lines.append(f"## {symbol} (unrealized P&L {plpc_str}, price {price_str})")
        articles = news_by_symbol.get(symbol) or []
        if not articles:
            lines.append(f"No news in the last {lookback_days} days.")
        for article in articles:
            lines.append(f"- [{article['source']}] {article['headline']}: {article['summary']}")
        lines.append("")
    return "\n".join(lines)


def generate_digest(
    mode: str,
    positions: list[dict[str, object]],
    news_by_symbol: dict[str, list[dict[str, object]]],
) -> str:
    """Ask Claude to synthesize positions + news into a short digest."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=config.NEWS_UPDATE_MODEL,
        max_tokens=config.NEWS_MAX_OUTPUT_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _format_user_message(mode, positions, news_by_symbol),
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to generate the digest (stop_reason=refusal)")
    text = "\n".join(block.text for block in response.content if block.type == "text")
    text = text.strip()
    if response.stop_reason == "max_tokens":
        # staff-engineer-reviewer finding: NEWS_MAX_OUTPUT_TOKENS is
        # generous relative to the ~1700-char target, so this should be
        # rare -- but without this check a truncated mid-sentence digest
        # would post with no indication it was ever cut off.
        text += "\n\n_(response truncated at the output limit)_"
    return text


def _post_discord_message(message: str) -> None:
    """POSTs `message` to config.DISCORD_NEWS_WEBHOOK_URL.

    Truncated to Discord's message limit rather than split across multiple
    messages -- a once-a-month digest running slightly long is an
    acceptable loss of the tail, not worth the added complexity of a
    multi-message send for this feature.

    Explicit User-Agent required: real bug found live (first end-to-end
    test) -- Discord's API sits behind Cloudflare, which blocks urllib's
    default "Python-urllib/3.x" User-Agent as a bot signature (error code
    1010), returning a 403 that run()'s soft-fail wrapper was silently
    swallowing on every single run. See config.DISCORD_USER_AGENT's own
    comment for the full story (pnl.py's _send_discord_alert had the
    identical bug, fixed the same way).
    """
    if len(message) > _DISCORD_MESSAGE_CHAR_LIMIT:
        message = message[: _DISCORD_MESSAGE_CHAR_LIMIT - 1] + "…"
    body = json.dumps({"content": message}).encode("utf-8")
    request = urllib.request.Request(
        config.DISCORD_NEWS_WEBHOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": config.DISCORD_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def _month_key(today: datetime.date) -> str:
    """Returns today in YYYY-MM form -- the unit _should_run_today dedupes on."""
    return today.strftime("%Y-%m")


def _load_last_posted_month() -> str | None:
    """Reads config.NEWS_DIGEST_STATE_PATH's "last_posted_month" marker.

    Returns None if the marker is missing or malformed (e.g. the very
    first run ever, or a fresh bot-state/bot-state-live branch).
    """
    if not config.NEWS_DIGEST_STATE_PATH.exists():
        return None
    try:
        data = json.loads(config.NEWS_DIGEST_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    month = data.get("last_posted_month") if isinstance(data, dict) else None
    return month if isinstance(month, str) else None


def _record_posted_month(today: datetime.date | None = None) -> None:
    """Marks `today`'s calendar month as posted.

    So a later invocation in the same month's grace window
    (NEWS_UPDATE_GRACE_DAYS) no-ops instead of posting a duplicate digest.
    Called only after a real digest has actually been posted successfully
    -- see run()'s call site.

    Atomic (temp file + rename), matching portfolio.py's StateTracker.save()
    for the same reason: a crash mid-write (the job's own 45-minute
    timeout, an OOM) must not leave a corrupt marker. A corrupt/malformed
    file would already fail safe via _load_last_posted_month()'s own
    handling (treated as "not yet posted"), but this avoids relying on
    that as the only safety net for a file this design depends on.
    """
    if today is None:
        today = datetime.datetime.now(datetime.UTC).date()
    tmp_path = config.NEWS_DIGEST_STATE_PATH.with_suffix(
        config.NEWS_DIGEST_STATE_PATH.suffix + ".tmp"
    )
    tmp_path.write_text(json.dumps({"last_posted_month": _month_key(today)}))
    tmp_path.replace(config.NEWS_DIGEST_STATE_PATH)


def _should_run_today(today: datetime.date | None = None) -> bool:
    """True if today is in the trigger window and not already posted.

    The window is [NEWS_UPDATE_DAY_OF_MONTH, NEWS_UPDATE_DAY_OF_MONTH +
    NEWS_UPDATE_GRACE_DAYS] (M23: widened from a single exact day so a
    late/missed GitHub Actions schedule trigger on the target day still
    gets caught within the grace window) for what is otherwise a
    daily-scheduled workflow (daily-trade.yml/daily-trade-live.yml still
    run every calendar day for the actual trading/pnl.py/prices.py
    steps). The already-posted-this-month check (via
    _load_last_posted_month()) is what makes widening that window safe --
    without it, a grace window would post twice in most months, not just
    catch a genuinely missed one.

    Takes `today` explicitly (defaults to the real UTC date) so a specific
    calendar day is trivial to unit-test without monkeypatching
    datetime.datetime.now.
    """
    if today is None:
        today = datetime.datetime.now(datetime.UTC).date()
    window_end = config.NEWS_UPDATE_DAY_OF_MONTH + config.NEWS_UPDATE_GRACE_DAYS
    in_window = config.NEWS_UPDATE_DAY_OF_MONTH <= today.day <= window_end
    already_posted = _load_last_posted_month() == _month_key(today)
    return in_window and not already_posted


def run(pnl_snapshot: dict[str, object]) -> None:
    """Generates and posts the digest for one pnl.py-shaped snapshot.

    No-op if either config.DISCORD_NEWS_WEBHOOK_URL or
    config.ANTHROPIC_API_KEY is unset (config-gated, same shape as
    pnl.py's check_position_loss_alerts); if there are no held positions
    (nothing to summarize); or if today is outside the monthly trigger
    window / this month's digest was already posted, and
    config.NEWS_UPDATE_FORCE_RUN isn't set (M23's monthly-cadence gate --
    see _should_run_today()).

    Soft-fails on any error from the news fetch, the Anthropic call, or
    the Discord post -- logged as a warning, never raised. See this
    module's own docstring for why: this is a bonus display feature, and
    must never fail the trading job it runs alongside.
    """
    if not config.DISCORD_NEWS_WEBHOOK_URL or not config.ANTHROPIC_API_KEY:
        return
    if not config.NEWS_UPDATE_FORCE_RUN and not _should_run_today():
        return
    try:
        # _held_symbol_positions asserts on a malformed positions[] shape
        # (staff-engineer-reviewer finding: an earlier draft called this
        # ahead of the try block below, so a schema surprise in pnl.json --
        # a separate process's output, not something this module controls
        # -- would have raised an uncaught AssertionError and defeated the
        # whole soft-fail contract this function exists to provide).
        positions = _held_symbol_positions(pnl_snapshot)
        if not positions:
            return
        mode = str(pnl_snapshot.get("mode") or "paper")
        symbols = [str(p["symbol"]) for p in positions]
        news_by_symbol = fetch_recent_news(symbols)
        digest = generate_digest(mode, positions, news_by_symbol)
        # pm-reviewer finding (M22): this is unmoderated, AI-generated
        # commentary posted unattended about a real-money account -- the
        # message must say so plainly, so nobody (the user or anyone else
        # who can see the channel) mistakes it for vetted human analysis.
        header = (
            f"\N{NEWSPAPER} **{mode.upper()} monthly update** "
            "_(AI-generated summary, not reviewed or investment advice)_\n\n"
        )
        _post_discord_message(header + digest)
        # Recorded only after a successful post -- a soft-failed run below
        # must NOT mark this month as posted, or the grace window (whose
        # whole purpose is catching exactly that kind of miss) would
        # never get a second chance this month.
        _record_posted_month()
    except (
        AlpacaAPIError,
        anthropic.APIError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        AssertionError,
    ) as exc:
        # pm-/staff-engineer-reviewer finding: a logger.warning alone lands
        # in an ephemeral GitHub Actions log nobody opens unless they
        # already suspect a problem -- exactly what hid the Discord
        # User-Agent bug (see news_update._post_discord_message's own
        # docstring) for this feature's entire life. `::warning::` is a
        # GitHub Actions workflow command that surfaces the message in the
        # run's Annotations directly, for any future swallowed failure
        # here, not just today's cause. Deliberately not a bigger
        # mechanism (a persisted consecutive-failure counter) -- the
        # smallest change that makes the failure mode visible at all.
        print(f"::warning::Monthly news digest failed; skipping this run: {exc}")
        logger.warning("Monthly news digest failed; skipping this run: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Reads pnl.json rather than fetching positions itself, for the same
    # reason prices.py does -- always runs after pnl.py in the same job,
    # so the two can never disagree about which symbols are "held" today.
    if not config.PNL_DATA_PATH.exists():
        raise SystemExit(
            f"{config.PNL_DATA_PATH} not found -- news_update.py must run after "
            "pnl.py in the same job (it reads pnl.json for the held-symbol "
            "list)."
        )
    snapshot = json.loads(config.PNL_DATA_PATH.read_text())
    run(snapshot)
    logger.info("News digest step complete (ran or no-opped per M23 cadence gate).")
