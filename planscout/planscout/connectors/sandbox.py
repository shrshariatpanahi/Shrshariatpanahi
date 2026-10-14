"""A pretend carrier OAuth server + account API, so the consent flow can be demoed end to end.

In production the authorize page below is hosted by the carrier (or data partner) on their own
domain: the customer signs in there, PlanScout never sees their password, and it receives only a
revocable, read-only token for the scopes the customer approved.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..catalog import DATA_DIR, Catalog
from ..models import AccountSnapshot, DeviceOnAccount, LineOnAccount
from ..security import pkce_challenge
from .base import SCOPES, ProviderError, TokenSet

CLIENT_ID = "planscout-sandbox"
CALLBACK_PATH = "/api/connect/oauth/callback"
CODE_TTL = timedelta(minutes=5)
TOKEN_TTL = timedelta(hours=1)


@dataclass
class _Grant:
    carrier_id: str
    scopes: list[str]
    redirect_uri: str
    expires_at: datetime
    persona_id: str = ""
    state: str = ""
    code_challenge: str = ""
    refresh_token: str = field(default="")

    @property
    def expired(self) -> bool:
        return self.expires_at <= datetime.now(timezone.utc)


class SandboxCarrier:
    def __init__(self, catalog: Catalog, personas: list[dict]):
        self.catalog = catalog
        self.personas = {p["id"]: p for p in personas}
        self._requests: dict[str, _Grant] = {}
        self._codes: dict[str, _Grant] = {}
        self._access: dict[str, _Grant] = {}
        self._refresh: dict[str, _Grant] = {}
        self._lock = threading.Lock()

    @classmethod
    def load(cls, catalog: Catalog, data_dir: Path = DATA_DIR) -> SandboxCarrier:
        return cls(catalog, json.loads((data_dir / "sandbox_accounts.json").read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ "carrier API"

    def snapshot_for(self, carrier_id: str, persona_id: str) -> AccountSnapshot:
        persona = self.personas[persona_id]
        carrier = self.catalog.carriers[carrier_id]
        plans = [p for p in self.catalog.plans_for(carrier_id) if p.serves(persona.get("province"))]
        plans = plans or self.catalog.plans_for(carrier_id)
        plan = next((p for p in plans if p.tier == persona["tier"]), plans[0])
        lines = []
        for raw in persona["lines"]:
            line = LineOnAccount.model_validate(raw)
            if line.device and not carrier.offers_device_financing:
                line.device = DeviceOnAccount(model=line.device.model, unlocked=True)
            lines.append(line)
        province = persona.get("province")
        if not carrier.serves(province):
            province = carrier.provinces[0]  # e.g. a regional carrier: put the demo customer where it operates
        return AccountSnapshot(
            carrier_id=carrier_id,
            plan_name=plan.name,
            plan_id=plan.id,
            monthly_plan_cost=plan.monthly_cost(len(lines), autopay=persona["autopay"]),
            autopay=persona["autopay"],
            province=province,
            lines=lines,
            source="sandbox_oauth",
        )

    def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        with self._lock:
            grant = self._codes.pop(code, None)  # codes are single use
        if grant is None or grant.expired:
            raise ProviderError("Authorization code is invalid or expired")
        if grant.redirect_uri != redirect_uri:
            raise ProviderError("redirect_uri does not match the authorization request")
        if pkce_challenge(code_verifier) != grant.code_challenge:
            raise ProviderError("PKCE verification failed")
        return self._issue_tokens(grant)

    def refresh(self, refresh_token: str) -> TokenSet:
        with self._lock:
            grant = self._refresh.pop(refresh_token, None)
        if grant is None:
            raise ProviderError("Refresh token is invalid or revoked; please reconnect")
        return self._issue_tokens(grant)

    def account(self, access_token: str) -> AccountSnapshot:
        grant = self._access.get(access_token)
        if grant is None or grant.expired:
            raise ProviderError("Access token is invalid or expired")
        return self.snapshot_for(grant.carrier_id, grant.persona_id)

    def revoke(self, token: TokenSet) -> None:
        with self._lock:
            grant = self._access.get(token.access_token) or self._refresh.get(token.refresh_token or "")
            if grant is None:
                return
            self._refresh.pop(grant.refresh_token, None)
            for key in [k for k, g in self._access.items() if g.refresh_token == grant.refresh_token]:
                del self._access[key]

    def _issue_tokens(self, grant: _Grant) -> TokenSet:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + TOKEN_TTL
        issued = _Grant(
            carrier_id=grant.carrier_id,
            scopes=grant.scopes,
            redirect_uri=grant.redirect_uri,
            expires_at=expires,
            persona_id=grant.persona_id,
            refresh_token=refresh,
        )
        with self._lock:
            self._access[access] = issued
            self._refresh[refresh] = issued
        return TokenSet(access_token=access, refresh_token=refresh, expires_at=expires, scopes=grant.scopes)

    # ------------------------------------------------------------------ "carrier sign-in page"

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/sandbox", include_in_schema=False)

        @router.get("/oauth/authorize", response_class=HTMLResponse)
        def authorize_page(
            request: Request,
            client_id: str = "",
            carrier: str = "",
            redirect_uri: str = "",
            state: str = "",
            scope: str = "",
            code_challenge: str = "",
            code_challenge_method: str = "",
        ):
            target = urlparse(redirect_uri)
            if (
                client_id != CLIENT_ID
                or carrier not in self.catalog.carriers
                or target.path != CALLBACK_PATH
                or target.netloc != request.url.netloc
                or code_challenge_method != "S256"
                or not code_challenge
                or not state
            ):
                return HTMLResponse(_page("Invalid request", "<p>This authorization request is invalid.</p>"), 400)
            scopes = [s for s in scope.split() if s in SCOPES]
            request_id = secrets.token_urlsafe(16)
            with self._lock:
                self._requests[request_id] = _Grant(
                    carrier_id=carrier,
                    scopes=scopes,
                    redirect_uri=redirect_uri,
                    expires_at=datetime.now(timezone.utc) + CODE_TTL,
                    state=state,
                    code_challenge=code_challenge,
                )
            return HTMLResponse(self._consent_html(carrier, scopes, request_id))

        @router.post("/oauth/authorize")
        def authorize_decision(request_id: str = Form(...), decision: str = Form(...), persona: str = Form("")):
            with self._lock:
                pending = self._requests.pop(request_id, None)
            if pending is None or pending.expired:
                return HTMLResponse(_page("Expired", "<p>This sign-in request expired. Please start again.</p>"), 400)
            if decision != "allow" or persona not in self.personas:
                query = urlencode({"error": "access_denied", "state": pending.state})
                return RedirectResponse(f"{pending.redirect_uri}?{query}", status_code=303)
            code = secrets.token_urlsafe(24)
            pending.persona_id = persona
            pending.expires_at = datetime.now(timezone.utc) + CODE_TTL
            with self._lock:
                self._codes[code] = pending
            return RedirectResponse(
                f"{pending.redirect_uri}?{urlencode({'code': code, 'state': pending.state})}", status_code=303
            )

        return router

    def _consent_html(self, carrier_id: str, scopes: list[str], request_id: str) -> str:
        carrier = self.catalog.carriers[carrier_id]
        esc = html.escape
        persona_inputs = "".join(
            f'<label class="persona"><input type="radio" name="persona" value="{esc(pid)}"'
            f'{" checked" if i == 0 else ""}> {esc(p["name"])}</label>'
            for i, (pid, p) in enumerate(self.personas.items())
        )
        scope_items = "".join(f"<li>{esc(SCOPES[s])}</li>" for s in scopes)
        body = f"""
        <div class="banner">DEMO ONLY. This is not a real {esc(carrier.name)} page and doesn't connect to
        {esc(carrier.name)}. With a real integration, customers sign in on the carrier's own website.</div>
        <h1>Demo carrier sign-in</h1>
        <p class="muted">Simulating: {esc(carrier.name)}</p>
        <form method="post" action="/sandbox/oauth/authorize">
          <input type="hidden" name="request_id" value="{esc(request_id)}">
          <fieldset><legend>Sign in as a demo customer</legend>{persona_inputs}</fieldset>
          <h2>PlanScout wants read-only access to:</h2>
          <ul>{scope_items}</ul>
          <p class="muted">PlanScout can't make changes, see payment details, or read who you call or text.
          You can revoke access at any time.</p>
          <div class="actions">
            <button name="decision" value="deny" class="secondary">Deny</button>
            <button name="decision" value="allow">Allow</button>
          </div>
        </form>"""
        return _page("Demo carrier sign-in", body)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1c1e21;margin:0;padding:24px 16px}}
main{{max-width:440px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 4px #0002}}
.banner{{background:#fff4d6;border:1px solid #f0c36d;border-radius:8px;padding:10px;font-size:14px}}
fieldset{{border:1px solid #ddd;border-radius:8px;margin:16px 0}} .persona{{display:block;padding:6px 0}}
h2{{font-size:16px}} .muted{{color:#666;font-size:14px}} .actions{{display:flex;gap:12px;justify-content:flex-end}}
button{{border:0;border-radius:8px;padding:10px 20px;font-size:15px;color:#fff;background:#1b6ef3;cursor:pointer}}
button.secondary{{background:#e4e6eb;color:#1c1e21}}
</style></head><body><main>{body}</main></body></html>"""


class SandboxProvider:
    data_source = "sandbox_oauth"

    def __init__(self, carrier_id: str, server: SandboxCarrier):
        self.carrier_id = carrier_id
        self.server = server

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str, scopes: list[str]) -> str:
        return "/sandbox/oauth/authorize?" + urlencode(
            {
                "client_id": CLIENT_ID,
                "carrier": self.carrier_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": " ".join(scopes),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        return self.server.exchange_code(code, code_verifier, redirect_uri)

    def refresh(self, token: TokenSet) -> TokenSet:
        if not token.refresh_token:
            raise ProviderError("No refresh token; please reconnect")
        return self.server.refresh(token.refresh_token)

    def fetch_account(self, token: TokenSet) -> AccountSnapshot:
        return self.server.account(token.access_token)

    def revoke(self, token: TokenSet) -> None:
        self.server.revoke(token)
