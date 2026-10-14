import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from planscout.connectors.base import DEFAULT_SCOPES

SAMPLE_BILL = (Path(__file__).parent / "fixtures" / "sample_bill.txt").read_bytes()

MANUAL_ACCOUNT = {
    "plan_name": "Unlimited Elite",
    "monthly_plan_cost": 85,
    "autopay": True,
    "lines": [{"label": "Me", "avg_data_gb": 3, "hotspot_gb": 0, "device": None}],
}


def sandbox_sign_in(client, carrier="summit", persona="family", decision="allow"):
    """Walk the full consent flow: our app -> sandbox carrier sign-in page -> back to our callback."""
    start = client.post(f"/api/connect/{carrier}/oauth")
    assert start.status_code == 200
    page = client.get(start.json()["authorize_url"])
    assert page.status_code == 200
    assert "read-only access" in page.text
    request_id = re.search(r'name="request_id" value="([^"]+)"', page.text).group(1)
    to_callback = client.post(
        "/sandbox/oauth/authorize",
        data={"request_id": request_id, "decision": decision, "persona": persona},
        follow_redirects=False,
    )
    assert to_callback.status_code == 303
    callback_url = to_callback.headers["location"]
    return client.get(callback_url, follow_redirects=False), callback_url


def test_pages_and_catalog_are_public(client):
    assert client.get("/").status_code == 200
    catalog = client.get("/api/catalog").json()
    assert {c["id"] for c in catalog["carriers"]} == {"summit", "harbor", "nova", "pulse"}
    assert all(c["oauth"] and c["oauth_is_sandbox"] for c in catalog["carriers"])
    assert set(catalog["scopes"]) == set(DEFAULT_SCOPES)


def test_security_headers(client):
    response = client.get("/api/catalog")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_signup_login_logout(client):
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/signup", json={"email": "bad", "password": "long enough pw"}).status_code == 422
    assert client.post("/api/auth/signup", json={"email": "a@b.co", "password": "short"}).status_code == 422
    assert client.post("/api/auth/signup", json={"email": "A@B.co", "password": "long enough pw"}).status_code == 201
    assert client.get("/api/me").json()["email"] == "a@b.co"
    assert client.post("/api/auth/signup", json={"email": "a@b.co", "password": "long enough pw"}).status_code == 409
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "a@b.co", "password": "wrong password"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "nobody@b.co", "password": "wrong password"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "a@b.co", "password": "long enough pw"}).status_code == 200
    assert client.get("/api/me").status_code == 200


def test_api_requires_sign_in(client):
    assert client.get("/api/connections").status_code == 401
    assert client.post("/api/connect/summit/oauth").status_code == 401
    assert client.post("/api/connect/summit/account", json=MANUAL_ACCOUNT).status_code == 401


def test_manual_account_to_recommendations(signed_in):
    response = signed_in.post("/api/connect/summit/account", json=MANUAL_ACCOUNT)
    assert response.status_code == 201
    connection_id = response.json()["id"]

    detail = signed_in.get(f"/api/connections/{connection_id}").json()
    assert detail["method"] == "manual"
    assert detail["account"]["plan_id"] == "summit-unlimited-elite"  # matched by name

    report = signed_in.get(f"/api/connections/{connection_id}/recommendations?horizon=12").json()
    assert report["horizon_months"] == 12
    assert report["best_option"]["savings"] > 0
    assert report["catalog_is_sample"] is True

    edited = {**MANUAL_ACCOUNT, "monthly_plan_cost": 20, "plan_name": "Something cheap"}
    assert signed_in.put(f"/api/connections/{connection_id}/account", json=edited).status_code == 200
    assert signed_in.get(f"/api/connections/{connection_id}").json()["account"]["plan_id"] is None
    assert signed_in.post(f"/api/connections/{connection_id}/refresh").status_code == 400


def test_invalid_input_rejected(signed_in):
    assert signed_in.post("/api/connect/nope/account", json=MANUAL_ACCOUNT).status_code == 404
    assert signed_in.post("/api/connect/summit/account", json={**MANUAL_ACCOUNT, "lines": []}).status_code == 422
    bad_device = {"model": "X", "financed": True, "monthly_installment": 10, "installments_remaining": 2, "monthly_promo_credit": 50}
    bad = {**MANUAL_ACCOUNT, "lines": [{"label": "A", "avg_data_gb": 1, "device": bad_device}]}
    assert signed_in.post("/api/connect/summit/account", json=bad).status_code == 422


def test_bill_upload_returns_draft_without_storing(signed_in):
    response = signed_in.post("/api/connect/harbor/bill", files={"file": ("bill.txt", SAMPLE_BILL, "text/plain")})
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["plan_id"] == "harbor-max"
    assert len(body["draft"]["lines"]) == 3
    assert signed_in.get("/api/connections").json() == []  # nothing saved until the customer confirms

    confirmed = {k: body["draft"][k] for k in ("plan_name", "plan_id", "monthly_plan_cost", "autopay", "lines")}
    saved = signed_in.post("/api/connect/harbor/account", json={**confirmed, "source": "bill_upload"})
    assert saved.status_code == 201
    assert signed_in.get("/api/connections").json()[0]["method"] == "bill_upload"

    garbage = signed_in.post("/api/connect/harbor/bill", files={"file": ("bill.pdf", b"%PDF-broken", "application/pdf")})
    assert garbage.status_code == 422


def test_sandbox_oauth_flow_end_to_end(signed_in, tmp_path):
    response, _ = sandbox_sign_in(signed_in, carrier="summit", persona="family")
    assert response.status_code == 303
    match = re.match(r"/#/dashboard\?connection=(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    connection_id = int(match.group(1))

    detail = signed_in.get(f"/api/connections/{connection_id}").json()
    assert detail["method"] == "sandbox_oauth"
    assert detail["scopes"] == DEFAULT_SCOPES
    assert detail["account"]["carrier_id"] == "summit"
    assert len(detail["account"]["lines"]) == 4

    report = signed_in.get(f"/api/connections/{connection_id}/recommendations").json()
    assert report["best_option"]["carrier_id"] == "nova"
    assert any(d["promo_id"] == "summit-apex16-tradein" for d in report["device_deals"])

    # Tokens are encrypted at rest.
    row = sqlite3.connect(tmp_path / "test.db").execute("SELECT token_ciphertext FROM connections").fetchone()
    assert row[0] and b"access_token" not in row[0]

    assert signed_in.post(f"/api/connections/{connection_id}/refresh").status_code == 200

    # Revoking deletes the data and the carrier-side token stops working.
    assert signed_in.delete(f"/api/connections/{connection_id}").status_code == 204
    assert signed_in.get(f"/api/connections/{connection_id}").status_code == 404
    db = sqlite3.connect(tmp_path / "test.db")
    assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    events = [e["event"] for e in signed_in.get("/api/consents").json()]
    assert events == ["revoked", "granted"]


def test_sandbox_denied_and_replayed_callbacks(signed_in):
    response, _ = sandbox_sign_in(signed_in, decision="deny")
    assert response.headers["location"] == "/#/connect?error=denied"

    response, callback_url = sandbox_sign_in(signed_in)
    assert response.headers["location"].startswith("/#/dashboard")
    replay = signed_in.get(callback_url, follow_redirects=False)
    assert replay.headers["location"] == "/#/connect?error=expired"  # state is single use
    assert len(signed_in.get("/api/connections").json()) == 1


def test_callback_state_is_bound_to_the_user_who_started_it(client):
    client.post("/api/auth/signup", json={"email": "alice@example.com", "password": "alice password"})
    start = client.post("/api/connect/nova/oauth").json()["authorize_url"]
    state = parse_qs(urlparse(start).query)["state"][0]
    client.post("/api/auth/logout")

    client.post("/api/auth/signup", json={"email": "mallory@example.com", "password": "mallory password"})
    response = client.get(f"/api/connect/oauth/callback?state={state}&code=stolen", follow_redirects=False)
    assert response.headers["location"] == "/#/connect?error=expired"


def test_sandbox_rejects_foreign_redirect_uri(signed_in):
    url = signed_in.post("/api/connect/summit/oauth").json()["authorize_url"]
    evil = url.replace("http%3A%2F%2Ftestserver", "https%3A%2F%2Fevil.example")
    assert evil != url
    assert signed_in.get(evil).status_code == 400


def test_users_cannot_see_each_others_connections(client):
    client.post("/api/auth/signup", json={"email": "one@example.com", "password": "first password"})
    connection_id = client.post("/api/connect/summit/account", json=MANUAL_ACCOUNT).json()["id"]
    client.post("/api/auth/logout")
    client.post("/api/auth/signup", json={"email": "two@example.com", "password": "second password"})
    assert client.get(f"/api/connections/{connection_id}").status_code == 404
    assert client.get(f"/api/connections/{connection_id}/recommendations").status_code == 404
    assert client.delete(f"/api/connections/{connection_id}").status_code == 404


def test_delete_account_removes_everything(signed_in, tmp_path):
    sandbox_sign_in(signed_in)
    signed_in.post("/api/connect/summit/account", json=MANUAL_ACCOUNT)
    assert signed_in.delete("/api/me").status_code == 204
    assert signed_in.get("/api/me").status_code == 401
    db = sqlite3.connect(tmp_path / "test.db")
    for table in ("users", "connections", "snapshots", "consent_events"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
