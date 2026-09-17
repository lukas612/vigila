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
- `app/main.py` — FastAPI app exposing `POST /api/check` and
  `POST /api/waitlist`, and serving `../frontend` as static files for local
  end-to-end runs.

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
export DATABASE_URL=postgresql://postgres:<db-password>@db.wwxlzvfxlfuodikcamlq.supabase.co:5432/postgres
```

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

## Tests

```bash
pytest
```

Tests are fully offline — `tests/fixtures/` has real HTML/PDF snapshots
captured from `boe.es` during development, mocked in with `respx`.

## What's NOT implemented yet (fase 2, per the brief)

- Auth / user accounts, Stripe checkout + webhooks, customer portal.
- The cron job that loops over `monitored_ids` and sends email alerts
  (the pipeline it would call, `check_service.run_check`, already exists).
- User dashboard.
- Turnstile/CAPTCHA once the free-check rate limit is exceeded (currently
  just a 429 with a message).
- SMS/WhatsApp alerts.

## Environment variables

See `.env.example`.

## Privacy note

Per the brief: the free check never persists the raw DNI/matrícula — only a
SHA-256 hash, for rate-limiting/analytics (`checks_free.value_hash`). Raw
values are only ever kept in memory for the duration of a single request.
