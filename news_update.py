"""Daily Claude-written news/performance digest (user request, M22).

Reads pnl.py's freshly-written pnl.json for the held-symbol list (same
pattern prices.py already uses, for the same reason: the two modules can
never disagree about which symbols are "currently held" on a given run),
pulls each symbol's last-24h news from Alpaca, and asks Claude to write a
short, Buffett-disciplined digest -- does today's news change anything
about a held business's moat, management, or financial health, or is it
just price noise -- which is then posted to Discord.

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
You are writing a brief daily update for a long-term value investor who \
holds the positions described below. Apply Warren Buffett's discipline: \
judge whether today's news changes anything about a held business's \
durable competitive moat, management execution, or financial health -- \
not whether the price moved. Treat a price swing on its own as "Mr. \
Market" being emotional, never a signal to act on, unless the news \
reveals an actual change in business fundamentals.

For each position, in 1-3 sentences: say what happened (if anything \
notable) and, only if relevant, whether it plausibly affects the \
long-term investment thesis. If there is no notable news, say so briefly \
-- do not invent commentary to fill space. Do not add generic market \
commentary unrelated to a specific held company.

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
    once-daily, low-volume feed. include_content=False keeps the fetch to
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
    lines = [f"Date: {today} ({mode} account)", ""]
    for position in positions:
        symbol = str(position["symbol"])
        plpc = position.get("unrealized_plpc")
        price = position.get("current_price")
        plpc_str = f"{plpc:+.1%}" if isinstance(plpc, int | float) else "unknown"
        price_str = f"${price:,.2f}" if isinstance(price, int | float) else "unknown"
        lines.append(f"## {symbol} (unrealized P&L {plpc_str}, price {price_str})")
        articles = news_by_symbol.get(symbol) or []
        if not articles:
            lines.append("No news in the last day.")
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
        raise ValueError("Claude declined to generate the daily digest (stop_reason=refusal)")
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
    messages -- a once-daily digest running slightly long is an acceptable
    loss of the tail, not worth the added complexity of a multi-message
    send for this feature.
    """
    if len(message) > _DISCORD_MESSAGE_CHAR_LIMIT:
        message = message[: _DISCORD_MESSAGE_CHAR_LIMIT - 1] + "…"
    body = json.dumps({"content": message}).encode("utf-8")
    request = urllib.request.Request(
        config.DISCORD_NEWS_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def run(pnl_snapshot: dict[str, object]) -> None:
    """Generates and posts the daily digest for one pnl.py-shaped snapshot.

    No-op if either config.DISCORD_NEWS_WEBHOOK_URL or
    config.ANTHROPIC_API_KEY is unset (config-gated, same shape as
    pnl.py's check_position_loss_alerts), or if there are no held
    positions (nothing to summarize).

    Soft-fails on any error from the news fetch, the Anthropic call, or
    the Discord post -- logged as a warning, never raised. See this
    module's own docstring for why: this is a bonus display feature, and
    must never fail the trading job it runs alongside.
    """
    if not config.DISCORD_NEWS_WEBHOOK_URL or not config.ANTHROPIC_API_KEY:
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
            f"\N{NEWSPAPER} **{mode.upper()} daily update** "
            "_(AI-generated summary, not reviewed or investment advice)_\n\n"
        )
        _post_discord_message(header + digest)
    except (
        AlpacaAPIError,
        anthropic.APIError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        AssertionError,
    ) as exc:
        logger.warning("Daily news digest failed; skipping this run: %s", exc)


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
    logger.info("Daily news digest step complete.")
