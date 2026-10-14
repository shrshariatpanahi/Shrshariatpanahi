# PlanScout

Customers connect their phone carrier account, PlanScout reads their plan, usage and devices
(with their permission), and recommends:

- **Cheaper plans** that still fit their usage, at their current carrier or another one, priced
  honestly, including device payoffs, activation fees, forfeited credits and switching promotions.
- **Phone deals**: promotions on new devices at their carrier (upgrade/trade-in) or at other
  carriers (switcher deals), including the cost of any pricier plan the deal requires.
- **Quick wins** such as turning on AutoPay, and which phones are paid off and free to upgrade.

> The bundled catalog (carriers, plans, prices, phones) is **fictional sample data** so the app
> runs out of the box. Replace it with a real offer feed before showing it to customers.

## How "log in to your carrier" works

PlanScout **never asks for or stores a customer's carrier password**. Collecting passwords and
logging in on the customer's behalf ("screen scraping") breaks carrier terms of service, fails on
MFA, and would turn our database into a target: carrier account access is how SIM-swap attackers
take over people's phone numbers, and with them bank and email 2FA.

Instead there are three ways to connect, all with explicit consent:

| Method | How it works | Status |
| --- | --- | --- |
| **Sign in with your carrier** | OAuth 2.0 + PKCE. The customer signs in on the *carrier's own page* and approves read-only scopes (`plan:read`, `usage:read`, `devices:read`). We get a revocable token, encrypted at rest. | Works end to end against the built-in **sandbox carrier**. Real carriers need a partner agreement (see below). |
| **Upload a bill** | Customer uploads a PDF/text bill. We extract plan, cost, per-line usage and device payments, show the draft for review, and discard the file. Only the last 4 digits of phone numbers are kept. | Works with any carrier today (best-effort parsing). |
| **Enter details** | A short form. | Works with any carrier. |

Customers can see exactly what was shared, disconnect at any time (we revoke the token at the
carrier and delete their data), review their consent history, and delete their account.

## Quick start

```bash
cd planscout
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn planscout.main:create_app --factory --reload
```

Open http://127.0.0.1:8000, create an account, choose a carrier, and pick **Sign in with ...**.
The sandbox sign-in page offers three demo customers (a heavy-use family, a light single-line
user, a couple). API docs are at http://127.0.0.1:8000/api/docs.

Run the tests with `pytest`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PLANSCOUT_SECRET_KEY` | random per start | Signs session cookies. **Set in production.** |
| `PLANSCOUT_ENCRYPTION_KEY` | random per start | Fernet key that encrypts carrier tokens. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Set in production.** |
| `PLANSCOUT_DB_PATH` | `planscout.db` | SQLite database file. |
| `PLANSCOUT_SANDBOX` | `true` | Enables the demo carrier sign-in. Turn off in production. |
| `PLANSCOUT_HTTPS_ONLY` | `false` | Secure-only cookies. Turn on behind HTTPS. |
| `PLANSCOUT_BASE_URL` | from request | Public URL used to build the OAuth redirect URI. |
| `PLANSCOUT_OAUTH_<CARRIER>_CLIENT_ID`, `_CLIENT_SECRET`, `_AUTHORIZE_URL`, `_TOKEN_URL`, `_ACCOUNT_URL`, optional `_REVOKE_URL` | – | Real OAuth integration for a carrier or data partner (e.g. `PLANSCOUT_OAUTH_NOVA_CLIENT_ID`). Takes precedence over the sandbox for that carrier. |

## How recommendations are calculated

Every option is compared on the same basis: **N months of service (12, 24 or 36) plus finishing
the payments on the phones you have now**, so "stay" and "switch" both end with the customer
owning their current phones.

- **Stay:** current plan cost × N + remaining device payments net of promo credits.
- **Change plan, same carrier:** new plan price × N + the same device payments, minus
  existing-customer offers.
- **Switch carrier:** new plan price × N + activation fees + the full remaining device balance
  (it becomes due, and remaining credits are forfeited), minus switcher promotions (payoff
  reimbursements are capped at what is actually owed).
- A plan only qualifies if it covers each line's average data and hotspot use with 20% headroom.
- Promotions: all *stackable* offers apply plus the single best non-stackable one; expired offers
  and offers for the wrong customer type (new vs existing) are ignored.
- Device deals add the extra plan cost when a "free phone" requires a pricier plan for the
  length of its bill credits, and trade-in deals only show when a phone on the account qualifies.

Prices exclude taxes; alternatives assume AutoPay. The logic is in
[`planscout/recommender.py`](planscout/recommender.py) and covered by `tests/test_recommender.py`.

## Project layout

```
planscout/
  main.py              FastAPI app: auth, connect flows, connections, recommendations
  recommender.py       Pricing engine
  catalog.py           Loads and cross-validates the catalog
  models.py            Pydantic models (catalog, account snapshot, report)
  storage.py           SQLite: users, connections, snapshots, consent history
  security.py          scrypt password hashing, token encryption, PKCE
  connectors/
    base.py            Provider interface + read-only scopes
    oauth2.py          Generic OAuth 2.0 + PKCE client for real carriers/partners
    sandbox.py         Pretend carrier sign-in page + account API for demos
    bill_parser.py     PDF/text bill to draft account
  data/                Sample catalog: carriers, plans, promotions, devices, demo customers
  static/              Single-page web UI (no build step)
tests/
```

## Going live: what's still needed

1. **Real account data.** Carriers generally don't offer a public API for customer account
   data. Options are a partner agreement with each carrier, or a licensed data aggregator that
   already has consented access. Either way, plug it in with the `PLANSCOUT_OAUTH_*` variables and,
   if their payload differs from `AccountSnapshot`, override `OAuth2Provider.map_account`. Until
   then, bill upload covers every carrier.
2. **Real plans and promotions.** Replace `planscout/data/*.json` with a maintained feed:
   carrier partner or affiliate offer feeds, or a team curating published offers. Set
   `is_sample` to `false` in `catalog_meta.json`. Promotions change weekly, so automate this.
3. **Privacy and legal review.** Carrier account and usage data is regulated (e.g. CPNI rules in
   the US, PIPEDA in Canada, state privacy laws). Get counsel to review consent wording, retention,
   and disclosure, and disclose any affiliate/referral revenue next to recommendations.
4. **Production hardening.** HTTPS, set the secret and encryption keys (ideally from a secrets
   manager), turn off the sandbox, add login rate limiting and email verification, move from SQLite
   to Postgres for multiple instances, and add monitoring.
5. **Mobile apps.** The web UI is responsive. For app-store apps, the same API works behind a
   React Native/Flutter client; use the system browser (ASWebAuthenticationSession / Custom Tabs)
   for carrier sign-in.
