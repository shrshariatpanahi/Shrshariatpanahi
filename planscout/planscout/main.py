"""PlanScout web app: accounts, carrier connections (with consent), and recommendations.

Run with:  uvicorn planscout.main:create_app --factory --reload
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from pathlib import Path
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .catalog import Catalog
from .config import Settings
from .connectors.base import DEFAULT_SCOPES, SCOPES, CarrierProvider, ProviderError, TokenSet
from .connectors.bill_parser import MAX_BYTES, BillParseError, extract_text, parse_bill_text
from .connectors.oauth2 import OAuth2Provider
from .connectors.sandbox import CALLBACK_PATH, SandboxCarrier, SandboxProvider
from .models import AccountSnapshot, LineOnAccount, RecommendationReport
from .recommender import recommend
from .security import MIN_PASSWORD_LENGTH, TokenCipher, hash_password, new_pkce_pair, verify_password
from .storage import Database

log = logging.getLogger("planscout")
STATIC_DIR = Path(__file__).parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OAUTH_METHODS = {"oauth", "sandbox_oauth"}
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # keeps login timing equal for unknown emails


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class AccountInput(BaseModel):
    """Account details typed in by the customer, or a bill draft they reviewed and confirmed."""

    plan_name: str = Field("", max_length=80)
    plan_id: str | None = None
    monthly_plan_cost: float = Field(ge=0, le=10000)
    autopay: bool = True
    lines: list[LineOnAccount] = Field(min_length=1, max_length=20)
    source: Literal["manual", "bill_upload"] = "manual"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    catalog = Catalog.load(settings.data_dir)
    db = Database(settings.db_path)
    cipher = TokenCipher(settings.encryption_key)
    sandbox = SandboxCarrier.load(catalog, settings.data_dir) if settings.sandbox_enabled else None

    providers: dict[str, CarrierProvider] = {}
    for carrier_id in catalog.carriers:
        if carrier_id in settings.oauth_configs:
            providers[carrier_id] = OAuth2Provider(carrier_id, settings.oauth_configs[carrier_id])
        elif sandbox:
            providers[carrier_id] = SandboxProvider(carrier_id, sandbox)

    app = FastAPI(title="PlanScout", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.catalog, app.state.db, app.state.providers = catalog, db, providers
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="planscout_session",
        max_age=14 * 24 * 3600,
        same_site="lax",
        https_only=settings.https_only_cookies,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # OAuth codes travel in URLs; never leak them to other sites via the Referer header.
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ helpers

    def current_user(request: Request) -> int:
        user_id = request.session.get("user_id")
        if not user_id or not db.user_by_id(user_id):
            request.session.clear()
            raise HTTPException(401, "Please sign in")
        return user_id

    def require_carrier(carrier_id: str) -> None:
        if carrier_id not in catalog.carriers:
            raise HTTPException(404, "Unknown carrier")

    def owned_connection(connection_id: int, user_id: int):
        row = db.connection(connection_id, user_id)
        if not row:
            raise HTTPException(404, "Connection not found")
        return row

    def redirect_uri(request: Request) -> str:
        if settings.base_url:
            return settings.base_url.rstrip("/") + CALLBACK_PATH
        return str(request.url_for("oauth_callback"))

    def normalize(snapshot: AccountSnapshot, carrier_id: str) -> AccountSnapshot:
        """Pin the snapshot to the carrier we connected and link the plan to our catalog if we can."""
        plan = catalog.plan(snapshot.plan_id)
        if plan is None or plan.carrier_id != carrier_id:
            plan = catalog.match_plan(carrier_id, snapshot.plan_name)
        return snapshot.model_copy(update={"carrier_id": carrier_id, "plan_id": plan.id if plan else None})

    def from_input(body: AccountInput, carrier_id: str) -> AccountSnapshot:
        snapshot = AccountSnapshot(carrier_id=carrier_id, **body.model_dump())
        return normalize(snapshot, carrier_id)

    def stored_token(row) -> TokenSet | None:
        if not row["token_ciphertext"]:
            return None
        try:
            return TokenSet.from_dict(cipher.decrypt(row["token_ciphertext"]))
        except InvalidToken:
            log.warning("Stored token for connection %s can't be decrypted (encryption key changed?)", row["id"])
            return None

    def connection_summary(row) -> dict:
        snapshot = db.latest_snapshot(row["id"])
        return {
            "id": row["id"],
            "carrier_id": row["carrier_id"],
            "carrier_name": catalog.carriers[row["carrier_id"]].name if row["carrier_id"] in catalog.carriers else row["carrier_id"],
            "method": row["method"],
            "scopes": json.loads(row["scopes"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "plan_name": snapshot.plan_name if snapshot else None,
            "line_count": len(snapshot.lines) if snapshot else 0,
        }

    # ------------------------------------------------------------------ auth

    @app.post("/api/auth/signup", status_code=201)
    def signup(body: Credentials, request: Request):
        email = body.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise HTTPException(422, "Enter a valid email address")
        if len(body.password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(422, f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        if db.user_by_email(email):
            raise HTTPException(409, "An account with that email already exists")
        user_id = db.create_user(email, hash_password(body.password))
        request.session.clear()
        request.session["user_id"] = user_id
        return {"email": email}

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request):
        email = body.email.strip().lower()
        user = db.user_by_email(email)
        valid = verify_password(body.password, user["password_hash"] if user else _DUMMY_HASH)
        if not (user and valid):
            raise HTTPException(401, "Incorrect email or password")
        request.session.clear()
        request.session["user_id"] = user["id"]
        return {"email": email}

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request):
        request.session.clear()

    @app.get("/api/me")
    def me(user_id: int = Depends(current_user)):
        user = db.user_by_id(user_id)
        return {"email": user["email"], "created_at": user["created_at"]}

    @app.delete("/api/me", status_code=204)
    def delete_me(request: Request, user_id: int = Depends(current_user)):
        for row in db.connections(user_id):
            revoke_at_carrier(row)
        db.delete_user(user_id)
        request.session.clear()

    # ------------------------------------------------------------------ catalog

    @app.get("/api/catalog")
    def get_catalog():
        return {
            "as_of": catalog.as_of,
            "is_sample": catalog.is_sample,
            "currency_symbol": catalog.currency_symbol,
            "scopes": SCOPES,
            "carriers": [
                {
                    "id": c.id,
                    "name": c.name,
                    "type": c.type,
                    "brand_color": c.brand_color,
                    "oauth": c.id in providers,
                    "oauth_is_sandbox": isinstance(providers.get(c.id), SandboxProvider),
                }
                for c in catalog.carriers.values()
            ],
            "plans": [{"id": p.id, "carrier_id": p.carrier_id, "name": p.name} for p in catalog.plans],
        }

    # ------------------------------------------------------------------ connect: sign in with carrier

    @app.post("/api/connect/{carrier_id}/oauth")
    def start_oauth(carrier_id: str, request: Request, user_id: int = Depends(current_user)):
        require_carrier(carrier_id)
        provider = providers.get(carrier_id)
        if provider is None:
            raise HTTPException(400, "Signing in isn't available for this carrier yet; upload a bill instead")
        state = secrets.token_urlsafe(32)
        verifier, challenge = new_pkce_pair()
        db.save_pending(state, user_id, carrier_id, verifier)
        url = provider.authorization_url(
            state=state, code_challenge=challenge, redirect_uri=redirect_uri(request), scopes=DEFAULT_SCOPES
        )
        return {"authorize_url": url}

    @app.get(CALLBACK_PATH, name="oauth_callback", include_in_schema=False)
    def oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        user_id = request.session.get("user_id")
        if not user_id:
            return RedirectResponse("/#/login", status_code=303)
        pending = db.pop_pending(state, user_id) if state else None
        if pending is None:
            return RedirectResponse("/#/connect?error=expired", status_code=303)
        if error or not code:
            return RedirectResponse("/#/connect?error=denied", status_code=303)
        carrier_id = pending["carrier_id"]
        provider = providers.get(carrier_id)
        try:
            if provider is None:
                raise ProviderError(f"No provider configured for {carrier_id}")
            token = provider.exchange_code(
                code=code, code_verifier=pending["code_verifier"], redirect_uri=redirect_uri(request)
            )
            snapshot = normalize(provider.fetch_account(token), carrier_id)
        except ProviderError as exc:
            log.warning("OAuth connection to %s failed: %s", carrier_id, exc)
            return RedirectResponse("/#/connect?error=carrier", status_code=303)
        connection_id = db.create_connection(
            user_id,
            carrier_id,
            provider.data_source,
            token.scopes or DEFAULT_SCOPES,
            snapshot,
            cipher.encrypt(token.to_dict()),
        )
        return RedirectResponse(f"/#/dashboard?connection={connection_id}", status_code=303)

    # ------------------------------------------------------------------ connect: bill upload / manual

    @app.post("/api/connect/{carrier_id}/bill")
    async def parse_bill(carrier_id: str, file: UploadFile = File(...), user_id: int = Depends(current_user)):
        """Parse a bill into a draft. Nothing is stored until the customer confirms the draft."""
        require_carrier(carrier_id)
        data = await file.read(MAX_BYTES + 1)
        try:
            text = extract_text(file.filename or "", data)
        except BillParseError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = parse_bill_text(text, carrier_id, catalog)
        return {"draft": result.draft, "warnings": result.warnings, "found": result.found}

    @app.post("/api/connect/{carrier_id}/account", status_code=201)
    def save_account(carrier_id: str, body: AccountInput, user_id: int = Depends(current_user)):
        require_carrier(carrier_id)
        snapshot = from_input(body, carrier_id)
        connection_id = db.create_connection(user_id, carrier_id, body.source, [], snapshot)
        return {"id": connection_id}

    # ------------------------------------------------------------------ connections

    @app.get("/api/connections")
    def list_connections(user_id: int = Depends(current_user)):
        return [connection_summary(row) for row in db.connections(user_id)]

    @app.get("/api/connections/{connection_id}")
    def get_connection(connection_id: int, user_id: int = Depends(current_user)):
        row = owned_connection(connection_id, user_id)
        return {**connection_summary(row), "account": db.latest_snapshot(connection_id)}

    @app.put("/api/connections/{connection_id}/account")
    def update_account(connection_id: int, body: AccountInput, user_id: int = Depends(current_user)):
        row = owned_connection(connection_id, user_id)
        if row["method"] in OAUTH_METHODS:
            raise HTTPException(400, "This account syncs from your carrier; use refresh instead")
        db.add_snapshot(connection_id, from_input(body, row["carrier_id"]), None)
        return connection_summary(db.connection(connection_id, user_id))

    @app.post("/api/connections/{connection_id}/refresh")
    def refresh_connection(connection_id: int, user_id: int = Depends(current_user)):
        row = owned_connection(connection_id, user_id)
        if row["method"] not in OAUTH_METHODS:
            raise HTTPException(400, "Upload a newer bill or edit your details to update this account")
        token, provider = stored_token(row), providers.get(row["carrier_id"])
        if token is None or provider is None:
            raise HTTPException(409, "Access to this carrier account has lapsed; please reconnect")
        try:
            if token.expired:
                token = provider.refresh(token)
            snapshot = normalize(provider.fetch_account(token), row["carrier_id"])
        except ProviderError as exc:
            log.info("Refresh failed for connection %s: %s", connection_id, exc)
            raise HTTPException(409, "Access to this carrier account has lapsed; please reconnect") from exc
        db.add_snapshot(connection_id, snapshot, cipher.encrypt(token.to_dict()))
        return connection_summary(db.connection(connection_id, user_id))

    def revoke_at_carrier(row) -> None:
        if row["method"] not in OAUTH_METHODS:
            return
        token, provider = stored_token(row), providers.get(row["carrier_id"])
        if token and provider:
            try:
                provider.revoke(token)
            except ProviderError as exc:
                log.warning("Couldn't revoke token at %s: %s", row["carrier_id"], exc)

    @app.delete("/api/connections/{connection_id}", status_code=204)
    def delete_connection(connection_id: int, user_id: int = Depends(current_user)):
        """Revoke access: tells the carrier to kill the token, then deletes the token and all data."""
        row = owned_connection(connection_id, user_id)
        revoke_at_carrier(row)
        db.delete_connection(connection_id, user_id)

    @app.get("/api/connections/{connection_id}/recommendations", response_model=RecommendationReport)
    def get_recommendations(
        connection_id: int,
        horizon: int = Query(24, ge=6, le=36),
        user_id: int = Depends(current_user),
    ):
        owned_connection(connection_id, user_id)
        snapshot = db.latest_snapshot(connection_id)
        if snapshot is None:
            raise HTTPException(404, "No account data for this connection")
        return recommend(snapshot, catalog, horizon_months=horizon)

    @app.get("/api/consents")
    def consent_history(user_id: int = Depends(current_user)):
        return [
            {
                "carrier_id": row["carrier_id"],
                "method": row["method"],
                "event": row["event"],
                "scopes": json.loads(row["scopes"]),
                "at": row["created_at"],
            }
            for row in db.consent_events(user_id)
        ]

    # ------------------------------------------------------------------ pages

    if sandbox:
        app.include_router(sandbox.router())

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app
