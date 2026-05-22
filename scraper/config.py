"""
Configuration constants for the dallas-intel pipeline.

Most values are static literals tied to a §-tagged decision in
docs/ARCHITECTURE.md. A few values that vary between runs or environments
are read from environment variables.

Never hardcode secrets here. The Discord webhook URL is read from
``DISCORD_WEBHOOK_URL`` (set as a GitHub Actions secret in production).
"""

import os
from pathlib import Path

# Auto-load .env from the project root if present. This is a local-dev
# convenience; in GitHub Actions the env vars come from real secrets and
# load_dotenv() is a no-op (no .env file exists in CI).
try:
    from dotenv import load_dotenv
    _project_root = Path(__file__).resolve().parent.parent
    load_dotenv(_project_root / ".env")
except ImportError:
    pass

# ═══════════════════════════════════════════════════════════════════════════
# Environment-derived (run-specific)
# ═══════════════════════════════════════════════════════════════════════════

# Contact email surfaced in the User-Agent header. Set via the
# CONTACT_EMAIL env var or GitHub Actions secret in production.
CONTACT_EMAIL: str = os.getenv("CONTACT_EMAIL", "unset@example.com")

# Discord webhook URL for run notifications. NEVER commit the literal value;
# always read from the DISCORD_WEBHOOK_URL secret.
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

# Year of DCAD bulk-data ZIP to use. Defaults to 2025 — the most recent
# certified-with-supplements dataset, which has both ownership AND values.
# The 2026 ZIP exists but is labeled "No Values — Most Current Ownership"
# (DCAD certifies values in late July each year). Switch to the next year
# after that year's certification lands (~late July). Overridable via the
# DCAD_TARGET_YEAR env var for backfill or testing.
DCAD_TARGET_YEAR: int = int(os.getenv("DCAD_TARGET_YEAR", "2025"))

# Optional ad-hoc override for the daily lookback window.
_DAYS_BACK_OVERRIDE = os.getenv("DAYS_BACK_OVERRIDE")

# ═══════════════════════════════════════════════════════════════════════════
# Pipeline tuning (§3.6.x, §E.4)
# ═══════════════════════════════════════════════════════════════════════════

# §3.6.2 — daily scraper lookback window.
DAYS_BACK: int = int(_DAYS_BACK_OVERRIDE) if _DAYS_BACK_OVERRIDE else 7

# §3.7.4 — initial historical pull on first run only.
BACKFILL_DAYS: int = 30

# §E.4 — score weights (default carry-over from Harris-Intel; §3.6.1).
SCORE_BASE                   = 30
SCORE_PER_CATEGORY_FLAG      = 10
SCORE_LP_FC_COMBO            = 20
SCORE_VALUE_100K             = 15
SCORE_VALUE_50K              = 10
SCORE_NEW_THIS_WEEK          = 5
SCORE_HAS_ADDRESS            = 5
SCORE_STACKING_ADDRESS_MATCH = 30  # §3.4.4
SCORE_CAP                    = 100

# Value bands (USD) for scoring.
VALUE_BAND_HIGH = 100_000
VALUE_BAND_MID  = 50_000

# ═══════════════════════════════════════════════════════════════════════════
# Data sources
# ═══════════════════════════════════════════════════════════════════════════

PUBLICSEARCH_BASE         = "https://dallas.tx.publicsearch.us"
DALLASCOUNTY_BASE         = "https://www.dallascounty.org"
DALLASCOUNTY_FORECLOSURES = (
    "https://www.dallascounty.org/government/county-clerk/recording/foreclosures.php"
)
DCAD_BASE                 = "https://www.dallascad.org"
DCAD_DATAPRODUCTS_URL     = "https://www.dallascad.org/DataProducts.aspx"
GITHUB_PAGES_URL          = "https://16171290.github.io/dallas-intel/"

# ═══════════════════════════════════════════════════════════════════════════
# Conduct profile (§3.7.7)
# ═══════════════════════════════════════════════════════════════════════════

USER_AGENT: str = f"dallas-intel/0.1 (+{CONTACT_EMAIL})"

# (min, max) inter-request delay in seconds, by host.
RATE_LIMITS: dict[str, tuple[float, float]] = {
    "publicsearch.us":  (2.0, 4.0),
    "dallascounty.org": (1.5, 2.0),
    "dallascad.org":    (2.0, 3.0),
}

# Halt the run after this many consecutive 4xx/5xx responses from any host.
CIRCUIT_BREAKER_THRESHOLD: int = 5

# §3.7.9 — random delay 0..N minutes at run start (set 0 to disable jitter).
CRON_JITTER_MINUTES: int = 0

# 429-handling exponential-backoff parameters.
BACKOFF_BASE_SECONDS:  int = 60
BACKOFF_MAX_SECONDS:   int = 30 * 60

# ═══════════════════════════════════════════════════════════════════════════
# Dallas cities (foreclosure-PDF directories, §A.5)
# ═══════════════════════════════════════════════════════════════════════════

DALLAS_CITIES: list[str] = [
    "Addison", "Balch-Springs", "Carrollton", "Cedar-Hill", "Coppell",
    "Dallas", "DeSoto", "Duncanville", "Farmers-Branch", "Garland",
    "Glenn-Heights", "Grand-Prairie", "Hutchins", "Irving", "Lancaster",
    "Mesquite", "Other", "Richardson", "Rowlett", "Sachse",
    "Seagoville", "Wilmer", "Wylie",
]

# Variant spellings observed on dallascounty.org foreclosure-PDF index.
DALLAS_CITY_ALIASES: dict[str, str] = {
    "Glenn Heights": "Glenn-Heights",
}

# ═══════════════════════════════════════════════════════════════════════════
# Instrument code map (§A.3)
# ═══════════════════════════════════════════════════════════════════════════
# Harris-Intel category label → list of Dallas literal codes that map to it.
# VERIFIED 2026-05-13 against publicsearch.us RP (Real Property) department
# document-type table.
#
# Codes appearing in INSTRUMENT_CODES but absent from DALLAS_CODE_DISPLAY_NAMES
# below (e.g. NOF) are still valid labels for records from other sources —
# the foreclosure-PDF walker, for example, tags its records dallas_code="NOF".
# Such codes are simply skipped during publicsearch.us iteration.

INSTRUMENT_CODES: dict[str, list[str]] = {
    "L/P":    ["LP", "RLP"],
    "NOTICE": ["NOF"],                                  # PDF walker only
    "TRSALE": ["TD"],
    "LIEN":   ["LN", "LC", "ML"],
    "T/L":    ["TXL"],
    "JUDGE":  ["JUD"],
    "A/J":    ["AJ"],
    "PROB":   ["PB"],
    "DEED":   ["WD", "QCD", "SD", "DE", "SW", "GN"],    # was [D, WD, SWD, GWD, QCD, SD]
    "BNKRCY": ["BR"],
    "LEVY":   ["SZS"],                                  # removed TXL (it's in T/L)
    "REL":    ["REL"],
    # FTL category removed — no "FEDERAL TAX LIEN" code exists in Dallas RP
}

# Code → publicsearch.us display name. Used to type into the doc-type
# typeahead filter, since the filter matches against display names rather
# than literal codes. VERIFIED 2026-05-13 from window.__data.configuration
# embedded on /results pages.
#
# Codes NOT in this map are not searchable on publicsearch.us:
#   - NOF: Notice of Foreclosure (handled by scraper/foreclosures_ps.py
#          via the publicsearch.us Foreclosures *department*, not the
#          Real-Property doctype filter).
#
# Routine-transaction codes intentionally OMITTED (decision PR 12.8 —
# normal property transfers, not motivated-seller signals):
#   - WD  (WARRANTY DEED)              ─┐ Normal residential sales — the
#   - QCD (QUIT CLAIM DEED)             │ buyer just bought, NOT a
#   - DE  (DEED, generic)               │ motivated seller. WD alone was
#   - SW  (SPECIAL WARRANTY DEED)       │ hitting the 500-record/run cap.
#   - GN  (GENERAL WARRANTY DEED)      ─┘
#   - SD  (SHERIFF'S DEED)             ─┐ Post-sale deed transfers; the
#   - TD  (TRUSTEE'S/SUBSTITUTE         │ sale already happened. Buyer
#          TRUSTEE'S DEED)             ─┘ is usually an investor competitor.
#
# Medium-signal codes intentionally OMITTED (decision PR 12.9 — operator
# narrowed scope to HIGH-signal motivated-seller distress only):
#   - JUD (JUDGMENT)                    ─┐ Money judgments not necessarily
#   - LN  (LIEN, generic)               │ tied to property distress; often
#   - ML  (MECHANIC'S LIEN ...)         │ resolved via REL; LC dominated by
#   - LC  (LIEN AFFIDAVIT/CLAIM/NOTICE)─┘ city/utility/HOA liens (low ROI).
#
# To re-enable any of the above, add the literal display name back here
# (must match publicsearch.us's doctype-typeahead string character for
# character — see the source-of-truth comment above).
DALLAS_CODE_DISPLAY_NAMES: dict[str, str] = {
    # ── HIGH-signal motivated-seller distress ──
    "LP":  "LIS PENDENS (NOTICE OF)",        # lawsuit affecting title
    "TXL": "TAX LIEN",                       # owner can't pay property tax
    "AJ":  "ABSTRACT OF JUDGMENT",           # creditor judgment attached to property
    "PB":  "PROBATE PROCEEDINGS",            # death of owner → heirs liquidating
    "BR":  "BANKRUPTCY PROCEEDINGS",         # owner in bankruptcy
    "SZS": "SEIZURE & SALE",                 # IRS / govt forced sale

    # ── Suppression signals (mark earlier records inactive) ──
    "REL": "RELEASE",                        # lien released — resolves an earlier LN/etc
    "RLP": "RELEASE OF LIS PENDENS",         # suit dropped — resolves an earlier LP
}

# Codes that mark a previously-seen record as inactive.
SUPPRESSION_CODES: set[str] = {"REL", "RLP"}

# Category pair that earns the LP+FC combo bonus (§E.4).
LP_FC_PAIR: tuple[str, str] = ("L/P", "NOTICE")

# ═══════════════════════════════════════════════════════════════════════════
# HOA detection (§3.3.1)
# ═══════════════════════════════════════════════════════════════════════════

# Seed denylist; populated with real Dallas-area HOAs during Phase 4.1.
HOA_DENYLIST: list[str] = []

# Regex patterns that strongly indicate an HOA / association entity.
# Patterns are case-insensitive (compiled with re.IGNORECASE in normalize.py).
HOA_PATTERNS: list[str] = [
    r"\bHOA\b",
    r"\bHomeowners?[\s\-]?Association\b",
    r"\bProperty[\s\-]?Owners?[\s\-]?Association\b",
    r"\bCondominium[\s\-]?Association\b",
    r"\bCondo[\s\-]?Association\b",
    r"\bTownhomes?[\s\-]?Association\b",
    r"\bNeighborhood[\s\-]?Association\b",
    r"\bMaster[\s\-]?Association\b",
    r"\bCommunity[\s\-]?Association\b",
    r"\bOwners?[\s\-]?Association\b",
]

# ═══════════════════════════════════════════════════════════════════════════
# Filesystem paths
# ═══════════════════════════════════════════════════════════════════════════

REPO_ROOT:      Path = Path(__file__).parent.parent
SCRAPER_DIR:    Path = Path(__file__).parent
DATA_DIR:       Path = REPO_ROOT / "data"
EXPORTS_DIR:    Path = REPO_ROOT / "exports"
DASHBOARD_DIR:  Path = REPO_ROOT / "dashboard"
TESTS_DIR:      Path = REPO_ROOT / "tests"

RECORDS_JSON:   Path = DATA_DIR / "records.json"

# Local cache for DCAD bulk ZIP. In CI this is keyed via actions/cache.
DCAD_CACHE_DIR: Path = Path(
    os.getenv("DCAD_CACHE_DIR", str(Path.home() / ".dcad_cache"))
)

# Ensure local-run directories exist on import.
for _d in (DATA_DIR, EXPORTS_DIR, DCAD_CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ============================================================================
# Probate (re:SearchTX) - PR 4.1
# ============================================================================

# Master kill-switch for the probate stage. When False, scraper/probate.py
# returns an empty list immediately without any HTTP call. Default False so
# probate is opt-in until full integration cutover.
PROBATE_ENABLED: bool = os.getenv("PROBATE_ENABLED", "").strip().lower() in {
    "1", "true", "yes",
}

# Credentials for re:SearchTX login via Tyler Odyssey IdP.
# In GitHub Actions: set as repository secrets RESEARCH_TX_EMAIL and
# RESEARCH_TX_PASSWORD. Locally for testing: set as env vars before
# running probe_probate_auth_live.py.
PROBATE_EMAIL_ENV:    str = "RESEARCH_TX_EMAIL"
PROBATE_PASSWORD_ENV: str = "RESEARCH_TX_PASSWORD"
