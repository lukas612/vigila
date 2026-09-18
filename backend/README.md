# Vigila — backend

Monitors the BOE's Tablón Edictal Único (TEU) for traffic-fine notifications
and powers the free instant check on the landing page. See
`VIGILA_BRIEF.md` (in the repo root, if present) for the full product brief.

## What's implemented here

- `app/boe_client.py` — queries the public BOE notifications search endpoint
  (no login/CAPTCHA) and extracts candidate PDF references.
- `app/pdf_parser.py` — downloads a candidate PDF and extracts its sanction
  table, matched by column header (handles the minor column differences
  between provincial bulletins).
- `app/check_service.py` — orchestrates search → PDF download → match, and
  is the single source of truth both the free check endpoint and the future
  subscription cron job should call.
- `app/validators.py` — DNI/NIE/plate format + DNI check-digit validation.
- `app/rate_limit.py` — in-memory per-IP rate limiting for the free check.
- `app/models.py` — SQLAlchemy models for the schema in the brief (users,
  monitored_ids, checks_free, notifications, notification_runs, plus a
  waitlist_signups table for the landing page's email capture).
- `app/main.py` — FastAPI app exposing `POST /api/check`, `POST /api/waitlist`
  and `GET /api/stats/latest`, and serving `../frontend` as static files for
  local end-to-end runs.
- `scripts/crawl_daily_stats.py` — a separate pipeline from the per-user
  check: crawls every traffic bulletin published nationally on a given day
  (~150-200 PDFs, no DNI/matrícula filter) and stores **aggregate-only**
  counts and totals per locality in `daily_stats`. Powers the "¿cuántos
  multaron ayer?" hook section on the landing page. Runs from GitHub Actions
  (`.github/workflows/daily_stats.yml`), never from the Render app — parsing
  that many PDFs is far too slow for a web request, especially on a
  CPU-limited free tier.

### Why the pipeline never trusts the BOE search alone

Live testing against `boe.es` showed the search endpoint does full-text
matching over PDF content, not exact identifier matching — a fabricated DNI
(`00000001A`) matched 200+ unrelated documents. Because of this,
`check_service.run_check` treats every search hit as a **candidate only**:
it always downloads the PDF and requires an exact match (case/space
insensitive) on the IDENTIF or MATRICULA column before reporting "found".
This is covered by `tests/test_check_service.py`, using real captured
HTML/PDF fixtures.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

Then open http://localhost:8000 — the landing page is served from
`../frontend` and calls the API on the same origin.

Set `DATABASE_URL` to point at Postgres/Supabase in production (defaults to
a local `vigila.db` SQLite file otherwise). The `vigila` Supabase project
(ref `wwxlzvfxlfuodikcamlq`, eu-west-1) already has the schema applied:

```bash
export DATABASE_URL=postgresql://postgres.wwxlzvfxlfuodikcamlq:<db-password>@aws-1-eu-west-1.pooler.supabase.com:5432/postgres
```

**Use the Supavisor pooler, not the direct connection.** The direct
`db.wwxlzvfxlfuodikcamlq.supabase.co:5432` host is IPv6-only, and several
common hosts (Render, Vercel, GitHub Actions) have no IPv6 egress at all —
this is exactly what broke the first Render deploy (`Network is
unreachable`). The pooler host above is always IPv4. Locally this doesn't
matter (most ISPs/laptops have IPv6), so either works for local dev.

**The `aws-N-` cluster index is not derivable from the region** — Supabase's
own docs warn against assuming `aws-0`. This project's tenant turned out to
be registered on `aws-1-eu-west-1` (confirmed live against the deployed
Render service; `aws-0` fails with `FATAL: tenant/user ... not found`). If
this project is ever recreated, get the exact string from the Dashboard's
[Connect](https://supabase.com/dashboard/project/wwxlzvfxlfuodikcamlq?showConnect=true&method=session)
dialog rather than guessing.

**⚠️ Row Level Security is disabled on all 6 tables** (Supabase flags this
as a critical finding, since the `public` schema is exposed via its
auto-generated REST API to anyone holding the anon/publishable key). This
backend never uses that REST API — it talks to Postgres directly via
`DATABASE_URL` with SQLAlchemy — so there's no exposure through this app
itself. Still, enable RLS before this project's anon key is ever used
anywhere else (e.g. a future client-side Supabase SDK integration):

```sql
ALTER TABLE "public"."users" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."monitored_ids" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."checks_free" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."notifications" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."notification_runs" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."waitlist_signups" ENABLE ROW LEVEL SECURITY;
```

This wasn't auto-applied: enabling RLS with no policies blocks all access
through PostgREST, so add policies (or explicitly decide these tables
should only ever be reached through this backend's own DB role) before
flipping it on.

## Deploying (Render)

The landing page is hosted as a static site on GitHub Pages
(https://lukas612.github.io/vigila/), which can't run the Python backend —
`render.yaml` at the repo root defines a Render web service for that:

1. In Render: **New → Blueprint**, pick the `lukas612/vigila` repo. It reads
   `render.yaml` and creates a `vigila-api` free web service with
   `rootDir: backend`.
2. Set the `DATABASE_URL` env var (left blank in the blueprint on purpose,
   since it contains the DB password — never commit it) to the **Supavisor
   pooler** string, not the direct connection (see "Use the Supavisor
   pooler" above — Render has no IPv6 egress):
   `postgresql://postgres.wwxlzvfxlfuodikcamlq:<db-password>@aws-1-eu-west-1.pooler.supabase.com:5432/postgres`
3. Deploy. Render assigns `https://vigila-api.onrender.com` (the name in
   `render.yaml`) unless that subdomain is already taken, in which case
   update the hardcoded URL in the repo root's `index.html`
   (`window.VIGILA_API_BASE`) to match.
4. **Free-tier Render services spin down after ~15 minutes without
   traffic**, and the next request pays a 30-60s+ cold start — sometimes
   even a `502` if it lands mid-restart. `.github/workflows/keepalive.yml`
   pings `GET /` every 10 minutes (via GitHub Actions, so it runs whether
   or not anyone has this repo open) to keep it warm. This is a
   workaround, not a fix: it doesn't help the very first request after the
   workflow itself has been idle, and it eats into the free plan's
   monthly hours. For anything beyond testing, move to a paid plan
   (Starter, no spin-down) instead.

Note the root `index.html` (served by GitHub Pages) and
`frontend/index.html` (served locally by this app for full-stack dev) are
two copies — only the root one hardcodes `VIGILA_API_BASE`, since the local
copy is same-origin with the API and needs no override.

### Daily stats crawl (GitHub Actions)

`.github/workflows/daily_stats.yml` needs a `DATABASE_URL` repository
secret (Settings → Secrets and variables → Actions → New repository
secret) set to the same Supavisor pooler string as Render's. It runs once
a day automatically; trigger it manually from the Actions tab (or
`workflow_dispatch`, optionally with a `date` input) to backfill a specific
day. The landing page's hook section just hides itself if
`/api/stats/latest` 404s (no day crawled yet), so the site works fine
before this has ever run.

## Tests

```bash
pytest
```

Tests are fully offline — `tests/fixtures/` has real HTML/PDF snapshots
captured from `boe.es` during development, mocked in with `respx`.

## Accounts and monitoring

Passwordless login (magic link) via Supabase Auth, plus a small dashboard
(`cuenta.html`) where a logged-in user saves the DNI/NIE/matrícula they
want watched.

- **Login**: the frontend calls `supabase.auth.signInWithOtp({email})`
  directly (Supabase's own JS client, loaded from a CDN) — this backend
  never sees a password or handles the email itself. Once the user clicks
  the link, the frontend hands the resulting access/refresh tokens to
  `POST /api/auth/session`, which verifies them against Supabase
  (`GET /auth/v1/user`) and wraps them in `HttpOnly; Secure; SameSite=None`
  cookies (`app/auth.py`). Every other `/api/*` call reads those cookies —
  the tokens are never exposed to page JS, and `get_current_user` silently
  refreshes an expired access token using the refresh-token cookie, so a
  session survives well past Supabase's ~1h access-token lifetime.
- **Why cookies and not a JWT in localStorage**: HttpOnly cookies can't be
  read by page JS at all, which is the point — an XSS bug can't walk off
  with the session. The cost is CORS has to allow credentials
  (`allow_credentials=True`) and the frontend has to pass
  `credentials: 'include'` on every fetch, since the frontend (GitHub
  Pages) and this API (Render) are different origins.
- **Monitored targets** (`monitored_ids` table): the DNI/NIE/matrícula
  itself is Fernet-encrypted at rest (`app/crypto.py`,
  `TARGET_ENCRYPTION_KEY`) rather than hashed like `checks_free` — hashing
  is one-way and useless here, since `scripts/check_monitored_targets.py`
  has to decrypt the value to re-query the BOE. `/api/targets` only ever
  returns a masked value (`••••5678A`) to the browser, never the plaintext.
- **The monitoring cron** (`check_targets.yml`, twice daily) reuses
  `check_service.run_check` — the exact same pipeline the free checker
  uses, so "found" never means something different between the two paths —
  and records new matches in `notifications`, deduped on
  `(monitored_id, boe_ref, expediente)` so a repeated run never double-counts.
- **Push email alert** (`app/email.py`, via Resend): a new match — whether
  found by the cron or by the immediate check `create_target` runs right
  when a target is added — emails the target's owner
  (`app/monitoring.py`'s `check_target`, shared by both call sites so the
  email logic can't drift between them). Sending is best-effort: a Resend
  failure is logged and never breaks the check that triggered it. Separate
  from Supabase Auth's own mailer, which only ever sends its own login
  emails, not arbitrary content.

### One-time setup this session couldn't do itself

1. **Supabase → Authentication → URL Configuration → Redirect URLs**: add
   `https://vigilamultas.com/cuenta.html` (and
   `http://localhost:8000/cuenta.html` if testing locally). Supabase
   rejects `emailRedirectTo` values that aren't on this list.
2. Render env vars (Dashboard → vigila-api → Environment): `SUPABASE_URL`,
   `SUPABASE_ANON_KEY`, `TARGET_ENCRYPTION_KEY`, `RESEND_API_KEY`,
   `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
   `STRIPE_PRICE_ID_INDIVIDUAL`, `STRIPE_PRICE_ID_FAMILIAR` — see
   `.env.example`.
3. GitHub Actions secrets on `check_targets.yml` (Settings → Secrets and
   variables → Actions): `TARGET_ENCRYPTION_KEY` (**must be the exact same
   value as Render's**, or the cron can't decrypt what the API encrypted)
   and `RESEND_API_KEY` (so the cron's own matches get emailed too, not
   just the ones found at target-creation time). Stripe's keys aren't
   needed here — only the web API handles billing.
4. Supabase Auth → Emails → SMTP Settings → custom SMTP via Resend
   (`smtp.resend.com`, port 465, username `resend`, password = the Resend
   API key, sender `noreply@vigilamultas.com`) — moves magic-link delivery
   off Supabase's shared mailer, which has a low project-wide rate limit
   that a few quick retries can exhaust for up to an hour.

## Billing (Stripe)

`app/billing.py` — raw `httpx` calls against Stripe's REST API (no SDK,
same reasoning as `email.py`'s Resend client): checkout, the customer
portal, and one webhook.

- **`POST /api/billing/checkout`** (`{"plan": "individual"|"familiar"}`,
  authenticated): creates a Stripe Checkout Session in `mode=subscription`
  for the plan's monthly Price, with `billing_address_collection=required`
  and `tax_id_collection[enabled]=true` so a business customer can enter
  their CIF/VAT id and get a valid invoice. Returns `{"url": ...}` for the
  frontend to redirect to.
- **`POST /api/billing/portal`** (authenticated): creates a Stripe Billing
  Portal session for a user who already has a `stripe_customer_id`, so they
  can update payment details, cancel, **switch between Individual and
  Familiar (upgrade/downgrade)**, or view/download invoices without us
  building any of that UI. Plan switching needs a
  `billing_portal.configuration` with `subscription_update` enabled and
  both prices listed — Stripe's own default configuration is
  cancel/payment-method-only. One was created via `POST
  /v1/billing_portal/configurations` (became the account's default
  automatically, since it was the first) and its id also set as
  `STRIPE_PORTAL_CONFIGURATION_ID` so `create_portal_session` references it
  explicitly rather than relying on whatever's marked default. A plan
  switch there fires the same `customer.subscription.updated` webhook as
  any other subscription change, so `billing.apply_event` picks up the new
  plan the normal way — no separate handling needed.
- **`POST /api/billing/webhook`**: Stripe calls this on
  `checkout.session.completed`, `customer.subscription.updated` and
  `customer.subscription.deleted`. The raw request body is verified against
  `Stripe-Signature` by hand (`billing.verify_webhook_signature` —
  HMAC-SHA256 of `"{timestamp}.{body}"`, constant-time compared, 5-minute
  replay tolerance) before anything in it is trusted, then
  `billing.apply_event` updates the user's `plan`/`subscription_status`/
  `stripe_customer_id` (`app/models.py`'s `User`).
- **Plan-gated target limits**: `main._max_targets` returns 0 unless
  `subscription_status` is `active` or `trialing`, otherwise the plan's own
  cap (`billing.PLAN_TARGET_LIMITS`: Individual = 1, Familiar = 5).
  `POST /api/targets` enforces this instead of the old flat
  `MAX_TARGETS_PER_USER` constant, and `/api/auth/me` reports it as
  `max_targets` so `cuenta.html`'s "Mi plan" card can show it without a
  separate call.
- The webhook endpoint (`https://api.vigilamultas.com/api/billing/webhook`)
  and the Individual/Familiar Products + Prices already exist on the Stripe
  side — only the four env vars in step 2 above need setting on Render for
  this to go live.

## Admin panel

`admin.html` (not linked from the public nav — reachable only by URL) shows
two things behind `/api/admin/*`:

- **Usuarios**: every account, its plan/subscription/Stripe fields (now
  populated once a user subscribes — see "Billing (Stripe)"), and how many
  targets/matches it has.
- **Comprobaciones gratuitas**: aggregate totals from `checks_free` (how
  many checks, how many actually found a fine) plus a per-day breakdown.
  This can only ever be counts — `checks_free.value_hash` is a one-way
  hash, so there's no way to see *which* DNI/matrícula was checked, by
  design.

Gated by `auth.require_admin`: a logged-in user whose email isn't in the
`ADMIN_EMAILS` env var (comma-separated) gets a 403, not a redirect — set
it on Render to whichever email(s) should have access, matching exactly
what they log into Vigila with.

## What's NOT implemented yet (fase 2, per the brief)

- Turnstile/CAPTCHA once the free-check rate limit is exceeded (currently
  just a 429 with a message).
- SMS/WhatsApp alerts (email is wired up — see "Push email alert" above).
- The landing page's anonymous "Quiero este plan" buttons (`index.html`)
  are still email-capture only (`/api/waitlist`), not real checkout — real
  subscribing happens from the logged-in "Mi plan" card in `cuenta.html`
  instead, since Stripe checkout needs to be tied to an already-created
  account (`client_reference_id`/`metadata.user_id`) for the webhook to
  know whose row to update.

## Environment variables

See `.env.example`.

## Privacy note

Per the brief: the free check never persists the raw DNI/matrícula — only a
SHA-256 hash, for rate-limiting/analytics (`checks_free.value_hash`). Raw
values are only ever kept in memory for the duration of a single request.
