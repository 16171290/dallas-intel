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


# ============================================================================
# Owner / grantor / grantee name formatting (2026-05-26)
# ============================================================================
#
# Records arrive with names from three sources, each with different dirt:
#
#   1. publicsearch list-view scrape - usually "LAST, FIRST MIDDLE" form,
#      often with stray internal whitespace ("BARBA , JUDITH")
#   2. DCAD ACCOUNT_INFO.OWNER_NAME1 - "LAST FIRST MIDDLE" form (no comma),
#      joint owners as "LAST FIRST & SECOND_FIRST" (e.g. "FLORES JAIME & MARIE")
#   3. foreclosure_ocr extracted grantor - "First Last" or "FIRST LAST",
#      heavily decorated with marital-status boilerplate ("A SINGLE MAN",
#      "WIFE AND HUSBAND"), often truncated mid-sentence by OCR
#
# format_owner_name normalizes all three to "Last, First Middle [Suffix]"
# title-case, e.g. "Smith, John A Jr". Entity names (TRUST/LLC/CORP) are
# title-cased but not re-ordered. Joint owners separated by ' & '.

# Anywhere these phrases appear, everything from that point onward is
# OCR boilerplate / marital-status decoration, not part of the name.
# Most-specific patterns first so "husband and wife" wins over "husband".
_NAME_TAIL_SENTINELS: list[re.Pattern] = [
    re.compile(r",?\s+husband\s+and\s+wife\b.*$",   re.I),
    re.compile(r",?\s+wife\s+and\s+husband\b.*$",   re.I),
    re.compile(r",?\s+an?\s+single\s+(?:man|woman)\b.*$", re.I),
    re.compile(r",?\s+an?\s+married\s+(?:man|woman)\b.*$", re.I),
    re.compile(r",?\s+an?\s+unmarried\s+wom\w*\b.*$", re.I),  # OCR-truncated "WOMA"
    re.compile(r",?\s+an?\s+unmarried\s+man\b.*$",  re.I),
    re.compile(r",?\s+as\s+(?:her|his)\s+sole\b.*$", re.I),
    re.compile(r",?\s+as\s+(?:her|his)\s+separate\b.*$", re.I),
    re.compile(r",?\s+husband\b.*$",    re.I),
    re.compile(r",?\s+wife\b.*$",       re.I),
    re.compile(r",?\s+single\b.*$",     re.I),
    re.compile(r",?\s+unmarried\b.*$",  re.I),
    re.compile(r",?\s+married\b.*$",    re.I),
    re.compile(r"\s+provides\s+\w+.*$", re.I),  # OCR-leaked will boilerplate
]

# DECD / ESTATE / AKA markers - strip these as standalone tokens.
# Note: "LIFE ESTATE" and "FAMILY TRUST" are entity markers; handled separately.
_DECD_MARKER_RE = re.compile(
    r"\b(?:DECD|DECEASED|AKA|A\.K\.A\.)\b\s*", re.I
)
# Trailing " ESTATE" only when not preceded by "LIFE" (which is an entity).
_TRAILING_ESTATE_RE = re.compile(r"(?<!\bLIFE)\s+ESTATE\s*$", re.I)

# "X AND HIS/HER WIFE/HUSBAND, Y" -> "X AND Y" pre-processor.
# Without this, the WIFE/HUSBAND sentinel above would clip Y entirely.
_JOINT_VIA_SPOUSE_RE = re.compile(
    r"\s+AND\s+(?:HIS|HER)\s+(?:WIFE|HUSBAND)\s*,\s*", re.I
)

# Entity markers - when present, don't re-order LAST/FIRST. Title-case only.
_ENTITY_TOKENS = {
    "TRUST", "LLC", "L.L.C.", "LP", "L.P.", "LTD", "INC", "CORP",
    "CORPORATION", "COMPANY", "CO", "PARTNERSHIP", "PARTNERS",
    "ASSOCIATION", "ASSN", "FUND", "HOLDINGS", "PROPERTIES",
}
_ENTITY_PHRASES = [
    "LIFE ESTATE",
    "ESTATE OF",
    "FAMILY TRUST",
    "REVOCABLE TRUST",
    "LIVING TRUST",
]

# Joint-owner splitter. Word-boundary "and" / "AND" / "&".
_JOINT_SPLIT_RE = re.compile(r"\s+(?:and|AND|And|&)\s+")

# Personal-name suffixes (preserve, repositioned after first name).
_SUFFIX_RE = re.compile(r"^(?:Jr|Sr|II|III|IV|2nd|3rd)\.?$", re.I)


def _looks_like_entity(s: str) -> bool:
    """Return True if the string looks like a non-person entity (trust, LLC,
    estate-of-foo, etc.) and should NOT be re-ordered as a person name."""
    if not s:
        return False
    upper = s.upper()
    for phrase in _ENTITY_PHRASES:
        if phrase in upper:
            return True
    tokens = {t.rstrip(".,").upper() for t in s.split()}
    return bool(tokens & _ENTITY_TOKENS)


_ALL_CAPS_TOKENS = {
    "LLC", "L.L.C.", "LP", "L.P.", "LTD", "INC", "INC.",
    "CORP", "CORP.", "CO", "USA", "II", "III", "IV", "VI",
}


def _title_case_token(tok: str) -> str:
    """Title-case a single name token respecting Mc/Mac/O', Roman numerals,
    and entity suffixes (LLC, INC, LP).

      "MCDONALD" -> "McDonald"
      "MACARTHUR" -> "MacArthur"
      "O'CONNOR" -> "O'Connor"
      "II" -> "II"   (Roman numeral preserved)
      "M." -> "M."   (single-letter initial uppercase)
      "LLC" -> "LLC" (entity suffix all-caps)
    """
    if not tok:
        return ""
    # Always-uppercase tokens (entity suffixes + Roman numerals).
    if tok.upper() in _ALL_CAPS_TOKENS:
        return tok.upper()
    # Roman numerals (catch any not in the explicit set).
    if re.fullmatch(r"(?:II|III|IV|VI|VII|VIII|IX|XI|XII)", tok, re.I):
        return tok.upper()
    # Single-letter initial (possibly with period).
    bare = tok.rstrip(".")
    if len(bare) == 1 and bare.isalpha():
        return bare.upper() + ("." if tok.endswith(".") else "")
    # Mc prefix: "McDonald" / "McCullough". Safe to apply on all-caps
    # input because Mc-as-first-two-letters always indicates Mc surname.
    if re.match(r"^MC[A-Z]", tok, re.I) and len(tok) >= 3:
        return "Mc" + tok[2].upper() + tok[3:].lower()
    # NOTE: Mac-prefix rule deliberately omitted. "MACK" / "MACEY" are
    # regular surnames; from all-caps input we can't tell whether
    # "MACARTHUR" wants "MacArthur" or "Macarthur". Default title-case
    # gives "Macarthur" which is wrong less often than "MacK".
    # O' prefix: "O'Connor"
    if re.match(r"^O'[A-Z]", tok, re.I):
        return "O'" + tok[2].upper() + tok[3:].lower()
    # Hyphenated names: "Smith-Jones"
    if "-" in tok:
        return "-".join(_title_case_token(p) for p in tok.split("-"))
    # Default: first letter upper, rest lower.
    return tok[:1].upper() + tok[1:].lower()


def _title_case_name(s: str) -> str:
    """Apply token-aware title-casing to a full name string."""
    return " ".join(_title_case_token(t) for t in s.split())


def _extract_suffix(tokens: list[str]) -> tuple[list[str], str]:
    """Pop a trailing Jr/Sr/II/III suffix off the token list.

    Returns ``(remaining_tokens, suffix_str)`` where suffix_str is "" if
    no suffix was present. The suffix is stripped of any trailing period.
    """
    if not tokens:
        return tokens, ""
    last = tokens[-1].rstrip(".,")
    if _SUFFIX_RE.fullmatch(last):
        # Canonical-case the suffix.
        canonical = {"jr": "Jr", "sr": "Sr", "ii": "II", "iii": "III",
                     "iv": "IV", "2nd": "II", "3rd": "III"}
        return tokens[:-1], canonical.get(last.lower(), last)
    return tokens, ""


def _format_single_person(raw: str, *, no_comma_format: str = "first_last") -> str:
    """Convert one person's name to ``"Last, First Middle [Suffix]"``.

    Parses the input based on the presence of a comma:

      - Comma form ``"LAST, FIRST MIDDLE"`` (publicsearch / DCAD comma-style):
        first segment is the surname, rest is first + middle.
      - No-comma form: parse depends on ``no_comma_format``:
          * ``"first_last"`` (default): ``"First Middle Last"`` — OCR / deed
            text format; LAST token is the surname.
          * ``"last_first"``: ``"Last First Middle"`` — DCAD owner / probate
            decedent format; FIRST token is the surname.
    """
    raw = raw.strip().rstrip(",")
    if not raw:
        return ""

    # Comma form: parse first segment as surname.
    if "," in raw:
        parts = [p.strip() for p in raw.split(",")]
        suffix = ""
        if len(parts) >= 2 and _SUFFIX_RE.fullmatch(parts[-1].rstrip(".")):
            canonical = {"jr": "Jr", "sr": "Sr", "ii": "II", "iii": "III",
                         "iv": "IV", "2nd": "II", "3rd": "III"}
            suffix = canonical.get(parts[-1].rstrip(".").lower(),
                                   parts[-1].rstrip("."))
            parts = parts[:-1]

        if len(parts) == 1:
            # No explicit last-name separator — fall through to no-comma branch.
            return _format_single_person(
                parts[0] + (" " + suffix if suffix else ""),
                no_comma_format=no_comma_format,
            )

        last = parts[0].strip()
        first_middle = " ".join(p for p in parts[1:] if p).strip()
        fm_tokens = first_middle.split()
        fm_tokens, embedded_suffix = _extract_suffix(fm_tokens)
        if embedded_suffix and not suffix:
            suffix = embedded_suffix
        first_middle = " ".join(fm_tokens)

        last_tc = _title_case_name(last)
        fm_tc   = _title_case_name(first_middle)
        return (
            f"{last_tc}, {fm_tc}" + (f" {suffix}" if suffix else "")
        ).strip().rstrip(",")

    # No-comma form. Surname position depends on no_comma_format.
    tokens = raw.split()
    tokens, suffix = _extract_suffix(tokens)
    if not tokens:
        return ""
    if len(tokens) == 1:
        out = _title_case_token(tokens[0])
        return f"{out} {suffix}".strip() if suffix else out

    if no_comma_format == "last_first":
        # DCAD / probate-decedent format: FIRST token is the surname.
        last = tokens[0]
        first_middle = " ".join(tokens[1:])
    else:
        # Default: LAST token is the surname (deed/OCR convention).
        last = tokens[-1]
        first_middle = " ".join(tokens[:-1])

    last_tc = _title_case_name(last)
    fm_tc   = _title_case_name(first_middle)
    return (
        f"{last_tc}, {fm_tc}" + (f" {suffix}" if suffix else "")
    ).strip().rstrip(",")


def format_owner_name(raw: Optional[str], *, source_format: str = "auto") -> str:
    """Normalize an owner / grantor / grantee name to ``"Last, First Middle [Suffix]"``.

    Parameters
    ----------
    raw : str | None
        Raw name string from any source. Empty / None returns "".
    source_format : str
        How to interpret no-comma names (the comma-present form is
        unambiguous: it's always "LAST, FIRST MIDDLE"). One of:

        * ``"auto"`` (default) — detect: if "DECD" / "DECEASED" / "ESTATE"
          markers are present, treat as "LAST FIRST MIDDLE" (DCAD-style,
          common for probate decedents); otherwise treat as "FIRST MIDDLE
          LAST" (deed/OCR convention).
        * ``"last_first"`` — force "LAST FIRST MIDDLE" parse. Use for
          ``dcad_owner`` field where DCAD's ACCOUNT_INFO always puts the
          surname first.
        * ``"first_last"`` — force "FIRST MIDDLE LAST" parse.

    Examples::

        format_owner_name("JAMES N. KUN, A SINGLE MAN")
            -> "Kun, James N."
        format_owner_name("BARBA , JUDITH")
            -> "Barba, Judith"
        format_owner_name("Bobbie Meyers")
            -> "Meyers, Bobbie"
        format_owner_name("BERGER ANN M", source_format="last_first")
            -> "Berger, Ann M"
        format_owner_name("STAFFORD CHARLES & SHARON", source_format="last_first")
            -> "Stafford, Charles & Stafford, Sharon"
        format_owner_name("FLYNN LINDA BARFIELD DECD")
            -> "Flynn, Linda Barfield"   # auto-detects via DECD marker
        format_owner_name("DAVID WALKER and LINDA E. WALKER")
            -> "Walker, David & Walker, Linda E."
        format_owner_name("ARTHUR REVOCABLE TRUST THE")
            -> "Arthur Revocable Trust The"
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""

    # 1. Strip OCR-prefix garbage ("_", "|", quotes, leading whitespace).
    s = re.sub(r"^[_|\"'\s]+", "", s)

    # 2. Pre-process "X AND HIS/HER WIFE/HUSBAND, Y" -> "X AND Y" so we
    #    don't clip Y when stripping marital-status sentinels next.
    s = _JOINT_VIA_SPOUSE_RE.sub(" AND ", s)

    # 3. Auto-detection: if "DECD" / "DECEASED" / "ESTATE" appears AND
    #    the caller didn't override, this is DCAD-style "LAST FIRST".
    if source_format == "auto":
        if _DECD_MARKER_RE.search(s) or _TRAILING_ESTATE_RE.search(s):
            no_comma_format = "last_first"
        else:
            no_comma_format = "first_last"
    elif source_format in ("last_first", "first_last"):
        no_comma_format = source_format
    else:
        raise ValueError(f"Unknown source_format: {source_format!r}")

    # 4. Drop trailing marital-status / OCR-boilerplate sentinels.
    for pat in _NAME_TAIL_SENTINELS:
        s = pat.sub("", s)

    # 5. Strip DECD / DECEASED / AKA markers anywhere.
    s = _DECD_MARKER_RE.sub("", s)
    # 6. Strip trailing standalone ESTATE (but not "LIFE ESTATE").
    s = _TRAILING_ESTATE_RE.sub("", s)

    # 7. Collapse runs of whitespace and trim.
    s = re.sub(r"\s+", " ", s).strip().rstrip(",")
    if not s:
        return ""

    # 8. Entity short-circuit — title-case but don't re-order.
    if _looks_like_entity(s):
        return _title_case_name(s)

    # 9. Joint-owner split. After cleaning above, AND/and/& separates
    #    distinct people (the WIFE/HUSBAND clip was pre-processed).
    parts = [p.strip().rstrip(",") for p in _JOINT_SPLIT_RE.split(s) if p.strip()]

    if len(parts) == 1:
        return _format_single_person(parts[0], no_comma_format=no_comma_format)

    # 10. Multi-person inheritance. When a later part is missing its own
    #     surname (e.g. "GINA" in "MEEKING STEVEN & GINA"), prepend the
    #     primary owner's surname before formatting. This logic only
    #     applies when the primary name is in LAST-FIRST form (DCAD); in
    #     FIRST-LAST form the inheritance pattern is rare and risky.
    formatted: list[str] = []
    primary_surname_tokens: list[str] = []
    for i, part in enumerate(parts):
        if (
            i > 0
            and no_comma_format == "last_first"
            and "," not in part
            and _looks_like_partner_first_only(part, no_comma_format)
            and primary_surname_tokens
        ):
            part = " ".join(primary_surname_tokens) + " " + part
        formatted_part = _format_single_person(part, no_comma_format=no_comma_format)
        if formatted_part:
            formatted.append(formatted_part)
            if i == 0 and "," in formatted_part:
                # Capture the primary's surname (everything before the
                # first comma) so subsequent partner-only parts can
                # inherit it. Upper-case so single-person reformat sees
                # it in the original DCAD shape.
                primary_surname_tokens = formatted_part.split(",", 1)[0].upper().split()

    return " & ".join(formatted)


def _looks_like_partner_first_only(s: str, no_comma_format: str = "first_last") -> bool:
    """True if the string looks like a spouse's first name (and optional
    middle) without a surname of its own.

    In DCAD's ``last_first`` format, joint owners are almost always spouses
    sharing a surname — the second part is the spouse's first/middle name
    alone (e.g. ``"MOORE ALBERT & SUSIE MAE"`` -> SUSIE MAE inherits MOORE).
    So in last_first mode we treat 1-2 token parts as inheritors. A 3+
    token second part is treated as having its own surname.

    In ``first_last`` mode (deed convention) inheritance is rare and risky
    because deeds typically spell out both surnames; we only inherit on
    bare single-token names or "FIRST INITIAL" forms.
    """
    tokens = s.split()
    if not tokens:
        return False
    if no_comma_format == "last_first":
        return len(tokens) <= 2
    # first_last default: bare first name only.
    if len(tokens) == 1:
        return True
    if len(tokens) == 2 and len(tokens[-1].rstrip(".")) <= 1:
        return True
    return False

