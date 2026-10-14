# PlanScout

PlanScout is set up for **Canadian** carriers. Customers connect their phone carrier account,
PlanScout reads their plan, usage and devices (with their permission), and recommends:

- **Cheaper plans** that still fit their usage, at their current carrier or another one, priced
  honestly, including device payoffs, activation fees, forfeited credits and switching promotions.
- **Phone deals**: promotions on new devices at their carrier (upgrade/trade-in) or at other
  carriers (switcher deals), including the cost of any pricier plan the deal requires.
- **Quick wins** such as turning on AutoPay, and which phones are paid off and free to upgrade.

## Canadian carriers and data

<!-- CARRIER_TABLE -->

Plans, prices, connection fees and promotions in [`planscout/data/`](planscout/data/) were researched on
**<!-- AS_OF -->** from carrier websites and Canadian telecom news (iPhone in Canada, MobileSyrup,
RedFlagDeals, PlanHub and others), then independently re-checked. Every plan and promotion lists the
URLs it came from (`source_urls`). Items that couldn't be corroborated were left out rather than guessed.

**Canadian plans change monthly.** The app shows customers the date prices were checked and warns them
once the data is more than 45 days old. To update, edit the JSON files (or re-run the research), bump
`as_of` in `catalog_meta.json`, and run `pytest`: `tests/test_catalog_canada.py` checks that every carrier,
plan and promotion is complete, sourced, and not expired.

What's specific to Canada:

- **Provinces.** Each brand lists where it sells service (e.g. SaskTel in Saskatchewan, Eastlink in
  Atlantic Canada, Vidéotron in Quebec and beyond). Customers pick their province, or it's detected from
  the postal code on their bill (only the province is kept), and only carriers that serve them are compared.
- **Sales tax.** Comparisons are before tax (the rate is the same whichever plan you pick), but the dashboard
  shows tax-inclusive estimates using the GST/HST/PST/QST rate for the customer's province.
- **Sister brands.** Rogers/Fido/chatr, Bell/Virgin Plus/Lucky Mobile, TELUS/Koodo/Public Mobile and
  Freedom/Vidéotron/Fizz share owners. Switcher offers that exclude ports from sister brands are respected,
  and customers are warned when a move is between sister brands.
- **CRTC Wireless Code.** Leaving a carrier makes the remaining device balance due (included in every
  comparison), and carriers must unlock phones for free, so locked phones are flagged, not treated as blockers.
- **French bills.** Bill upload reads French bills (e.g. "Total à payer", "Forfait", "Données utilisées : 3,5 Go",
  "45,00 $") as well as English ones.
- **Demo sign-in.** No Canadian carrier offers third-party account access today, so "sign in with your carrier"
  runs against a clearly labelled demo that simulates the chosen carrier with sample customers.

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

Open http://127.0.0.1:8000, create an account, pick your province and carrier, then choose
**Start the demo**. The demo sign-in offers three sample customers (a heavy-use family in Ontario,
a light single-line user in Quebec, a couple in BC). Or upload a bill, or type your details in.
API docs are at http://127.0.0.1:8000/api/docs.

Run the tests with `pytest`.

## Get a public link (free, on Render)

The repo includes a [Render Blueprint](../render.yaml), so anyone can reach the app at a public
`https://….onrender.com` link:

1. Sign up at [render.com](https://render.com) with your GitHub account (no credit card needed for the free plan).
2. In the Render dashboard choose **New > Blueprint**, connect this GitHub repo, pick the branch that has
   PlanScout on it, and click **Deploy Blueprint**.
3. After the first build (a few minutes), the service page shows the link, e.g. `https://planscout.onrender.com`
   (Render adds a suffix if that name is taken).

Every push to that branch redeploys automatically. Render generates the session and encryption secrets.

Free-plan limits to know: the app goes to sleep after 15 minutes without visitors and takes about a minute to
wake up, and the free plan has no persistent disk, so **the database (accounts and connections) is wiped on
every restart or redeploy**. The app tells visitors this (`PLANSCOUT_EPHEMERAL`). For a lasting deployment,
attach a paid persistent disk and point `PLANSCOUT_DB_PATH` at it, or move storage to Postgres.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PLANSCOUT_SECRET_KEY` | random per start | Signs session cookies. **Set in production.** |
| `PLANSCOUT_ENCRYPTION_KEY` | random per start | Encrypts carrier tokens at rest: a Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) or any random secret of 32+ characters. **Set in production.** |
| `PLANSCOUT_DB_PATH` | `planscout.db` | SQLite database file. |
| `PLANSCOUT_SANDBOX` | `true` | Enables the demo carrier sign-in. Turn off in production. |
| `PLANSCOUT_HTTPS_ONLY` | `false` | Secure-only cookies. Turn on behind HTTPS. |
| `PLANSCOUT_BASE_URL` | from request | Public URL used to build the OAuth redirect URI. Behind a proxy, run uvicorn with `--proxy-headers` instead (as `render.yaml` does). |
| `PLANSCOUT_EPHEMERAL` | `false` | Tells visitors that accounts are deleted on restart (hosts without persistent disk). |
| `PLANSCOUT_OAUTH_<CARRIER>_CLIENT_ID`, `_CLIENT_SECRET`, `_AUTHORIZE_URL`, `_TOKEN_URL`, `_ACCOUNT_URL`, optional `_REVOKE_URL` | – | Real OAuth integration for a carrier or data partner, keyed by carrier id with dashes as underscores (e.g. `PLANSCOUT_OAUTH_KOODO_CLIENT_ID`). Takes precedence over the demo for that carrier. |

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
- A plan only qualifies if its full-speed data covers each line's average use (hotspot included) with
  20% headroom, and the carrier sells service in the customer's province.
- Promotions: all *stackable* offers apply plus the single best non-stackable one; expired offers,
  offers for the wrong customer type (new vs existing) and offers that exclude the customer's current
  brand are ignored.
- Device deals add the extra plan cost when a "free phone" requires a pricier plan for the length of
  its bill credits. Trade-in deals that list eligible models only show when a phone on the account
  qualifies; otherwise the carrier's trade-in terms are shown with the deal.

Prices exclude sales tax; alternatives assume AutoPay. The logic is in
[`planscout/recommender.py`](planscout/recommender.py). Engine tests (`tests/test_recommender.py`,
`tests/test_canada.py`) run against a frozen fictional catalog in `tests/fixtures/catalog/`, so they
don't break when real prices change.

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
    bill_parser.py     PDF/text bill (English or French) to draft account
  data/                Canadian catalog: carriers, plans, promotions, devices, tax; demo customers
  static/              Single-page web UI (no build step)
tests/
  fixtures/catalog/    Frozen fictional catalog for engine tests
```

## Going live: what's still needed

1. **Real account data.** Canadian carriers don't offer a public API for customer account data.
   Options are a partner agreement with each carrier, or a licensed data aggregator that already has
   consented access. Either way, plug it in with the `PLANSCOUT_OAUTH_*` variables and, if their payload
   differs from `AccountSnapshot`, override `OAuth2Provider.map_account`. Until then, bill upload and
   manual entry cover every carrier.
2. **Keeping offers current.** The researched catalog is a snapshot. Before launch, set up a regular
   refresh (carrier partner or affiliate offer feeds, or a person curating published offers weekly) and
   spot-check the numbers against carrier sites.
3. **Canadian privacy and consumer law.** Have a Canadian lawyer review before launch:
   - **PIPEDA** (and Alberta/BC PIPA): meaningful consent, purpose limits, retention, breach reporting.
   - **Quebec Law 25**: stricter consent, a named privacy officer, privacy impact assessments, and rules on
     data leaving Quebec.
   - **Charter of the French Language (Bill 96)**: Quebec customers must be able to use the service in
     French; the UI is English-only today.
   - **Competition Act**: prices shown must not be misleading, including "drip pricing" (mandatory fees must
     be included; PlanScout includes connection fees in comparisons), and affiliate/referral relationships
     must be disclosed.
   - **CASL** if you email customers about new deals.
4. **Production hardening.** HTTPS, set the secret and encryption keys (ideally from a secrets
   manager), turn off the sandbox, add login rate limiting and email verification, move from SQLite
   to Postgres for multiple instances, and add monitoring.
5. **Mobile apps.** The web UI is responsive. For app-store apps, the same API works behind a
   React Native/Flutter client; use the system browser (ASWebAuthenticationSession / Custom Tabs)
   for carrier sign-in.
