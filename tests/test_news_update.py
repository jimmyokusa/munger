"""Unit tests for news_update.py, mocking the Alpaca and Anthropic SDKs
(same pattern as test_prices.py/test_pnl.py -- no live credentials
available in this environment)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import MagicMock

import anthropic
import pytest
from alpaca.common.exceptions import APIError as AlpacaAPIError
from alpaca.data.models.news import NewsSet

import config
import news_update


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DISCORD_NEWS_WEBHOOK_URL", "https://discord.example/news")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(config, "NEWS_PER_SYMBOL_LIMIT", 5)
    monkeypatch.setattr(config, "NEWS_LOOKBACK_HOURS", 24)


def _fake_article(headline: str, summary: str, source: str, symbols: list[str]) -> MagicMock:
    article = MagicMock()
    article.headline = headline
    article.summary = summary
    article.source = source
    article.symbols = symbols
    return article


def _fake_news_client(articles: list[MagicMock]) -> MagicMock:
    client = MagicMock()
    news_set = MagicMock(spec=NewsSet)
    news_set.data = {"news": articles}
    client.get_news.return_value = news_set
    return client


def _position(symbol: str, plpc: float = 0.05, price: float = 100.0) -> dict[str, object]:
    return {"symbol": symbol, "unrealized_plpc": plpc, "current_price": price}


# --- _held_symbol_positions ---


def test_held_symbol_positions_sorts_and_skips_malformed() -> None:
    snapshot: dict[str, object] = {
        "positions": [
            {"symbol": "MSFT"},
            {"symbol": "AAPL"},
            {"qty": 5.0},  # missing symbol
            {"symbol": None},
            {"symbol": ""},
        ]
    }

    result = news_update._held_symbol_positions(snapshot)

    assert [p["symbol"] for p in result] == ["AAPL", "MSFT"]


def test_held_symbol_positions_handles_no_positions() -> None:
    assert news_update._held_symbol_positions({}) == []
    assert news_update._held_symbol_positions({"positions": []}) == []


def test_held_symbol_positions_dedupes_keeping_first_occurrence() -> None:
    snapshot: dict[str, object] = {
        "positions": [
            {"symbol": "AAPL", "current_price": 100.0},
            {"symbol": "AAPL", "current_price": 999.0},
        ]
    }

    result = news_update._held_symbol_positions(snapshot)

    assert len(result) == 1
    assert result[0]["current_price"] == 100.0


# --- fetch_recent_news ---


def test_fetch_recent_news_with_no_symbols_returns_empty_without_calling_alpaca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*args: Any, **kwargs: Any) -> MagicMock:
        pytest.fail("NewsClient constructed with no symbols to fetch")

    monkeypatch.setattr(news_update, "NewsClient", _fail)

    assert news_update.fetch_recent_news([]) == {}


def test_fetch_recent_news_groups_articles_by_held_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    articles = [
        _fake_article("AAPL up", "summary1", "benzinga", ["AAPL"]),
        _fake_article("Multi", "summary2", "benzinga", ["AAPL", "MSFT"]),
        _fake_article("Unrelated", "summary3", "benzinga", ["TSLA"]),
    ]
    client_mock = _fake_news_client(articles)
    monkeypatch.setattr(news_update, "NewsClient", lambda *args, **kwargs: client_mock)

    result = news_update.fetch_recent_news(["AAPL", "MSFT"])

    assert [a["headline"] for a in result["AAPL"]] == ["AAPL up", "Multi"]
    assert [a["headline"] for a in result["MSFT"]] == ["Multi"]
    # A symbol with no matching articles still gets an (empty) entry, and a
    # symbol not in the requested list (TSLA) is never added.
    assert "TSLA" not in result


def test_fetch_recent_news_respects_per_symbol_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "NEWS_PER_SYMBOL_LIMIT", 2)
    articles = [_fake_article(f"headline{i}", "s", "benzinga", ["AAPL"]) for i in range(5)]
    client_mock = _fake_news_client(articles)
    monkeypatch.setattr(news_update, "NewsClient", lambda *args, **kwargs: client_mock)

    result = news_update.fetch_recent_news(["AAPL"])

    assert len(result["AAPL"]) == 2


def test_fetch_recent_news_rejects_an_unexpected_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_mock = MagicMock()
    client_mock.get_news.return_value = {"news": []}  # not a NewsSet instance
    monkeypatch.setattr(news_update, "NewsClient", lambda *args, **kwargs: client_mock)

    with pytest.raises(ValueError, match="unexpected get_news"):
        news_update.fetch_recent_news(["AAPL"])


def test_fetch_recent_news_requests_headlines_only_no_full_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_mock = _fake_news_client([])
    monkeypatch.setattr(news_update, "NewsClient", lambda *args, **kwargs: client_mock)

    news_update.fetch_recent_news(["AAPL"])

    request = client_mock.get_news.call_args.args[0]
    assert request.include_content is False
    assert request.exclude_contentless is True
    assert request.symbols == "AAPL"


def test_fetch_recent_news_scales_the_request_limit_with_symbol_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fixed small limit on the combined request can let one or two
    # unusually newsy held symbols crowd out the whole recency-sorted
    # feed, starving other held symbols of coverage they should have
    # gotten -- the request's own limit must grow with how many articles
    # could actually be used across all held symbols.
    monkeypatch.setattr(config, "NEWS_PER_SYMBOL_LIMIT", 5)
    client_mock = _fake_news_client([])
    monkeypatch.setattr(news_update, "NewsClient", lambda *args, **kwargs: client_mock)

    symbols = [f"SYM{i}" for i in range(15)]  # matches config.TARGET_POSITION_COUNT scale
    news_update.fetch_recent_news(symbols)

    request = client_mock.get_news.call_args.args[0]
    assert request.limit >= len(symbols) * config.NEWS_PER_SYMBOL_LIMIT


# --- generate_digest ---


def _fake_anthropic_client(text: str, stop_reason: str = "end_turn") -> MagicMock:
    client = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text
    response = MagicMock()
    response.content = [text_block]
    response.stop_reason = stop_reason
    client.messages.create.return_value = response
    return client


def test_generate_digest_returns_stripped_text(monkeypatch: pytest.MonkeyPatch) -> None:
    client_mock = _fake_anthropic_client("  Digest text.  ")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: client_mock)

    result = news_update.generate_digest("paper", [_position("AAPL")], {"AAPL": []})

    assert result == "Digest text."
    call_kwargs = client_mock.messages.create.call_args.kwargs
    assert call_kwargs["model"] == config.NEWS_UPDATE_MODEL
    assert call_kwargs["max_tokens"] == config.NEWS_MAX_OUTPUT_TOKENS


def test_generate_digest_raises_on_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    client_mock = _fake_anthropic_client("", stop_reason="refusal")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: client_mock)

    with pytest.raises(ValueError, match="refusal"):
        news_update.generate_digest("paper", [_position("AAPL")], {"AAPL": []})


def test_generate_digest_notes_truncation_on_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    client_mock = _fake_anthropic_client("Cut off mid-sen", stop_reason="max_tokens")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: client_mock)

    result = news_update.generate_digest("paper", [_position("AAPL")], {"AAPL": []})

    assert result.startswith("Cut off mid-sen")
    assert "truncated" in result


def test_format_user_message_includes_symbol_and_no_news_note() -> None:
    message = news_update._format_user_message(
        "paper", [_position("AAPL", plpc=0.1234, price=150.5)], {"AAPL": []}
    )

    assert "AAPL" in message
    assert "+12.3%" in message
    assert "No news in the last day." in message


# --- _post_discord_message ---


def test_post_discord_message_sends_content(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    news_update._post_discord_message("hello")

    assert captured["url"] == "https://discord.example/news"
    assert captured["body"] == {"content": "hello"}


def test_post_discord_message_truncates_long_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["body"] = json.loads(request.data)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    news_update._post_discord_message("x" * 5000)

    assert len(captured["body"]["content"]) == news_update._DISCORD_MESSAGE_CHAR_LIMIT


# --- run() ---


def test_run_noop_when_webhook_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DISCORD_NEWS_WEBHOOK_URL", "")
    monkeypatch.setattr(
        news_update, "fetch_recent_news", lambda symbols: pytest.fail("should not be called")
    )

    news_update.run({"mode": "paper", "positions": [_position("AAPL")]})  # must not raise


def test_run_noop_when_api_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(
        news_update, "fetch_recent_news", lambda symbols: pytest.fail("should not be called")
    )

    news_update.run({"mode": "paper", "positions": [_position("AAPL")]})  # must not raise


def test_run_noop_when_no_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []
    monkeypatch.setattr(news_update, "_post_discord_message", posted.append)

    news_update.run({"mode": "paper", "positions": []})

    assert posted == []


def test_run_posts_digest_with_mode_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(news_update, "fetch_recent_news", lambda symbols: {"AAPL": []})
    monkeypatch.setattr(
        news_update, "generate_digest", lambda mode, positions, news_by_symbol: "The digest."
    )
    posted: list[str] = []
    monkeypatch.setattr(news_update, "_post_discord_message", posted.append)

    news_update.run({"mode": "live", "positions": [_position("AAPL")]})

    assert len(posted) == 1
    assert "LIVE" in posted[0]
    assert "The digest." in posted[0]


@pytest.mark.parametrize(
    "exc",
    [
        AlpacaAPIError("simulated"),  # type: ignore[no-untyped-call]
        anthropic.APIConnectionError(request=MagicMock()),
        urllib.error.URLError("simulated"),
        TimeoutError("simulated"),
        ValueError("refusal"),
    ],
)
def test_run_swallows_errors_from_any_stage(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(news_update, "fetch_recent_news", _raise)

    news_update.run({"mode": "paper", "positions": [_position("AAPL")]})  # must not raise


def test_run_swallows_a_malformed_positions_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # pnl.json is written by a separate process/step -- a schema surprise
    # there must not crash this step either, same as a broken Alpaca/
    # Anthropic/Discord call. _held_symbol_positions asserts on this, and
    # that assert must be reachable from run()'s try block, not called
    # ahead of it.
    news_update.run({"mode": "paper", "positions": "not-a-list"})  # must not raise
