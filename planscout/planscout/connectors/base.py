"""The interface every "Sign in with your carrier" integration implements."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from ..models import AccountSnapshot, DataSource

# Read-only scopes we ask for. Deliberately absent: making changes, payment details,
# call/text logs, location, and anything that could be used to port a number out.
SCOPES: dict[str, str] = {
    "plan:read": "Your plan name and what you pay for it each month",
    "usage:read": "Average data and hotspot use per line (not who you call or text)",
    "devices:read": "Phones on your account and any remaining device payments",
}
DEFAULT_SCOPES = list(SCOPES)


class ProviderError(Exception):
    """A carrier integration failed (network error, rejected code, expired token...)."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TokenSet:
        expires = data.get("expires_at")
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(expires) if expires else None,
            scopes=list(data.get("scopes") or []),
        )


class CarrierProvider(Protocol):
    carrier_id: str
    data_source: DataSource

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str, scopes: list[str]) -> str: ...

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet: ...

    def refresh(self, token: TokenSet) -> TokenSet: ...

    def fetch_account(self, token: TokenSet) -> AccountSnapshot: ...

    def revoke(self, token: TokenSet) -> None: ...
