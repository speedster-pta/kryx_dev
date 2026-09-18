"""storage/knowledge_base.py

The AI Assistant's Knowledge Base: entries the RAG retrieval step
searches over, plus the CRUD an org's admin UI needs.

Retrieval (search_active_entries) is a plain keyword-overlap ranking done
in Python over SQLite rows - no vector DB, no embeddings. Appropriate at
the scale a single organisation's knowledge base runs to (tens to low
hundreds of entries), and it means no separate embedding pipeline/cost to
keep in sync with edits.

Scoping is always org_id first: a nullable unit_id lets an org store
"org-wide" entries (unit_id IS NULL, shared across every one of that
org's units/numbers) alongside unit-specific ones, but a NULL-unit entry
is still hard-scoped to its own org_id - see the schema.py docstring on
knowledge_base_entries for why this must never become a platform-wide
"global" concept the way the single-tenant parent project's did.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ._db import _connect

_ENTRY_COLUMNS = [
    "id", "org_id", "unit_id", "title", "content", "source_type", "source_ref",
    "chunk_index", "is_active", "last_refreshed_at", "created_at", "updated_at", "document_title",
]

# Standard English stopwords - excluded from retrieval scoring so common
# words (e.g. "more", "find", "want") don't score a hit against nearly
# every entry and drown out the words that actually distinguish one
# knowledge-base entry from another.
_STOPWORDS = frozenset("""
a about above after again against all am an and any are aren't as at be
because been before being below between both but by can't cannot could
couldn't did didn't do does doesn't doing don't down during each few for
from further had hadn't has hasn't have haven't having he he'd he'll
he's her here here's hers herself him himself his how how's i i'd i'll
i'm i've if in into is isn't it it's its itself let's me more most
mustn't my myself no nor not of off on once only or other ought our ours
ourselves out over own same shan't she she'd she'll she's should
shouldn't so some such than that that's the their theirs them themselves
then there there's these they they'd they'll they're they've this those
through to too under until up very was wasn't we we'd we'll we're we've
were weren't what what's when when's where where's which while who
who's whom why why's with won't would wouldn't you you'd you'll you're
you've your yours yourself yourselves please just also like want need
know get got make made find want
""".split())

_WORD_RE = re.compile(r"[a-z0-9']+")


def _normalize_word(word: str) -> str:
    """Conservative trailing-plural stripping ("weekends" -> "weekend")
    so a query and an entry using different but equivalent forms of the
    same word still match. Deliberately narrow: only strips a single
    trailing "s" on words longer than 3 characters, and never touches a
    trailing "ss" (e.g. "class"), to avoid mangling short words or
    genuinely-different word pairs."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokenize(text: str) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    return {_normalize_word(w) for w in words if len(w) > 2 and w not in _STOPWORDS}


def _row_to_entry(row) -> dict:
    return dict(zip(_ENTRY_COLUMNS, row))


def list_entries(org_id: int, unit_id: int | None = None, include_inactive: bool = False) -> list[dict]:
    """unit_id=None lists every entry visible to the org (its own units'
    entries plus org-wide ones); pass an explicit unit_id to filter down
    to just that unit's own entries plus org-wide ones - same "OR unit_id
    IS NULL" relaxation search_active_entries uses, just without the
    active-only/scoring parts."""
    query = "SELECT {} FROM knowledge_base_entries WHERE org_id = ?".format(", ".join(_ENTRY_COLUMNS))
    params: list = [org_id]
    if unit_id is not None:
        query += " AND (unit_id = ? OR unit_id IS NULL)"
        params.append(unit_id)
    if not include_inactive:
        query += " AND is_active = 1"
    query += " ORDER BY updated_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_entry(r) for r in rows]


def get_entry(entry_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_ENTRY_COLUMNS)} FROM knowledge_base_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return _row_to_entry(row) if row else None


def create_entry(
    org_id: int, unit_id: int | None, title: str, content: str, *,
    source_type: str = "manual", source_ref: str | None = None, chunk_index: int = 0,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO knowledge_base_entries
                (org_id, unit_id, title, content, source_type, source_ref,
                 chunk_index, is_active, last_refreshed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (org_id, unit_id, title, content, source_type, source_ref, chunk_index, now, now, now),
        )
        conn.commit()
        return cur.lastrowid


def update_entry(entry_id: int, *, title: str, content: str, is_active: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE knowledge_base_entries
            SET title = ?, content = ?, is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, content, int(is_active), now, entry_id),
        )
        conn.commit()


def delete_entry(entry_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM knowledge_base_entries WHERE id = ?", (entry_id,))
        conn.commit()


def replace_source_entries(
    org_id: int, unit_id: int | None, source_type: str, source_ref: str, chunks: list[dict],
    *, document_title: str | None = None,
) -> list[int]:
    """Delete-then-reinsert every entry previously ingested from this
    source_ref (a URL or filename), keyed within this org/unit scope -
    used by URL/PDF (re-)ingestion so re-running a scrape doesn't
    accumulate stale duplicate chunks alongside the fresh ones.
    `chunks` is a list of {"title": ..., "content": ...} dicts, in order.

    `document_title` (the source document/page's own title, not any one
    chunk's own `title`/question) is stamped onto every inserted row so
    get_source_document_title() can recover it on a later re-scrape/
    re-upload that doesn't pass a fresh one in."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        if unit_id is None:
            conn.execute(
                "DELETE FROM knowledge_base_entries "
                "WHERE org_id = ? AND unit_id IS NULL AND source_type = ? AND source_ref = ?",
                (org_id, source_type, source_ref),
            )
        else:
            conn.execute(
                "DELETE FROM knowledge_base_entries "
                "WHERE org_id = ? AND unit_id = ? AND source_type = ? AND source_ref = ?",
                (org_id, unit_id, source_type, source_ref),
            )
        new_ids = []
        for index, chunk in enumerate(chunks):
            cur = conn.execute(
                """
                INSERT INTO knowledge_base_entries
                    (org_id, unit_id, title, content, source_type, source_ref,
                     chunk_index, is_active, last_refreshed_at, created_at, updated_at, document_title)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (org_id, unit_id, chunk["title"], chunk["content"], source_type, source_ref,
                 index, now, now, now, document_title),
            )
            new_ids.append(cur.lastrowid)
        conn.commit()
        return new_ids


def get_source_document_title(org_id: int, unit_id: int | None, source_type: str, source_ref: str) -> str | None:
    """The document_title last stamped on this source's chunks by
    replace_source_entries, if any - lets scrape_url/ingest_pdf fall back
    to a staff member's previously-chosen title on a re-scrape/re-upload
    that doesn't specify a new one, rather than reverting to whatever the
    page's <title>/filename happens to be."""
    with _connect() as conn:
        if unit_id is None:
            row = conn.execute(
                "SELECT document_title FROM knowledge_base_entries "
                "WHERE org_id = ? AND unit_id IS NULL AND source_type = ? AND source_ref = ? LIMIT 1",
                (org_id, source_type, source_ref),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT document_title FROM knowledge_base_entries "
                "WHERE org_id = ? AND unit_id = ? AND source_type = ? AND source_ref = ? LIMIT 1",
                (org_id, unit_id, source_type, source_ref),
            ).fetchone()
        return row[0] if row and row[0] else None


def search_active_entries(org_id: int, unit_id: int | None, query: str, limit: int = 5) -> list[dict]:
    """Ranks every active entry visible to (org_id, unit_id) - this org's
    own org-wide entries plus this specific unit's own entries - by
    keyword overlap with `query`, returning the top `limit`.

    Scoring: distinct query words appearing as whole words (word-boundary
    matched, not substring) in title+content. Ties broken by density
    (score / entry word count, favouring short/specific entries over long
    generic ones) then by id descending (a newer, more specific entry
    wins a remaining tie over an older generic one)."""
    query_words = _tokenize(query)
    if not query_words:
        return []

    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_ENTRY_COLUMNS)} FROM knowledge_base_entries "
            "WHERE org_id = ? AND (unit_id = ? OR unit_id IS NULL) AND is_active = 1",
            (org_id, unit_id),
        ).fetchall()

    scored = []
    for row in rows:
        entry = _row_to_entry(row)
        haystack_words = _tokenize(f"{entry['title']} {entry['content']}")
        if not haystack_words:
            continue
        score = len(query_words & haystack_words)
        if score == 0:
            continue
        density = score / len(haystack_words)
        scored.append((score, density, entry["id"], entry))

    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [entry for _score, _density, _id, entry in scored[:limit]]
