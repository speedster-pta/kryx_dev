# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**autosend** (branding: "Kryx"; internal package/DB paths still say `autosend`; some strings/config still say "Shofar Automation" — a rename is mid-flight, expect inconsistency) is a FastAPI backend that sends automated, template-based WhatsApp messages driven by Planning Center Online (PCO) data, plus a standalone bulk/blast WhatsApp campaign sender. It was forked from a single-organisation production system into a **multi-tenant platform**: the WhatsApp campaign sender is the universal core product, and PCO integration is an optional, opt-in module per organisation.

All application code is under `api/autosend/`; tests under `api/tests/`.

## Commands

Run all commands from `api/` (there is no root-level tooling — no `pyproject.toml`, no configured linter/formatter/type-checker).

```bash
cd api
pip install -r requirements-dev.txt   # requirements.txt + pytest
pytest                                 # full suite (testpaths = tests, per pytest.ini)
pytest tests/test_cross_org_isolation.py            # one file
pytest tests/test_cross_org_isolation.py::TestName::test_method  # one test
uvicorn autosend.main:app --reload --port 8000       # run locally
```

Local dev needs a `.env` (copy `.env.example`) with real `TOKEN_ENCRYPTION_KEY` / `SESSION_SECRET_KEY` values — the defaults baked into `config.py` are dev placeholders only, never reuse them in a real deployment. WhatsApp/PCO credentials are **not** env vars; they're entered per-organisation via the admin UI and Fernet-encrypted in the DB.

There is no CI pipeline in this repo — treat `pytest` (and manual verification against the running app) as the whole safety net before pushing.

## Architecture

### Entry point (`api/autosend/main.py`)

FastAPI app, Sentry-instrumented (error-only, `traces_sample_rate=0.0`, disabled when `SENTRY_DSN` is blank). Startup (`lifespan`): `init_db()`, optionally register the PCO registration poller on APScheduler, start the scheduler, reload pending campaigns/serving-reminder jobs. Middleware order is load-bearing and documented inline: `SessionMiddleware` first, then `ScopeCleanupMiddleware` (a plain ASGI middleware, deliberately *not* `BaseHTTPMiddleware`, so it doesn't break contextvar propagation for `admin_auth.current_scope`) — any future `BaseHTTPMiddleware` must wrap outside `ScopeCleanupMiddleware`. `setup_admin(app)` (SQLAdmin, mounted at `/`) **must stay the last line of the file** — it's a root `Mount` that would shadow every route registered after it. `/ops/*` diagnostic endpoints are gated by a simple `X-Admin-Key` header check (`autosend/auth.py::require_admin_key`), a separate, much weaker mechanism than session auth — never treat it as a real authz boundary.

### Database & schema — no migration tool

SQLite only, one file (`DATABASE_PATH`, default `/data/autosend.db`). Two parallel access layers over the same file:
- **Raw `sqlite3`** (`storage/_db.py`) is the source of truth for reads/writes — rows are plain tuples, manually zipped with column names.
- **SQLAlchemy ORM** (`admin_models.py`) exists only so SQLAdmin has models to build CRUD screens from; `Base.metadata.create_all()` is never called.

Schema is raw SQL (`CREATE TABLE IF NOT EXISTS`), split across `storage/schema.py` (core: organisations, units, users, campaigns, whatsapp_*, send_log, login_attempts, ...) and `integrations/pco/schema.py` (PCO-only tables), both run once at startup by `core/db_init.py::init_db()` in that order (PCO tables FK into core tables). **There is no Alembic and no migration history** — this is treated as a fresh project with no data to evolve, so schema changes are just new `CREATE TABLE IF NOT EXISTS` blocks, never `ALTER TABLE`/renames/backfills. The one sanctioned exception is `_add_column_if_missing()` in `schema.py`, used once for a guarded, nullable-column `ADD COLUMN`. **If real production data exists when you need to change a table shape, do not improvise an ALTER TABLE** — read the `schema.py` docstring first; the inherited discipline for that case is rename → recreate → copy → drop, PRAGMA-guarded, run idempotently on every startup.

### Multi-tenancy model

`Organisation` → `Unit` (renamed from "Congregation"; a campus/branch) → everything else (WhatsApp numbers, templates, campaigns, staff unit-assignments). A `module` (currently only `"pco"`) is an org-wide feature flag, orthogonal to the org/unit tree, with two tiers: `organisation_module_grants` (superadmin-only entitlement — "sold this org module X") and `organisation_modules` (org-admin toggle — "on right now"); `modules.enable()` raises if not granted first. `storage.modules.is_enabled(org_id, module_key)` is the single choke point every integration/scheduler/webhook/admin-nav check calls — toggling takes effect immediately, no restart needed.

**Column pattern (load-bearing, documented in `schema.py`):** `org_id` lives authoritatively only on `organisations`, `organisation_modules`, `organisation_module_grants`, `units`, and `users`. Every unit-scoped table (`whatsapp_numbers`, `campaigns`, `send_log`, etc.) scopes via `unit_id` only, not a denormalised `org_id` — isolation holds via a join through `units` (both `unit_id` and `units.org_id` are `NOT NULL`). Exceptions: `pco_organization_settings.org_id` is direct (one row per org), `users.org_id` is direct and nullable (`NULL` only for superadmins). PCO dedup tables (`processed_registrations`, `signup_watermark`, `processed_form_submissions`) carry no org/unit scoping at all, keyed purely by PCO's own IDs.

**Enforcement is not just query filtering.** SQLAdmin re-fetches a row by raw pk (bypassing `list_query`) when rendering edit/details pages and inside `update_model`/`delete_model` — so `admin_scoping.py::ScopedModelView` overrides `form_edit_query`/`details_query` too, and explicitly re-checks `_row_in_scope()` before every update/delete, raising 404 otherwise. Any new admin view over tenant-scoped data must follow this same pattern (either inherit `ScopedModelView`, or hand-roll the identical `list_query` + `form_edit_query`/`details_query` + `update_model`/`delete_model` re-check, as `PCOOrganizationSettingsAdmin`/`UserAdmin` in `admin_views.py` do for org-level-only scoping). **`BaseView.@expose` routes are never auto-guarded** by SQLAdmin's `is_accessible`/`is_visible` (only auto-generated CRUD/nav routes get that) — every hand-rolled route (e.g. `admin_org_pages.py`) must re-check permissions inline itself.

Roles (all on `users`): `is_superadmin` (spans every org, `org_id` NULL), `is_org_admin` (scoped to own `org_id`; manages own units/staff/module toggles, cannot create orgs or grant entitlements), plain staff (scoped to explicit `user_units` rows). Any field a client could use to escalate scope (`org_id`, `is_superadmin`, unit assignments) must be defended twice: hidden in `scaffold_form` (UX only) **and** forced/stripped server-side in `insert_model`/`update_model` regardless of what was POSTed — never trust the client here. `test_cross_org_isolation.py` is the regression suite for exactly this class of bug (including a previously-real gap where an org-admin could grant a user access to another org's unit via a crafted multi-select POST even though the dropdown itself was filtered).

### Auth

Cookie session only (`SessionMiddleware`, `itsdangerous`-signed via `SESSION_SECRET_KEY`), not JWT. There is exactly one login path: SQLAdmin's own `/login`, backed by `admin_auth.py::AdminAuth` (a custom `AuthenticationBackend`). `authenticate_user()` runs `bcrypt.checkpw` against a dummy hash even on unknown usernames, to avoid timing-based user enumeration. Brute-force lockout (`web/login_security.py`): 5 failed attempts / 15 min window / 15 min lockout, tracked independently per-username **and** per-IP (stops both credential stuffing and username spraying), with a sanitised security log written for external tools (e.g. fail2ban) to tail. `web/auth.py::resolve_unit_ids()` is the single choke point for turning a session into an effective unit scope (org-admins get every unit in their org, resolved live, not cached).

### Integrations

- **Planning Center Online** (`integrations/planning_center.py`) — HTTP Basic auth PCO Personal Access Token client; People API (registrations, person/phone lookup) and Services API (service types, plans, team members) for serving reminders. Several PCO-specific quirks are handled explicitly in code comments (case-insensitive campus↔folder name matching, team-member status codes `U`/`C`/`D`).
- **WhatsApp — two separate clients, deliberately not unified**: `integrations/whatsapp.py` is async (httpx), used for transactional automation sends and runs on the event loop; `web/whatsapp_bulk.py` is sync (`requests`), used only by bulk campaigns which run on background threads/executors. Do not merge these — the seed doc states this split is intentional, not historical accident.
- **Webhooks** (`integrations/webhooks.py`) — PCO people-form webhook (HMAC-SHA256 verified per-unit secret, acks immediately then processes in `background_tasks` since PCO retries on timeout), Meta WhatsApp webhook (verify-token handshake + HMAC-verified account-update events).
- **Stitch** (`integrations/stitch.py`) — not an API client; pure formatting helpers for Stitch Money (SA payments provider) EFT references and payment-link WhatsApp buttons.
- **`clients.py`** — cached client registry (`get_whatsapp_client_for_number`, `get_pco_client`, keyed by number/unit id). Editing a credential in the admin UI requires an app restart to take effect — there is no cache invalidation.

### Core automation logic

`core/automation_engine.py` provides a generic `register(module_key, trigger_key, handler)` / `fire(trigger_key, org_id, payload)` extension point, gated on `is_enabled(org_id, module_key)` — core code should never import PCO internals directly, only fire/register through this or check `storage.modules.is_enabled()`. `scheduler.py` holds one shared APScheduler instance for both one-shot scheduled campaigns and recurring serving-reminder `CronTrigger` jobs; job state is in-memory only and rebuilt from the DB (`reload_pending_campaigns`/`reload_serving_rules`) on every startup, not persisted as a job store. `services/registration_poller.py`, `services/serving_reminder.py`, and `services/people_forms.py`/`form_response.py` implement the three PCO-triggered automations (registration confirmations, serving reminders, form-submission confirmations); each records every send outcome to the append-only `send_log` table and treats WhatsApp's 24h messaging-limit rejection (`MessagingLimitExceeded`) as a defer-and-retry case distinct from a hard failure.

### Admin panel

SQLAdmin, composed across `admin.py` (composition root — `setup_admin()`), `admin_models.py` (SQLAlchemy mirror of the schema, plus `EncryptedString` — a `TypeDecorator` that transparently Fernet-encrypts/decrypts credential columns via `autosend/crypto.py`), `admin_pages.py` (`BaseView` page shells rendering templates, actual data via JSON routers), `admin_views.py` (`ModelView`/`ScopedModelView` CRUD screens), `admin_org_pages.py` (consolidated organisation/PCO-settings pages), `admin_scoping.py` (tenant scoping — see Multi-tenancy above), `admin_widgets.py` (form widget helpers). Custom templates live in `web/sqladmin_theme/`.

### Web routers (`api/autosend/web/`)

JSON/HTML routers for account settings, campaigns CRUD, the Automations single-page UI (registration templates, PCO form mappings, serving-reminder rules), WhatsApp number/template lookups, the Embedded Signup OAuth onboarding flow, CSV/Excel/Google-Sheets recipient import, and public self-serve org signup (`signup_router.py` — the only way an org is created outside a superadmin/CLI action).

## Coding conventions carried over from the fork's design doc

- **Admin views exposing a secret column must pair two things**: `form_overrides = {"field": PasswordField}` (masks it on create/edit) *and* a `column_details_exclude_list` entry for the same field (hides it on the details view — a separate exclusion, since `PasswordField` alone doesn't cover details). See `admin_views.py` for the pattern on every credential field (`pco_webhook_secret`, `pco_token_secret`, `app_secret`, `webhook_verify_token`, `access_token`, `password_hash`). Follow this exactly when adding any new admin view over a table with a credential/secret column.
- **`storage/__init__.py` re-exports every name explicitly, by hand**, via named imports from each submodule — its own docstring states this is so existing call sites (`from autosend import storage; storage.get_x(...)`) don't break. It also carries a hand-kept `__all__` list mirroring those imports, kept in sync manually rather than derived from them. When adding a new function to any `storage/*.py` submodule that other code should call via `storage.`, add the explicit import **and** the `__all__` entry to `storage/__init__.py` — don't rely on wildcard imports, and don't assume updating one keeps the other in sync.
- **Open architectural question, not yet resolved**: the app runs on a single SQLite file for every organisation. As tenant count grows this may need revisiting (per-org DB files vs. a different engine) — there's no current plan to do so, but don't assume the single-file model is a settled decision if a change touches DB provisioning/scaling.

## Gotchas found in this codebase

- **Duplicate crypto modules**: `api/autosend/crypto.py` (uses `settings.token_encryption_key`, functions `encrypt_token`/`decrypt_token`) is the one actually imported everywhere (`admin_models.py`, `storage/units.py`). `api/autosend/core/crypto.py` is a different, incompatible implementation (reads `KRYX_FERNET_KEY` env var directly, functions `encrypt`/`decrypt`) that appears unused — don't assume it's live, and don't add new imports of it without first confirming which module is meant to be canonical.
- **`api/autosend/scripts/check_dependency_direction.py` is currently a no-op.** It scans `api/storage`, `api/core`, `api/app` for illegal imports into `integrations.*` — none of those paths exist (the real code is under `api/autosend/{storage,core,web}`), so `scan_dir.exists()` is always `False` and it silently prints "OK" without checking anything. If you rely on this as a layering guard, fix the paths first (and note the real composition root is `api/autosend/main.py`, not `api/app/main.py`).
- **`APP_NAME`/branding is mid-rename** across "Shofar Automation" (some code/log-path defaults), "Kryx" (`.env.example`, Docker image/volume names), and `autosend` (package name, DB filename). Don't assume one name is used consistently; check the specific file.
- Campaign/serving-reminder scheduler state is APScheduler's default **in-memory** job store — it is intentionally rebuilt from the DB on every startup rather than persisted, so don't add code that assumes a job survives a process restart without a reload call.

## Testing conventions

Tests use `TestClient` against the real app over real HTTP routes (not calling storage functions directly) — the stated goal in `conftest.py` is exercising the tenant boundary "the way a logged-in-but-unauthorized staff member actually could." Follow this pattern for new tenant-boundary tests rather than unit-testing storage functions in isolation.

`conftest.py` repoints `DATABASE_PATH` to a fresh temp file **before** importing anything under `autosend` — `admin_models.py` builds its SQLAlchemy engine at import time from the DB path, so there is no supported way to repoint it later; if you add a new module that builds a DB connection/engine at import time, it must respect the same ordering constraint. The `tenants` fixture seeds two fully independent, uuid-tagged organisations (`a`/`b`) each with a unit, number, PCO settings, staff user, and org-admin — reuse it for any new cross-tenant test rather than hand-rolling fixtures. All fixture users share one fixed password (`"correct-horse-battery-staple"`); log in via `login_as(client, username)`.

When adding a new SQLAdmin view (or any admin page) over tenant-scoped data, add a corresponding case to `test_cross_org_isolation.py`'s pattern: list excludes other org's rows, details/edit 404 on a guessed other-tenant pk, update/delete POSTs on a guessed pk are rejected, create-form dropdowns don't leak other orgs' names, superadmin sees everything.

## Git conventions

Commit messages are short, imperative, present-tense one-liners (e.g. "Add signup brute-force lockout and Sentry error monitoring", "Make username globally unique, not per-organisation"). Single `main` branch, no PR-based workflow visible in history — commit directly.

## Deployment workflow

**As of 17/08/2026, Kryx and the original single-org "Shofar Automation" project have split onto separate servers.** The original VPS (SSH alias `shofar-cloud`, `84.8.137.235`) keeps running the original Shofar Automation project only. Kryx now deploys to a new Oracle A1 server (IP `92.4.152.78`) — set up an SSH alias for it (e.g. `kryx-cloud`) in `~/.ssh/config` rather than reusing `shofar-cloud`. `push.sh`/`push_clear.sh`/`pull.sh` in this repo target the Kryx server's alias.

### Dev vs. production — everything runs on the VPS, this local checkout is not used

**As of 20/08/2026, all work has moved to the VPS and the local, on-device checkout referenced elsewhere in this file is no longer relevant.** There is no local development workflow anymore — not "deprecated but still an option," genuinely not used. All work, dev and production alike, happens directly against the two Docker containers running side by side on `kryx-cloud`: `kryx-dev` (development) and `kryx` (production). Do not edit this local copy expecting it to reach either environment, and do not treat it as reflecting current deployed state — always read/edit files over SSH on the server itself (e.g. via an SSH-connected Claude Code session).

**Before assuming you need to SSH anywhere, check whether this session is already running on `kryx-cloud`.** Some Claude Code sessions (including sandboxed/cloud ones) are launched with their working directory already on the `kryx-cloud` box itself, so a path like `/home/ubuntu/kryx-dev` in front of you may already *be* the real, live dev checkout, not a stale local mirror that needs syncing. Run `hostname` (expect `kryx-cloud`) before reaching for `ssh kryx-cloud` or narrating a "let me push this over SSH" step — if you're already on the server, just edit the files in place and restart the container directly; do not manufacture an unnecessary SSH hop or generate a fresh keypair when a plain `hostname` check would have shown you're already there.

`~/kryx/` is production (`kryx` container, `kryx-data` volume, port 8001, public hostname `kryx.co.za`/`www.kryx.co.za`). `~/kryx-dev/` is the development environment (`kryx-dev` container, `kryx-dev-data` volume, port 8002, public hostname `dev.kryx.co.za`) — same codebase, own image, own Docker volume (**a genuinely separate SQLite database, not a copy of production data**), own `.env` with independently generated `TOKEN_ENCRYPTION_KEY`/`SESSION_SECRET_KEY`/`ADMIN_API_KEY` and `ENVIRONMENT=development`, and its own log directory (`/var/log/kryx-dev`). Both hostnames are proxied by nginx (`/etc/nginx/sites-enabled/kryx` and `kryx-dev`) sitting behind Cloudflare (origin-only access enforced via `$is_cloudflare`), which forwards to the matching `127.0.0.1` port.

**Claude must default to working against `kryx-dev`, and must only touch the production `kryx` environment (`~/kryx/`, the `kryx` container, or the `kryx-data` volume) when the user explicitly asks for that.** If a task is ambiguous about which environment it's for, ask rather than assuming production.

#### Git — push-only backup, not the deployment mechanism

Both `~/kryx` and `~/kryx-dev` are git repos, but git here is **backup only**, not how code moves between environments:
- `~/kryx-dev` pushes to `speedster-pta/kryx_dev` (`main` branch). Deploy key `kryx-dev-deploy-key` on the server, via SSH host alias `github-kryx-dev`.
- `~/kryx` pushes to `speedster-pta/kryx` (`main` branch). Deploy key `kryx-prod-deploy-key` on the server, via SSH host alias `github-kryx-prod`.
- These are **two separate repos with no merging or syncing between them** — pushing to one never touches the other. Commit and push from whichever environment you just edited, purely so the change history is backed up off the VPS.
- `~/kryx-dev/docker-compose.yml` is marked `git update-index --skip-worktree` so `git pull`/`reset` there never clobbers its dev-specific container name/port/volume — if you ever need to intentionally edit it via git, run `git update-index --no-skip-worktree docker-compose.yml` first.

#### Promotion: kryx pulls from kryx-dev via rsync, not git

Once code on `kryx-dev` is tested, promote it to production by rsyncing **directly from `~/kryx-dev` to `~/kryx` on the server** — run `~/kryx-dev/promote_to_prod.sh` (or `promote_to_prod_clear.sh` for a `--delete` sync that makes prod exactly match dev) over SSH on `kryx-cloud`. This is a local rsync between the two directories on the same box, using the same `.rsync-exclude` list as before, now with `docker-compose.yml` added to it — each environment's compose file (`kryx` vs `kryx-dev` container name/port/volume) is never overwritten by a promote. `.env` and `data/` stay excluded too, so each environment's secrets and DB are untouched.

The old `push.sh`/`push_clear.sh`/`pull.sh`/`push_dev.sh`/`push_dev_clear.sh`/`pull_dev.sh` scripts (local machine ↔ server rsync) still exist in this repo but are obsolete now that no work happens on a local machine at all — don't use them.

Both environments run via their own `docker-compose.yml` in their own directory (`~/kryx/docker-compose.yml` for prod, `~/kryx-dev/docker-compose.yml` for dev): one container per environment built from `api/Dockerfile`, with `./api/autosend` **bind-mounted over** the image's copy — so promoting new source and restarting that environment's container picks up changes without a rebuild. Persistent state (SQLite DB, header images, backups) lives in each environment's own named volume (`kryx-data` for prod, `kryx-dev-data` for dev); each is published only on its own `127.0.0.1` port (8001 prod, 8002 dev), implying a reverse proxy (nginx) in front. `backup_db.py` runs via cron using SQLite's online backup API (`sqlite3.Connection.backup()`, safe against a concurrently-writing app) plus a `PRAGMA integrity_check` before trusting the backup — this is set up for production; the dev DB is disposable and not expected to need backups.

`.env` is excluded from rsync/promotion by design (see `.rsync-exclude`) — each environment's `.env` lives only on the server and is never touched by a promote. `TOKEN_ENCRYPTION_KEY`/`SESSION_SECRET_KEY`/`ADMIN_API_KEY` can either be carried over from an existing `.env` or regenerated fresh on a new environment — regenerating is safe as long as there's no encrypted data in that environment's DB yet to be locked out of (this is why dev's secrets were freshly generated rather than copied from prod).

When changing code that will be deployed this way, remember: **container restart is required** for a source change to take effect (bind mount, not a rebuild loop) and separately **an app restart is required** for a changed credential/token in the admin UI to be picked up by `clients.py`'s cache. Restart the environment you actually changed — restarting prod when you meant to test in dev (or vice versa) affects the wrong environment's live jobs/scheduler state.
