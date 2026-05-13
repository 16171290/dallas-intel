"""
DCAD bulk-data ZIP fetcher and parser.

Per §3.7.6 = (a) — direct download from DCAD's /DataProducts.aspx with a
weekly local cache. No Google Drive mirror.

CONFIRMED (audit-derived):
  - DCAD bulk data is free and explicitly sanctioned (§B.2.3, §A.2.3)
  - robots.txt permits /DataProducts.aspx

STRONG INFERENCE (Phase 1 verification step D.1.1 confirms):
  - The DataProducts.aspx page lists per-year ZIPs as <a> links
  - The exact column schema inside each table file (§A.4)
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

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

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
    """Build a normalized-address → ACCOUNT_NUM lookup map.

    Uses the ``ACCOUNT_INFO`` table per §A.4. STRONG INFERENCE on the
    column names; the function defensively checks for the expected
    columns and returns an empty dict if they're missing (so the
    pipeline degrades gracefully and the enrichment-hit-rate metric
    surfaces the schema mismatch).
    """
    if "ACCOUNT_INFO" not in tables:
        logger.warning("ACCOUNT_INFO table not in DCAD bundle; address index empty")
        return {}

    df = tables["ACCOUNT_INFO"]
    needed = {"ACCOUNT_NUM", "STREET_NUM", "STREET_NAME"}
    missing = needed - set(df.columns)
    if missing:
        logger.warning(
            "ACCOUNT_INFO missing expected columns %s; address index empty. "
            "Update §A.4 in docs/DCAD_SCHEMA.md with the actual column names.",
            missing,
        )
        return {}

    index: dict[str, str] = {}
    optional_cols = ("STREET_NUM", "PREFIX_DIR", "STREET_NAME", "STREET_SUFFIX", "SUFFIX_DIR")
    available = [c for c in optional_cols if c in df.columns]

    for _, row in df.iterrows():
        parts = [str(row.get(c, "")).strip() for c in available]
        raw = " ".join(p for p in parts if p).strip()
        norm = normalize.normalize_address(raw)
        if norm:
            index[norm] = str(row["ACCOUNT_NUM"]).strip()

    logger.info("Address index built: %d unique addresses", len(index))
    return index


class DCADFetchError(Exception):
    """Raised when DCAD bulk-data fetching fails (network or discovery)."""


# ═══════════════════════════════════════════════════════════════════════════
# Internals
# ═══════════════════════════════════════════════════════════════════════════

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

    STRONG INFERENCE — assumes links are <a> elements whose text or href
    references the year and "comma" or ".zip". Scores candidates and
    returns the highest. If nothing scores positive, raises.
    """
    resp = requests.get(
        config.DCAD_DATAPRODUCTS_URL,
        headers={"User-Agent": config.USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "lxml")
    year_str = str(year)
    candidates: list[tuple[int, str, str]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href:
            continue

        score = 0
        if year_str in text or year_str in href:
            score += 10
        text_lower = text.lower()
        href_lower = href.lower()
        if "comma" in text_lower:
            score += 5
        if "fixed" in text_lower:
            score -= 3  # prefer comma over fixed-width
        if href_lower.endswith(".zip"):
            score += 3
        if "zip" in href_lower:
            score += 1
        if score > 0:
            candidates.append((score, href, text))

    if not candidates:
        raise DCADFetchError(
            f"Could not auto-discover the {year} ZIP URL on "
            f"{config.DCAD_DATAPRODUCTS_URL}. Inspect the page manually "
            f"and set the DCAD_ZIP_URL env var to the direct ZIP URL."
        )

    candidates.sort(key=lambda x: -x[0])
    best_score, best_href, best_text = candidates[0]
    logger.info(
        "Discovered DCAD ZIP (score=%d, text=%r): %s",
        best_score, best_text, best_href,
    )

    if not best_href.lower().startswith("http"):
        best_href = urljoin(config.DCAD_BASE + "/", best_href)

    return best_href


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
