"""
Address normalization, instrument-code mapping, and HOA detection.

This is the contract every other module that touches raw scraped data
depends on. Keep it pure (no I/O, no side effects).
"""

import re
from typing import Optional

from . import config


# ═══════════════════════════════════════════════════════════════════════════
# Instrument code mapping (§A.3)
# ═══════════════════════════════════════════════════════════════════════════

# Reverse-lookup: Dallas literal code → first Harris-Intel category that
# claims it. Some codes (notably TXL) appear in multiple categories;
# ``harris_categories_for_dallas_code`` returns the full list.
_DALLAS_TO_HARRIS: dict[str, str] = {
    code: category
    for category, codes in config.INSTRUMENT_CODES.items()
    for code in codes
}


def dallas_code_to_category(dallas_code: str) -> Optional[str]:
    """Map a Dallas literal instrument code to its Harris-Intel category.

    Returns the Harris-Intel category string (e.g. ``"L/P"``, ``"NOTICE"``)
    if the code is recognized; ``None`` if unknown. Case-insensitive.
    """
    if not dallas_code:
        return None
    return _DALLAS_TO_HARRIS.get(dallas_code.upper().strip())


def harris_categories_for_dallas_code(dallas_code: str) -> list[str]:
    """All Harris-Intel categories a Dallas code might map to.

    Some codes belong to multiple categories — e.g. ``TXL`` is both ``T/L``
    (Tax Lien) and ``LEVY`` per §A.3. Returns a list, possibly empty.
    """
    if not dallas_code:
        return []
    code = dallas_code.upper().strip()
    return [
        category
        for category, codes in config.INSTRUMENT_CODES.items()
        if code in codes
    ]


def is_suppression_code(dallas_code: str) -> bool:
    """True if the code (e.g. ``REL``, ``RLP``) suppresses a prior record."""
    if not dallas_code:
        return False
    return dallas_code.upper().strip() in config.SUPPRESSION_CODES


# ═══════════════════════════════════════════════════════════════════════════
# Address normalization (USPS-style)
# ═══════════════════════════════════════════════════════════════════════════

# Standard USPS street-suffix abbreviations. Source: USPS Pub 28 Appendix C.
_USPS_SUFFIX: dict[str, str] = {
    "STREET": "ST", "STR": "ST", "STRT": "ST",
    "AVENUE": "AVE", "AV": "AVE", "AVN": "AVE", "AVEN": "AVE",
    "BOULEVARD": "BLVD", "BOUL": "BLVD", "BOULV": "BLVD",
    "DRIVE": "DR", "DRV": "DR",
    "ROAD": "RD",
    "LANE": "LN", "LNS": "LN",
    "COURT": "CT", "CRT": "CT",
    "CIRCLE": "CIR", "CIRC": "CIR",
    "PLACE": "PL", "PLC": "PL",
    "PARKWAY": "PKWY", "PKY": "PKWY", "PARKWY": "PKWY",
    "HIGHWAY": "HWY", "HIWAY": "HWY", "HIWY": "HWY", "HWAY": "HWY",
    "TRAIL": "TRL", "TR": "TRL", "TRAILS": "TRL",
    "TERRACE": "TER", "TERR": "TER",
    "WAY": "WAY", "WY": "WAY",
    "EXPRESSWAY": "EXPY", "EXPR": "EXPY", "EXP": "EXPY",
    "FREEWAY": "FWY", "FRWAY": "FWY", "FRWY": "FWY",
    "SQUARE": "SQ", "SQR": "SQ", "SQRE": "SQ",
    "PLAZA": "PLZ", "PLZA": "PLZ",
    "ROW": "ROW",
    "RUN": "RUN",
    "ALLEY": "ALY", "ALLY": "ALY",
    "BRIDGE": "BRG", "BRDGE": "BRG",
    "CROSSING": "XING", "CRSSNG": "XING",
    "POINT": "PT", "PNT": "PT",
    "RIDGE": "RDG", "RDGE": "RDG",
    "STATION": "STA", "STATN": "STA",
    "VALLEY": "VLY", "VLLY": "VLY",
    "VIEW": "VW",
    "VISTA": "VIS",
}

# Directional abbreviations.
_USPS_DIRECTIONAL: dict[str, str] = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW",
    "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}


_PUNCT_RE      = re.compile(r"[,;\.]")
_WHITESPACE_RE = re.compile(r"\s+")
_HASH_UNIT_RE  = re.compile(r"#\s*")
_UNIT_SEP_RE   = re.compile(
    r"\b(APT|APARTMENT|STE|SUITE|UNIT|BLDG|BUILDING|FL|FLOOR|RM|ROOM)\b\s*([A-Z0-9\-]+)",
    re.IGNORECASE,
)


def normalize_address(raw: Optional[str]) -> str:
    """Produce a USPS-style normalized address string.

    Operations:
      1. Strip leading/trailing whitespace; treat ``None`` as empty.
      2. Uppercase.
      3. Remove commas, semicolons, and periods.
      4. Collapse repeated whitespace.
      5. Expand directional words (NORTH→N) and street suffixes (STREET→ST)
         to USPS abbreviations.
      6. Strip unit-portion (APT/STE/etc.) — use :func:`extract_unit` to
         retrieve it separately.

    The output is the *primary* address (no unit), suitable as a join key
    against DCAD parcel data.

    Empty input returns an empty string.
    """
    if not raw:
        return ""

    s = str(raw).strip().upper()
    s = _PUNCT_RE.sub(" ", s)
    # Canonicalize "#5" → "UNIT 5" so the unit regex matches via \b.
    s = _HASH_UNIT_RE.sub("UNIT ", s)
    s = _WHITESPACE_RE.sub(" ", s)

    # Strip unit-portion before token expansion so unit tokens don't
    # accidentally match suffix abbreviations.
    s = _strip_unit(s)

    tokens = s.split()
    normalized: list[str] = []
    for tok in tokens:
        if tok in _USPS_DIRECTIONAL:
            normalized.append(_USPS_DIRECTIONAL[tok])
        elif tok in _USPS_SUFFIX:
            normalized.append(_USPS_SUFFIX[tok])
        else:
            normalized.append(tok)

    out = " ".join(normalized).strip()
    return _WHITESPACE_RE.sub(" ", out)


def _strip_unit(addr: str) -> str:
    """Remove unit/apt/suite portion from an address string."""
    return _UNIT_SEP_RE.sub("", addr).strip()


def extract_unit(raw: Optional[str]) -> Optional[str]:
    """Return the unit/apt/suite portion of an address, or ``None``.

    Normalizes the unit-type abbreviation (e.g. ``APARTMENT 5`` → ``APT 5``).
    """
    if not raw:
        return None
    s = str(raw).strip().upper()
    # Canonicalize "#5" → "UNIT 5" before matching.
    s = _HASH_UNIT_RE.sub("UNIT ", s)
    m = _UNIT_SEP_RE.search(s)
    if not m:
        return None

    unit_type = m.group(1).upper()
    unit_id   = m.group(2).upper()

    # Canonicalize unit-type abbreviation.
    type_map = {
        "APARTMENT": "APT",
        "SUITE":     "STE",
        "BUILDING":  "BLDG",
        "FLOOR":     "FL",
        "ROOM":      "RM",
    }
    unit_type = type_map.get(unit_type, unit_type)
    return f"{unit_type} {unit_id}"


# ═══════════════════════════════════════════════════════════════════════════
# Grantor / grantee extraction (§3.3.1 — the HOA-plaintiff bug fix)
# ═══════════════════════════════════════════════════════════════════════════
#
# Harris-Intel's bug surfaced ``owner`` as the lead-target field even when
# the record was an HOA suing a homeowner — the HOA name landed at the top
# of the dashboard as a "lead". The fix:
#
#   - GRANTOR = party filing the action (potentially an HOA)
#   - GRANTEE = party named AGAINST whom the action is filed (the actual
#     homeowner; the real lead target)
#
# Downstream, ``scorer.filter_hoa()`` removes records whose GRANTEE is
# missing AND whose GRANTOR is an HOA — i.e. cases where the only named
# party is the HOA itself.

def extract_grantor_grantee(raw_record: dict) -> tuple[str, str]:
    """Return ``(grantor, grantee)`` for a raw scraped record.

    Handles the field-name variants emitted by publicsearch.us and by the
    foreclosure-PDF extractor. Falls back gracefully for records that only
    carry one party name (e.g. some lien filings).

    Both returned strings are stripped; empty strings if unparseable.
    """
    grantor = (
        raw_record.get("grantor")
        or raw_record.get("filed_by")
        or raw_record.get("plaintiff")
        or raw_record.get("trustee")  # foreclosure-PDF synonym
        or ""
    )
    grantee = (
        raw_record.get("grantee")
        or raw_record.get("filed_against")
        or raw_record.get("defendant")
        or raw_record.get("debtor")    # foreclosure-PDF synonym
        or raw_record.get("owner")     # last-resort fallback
        or ""
    )
    return (str(grantor).strip(), str(grantee).strip())


# ═══════════════════════════════════════════════════════════════════════════
# HOA / association detection (§3.3.1)
# ═══════════════════════════════════════════════════════════════════════════

_HOA_REGEXES: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in config.HOA_PATTERNS
]


def is_hoa_entity(name: Optional[str]) -> bool:
    """True if ``name`` matches HOA/association heuristics.

    Two checks, in order:
      1. Exact match against ``config.HOA_DENYLIST`` (case-insensitive).
      2. Regex match against ``config.HOA_PATTERNS``.

    A bare corporate suffix (e.g. ``", Inc."``) is *not* sufficient — the
    pattern set is HOA-specific to avoid false positives like banks,
    churches, and ordinary LLCs.
    """
    if not name:
        return False

    s = str(name).strip()
    if not s:
        return False

    # Denylist check — exact, case-insensitive.
    s_upper = s.upper()
    for entry in config.HOA_DENYLIST:
        if entry.upper() == s_upper:
            return True

    # Pattern check.
    for pattern in _HOA_REGEXES:
        if pattern.search(s):
            return True

    return False
