from __future__ import annotations

import pytest

from app.market.alpaca_provider import AlpacaMarketDataProvider, _parse_timeframe
from app.market.provider import AuthenticationError


def test_missing_credentials_raise_authentication_error(monkeypatch):
    import app.market.alpaca_provider as alpaca_provider_module

    # A real local .env (e.g. one the developer added with live keys) would
    # otherwise repopulate these vars via load_dotenv() after we clear them -
    # stub it out so this test is deterministic regardless of what's on disk.
    monkeypatch.setattr(alpaca_provider_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(AuthenticationError):
        AlpacaMarketDataProvider(api_key=None, secret_key=None)


def test_explicit_credentials_bypass_env_lookup(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    provider = AlpacaMarketDataProvider(api_key="fake-key", secret_key="fake-secret")

    assert provider is not None


@pytest.mark.parametrize(
    "timeframe,expected",
    [
        ("1Day", "1Day"),
        ("1Hour", "1Hour"),
        ("5Min", "5Min"),
        ("1Week", "1Week"),
    ],
)
def test_parse_timeframe_accepts_supported_strings(timeframe, expected):
    assert str(_parse_timeframe(timeframe)) == expected


def test_parse_timeframe_rejects_unknown_unit():
    with pytest.raises(ValueError):
        _parse_timeframe("1Fortnight")
