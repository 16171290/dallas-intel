"""
re:SearchTX probate-case ingestion.

Phase 8 PR 4. Self-contained API client for Texas's centralized court
records portal at research.txcourts.gov. Used by ``main.py`` (stage 2.5,
wired in PR 5) to fetch newly-filed probate cases in Dallas County.

Why a separate module:
    Probate filings are the third public source of distressed-property
    signal in dallas-intel (after foreclosure PDFs and publicsearch).
    The structure is similar enough that the same canonical-record path
    can absorb the data, but the access pattern is materially different:
    JSON-over-POST with an opaque session cookie, vs. PDF download or
    HTML scrape.

Auth:
    A FedAuth session cookie, captured manually from a logged-in browser
    session and stored as the ``RESEARCH_TX_COOKIE`` env var (production:
    GitHub Actions secret). Cookie expiry is a known operational concern;
    this module detects expiry in all four observed failure modes and
    soft-fails (returns empty list) so the foreclosure pipeline never
    blocks on probate intake.

Sealed-record filter:
    Tyler's ToS §2.3 prohibits accessing or surfacing sealed records.
    Hits with ``isSealed`` or ``isHiddenFromPublic`` are filtered at
    parse time, before the canonical-record pipeline can touch them.
    Skip counts are returned in the parse summary so operators can
    monitor filter health.

Public API:
    fetch_dallas_probate(days_back=7) -> list[ProbateRecord]
        Idempotent. Returns [] on any failure. Detailed reason logged.

Failure modes detected (all return empty list):
    1. PROBATE_ENABLED=False                         (intentional)
    2. RESEARCH_TX_COOKIE env var missing            (setup error)
    3. HTTP 401/403                                  (cookie expired)
    4. HTTP 200 with empty body                      (silent expiry)
    5. HTTP non-200                                  (server side issue)
    6. Network exception                             (transient)
    7. JSON parse error                              (API drift)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import config
from . import probate_auth

logger = logging.getLogger(__name__)


# ============================================================================
# Verified API contract (Phase 1 recon, captured 2026-05-16)
# ============================================================================

API_URL = (
    "https://research.txcourts.gov/CourtRecordsSearch/search"
    "?timeZoneOffsetInMinutes=-300"
)

# Static request headers. The Cookie header is added per-request.
_STATIC_HEADERS = {
    "Content-Type":            "application/json",
    "Accept":                  "application/json, text/plain, */*",
    "Origin":                  "https://research.txcourts.gov",
    "Referer":                 "https://research.txcourts.gov/CourtRecordsSearch/ui/search?q=",
    "x-show-loading-spinner":  "true",
}


# Tyler exposes these relative-date buckets on the Case Filed Date facet.
# Verified against DCAD-2 production code + captured response.
def _date_bucket_for_days(days_back: int) -> str:
    """Map a ``days_back`` integer to Tyler's relative-date bucket key."""
    if days_back <= 1:
        return "Today"
    if days_back <= 7:
        return "In The Last Week"
    return "In The Last Month"


def _build_dallas_probate_payload(days_back: int) -> dict:
    """The verified Phase 1 payload, parameterized by recency window only."""
    return {
        "queryString":              "*",
        "pageSize":                 200,
        "pageIndex":                0,
        "SearchIndexType":          "Cases",
        "hearingListView":          True,
        "isCalendarView":           False,
        "isNewestFirst":            True,
        "isReturningFromDetails":   False,
        "sortFieldOrder":           0,
        "sortFields":               3,
        "selectedFacets": [
            {
                "facetKey":         "Location",
                "excludeSelf":      False,
                "selectedBuckets":  [{"bucketKey": "dallas county - county clerk"}],
            },
            {
                "facetKey":         "Case Category",
                "excludeSelf":      False,
                "selectedBuckets":  [
                    {"bucketKey": "probate"},
                    {"bucketKey": "probate - other"},
                ],
            },
            {
                "facetKey":         "Case Filed Date",
                "excludeSelf":      False,
                "selectedBuckets":  [{"bucketKey": _date_bucket_for_days(days_back)}],
            },
        ],
    }


# ============================================================================
# Data model
# ============================================================================


@dataclass
class ProbateRecord:
    """One probate case from re:SearchTX.

    Mirrors the shape pattern of ``ForeclosureRecord`` and
    ``BankruptcyRecord`` â€” source-tier dataclass that the canonicalizer
    (PR 5) consumes and converts into the canonical-record dict shape.

    Per Phase 3 Option A:
        grantor in the canonical record will be the decedent
        grantee in the canonical record will be the applicant

    Multi-party cases (joint applicants):
        The first applicant becomes ``applicant_name``; any others land
        in ``additional_applicants`` and ride along in canonical
        ``signal_metadata`` for operator visibility.
    """
    case_data_id:             str                              # Tyler UUID (primary key)
    case_number:              str                              # human-readable case #
    case_type:                Optional[str] = None
    case_status:              Optional[str] = None
    date_filed:               Optional[str] = None             # ISO date if normalizable
    decedent_name:            Optional[str] = None             # primary "Decedent" party
    applicant_name:           Optional[str] = None             # first "Applicant"/"Petitioner"
    additional_applicants:    list[str] = field(default_factory=list)
    judge:                    Optional[str] = None
    attorneys:                list[str] = field(default_factory=list)
    jurisdiction:             Optional[str] = None
    raw:                      dict = field(default_factory=dict)
    parse_warnings:           list[str] = field(default_factory=list)


# ============================================================================
# Public API
# ============================================================================


def fetch_dallas_probate(
    *,
    days_back: int = 7,
    session_cookies: Optional[dict[str, str]] = None,
) -> list[ProbateRecord]:
    """Fetch Dallas County probate cases filed within ``days_back`` days.

    Returns an empty list on ANY failure. Caller (PR 5 wiring in
    ``main.py``) is expected to catch this and continue with the
    foreclosure-only pipeline; probate is additive, never a hard
    dependency.

    Args:
        days_back: rolling window in days. Mapped to Tyler's relative-
            bucket facet â€” the wire protocol uses opaque strings like
            "In The Last Week", not numeric ranges.
        session_cookies: pre-acquired Tyler session cookies (PR 8). When
            None (default), the function acquires its own via
            probate_auth.acquire_session. main.py passes the same cookies
            to both this call and PR 8 Phase 2's case-detail fetcher so
            we authenticate only once per pipeline run.

    Returns:
        list of ProbateRecord. Empty on disabled / missing cookie /
        cookie expired / server error / API drift / network error.
    """
    if not config.PROBATE_ENABLED:
        logger.info("Probate: PROBATE_ENABLED is False; skipping fetch")
        return []

    if session_cookies is None:
        session_cookies = probate_auth.acquire_session()
    if session_cookies is None:
        logger.warning(
            "Probate: session acquisition failed; skipping probate fetch"
        )
        return []
    cookie_header = probate_auth.cookies_to_header(session_cookies)

    payload = _build_dallas_probate_payload(days_back)
    headers = {
        **_STATIC_HEADERS,
        "Cookie": cookie_header,
        "User-Agent": probate_auth.USER_AGENT,  # MUST match auth context UA; Tyler ALB enforces browser UA
    }

    logger.info("Probate: requesting Dallas filings, days_back=%d", days_back)
    try:
        response = requests.post(
            API_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Probate: request failed (%s): %s", type(exc).__name__, exc)
        return []

    # Cookie expiry: explicit auth failure
    if response.status_code in (401, 403):
        logger.warning(
            "Probate: session rejected (HTTP %d) - will retry on next run",
            response.status_code,
        )
        return []

    # Other server-side problems
    if response.status_code != 200:
        logger.warning(
            "Probate: unexpected HTTP %d; body preview: %r",
            response.status_code, response.text[:200],
        )
        return []

    # Cookie expiry: silent variant (DCAD-2 observed this; Tyler returns
    # 200 + empty body when the session is gone but the request still
    # parses)
    if not response.text or not response.text.strip():
        logger.warning(
            "Probate: HTTP 200 with empty body - treating as silent "
            "session-rejection; will retry on next run"
        )
        return []

    try:
        data = response.json()
    except ValueError as exc:
        logger.error("Probate: JSON parse failed: %s", exc)
        return []

    records, skipped_sealed, skipped_unparseable = _parse_probate_response(data)
    logger.info(
        "Probate: parsed %d cases; skipped %d sealed/hidden, %d unparseable",
        len(records), skipped_sealed, skipped_unparseable,
    )
    return records


# ============================================================================
# Parsing
# ============================================================================


def _parse_probate_response(data: dict) -> tuple[list[ProbateRecord], int, int]:
    """Convert the raw JSON response into a list of ProbateRecord.

    Returns ``(records, skipped_sealed, skipped_unparseable)``. Sealed and
    hidden hits are filtered HERE so the canonicalization stage can never
    see them. ToS §2.3 hard requirement.
    """
    # Tyler's response shape (verified Phase 1):
    #   { "result": { "searchResults": { "hits": [...] } } }
    result          = (data.get("result")          or {}) if isinstance(data, dict) else {}
    search_results  = (result.get("searchResults") or {}) if isinstance(result, dict) else {}
    hits            = search_results.get("hits") or []

    if not isinstance(hits, list):
        logger.warning("Probate: 'hits' is not a list; got %s", type(hits).__name__)
        return [], 0, 0

    records: list[ProbateRecord] = []
    skipped_sealed = 0
    skipped_unparseable = 0

    for hit in hits:
        if not isinstance(hit, dict):
            skipped_unparseable += 1
            continue

        # HARD FILTER: sealed / hidden records never proceed (ToS §2.3)
        if hit.get("isSealed") is True or hit.get("isHiddenFromPublic") is True:
            skipped_sealed += 1
            continue

        try:
            record = _hit_to_record(hit)
            records.append(record)
        except Exception as exc:
            logger.warning(
                "Probate: failed to parse hit %s: %s",
                hit.get("caseDataID", "<unknown>"), exc,
            )
            skipped_unparseable += 1

    return records, skipped_sealed, skipped_unparseable


def _hit_to_record(hit: dict) -> ProbateRecord:
    """Convert one Tyler ``hit`` dict to a ProbateRecord.

    Defensive about every field. Tyler's schema has varied over time and
    some fields are inconsistently populated; we use ``.get()`` with
    fallbacks throughout and emit warnings rather than raising.
    """
    warnings: list[str] = []

    case_data_id = str(hit.get("caseDataID") or "").strip()
    case_number  = str(hit.get("caseNumber") or hit.get("caseNum") or "").strip()
    case_type    = str(hit.get("caseTypeCode") or hit.get("caseType") or "").strip() or None
    case_status  = str(hit.get("status") or hit.get("caseStatus") or "").strip() or None
    date_filed   = str(hit.get("dateFiled") or hit.get("filedDate") or "").strip() or None
    judge        = str(hit.get("judicialOfficerName") or hit.get("judge") or "").strip() or None
    juris        = str(hit.get("jurisdiction") or "").strip() or None

    if not case_data_id:
        warnings.append("missing caseDataID")

    # Parties: find Decedent + Applicant/Petitioner roles
    parties = hit.get("parties")
    if not isinstance(parties, list):
        parties = []

    decedent_name = None
    applicant_names: list[str] = []
    attorneys: list[str] = []

    for party in parties:
        if not isinstance(party, dict):
            continue

        role = str(party.get("partyTypeCode") or party.get("partyType") or "").strip()
        name = _party_name(party)

        if role.casefold() == "decedent":
            if decedent_name is None and name:
                decedent_name = name
        elif role.casefold() in ("applicant", "petitioner"):
            if name:
                applicant_names.append(name)

        # Attorney names ride along with whichever party they represent
        attys = party.get("attorneyNames") or party.get("attorneys") or []
        if isinstance(attys, list):
            for atty in attys:
                if isinstance(atty, str) and atty.strip():
                    attorneys.append(atty.strip())
                elif isinstance(atty, dict):
                    atty_name = atty.get("name") or atty.get("fullName")
                    if atty_name:
                        attorneys.append(str(atty_name).strip())

    if decedent_name is None:
        warnings.append("no Decedent party found")
    if not applicant_names:
        warnings.append("no Applicant/Petitioner party found")

    applicant_name = applicant_names[0] if applicant_names else None
    additional = applicant_names[1:]

    return ProbateRecord(
        case_data_id=          case_data_id,
        case_number=           case_number,
        case_type=             case_type,
        case_status=           case_status,
        date_filed=            date_filed,
        decedent_name=         decedent_name,
        applicant_name=        applicant_name,
        additional_applicants= additional,
        judge=                 judge,
        attorneys=             attorneys,
        jurisdiction=          juris,
        raw=                   hit,
        parse_warnings=        warnings,
    )


def _party_name(party: dict) -> Optional[str]:
    """Extract a party's display name with several fallbacks.

    Tyler returns party names in mixed shapes:
      - sometimes a single ``name`` or ``fullName`` field
      - sometimes structured ``firstName``/``middleName``/``lastName``
    """
    direct = party.get("name") or party.get("fullName")
    if direct:
        return str(direct).strip()

    parts = [
        str(party.get(k) or "").strip()
        for k in ("lastName", "firstName", "middleName")
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None
    # Reorder to "LAST FIRST MIDDLE" so it lines up with DCAD's owner format
    # (the name_matcher will further normalize / re-format for lookup).
    if len(parts) >= 2:
        return " ".join(parts)
    return parts[0]


# ============================================================================
# Helper exposed for the live-probe script
# ============================================================================


def case_detail_url(case_data_id: str) -> str:
    """Operator-facing URL for one re:SearchTX case detail view.

    Route confirmed by live browser observation 2026-05-27: singular
    ``case``, no ``/details`` suffix. The earlier plural-with-suffix
    form rendered Tyler's Angular 404 page.
    """
    return (
        f"https://research.txcourts.gov/CourtRecordsSearch/ui/case/"
        f"{case_data_id}"
    )
