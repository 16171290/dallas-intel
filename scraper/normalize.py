"""
Address normalization, instrument-code mapping, and HOA detection.

This is the contract every other module that touches raw scraped data
depends on. Keep it pure (no I/O, no side effects).

2026-05-14: Added city/state/ZIP suffix stripping to normalize_address.
DCAD's address index is keyed on STREET_NUM + FULL_STREET_NAME only
(e.g. "1402 LEVEE LN"). Foreclosure PDFs frequently emit addresses with
the city/state/ZIP appended (e.g. "1402 LEVEE LN DALLAS TX 75201"),
which silently fails the join. The new preprocessing step strips that
suffix so PDF and DCAD addresses normalize to the same key.
"""

import re
from typing import Optional

from . import config


# ============================================================================
# Instrument code mapping (Sec A.3)
# ============================================================================

# Reverse-lookup: Dallas literal code -> first Harris-Intel category that
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

    Some codes belong to multiple categories - e.g. ``TXL`` is both ``T/L``
    (Tax Lien) and ``LEVY`` per Sec A.3. Returns a list, possibly empty.
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


# ============================================================================
# Address normalization (USPS-style)
# ============================================================================

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


# ----------------------------------------------------------------------------
# City/state/ZIP suffix stripping (2026-05-14)
# ----------------------------------------------------------------------------
# DCAD's address index keys are STREET_NUM + FULL_STREET_NAME only - no
# city, no state, no ZIP. PDF-sourced addresses commonly carry the full
# postal form ("1402 LEVEE LN DALLAS TX 75201"). Stripping the suffix
# before lookup raises the join hit-rate from ~5% to ~60%+ on the
# foreclosure-PDF source.
#
# Strategy:
#   1. If the address contains a comma, take everything before the first
#      comma. Postal-form addresses with commas put the street first.
#   2. After (1), or if no comma existed, scan for a known-city pattern
#      at the end of the string. Strip from the city onwards.
#
# False-positive guard: known cities are recognized as suffixes only when
# at the end of the string OR followed by state/ZIP indicators. This
# preserves street names that happen to contain a city word (e.g.
# "1234 DALLAS ST" keeps "DALLAS ST" intact - DALLAS is not at end and
# is not followed by TX/TEXAS/zip).

_KNOWN_CITY_SUFFIXES: list[str] = [
    # Dallas County proper
    "DALLAS", "GARLAND", "MESQUITE", "IRVING", "GRAND PRAIRIE",
    "RICHARDSON", "CARROLLTON", "ROWLETT", "DESOTO", "LANCASTER",
    "CEDAR HILL", "DUNCANVILLE", "FARMERS BRANCH", "ADDISON",
    "UNIVERSITY PARK", "HIGHLAND PARK", "COCKRELL HILL",
    "BALCH SPRINGS", "HUTCHINS", "WILMER", "SEAGOVILLE",
    "GLENN HEIGHTS", "COMBINE", "SUNNYVALE", "COPPELL",
    "SACHSE", "WYLIE",
    # Cross-county (Collin, Denton, Tarrant) - properties straddle
    "PLANO", "FRISCO", "MCKINNEY", "ALLEN", "MURPHY", "PARKER",
    "FAIRVIEW", "LEWISVILLE", "DENTON", "FORT WORTH", "ARLINGTON",
    "GRAPEVINE", "SOUTHLAKE", "EULESS", "BEDFORD", "KELLER",
    "COLLEYVILLE", "FLOWER MOUND", "THE COLONY", "LITTLE ELM",
]

# Sort longest first so "CEDAR HILL" matches before any potential "CEDAR"
# in a more permissive future list.
_KNOWN_CITY_SUFFIXES_SORTED = sorted(_KNOWN_CITY_SUFFIXES, key=len, reverse=True)

# Alternation. Internal whitespace must be flexible so "CEDAR  HILL" matches.
_CITY_ALT = "|".join(
    c.replace(" ", r"\s+") for c in _KNOWN_CITY_SUFFIXES_SORTED
)

# Pattern A: <city> <state> [zip] [trailing junk]
# Strips greedily once we've anchored on city + state.
_CITY_STATE_TRAIL_RE = re.compile(
    rf"\s+(?:{_CITY_ALT})\s+(?:TX|TEXAS)\b.*$",
    re.IGNORECASE,
)
# Pattern B: <city> <zip>
_CITY_ZIP_RE = re.compile(
    rf"\s+(?:{_CITY_ALT})\s+\d{{5}}(?:-\d{{4}})?\s*$",
    re.IGNORECASE,
)
# Pattern C: <city> alone at end of string (post-comma-split case).
# Won't match cities mid-string (e.g. "DALLAS" in "1234 DALLAS ST").
_CITY_ONLY_RE = re.compile(
    rf"\s+(?:{_CITY_ALT})\s*$",
    re.IGNORECASE,
)


def _strip_city_state_zip(addr: str) -> str:
    """Strip trailing city/state/ZIP from an address string.

    Examples:
        "1402 LEVEE LN, DALLAS, TX 75201"            -> "1402 LEVEE LN"
        "1011 E ALAN AVE CARROLLTON TEXAS 75006"     -> "1011 E ALAN AVE"
        "567 VILLAGE GREEN DR COPPELL, TEXAS 75019"  -> "567 VILLAGE GREEN DR"
        "4335 LASHLEY DR DALLAS TX 75232 EXTRA JUNK" -> "4335 LASHLEY DR"
        "1234 DALLAS ST"                             -> "1234 DALLAS ST" (unchanged)
        "5605 EVERGLADE RD"                          -> "5605 EVERGLADE RD" (unchanged)
    """
    if not addr:
        return addr

    # Strategy 1: comma form - take everything before first comma
    if "," in addr:
        addr = addr.split(",", 1)[0].strip()

    # Strategy 2: known-city pattern at end (or with state/zip after).
    # Try the most specific pattern first.
    addr = _CITY_STATE_TRAIL_RE.sub("", addr).strip()
    addr = _CITY_ZIP_RE.sub("", addr).strip()
    addr = _CITY_ONLY_RE.sub("", addr).strip()

    return addr


# ----------------------------------------------------------------------------
# Existing punctuation / whitespace / unit handling
# ----------------------------------------------------------------------------

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
      0. (NEW 2026-05-14) Strip trailing city/state/ZIP if present, so
         PDF-sourced postal-form addresses reduce to the same shape as
         DCAD's index keys (street-only).
      1. Strip leading/trailing whitespace; treat ``None`` as empty.
      2. Uppercase.
      3. Remove commas, semicolons, and periods.
      4. Collapse repeated whitespace.
      5. Expand directional words (NORTH->N) and street suffixes
         (STREET->ST) to USPS abbreviations.
      6. Strip unit-portion (APT/STE/etc.) - use :func:`extract_unit` to
         retrieve it separately.

    The output is the *primary* address (no unit), suitable as a join key
    against DCAD parcel data.

    Empty input returns an empty string.
    """
    if not raw:
        return ""

    s = str(raw).strip().upper()

    # NEW: strip city/state/zip suffix. Runs BEFORE the punctuation removal
    # so the comma-split branch can use the comma as a boundary marker.
    s = _strip_city_state_zip(s)

    s = _PUNCT_RE.sub(" ", s)
    # Canonicalize "#5" -> "UNIT 5" so the unit regex matches via \b.
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

    Normalizes the unit-type abbreviation (e.g. ``APARTMENT 5`` -> ``APT 5``).
    """
    if not raw:
        return None
    s = str(raw).strip().upper()
    # Canonicalize "#5" -> "UNIT 5" before matching.
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


# ============================================================================
# Grantor / grantee extraction (Sec 3.3.1 - the HOA-plaintiff bug fix)
# ============================================================================
#
# Harris-Intel's bug surfaced ``owner`` as the lead-target field even when
# the record was an HOA suing a homeowner - the HOA name landed at the top
# of the dashboard as a "lead". The fix:
#
#   - GRANTOR = party filing the action (potentially an HOA)
#   - GRANTEE = party named AGAINST whom the action is filed (the actual
#     homeowner; the real lead target)
#
# Downstream, ``scorer.filter_hoa()`` removes records whose GRANTEE is
# missing AND whose GRANTOR is an HOA - i.e. cases where the only named
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


# ============================================================================
# HOA / association detection (Sec 3.3.1)
# ============================================================================

_HOA_REGEXES: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in config.HOA_PATTERNS
]


def is_hoa_entity(name: Optional[str]) -> bool:
    """True if ``name`` matches HOA/association heuristics.

    Two checks, in order:
      1. Exact match against ``config.HOA_DENYLIST`` (case-insensitive).
      2. Regex match against ``config.HOA_PATTERNS``.

    A bare corporate suffix (e.g. ``", Inc."``) is *not* sufficient - the
    pattern set is HOA-specific to avoid false positives like banks,
    churches, and ordinary LLCs.
    """
    if not name:
        return False

    s = str(name).strip()
    if not s:
        return False

    # Denylist check - exact, case-insensitive.
    s_upper = s.upper()
    for entry in config.HOA_DENYLIST:
        if entry.upper() == s_upper:
            return True

    # Pattern check.
    for pattern in _HOA_REGEXES:
        if pattern.search(s):
            return True

    return False
