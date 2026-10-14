from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from planscout.connectors.base import ProviderError, TokenSet
from planscout.connectors.oauth2 import OAuth2Config, OAuth2Provider, load_oauth_configs

CONFIG = OAuth2Config(
    client_id="cid",
    client_secret="secret",
    authorize_url="https://partner.example/authorize",
    token_url="https://partner.example/token",
    account_url="https://partner.example/account",
    revoke_url="https://partner.example/revoke",
)


def make_provider(handler):
    return OAuth2Provider("nova", CONFIG, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_authorization_url_uses_pkce():
    url = make_provider(lambda r: httpx.Response(500)).authorization_url(
        state="s", code_challenge="c", redirect_uri="https://app/cb", scopes=["plan:read", "usage:read"]
    )
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["c"]
    assert query["scope"] == ["plan:read usage:read"]
    assert query["response_type"] == ["code"]


def test_exchange_fetch_and_revoke():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/token":
            body = parse_qs(request.content.decode())
            assert body["code_verifier"] == ["verifier"]
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "plan:read"})
        if request.url.path == "/account":
            assert request.headers["Authorization"] == "Bearer at"
            return httpx.Response(
                200,
                json={
                    "carrier_id": "spoofed",
                    "plan_name": "Unlimited",
                    "monthly_plan_cost": 50,
                    "lines": [{"label": "•••1234", "avg_data_gb": 4}],
                },
            )
        if request.url.path == "/revoke":
            return httpx.Response(200)
        return httpx.Response(404)

    provider = make_provider(handler)
    token = provider.exchange_code(code="code", code_verifier="verifier", redirect_uri="https://app/cb")
    assert token.access_token == "at" and token.refresh_token == "rt" and not token.expired
    snapshot = provider.fetch_account(token)
    assert snapshot.carrier_id == "nova"  # the partner can't claim to be another carrier
    assert snapshot.source == "oauth"
    provider.revoke(token)
    assert [r.url.path for r in seen] == ["/token", "/account", "/revoke"]


def test_errors_become_provider_errors():
    provider = make_provider(lambda r: httpx.Response(401, json={"error": "invalid_grant"}))
    with pytest.raises(ProviderError):
        provider.exchange_code(code="bad", code_verifier="v", redirect_uri="https://app/cb")
    with pytest.raises(ProviderError):
        provider.fetch_account(TokenSet(access_token="x"))
    with pytest.raises(ProviderError):
        provider.refresh(TokenSet(access_token="x"))  # no refresh token


def test_load_oauth_configs_from_env():
    env = {
        "PLANSCOUT_OAUTH_NOVA_CLIENT_ID": "id",
        "PLANSCOUT_OAUTH_NOVA_CLIENT_SECRET": "secret",
        "PLANSCOUT_OAUTH_NOVA_AUTHORIZE_URL": "https://a",
        "PLANSCOUT_OAUTH_NOVA_TOKEN_URL": "https://t",
        "PLANSCOUT_OAUTH_NOVA_ACCOUNT_URL": "https://acc",
    }
    configs = load_oauth_configs(env)
    assert configs["nova"].client_id == "id" and configs["nova"].revoke_url is None
    dashed = load_oauth_configs({k.replace("_NOVA_", "_VIRGIN_PLUS_"): v for k, v in env.items()})
    assert list(dashed) == ["virgin-plus"]
    del env["PLANSCOUT_OAUTH_NOVA_TOKEN_URL"]
    with pytest.raises(ValueError, match="TOKEN_URL"):
        load_oauth_configs(env)
