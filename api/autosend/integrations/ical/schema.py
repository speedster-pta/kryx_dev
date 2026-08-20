"""
integrations/ical/schema.py

iCalendar (.ics) generation: ical_events (the calendar-worthy facts about
one occurrence - title, time, location), ical_links (one opaque,
cryptographically random bearer token per recipient/access-grant), and
ical_link_events (the many-to-many join between them - one link can
bundle several events into a single .ics, e.g. a volunteer's whole
month of scheduled services combined into one "Add to calendar" tap).
Gated behind the "ical" module (see storage/modules.py) - core code
should never assume every org has this enabled.

Deliberately not tied to PCO specifically, even though the first caller
(services/serving_reminder.py) is PCO-driven - any future trigger source
(email_wa, sme_metrics, a manual admin action) can create rows here the
same way. That's why this lives in its own integrations/ical/ package
rather than integrations/pco/, and why it's initialised independently in
core/db_init.py rather than chained after init_pco_schema().

Must be called after storage.schema.init_core_schema() on the same
connection, since ical_events.unit_id references units(id).

Three tables, not one, because event and link have a genuine
many-to-many relationship: a single appointment is a link bundling
exactly one event, while a shared occurrence (several attendees) can
still give each attendee their own revocable link to the same event, and
a monthly digest gives one person one link bundling several distinct
events. Modelling it as event <-> link_events <-> link covers all three
shapes without a visibility flag or near-duplicate tables per shape.

Fresh project, no existing database to evolve - see storage/schema.py's
docstring for why there's no migration scaffolding here.
"""

from __future__ import annotations


def init_ical_schema(conn) -> None:
    _create_ical_events(conn)
    _create_ical_links(conn)
    _create_ical_link_events(conn)


def _create_ical_events(conn) -> None:
    # uid: the RFC 5545 UID - generated once at creation and never
    # changed, so a calendar app that re-imports the same event after a
    # reschedule (same uid, higher sequence) updates its existing entry
    # instead of creating a duplicate. sequence bumps on every edit to
    # date/time/location/status per RFC 5545 - see integrations/ical/
    # builder.py for how it's rendered.
    #
    # title/description are deliberately the ONLY text that ever reaches
    # the .ics file or a WhatsApp message - callers (e.g.
    # serving_reminder.py, a future email_wa provider) are responsible for
    # passing sanitised, calendar-safe text here (data-minimisation
    # principle: "calendar events contain scheduling information, not
    # sensitive information" - e.g. "Appointment - Jane's OT Practice",
    # never a clinical reason). There is no separate "internal notes"
    # column on this table on purpose - if a caller has sensitive detail,
    # it stays in that caller's own source data, never copied here.
    #
    # source_system + source_external_id: the upstream trigger's own
    # stable identifier for "this is the same occurrence as before", used
    # to decide update-in-place (bump sequence, keep uid/token) vs a brand
    # new row. source_external_id is nullable - a source with no stable ID
    # (e.g. a one-off inbound email with nothing to correlate against)
    # just leaves it NULL, and SQLite treats every NULL as distinct under
    # a UNIQUE constraint, so such sources naturally always insert a new
    # row rather than colliding with each other.
    #
    # expires_at is mandatory (NOT NULL) - every event has a hard cutoff
    # past which the public endpoint stops serving it, regardless of any
    # individual link's own expiry.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ical_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id),
            uid TEXT NOT NULL UNIQUE,
            sequence INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            description TEXT,
            location TEXT,
            starts_at TEXT NOT NULL,
            ends_at TEXT,
            status TEXT NOT NULL DEFAULT 'confirmed'
                CHECK (status IN ('confirmed', 'cancelled')),
            source_system TEXT NOT NULL,
            source_external_id TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (unit_id, source_system, source_external_id)
        )
        """
    )


def _create_ical_links(conn) -> None:
    # token: secrets.token_urlsafe(32) (256 bits) - generated server-side
    # only (storage/ical.py), never derived from anything guessable. This
    # is the entire authorization boundary for the public /ical/{token}.ics
    # endpoint - there is no session/org auth on that route by design, so
    # token entropy and expiry carry all the security weight.
    #
    # recipient_phone is the only identity guaranteed available across
    # every upstream source (PCO, inbound-email providers, ...) - not a
    # FK to a person/contact table, since no such table exists uniformly
    # across sources. Nullable: a purely "shared" link handed out with no
    # specific recipient in mind (e.g. one link mass-sent to a whole
    # group) leaves this NULL: SQLite's UNIQUE constraint treats every
    # NULL as distinct, so any number of such anonymous links can point
    # at the same event without colliding with the per-recipient
    # uniqueness below.
    #
    # link_key: caller-defined identity for "is this the same link as
    # before" - NOT tied 1:1 to a single event, because one link can
    # bundle several events (see ical_link_events below). A single
    # appointment's caller just uses something like f"appt:{event_uid}"
    # (a bundle of one); a monthly serving-reminder combines several PCO
    # plans into one link for one person, keyed by the exact set of plan
    # ids in that run (see services/serving_reminder.py) - not a
    # calendar-month label, since the actual run's plan set is already
    # the natural, self-describing identity and needs no separate concept
    # of "period".
    #
    # UNIQUE(link_key, recipient_phone) makes "get or create this
    # person's link for this key" idempotent - re-running an automation
    # that already sent reminders reuses the same token rather than
    # minting a new one, so a person's existing WhatsApp thread keeps
    # working.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ical_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_key TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            recipient_phone TEXT,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            accessed_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (link_key, recipient_phone)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ical_links_token ON ical_links(token)")


def _create_ical_link_events(conn) -> None:
    # Many-to-many: one link can serve several events in a single .ics
    # (e.g. a volunteer's whole month of scheduled services combined into
    # one "Add to calendar" tap - see services/serving_reminder.py), and
    # in principle one event could be attached to more than one link
    # (e.g. a shared occurrence with several independently-revocable
    # per-attendee links, each still just pointing at the one event).
    # INSERT OR IGNORE via the primary key makes attaching an event to a
    # link idempotent - reruns don't error on re-attaching the same pair.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ical_link_events (
            ical_link_id INTEGER NOT NULL REFERENCES ical_links(id),
            ical_event_id INTEGER NOT NULL REFERENCES ical_events(id),
            PRIMARY KEY (ical_link_id, ical_event_id)
        )
        """
    )
