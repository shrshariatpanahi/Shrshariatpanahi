"""Generic OAuth 2.0 (authorization code + PKCE) client for carriers or data partners.

No major carrier publishes a self-serve API for customer account data today; access comes
through partner agreements or a licensed data aggregator. When you have one, configure it with
environment variables (see README) and, if their account payload differs from
`AccountSnapshot`, override `map_account`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from ..models import AccountSnapshot
from .base import ProviderError, TokenSet

ENV_PREFIX = "PLANSCOUT_OAUTH_"
_REQUIRED = ("CLIENT_ID", "CLIENT_SECRET", "AUTHORIZE_URL", "TOKEN_URL", "ACCOUNT_URL")


@dataclass
class OAuth2Config:
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    account_url: str
    revoke_url: str | None = None


def load_oauth_configs(env) -> dict[str, OAuth2Config]:
    """Read PLANSCOUT_OAUTH_<CARRIER>_<FIELD> variables into one config per carrier id.

    Carrier ids use dashes, env vars use underscores: PLANSCOUT_OAUTH_VIRGIN_PLUS_* configures "virgin-plus".
    """
    carriers = {
        key[len(ENV_PREFIX) : -len("_CLIENT_ID")]
        for key in env
        if key.startswith(ENV_PREFIX) and key.endswith("_CLIENT_ID")
    }
    configs = {}
    for carrier in carriers:
        prefix = f"{ENV_PREFIX}{carrier}_"
        missing = [f for f in _REQUIRED if not env.get(prefix + f)]
        if missing:
            raise ValueError(f"OAuth config for {carrier.lower()} is missing: {', '.join(prefix + f for f in missing)}")
        configs[carrier.lower().replace("_", "-")] = OAuth2Config(
            client_id=env[prefix + "CLIENT_ID"],
            client_secret=env[prefix + "CLIENT_SECRET"],
            authorize_url=env[prefix + "AUTHORIZE_URL"],
            token_url=env[prefix + "TOKEN_URL"],
            account_url=env[prefix + "ACCOUNT_URL"],
            revoke_url=env.get(prefix + "REVOKE_URL") or None,
        )
    return configs


class OAuth2Provider:
    data_source = "oauth"

    def __init__(self, carrier_id: str, config: OAuth2Config, http: httpx.Client | None = None):
        self.carrier_id = carrier_id
        self.config = config
        self.http = http or httpx.Client(timeout=10)

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str, scopes: list[str]) -> str:
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in self.config.authorize_url else "?"
        return f"{self.config.authorize_url}{separator}{params}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    def refresh(self, token: TokenSet) -> TokenSet:
        if not token.refresh_token:
            raise ProviderError("Access expired and no refresh token is available; please reconnect")
        refreshed = self._token_request({"grant_type": "refresh_token", "refresh_token": token.refresh_token})
        refreshed.refresh_token = refreshed.refresh_token or token.refresh_token
        return refreshed

    def fetch_account(self, token: TokenSet) -> AccountSnapshot:
        try:
            response = self.http.get(
                self.config.account_url, headers={"Authorization": f"Bearer {token.access_token}"}
            )
            response.raise_for_status()
            return self.map_account(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Could not read account from {self.carrier_id}: {exc}") from exc

    def map_account(self, payload: dict) -> AccountSnapshot:
        """Translate the partner's account payload into our model. Override per partner."""
        return AccountSnapshot.model_validate({**payload, "carrier_id": self.carrier_id, "source": "oauth"})

    def revoke(self, token: TokenSet) -> None:
        if not self.config.revoke_url:
            return
        try:
            self.http.post(
                self.config.revoke_url,
                data={"token": token.refresh_token or token.access_token},
                auth=(self.config.client_id, self.config.client_secret),
            ).raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not revoke access at {self.carrier_id}: {exc}") from exc

    def _token_request(self, data: dict) -> TokenSet:
        try:
            response = self.http.post(
                self.config.token_url, data=data, auth=(self.config.client_id, self.config.client_secret)
            )
            response.raise_for_status()
            body = response.json()
            expires_in = body.get("expires_in")
            return TokenSet(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token"),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)) if expires_in else None,
                scopes=body.get("scope", "").split(),
            )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderError(f"Token request to {self.carrier_id} failed: {exc}") from exc
