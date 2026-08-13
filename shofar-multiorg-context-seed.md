# Context Seed: Multi-Organisation WhatsApp Campaign Platform (Shofar Online Fork)

## 1. Purpose of this document

This is a context seed for a **new fork** of the existing `whatsapp-manager` project ("Shofar Online"), repositioned from a single-organisation, church-specific tool into a **multi-tenant, multi-organisation WhatsApp campaign platform**. The parent project is a working, deployed system for one church organisation with multiple congregations. This fork generalises it so unrelated organisations can each onboard independently, with the WhatsApp campaign sender as the core universal product and Planning Center Online (PCO) automations as an **optional, opt-in add-on module** rather than a baked-in assumption.

---

## 2. Parent project — what exists today (baseline to fork from)

**Product today:** Shofar Online — a self-hosted WhatsApp Business automation platform serving *one* church organisation with multiple congregations (Pretoria, Support Centre, Potchefstroom, Durbanville, Boston, Meta).

**Tenancy model today:** Single-organisation, multi-congregation. There is no "organisation" concept above congregation — congregation is effectively the top-level tenant boundary, and PCO settings are split between a **singleton** `pco_organization_settings` table (org-wide token) and per-congregation fields (`pco_campus_id`, `pco_webhook_secret`).

**Core capabilities today:**
- Bulk WhatsApp campaigns (CSV/Excel/Google Sheets/OneDrive recipient import)
- Transactional automation: event registration confirmations (free/paid), PCO form response confirmations, PCO Services serving reminders
- Meta Embedded Signup onboarding for connecting WhatsApp numbers
- Multi-congregation staff access with row-level scoping via SQLAdmin

**Tech stack:** FastAPI, SQLite (raw `sqlite3` for storage; SQLAlchemy ORM only for SQLAdmin), SQLAdmin 0.29–0.30, APScheduler (`AsyncIOScheduler`), Jinja2, vanilla JS, Tailwind (CDN), Docker Compose, nginx, Ubuntu VPS.

**Architecture:**
- `storage/` package: `schema.py`, `_db.py`, `dedup.py`, `congregations.py`, `users.py`, `campaigns.py`, `auth_lockout.py`, `limits.py`, `serving.py` — `__init__.py` requires **explicit named re-exports**, `__all__` alone is insufficient
- Admin split: `admin_models.py`, `admin_auth.py`, `admin_scoping.py`, `admin_views.py`, `admin_pages.py`, `admin_widgets.py`, `admin.py` (composition root)
- Web routers: `campaigns_router.py`, `campaign_runner.py`, `numbers_router.py`, `automations_router.py`, `templates_router.py`, `onboarding_router.py`, `account_router.py`, `webhooks.py`
- Credentials Fernet-encrypted at rest (`crypto.py`); PCO/Meta secrets in singleton tables; SQLite migrations use rename→recreate→copy→drop, idempotent, PRAGMA-guarded, run on every startup
- Session scoping via `current_scope` ContextVar; `ScopedModelView` for congregation-scoped CRUD
- Two independent WhatsApp send paths kept deliberately separate: synchronous `requests` + ThreadPoolExecutor for campaigns (`whatsapp_bulk.py`), async `WhatsAppClient` for transactional sends

**Deployment reality:** Docker Compose on a single Ubuntu VPS, bind-mounted source (no rebuild needed for Python/template changes), nightly encrypted DB backup to Google Drive via rclone.

This baseline is proven in production and the fork should treat its patterns (migration discipline, storage module re-export rule, Fernet encryption, incremental file-by-file delivery) as inherited defaults unless a reason emerges to change them.

---

## 3. Why fork: the product repositioning

The parent system was built PCO-first, for one organisation. Real-world interest exists from **other organisations that want WhatsApp campaign sending but have no relationship with Planning Center at all** (e.g. businesses, NGOs, other church networks using different ChMS or none). Bolting PCO assumptions into the core (singleton org-level PCO settings, PCO-driven automations as first-class citizens) makes it awkward to sell/deploy the campaign sender alone.

**New product framing:**
- **Core product (required, all tenants):** WhatsApp Campaign Sender — bulk messaging, template management, recipient import, number management, delivery/throttle handling, staff/user management, multi-tenant admin.
- **Optional module (opt-in per organisation):** Planning Center Online integration — event registration confirmations, form response confirmations, serving reminders, and any future PCO-driven automation. An organisation with the module disabled should see no PCO UI, no PCO scheduler jobs, no PCO credential fields, and pay no PCO-related overhead.
- **Future optional modules (not yet scoped):** other ChMS integrations, other automation triggers — the module boundary should be designed generically enough to admit these later without another architectural rewrite.

---

## 4. Target multi-tenancy model

Introduce an explicit **Organisation** layer above Congregation:

```
Organisation (new top-level tenant)
 ├── org-level settings: name, slug, branding(?), enabled_modules[]
 ├── WhatsApp number(s) — org can have one or many, same as today
 ├── Staff users — scoped to organisation, optionally further scoped to congregation(s)
 ├── Campaigns, templates, recipient lists — scoped to organisation
 └── Congregation (existing concept, now a child of Organisation, OPTIONAL)
      ├── only meaningful if org uses PCO module (campus mapping) or wants
      │   internal segmentation of contacts/staff
      └── PCO module settings, if enabled: pco_campus_id, pco_webhook_secret, etc.
```

Key design questions to resolve early in the fork (flag as open decisions, not yet answered):
1. **"Congregation" is renamed to "Unit"**, a neutral label, since non-church organisations won't use "Congregation" — congregation is the church-specific instance of the concept. (Resolved: Unit.)
2. **Where does `pco_organization_settings` move?** Today it's a global singleton; in multi-org it must become **one row per Organisation**, gated by whether that org has the PCO module enabled.
3. **Module enablement storage:** a simple `organisation_modules` table (`org_id`, `module_key`, `enabled_at`) is the likely lightest-weight approach — avoids schema-per-module flags scattered across the Organisation table as new modules are added later.
4. **Row-level scoping:** the existing `current_scope` ContextVar / `ScopedModelView` pattern generalises naturally — add an outer `organisation_id` scope above the existing congregation scope, so staff are always scoped to exactly one organisation, optionally further scoped within it.
5. **Cross-org data isolation:** must be airtight at the storage-module level (raw SQL, not just admin UI filtering) — every campaigns/templates/numbers/users query needs an `organisation_id` predicate, not just a UI-layer scope.
6. **Super-admin role:** need a platform-level admin role that spans all organisations (for onboarding new orgs, support, billing) distinct from per-org staff roles.
7. **Billing/plan tier hook (if applicable):** even if not built in v1, the module-enablement table doubles conveniently as a future plan/entitlement gate — worth designing with that in mind even if billing itself is out of scope initially.

---

## 5. Core product: Campaign Sender (must work with zero PCO dependency)

Everything under this heading should function for an organisation with **no PCO module enabled at all**:

- Meta Embedded Signup onboarding to connect a WhatsApp number — already org-agnostic in the parent, should port cleanly
- Template management (create/edit within Meta's constraints, preview) — already generic
- Bulk campaign sending: CSV/Excel/Google Sheets/OneDrive import, recipient dedup, throttle/24h-window handling, ThreadPoolExecutor concurrency at the send boundary, `send_concurrency` per number (default 10, max 20 per Meta's coexistence ceiling)
- Staff/user management and auth (bcrypt, lockout) — generalise from congregation-scoped to organisation-scoped
- SQLAdmin-based back office — generalise `ScopedModelView` to the new org/unit scope hierarchy

None of this currently depends on PCO in the parent codebase's data model for sending itself — PCO involvement today is entirely on the **automation trigger side** (registrations, form responses, serving reminders), which is good: it suggests the core sender and the PCO module are already fairly decoupled in practice, and the main fork work is tenancy (org layer) plus formalising the module boundary rather than untangling deeply intertwined logic.

---

## 6. Optional module: PCO integration + automations

Everything currently driven by Planning Center becomes conditional on `organisation_modules.pco.enabled`:

- PCO org-level token (`pco_token_id` / `pco_token_secret`) — becomes per-organisation, not global singleton
- Per-unit (ex-congregation) `pco_campus_id`, `pco_webhook_secret`
- Automations: free/paid event registration confirmations, PCO form response confirmations, serving reminders (PCO Services)
- Deferred serving reminder retry: 15-minute `IntervalTrigger` APScheduler job (`recheck_deferred_serving_reminders`) — should only be scheduled for organisations with the module enabled, not run unconditionally
- Service type cache keyed by `(unit_id, cached_date)`, refreshed daily — org-scoped
- Webhook routes (`/webhooks/planning-center/...`) — must resolve organisation from the URL/slug before touching any PCO logic, and should 404 or no-op cleanly for orgs without the module rather than assuming PCO configuration exists

**Known PCO-specific quirks to carry forward as institutional knowledge:**
- Campus-to-folder matching is case-insensitive name comparison (PCO's `campus` relationship on folders is inconsistently populated)
- Template edits use `POST /{template_id}` with numeric ID; restricted to APPROVED/REJECTED/PAUSED status; name/language immutable post-creation
- Variables can't appear at the very start/end of body/header text (`error_subcode 2388299`)
- One Meta webhook callback URL per WABA — matters if an org shares a number with another inbox tool (e.g. Chatwoot)
- A unit with `active=1` but no webhook secret set must fail safely (currently 404s before signature verification) — this pattern should generalise to "module disabled ⇒ safe no-op," not just "misconfigured ⇒ 404"

---

## 7. Inherited engineering principles (carry forward unless explicitly revisited)

- Work from actual uploaded/current file content only — never reconstruct schema or function signatures from memory
- Full replacement files for delivery of non-trivial changes; targeted edits for small ones
- Validate every change before delivery: `py_compile`/`ast.parse` (Python), `node --check` (JS), Jinja2 `Environment().parse()`, `init_db()` run twice against fresh SQLite to confirm migration idempotency
- SQLite migrations: rename→recreate→copy→drop pattern only, never `ALTER TABLE DROP COLUMN`; idempotent, PRAGMA-guarded, run on every startup
- `storage/__init__.py` needs explicit named re-exports per function — this rule becomes more important, not less, as more storage modules are added for org-scoping
- Multi-file features that share a dependency must deploy and restart together
- Fernet encryption for all credentials at rest, `column_details_exclude_list` on any admin view exposing them, `form_overrides = PasswordField` for edit/create only (Details view needs separate exclusion)
- Two-send-path separation (sync/ThreadPoolExecutor for bulk, async for transactional) should be preserved — it exists because the data models are genuinely incompatible, not from historical accident

---

## 8. Open questions to resolve before/during the fork (not yet decided)

1. Terminology: what replaces "Congregation" as the neutral sub-org grouping label?
2. Does every organisation get exactly one WhatsApp number, or can orgs have multiple numbers from day one (parent already supports multiple numbers, so likely inherited as-is)?
3. Onboarding flow for a *new organisation* — self-serve signup vs. platform-admin-provisioned? Parent has no equivalent (it's single-org, provisioned by deployment).
4. How is the module system exposed to org admins — a settings toggle they can flip themselves once PCO is set up, or something platform-admin-gated?
5. Should PCO module settings live in the same DB with an org-scoping column, or should modules be architected as more fully pluggable (e.g. separate tables/migrations per module) to make future modules (non-PCO ChMS, other automation sources) cheaper to add?
6. Any multi-tenancy at the infrastructure level (separate DB per org vs. shared DB with org_id everywhere) — parent uses a single SQLite file; confirm this scales acceptably for the expected number of tenants or whether per-org DB files / a different DB engine should be considered now rather than later.

---

## 9. Suggested first milestones for the fork

1. Introduce `organisations` table + `organisation_modules` table; migrate existing single-org data into one Organisation row with PCO module enabled.
2. Generalise scoping: add `organisation_id` to the `current_scope` ContextVar and `ScopedModelView`, above the existing congregation-level scope.
3. Move `pco_organization_settings` from singleton to per-organisation row, gated by module enablement.
4. Audit every storage-module query (`campaigns.py`, `users.py`, `congregations.py`, etc.) for organisation-level isolation, not just congregation-level.
5. Gate PCO scheduler jobs and webhook routes on module enablement, with safe no-op behaviour for disabled orgs.
6. Rename/relabel the congregation concept if the neutral-terminology question (§8.1) is resolved before this point — better to do it during the tenancy refactor than after.
