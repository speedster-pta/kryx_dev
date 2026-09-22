"""Knowledge Base ingestion for the AI Assistant module.

Three input shapes, all funnelled through the same AI "FAQ-ification"
pass (_generate_faq_pairs) except manual entry (already clean, no AI pass
needed):
  - save_manual_qa()  - a staff-typed Q&A pair, stored verbatim.
  - scrape_url()      - httpx GET + BeautifulSoup cleanup -> FAQ pass.
  - ingest_pdf()      - pypdf text extraction -> FAQ pass.

Not implemented here (scoped out of this pass): a headless-browser
fallback for JS-rendered pages where the static httpx fetch yields too
little text. That needs a Playwright/Chromium install with OS-level
dependencies baked into the Docker image, which is a bigger, separate
change to api/Dockerfile - scrape_url() below only does the static fetch.
"""
from __future__ import annotations

import io

import httpx
import pydantic
from bs4 import BeautifulSoup
from pydantic import BaseModel
from pypdf import PdfReader

from autosend import clients, storage
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; KryxBot/1.0; +https://kryx.co.za)"
_MAX_PDF_BYTES = 15 * 1024 * 1024
_MAX_PDF_PAGES = 200
_MAX_INGEST_CHARS = 60_000
_MIN_TEXT_CHARS = 50

_FAQ_SYSTEM_PROMPT = """You turn raw text scraped from a website or PDF into a set of clear,
self-contained FAQ-style question/answer pairs for a WhatsApp AI assistant's
knowledge base.

Rules:
- Each answer must be understandable on its own, with no reference to
  "the page above" or similar context the reader won't have.
- Prefer several short, specific pairs over one long generic pair.
- Only include information actually present in the text - never invent
  facts, dates, prices, or contact details.
- Skip navigation boilerplate, cookie notices, and anything that isn't a
  real answerable fact about the organisation.
- Write answers in a warm, conversational tone suitable for a WhatsApp reply."""


class IngestError(Exception):
    """Raised for a clean, user-facing ingestion failure (bad input,
    AI ingestion settings not configured, etc) - callers should surface
    str(exc) directly rather than a generic 500."""


class _FAQPair(BaseModel):
    question: str
    answer: str


class _FAQPairs(BaseModel):
    pairs: list[_FAQPair]


def _clean_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form", "iframe"]):
        tag.decompose()
    for el in soup.find_all(attrs={"aria-hidden": "true"}):
        el.decompose()
    for el in soup.select('[style*="display:none"], [style*="display: none"]'):
        el.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


async def _generate_faq_pairs(
    text: str, title: str, *, org_id: int, unit_id: int | None, source_type: str, source_ref: str | None,
) -> list[dict]:
    settings = storage.get_ai_ingestion_settings()
    model = settings.get("model") if settings else None
    if not model:
        raise IngestError(
            "AI ingestion settings aren't configured yet - a superadmin needs to select a "
            "model under the AI Credentials page's Ingestion tab first."
        )

    # The Anthropic key itself is the one shared platform-wide credential
    # (ai_credentials.api_key, see clients.get_anthropic_client()'s
    # docstring) - not a second copy configured here.
    try:
        client = clients.get_anthropic_client()
    except ValueError as exc:
        raise IngestError(str(exc)) from exc
    try:
        response = await client.messages.parse(
            model=model,
            # A large source document can legitimately produce a long list
            # of FAQ pairs; too low a ceiling here truncates the model's
            # JSON output mid-string, which surfaces as the
            # pydantic.ValidationError caught below. Kept well under the
            # Anthropic SDK's own ~21333 "expected_time > 10 minutes" cutoff
            # for a single non-streaming call (an uncatchable ValueError,
            # not an anthropic.APIError) - 32000 previously took down every
            # PDF/URL ingestion in production on the sibling shofar-cloud
            # project.
            max_tokens=16000,
            system=_FAQ_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Document title: {title}\n\nSource text:\n{text[:_MAX_INGEST_CHARS]}",
            }],
            output_format=_FAQPairs,
        )
    except pydantic.ValidationError as exc:
        # The model's JSON output didn't parse - most commonly cut off
        # mid-string because the document produced more FAQ content than
        # max_tokens above allows. Without this, the raw ValidationError
        # propagated uncaught past the router's `except IngestError`
        # handling and 500'd the request instead of showing the staff
        # member a clean message.
        raise IngestError(
            "The AI ingestion pass produced an incomplete response, likely because this "
            "document generated more FAQ content than a single pass can handle. Try "
            "splitting it into smaller documents."
        ) from exc

    output = response.parsed_output
    if output is None or not output.pairs:
        raise IngestError("The AI couldn't extract any usable FAQ pairs from this content.")

    storage.record_ai_ingestion(
        org_id=org_id, unit_id=unit_id, source_type=source_type, source_ref=source_ref,
        prompt_tokens=response.usage.input_tokens, completion_tokens=response.usage.output_tokens, model=model,
    )
    return [{"title": p.question, "content": p.answer} for p in output.pairs]


def save_manual_qa(org_id: int, unit_id: int | None, title: str, content: str) -> int:
    """A staff-typed Q&A pair, stored verbatim - already clean, so this
    skips the AI FAQ-ification pass entirely."""
    return storage.create_knowledge_base_entry(org_id, unit_id, title, content, source_type="manual")


async def scrape_url(org_id: int, unit_id: int | None, url: str, title: str | None = None) -> list[int]:
    """`title` is an optional staff-supplied document title, given to the
    AI extraction pass as context and stamped onto the resulting entries
    (storage.get_knowledge_base_source_document_title). Left blank, a
    re-scrape of a URL that already has one (whether previously set
    manually or scraped) keeps that title rather than reverting to
    whatever the page's <title> happens to be right now; only a URL with
    no prior title at all falls back to the page's <title>, then the URL
    itself."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": _USER_AGENT}) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise IngestError(f"Couldn't fetch {url}: {exc}") from exc

    text = _clean_html_to_text(response.text)
    if len(text) < _MIN_TEXT_CHARS:
        raise IngestError(
            "This page had no usable text content to ingest (it may be a JS-rendered "
            "page this scraper can't read - try pasting the content manually instead)."
        )

    resolved_title = (title or "").strip()
    if not resolved_title:
        resolved_title = storage.get_knowledge_base_source_document_title(org_id, unit_id, "url", url) or ""
    if not resolved_title:
        page_soup = BeautifulSoup(response.text, "html.parser")
        resolved_title = page_soup.title.get_text(strip=True) if page_soup.title else ""
    resolved_title = resolved_title or url

    pairs = await _generate_faq_pairs(
        text, resolved_title, org_id=org_id, unit_id=unit_id, source_type="url", source_ref=url,
    )
    return storage.replace_knowledge_base_source_entries(
        org_id, unit_id, "url", url, pairs, document_title=resolved_title,
    )


def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception as exc:
        raise IngestError(f"Couldn't read this PDF: {exc}") from exc
    if len(reader.pages) > _MAX_PDF_PAGES:
        raise IngestError(f"PDF has too many pages (max {_MAX_PDF_PAGES}).")
    return "\n".join(page.extract_text() or "" for page in reader.pages)


async def ingest_pdf(
    org_id: int, unit_id: int | None, filename: str, file_bytes: bytes, title: str | None = None,
) -> list[int]:
    """`title` is an optional staff-supplied document title - same
    "left blank keeps the prior title" re-ingest behaviour as scrape_url's,
    see its docstring. Only a filename with no prior title at all falls
    back to the raw filename itself."""
    if len(file_bytes) > _MAX_PDF_BYTES:
        raise IngestError(f"PDF is too large (max {_MAX_PDF_BYTES // (1024 * 1024)}MB).")

    text = _extract_pdf_text(file_bytes)
    if len(text.strip()) < _MIN_TEXT_CHARS:
        raise IngestError("Couldn't extract any usable text from this PDF (it may be scanned images).")

    resolved_title = (title or "").strip()
    if not resolved_title:
        resolved_title = storage.get_knowledge_base_source_document_title(org_id, unit_id, "pdf", filename) or ""
    resolved_title = resolved_title or filename

    pairs = await _generate_faq_pairs(
        text, resolved_title, org_id=org_id, unit_id=unit_id, source_type="pdf", source_ref=filename,
    )
    return storage.replace_knowledge_base_source_entries(
        org_id, unit_id, "pdf", filename, pairs, document_title=resolved_title,
    )
