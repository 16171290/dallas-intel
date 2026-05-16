"""
DCAD bulk-data ZIP fetcher and parser.

Per Â§3.7.6 = (a) â€” direct download from DCAD's /DataProducts.aspx with a
weekly local cache. No Google Drive mirror.

CONFIRMED (audit-derived):
  - DCAD bulk data is free and explicitly sanctioned (Â§B.2.3, Â§A.2.3)
  - robots.txt permits /DataProducts.aspx

STRONG INFERENCE (Phase 1 verification step D.1.1 confirms):
  - The DataProducts.aspx page lists per-year ZIPs as <a> links
  - The exact column schema inside each table file (Â§A.4)
  - File extension is .csv or .txt with comma delimiter

Fallback for failed discovery: set DCAD_ZIP_URL env var to the literal
ZIP download URL, sidestepping discovery entirely.
"""

import logging
import os
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

from . import config, normalize
from .normalize import normalize_address

logger = logging.getLogger(__name__)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Public API
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def fetch_dcad_zip(
    year: Optional[int] = None,
    force_refresh: bool = False,
) -> Path:
    """Fetch the DCAD bulk-data ZIP for ``year`` (default current target year).

    Returns the cached local path. The cache key is ``dcad-{year}-week{YYYYWW}.zip``,
    so the same ZIP is reused for the entire ISO week and refreshed on Monday.
    ``force_refresh=True`` bypasses the cache.

    Resolution order for the ZIP download URL:
      1. ``DCAD_ZIP_URL`` env var (manual override)
      2. HTML discovery on ``DCAD_DATAPRODUCTS_URL``
      3. Raise :class:`DCADFetchError` with a remediation hint
    """
    target_year = year or config.DCAD_TARGET_YEAR
    cache_path = _cache_path(target_year)

    if cache_path.exists() and not force_refresh:
        logger.info("DCAD ZIP cache hit: %s", cache_path.name)
        return cache_path

    zip_url = _resolve_zip_url(target_year)
    logger.info("Downloading DCAD ZIP from %s", zip_url)
    _download_zip(zip_url, cache_path)
    return cache_path


def parse_dcad_tables(zip_path: Path) -> dict[str, pd.DataFrame]:
    """Extract and parse the CSV-like members of a DCAD ZIP.

    Returns a dict keyed by uppercased filename without extension:

        {"ACCOUNT_INFO": <DataFrame>, "MULTI_OWNER": <DataFrame>, ...}

    All columns are read as strings (``dtype=str``) to avoid pandas's
    aggressive type-inference dropping leading zeros from account numbers
    and ZIP codes. Conversion to numeric happens downstream where needed.

    Skips obvious non-data members (readme, schema, layout, *.pdf).
    """
    tables: dict[str, pd.DataFrame] = {}

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if not _is_data_member(member):
                continue
            name = Path(member).stem.upper()
            logger.debug("Parsing %s", member)
            with zf.open(member) as f:
                df = pd.read_csv(
                    f,
                    sep=",",
                    dtype=str,
                    keep_default_na=False,
                    on_bad_lines="warn",
                    encoding_errors="replace",
                )
            tables[name] = df

    logger.info("Parsed %d DCAD tables: %s", len(tables), sorted(tables.keys()))
    return tables


def build_address_index(tables: dict[str, pd.DataFrame]) -> dict[str, str]:
    """Build a normalized-address â†’ ACCOUNT_NUM lookup map.

    Uses the ``ACCOUNT_INFO`` table. CONFIRMED schema (verified 2026-05-13
    against DCAD 2025 bundle):

      - ``ACCOUNT_NUM``       â€” primary key
      - ``STREET_NUM``         â€” house number
      - ``STREET_HALF_NUM``    â€” fractional component (e.g. "1/2")
      - ``FULL_STREET_NAME``   â€” directional + name + suffix in one field
                                 (e.g. "N MAIN ST")

    See docs/DCAD_SCHEMA.md for the full table layout.
    """
    if "ACCOUNT_INFO" not in tables:
        logger.warning("ACCOUNT_INFO table not in DCAD bundle; address index empty")
        return {}

    df = tables["ACCOUNT_INFO"]
    needed = {"ACCOUNT_NUM", "STREET_NUM", "FULL_STREET_NAME"}
    missing = needed - set(df.columns)
    if missing:
        logger.warning(
            "ACCOUNT_INFO missing expected columns %s; address index empty. "
            "Update docs/DCAD_SCHEMA.md with the actual column names.",
            missing,
        )
        return {}

    index: dict[str, str] = {}
    has_half = "STREET_HALF_NUM" in df.columns

    for _, row in df.iterrows():
        num   = str(row.get("STREET_NUM", "")).strip()
        half  = str(row.get("STREET_HALF_NUM", "")).strip() if has_half else ""
        name  = str(row.get("FULL_STREET_NAME", "")).strip()
        raw   = " ".join(p for p in (num, half, name) if p).strip()
        norm  = normalize.normalize_address(raw)
        if norm:
            index[norm] = str(row["ACCOUNT_NUM"]).strip()

    logger.info("Address index built: %d unique addresses", len(index))
    return index


class DCADFetchError(Exception):
    """Raised when DCAD bulk-data fetching fails (network or discovery)."""


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Internals
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _cache_path(year: int) -> Path:
    """Cache location: ``<DCAD_CACHE_DIR>/dcad-{year}-week{YYYYWW}.zip``."""
    iso = date.today().isocalendar()
    week_str = f"{iso.year}{iso.week:02d}"
    return config.DCAD_CACHE_DIR / f"dcad-{year}-week{week_str}.zip"


def _resolve_zip_url(year: int) -> str:
    """Return the ZIP URL via env var or discovery."""
    env_url = os.getenv("DCAD_ZIP_URL", "").strip()
    if env_url:
        logger.info("Using DCAD_ZIP_URL override")
        return env_url
    return _discover_zip_url(year)


def _discover_zip_url(year: int) -> str:
    """Scrape DataProducts.aspx to find the year's comma-delimited ZIP.

    CONFIRMED page structure (web-fetched 2026-05-13):
    DataProducts.aspx contains several ZIP sections. The property data
    we want has href ending in ``DCAD{YEAR}_CURRENT.zip`` (case-insensitive).
    Other sections (BPP, ARB, appraisal notice mailings, fixed-format
    certified rolls) are explicitly excluded via the deny-list.

    Resolution order:
      1. Primary â€” first href containing ``dcad{year}_current.zip``
      2. Fallback â€” any year + "Data Files" / "Comma Delimited" link
                    that isn't on the deny-list
      3. Raise DCADFetchError with a remediation hint
    """
    resp = requests.get(
        config.DCAD_DATAPRODUCTS_URL,
        headers={"User-Agent": config.USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "html.parser")
    year_str = str(year)
    target_pattern = f"dcad{year}_current.zip"

    # Categories present on DataProducts.aspx that are NOT the property
    # database. If any of these terms appear in the link text or href,
    # skip the candidate.
    deny_terms = (
        "arb",                 # Appraisal Review Board (protests)
        "bpp",                 # Business Personal Property â€” separate dataset
        "appraisal notice",    # Mail-1/2/3 annual notice data
        "notice data",         # same as above, alternate phrasing
        "real property cert",  # fixed-format certified appraisal roll
        "fixed format",        # all fixed-format datasets
        "appraisal roll",      # certified roll snapshots
    )

    def _on_deny_list(text_lower: str, href_lower: str) -> bool:
        return any(term in text_lower or term in href_lower for term in deny_terms)

    # â”€â”€â”€ Tier 1: exact DCAD{YEAR}_CURRENT.zip match â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        href_lower = href.lower()
        text = a.get_text(strip=True)
        text_lower = text.lower()

        if _on_deny_list(text_lower, href_lower):
            continue
        if target_pattern not in href_lower:
            continue

        full_url = href if href_lower.startswith("http") else urljoin(
            config.DCAD_BASE + "/", href
        )
        logger.info(
            "DCAD ZIP URL resolved (tier-1 exact match) for year %s: %s "
            "(link text=%r)",
            year, full_url, text,
        )
        return full_url

    # â”€â”€â”€ Tier 2: year + "Data Files" / "Comma Delimited" fallback â”€â”€â”€â”€â”€â”€
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        href_lower = href.lower()
        text = a.get_text(strip=True)
        text_lower = text.lower()

        if _on_deny_list(text_lower, href_lower):
            continue
        if year_str not in text and year_str not in href:
            continue
        if "data files" not in text_lower and "comma delimited" not in text_lower:
            continue
        if not (href_lower.endswith(".zip") or ".zip" in href_lower):
            continue

        full_url = href if href_lower.startswith("http") else urljoin(
            config.DCAD_BASE + "/", href
        )
        logger.warning(
            "DCAD ZIP URL resolved (tier-2 fallback) for year %s: %s "
            "(link text=%r). DCAD may have renamed the canonical _CURRENT "
            "file â€” verify and consider updating the resolver.",
            year, full_url, text,
        )
        return full_url

    raise DCADFetchError(
        f"Could not find a DCAD{year}_CURRENT.zip link on "
        f"{config.DCAD_DATAPRODUCTS_URL}. Inspect the page manually and "
        f"set the DCAD_ZIP_URL env var to the direct ZIP URL."
    )


def _download_zip(url: str, dest: Path) -> None:
    """Stream-download a ZIP to ``dest`` atomically (tmp + rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")

    try:
        with requests.get(
            url,
            stream=True,
            headers={"User-Agent": config.USER_AGENT},
            timeout=300,
        ) as resp:
            resp.raise_for_status()
            total = 0
            with tmp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MB
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
            logger.info("Downloaded %s (%d bytes)", dest.name, total)
        tmp.replace(dest)
    except Exception:
        # Clean up partial file on any failure
        if tmp.exists():
            tmp.unlink()
        raise


_SKIP_PATTERNS = ("readme", "field", "schema", "layout", "documentation")


def _is_data_member(member_name: str) -> bool:
    """True if a ZIP member looks like a data file (CSV/TXT, not docs)."""
    p = Path(member_name)
    if p.suffix.lower() not in (".csv", ".txt"):
        return False
    name_lower = p.stem.lower()
    return not any(skip in name_lower for skip in _SKIP_PATTERNS)


def build_account_index(tables: "dict[str, pd.DataFrame]") -> "dict[str, dict]":
    """Build ``{ACCOUNT_NUM: {address_normalized, address_city, address_state, address_zip}}``.

    The companion to ``build_address_index``: same ``ACCOUNT_INFO`` source,
    same address-normalization rules, but keyed the *other* direction so a
    caller holding a DCAD account number can recover the property's
    canonical address fields.

    Added for PR 3 of the probate integration. Probate canonicalization
    (PR 5) matches a decedent name to a list of DCAD ``ACCOUNT_NUM`` values
    via ``dcad_owner_index``; we then need each account's address parts so
    the resulting canonical record can carry a full address all the way to
    the CSV writer.

    Defensive about missing optional columns: ``PROPERTY_CITY`` and
    ``PROPERTY_ZIPCODE`` are common in DCAD's bundle but the function
    tolerates their absence and emits ``None`` for those fields rather
    than failing.

    Args:
        tables: dict of DCAD tables as returned by ``parse_dcad_tables``.

    Returns:
        ``{account_num: {"address_normalized", "address_city",
        "address_state", "address_zip"}}``. Empty dict if ACCOUNT_INFO is
        absent or has no usable rows.
    """
    df = tables.get("ACCOUNT_INFO")
    if df is None or df.empty:
        return {}

    needed_min = {"ACCOUNT_NUM", "STREET_NUM", "FULL_STREET_NAME"}
    missing = needed_min - set(df.columns)
    if missing:
        logger.warning(
            "build_account_index: ACCOUNT_INFO missing required columns %s; "
            "returning empty index",
            sorted(missing),
        )
        return {}

    has_city = "PROPERTY_CITY" in df.columns
    has_zip  = "PROPERTY_ZIPCODE" in df.columns
    has_half = "STREET_HALF_NUM" in df.columns

    index: "dict[str, dict]" = {}
    for row in df.itertuples(index=False):
        # itertuples uses underscored attribute names for columns with spaces,
        # but DCAD's columns are already underscore-safe so direct access works
        account = str(getattr(row, "ACCOUNT_NUM", "") or "").strip()
        if not account:
            continue

        num   = str(getattr(row, "STREET_NUM", "") or "").strip()
        half  = str(getattr(row, "STREET_HALF_NUM", "") or "").strip() if has_half else ""
        name  = str(getattr(row, "FULL_STREET_NAME", "") or "").strip()

        # Build a raw "NNN HALF NAME" then run it through the same address
        # normalizer the foreclosure pipeline uses, so we land on the same
        # canonical form (collisions with build_address_index will tie out).
        parts = [p for p in (num, half, name) if p]
        if not parts:
            continue
        raw_addr = " ".join(parts)
        norm = normalize_address(raw_addr) or None

        city = str(getattr(row, "PROPERTY_CITY", "") or "").strip() if has_city else ""
        zipc = str(getattr(row, "PROPERTY_ZIPCODE", "") or "").strip() if has_zip else ""

        index[account] = {
            "address_normalized": norm,
            "address_city":       city or None,
            "address_state":      "TX",
            "address_zip":        zipc or None,
        }

    logger.info("Built DCAD account index: %d accounts", len(index))
    return index
