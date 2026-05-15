"""Buy-box filter.

Annotates each canonical record with whether it falls within the operator's
acquisition criteria. The filter is operator-configurable via environment
variables OR an optional ``BuyBox`` constructor.

Design choices
--------------
1. **Annotate, don't destroy.** Records outside the buy-box receive
   ``in_buy_box=False`` plus a list of human-readable reasons. They are
   still written to ``records.json`` so the operator can audit what was
   excluded. The CSV export (the actual call list for GHL) uses only
   in-buy-box records.

2. **Missing data is not exclusion.** If a record has no
   ``dcad_market_value``, the price check is skipped (we don't exclude
   on the basis of "we don't know"). The same applies to missing ZIP
   AND -- as of Phase 3.E -- missing or unparseable filing_date.

   The previous strict semantic (Phase 3.D) treated parser uncertainty
   as lead invalidity. That was wrong: ~40% of foreclosure PDFs don't
   carry an extractable notice-filing date in their text, but those
   records are still in the current foreclosure index, meaning they
   are CURRENT foreclosures we just can't date-extract from the PDF.

   Phase 3.E semantic:
     * verified-fresh (date parsed AND within window)  -> in buy-box
     * unknown (date missing or unparseable)           -> in buy-box (pass through)
     * verified-stale (date parsed AND older)          -> excluded

3. **Environment-driven config.** The buy-box config comes from env
   vars by default. This lets the operator tune the filter without code
   changes. The defaults are deliberately permissive.

Public API
----------
``BuyBox`` - dataclass holding the filter criteria.
``BuyBox.from_env()`` - read criteria from environment variables.
``BuyBox.evaluate(record)`` - return ``(in_buy_box: bool, reasons: list[str])``.
``annotate_records(records, buy_box=None) -> dict`` - annotate in place,
return summary stats.

Environment variables
---------------------
``BUY_BOX_MIN_PRICE``                  (float, optional)   minimum DCAD market value
``BUY_BOX_MAX_PRICE``                  (float, optional)   maximum DCAD market value
``BUY_BOX_ZIP_ALLOWLIST``              (CSV string, opt.)  e.g. ``75201,75202,75204``
``BUY_BOX_ZIP_DENYLIST``               (CSV string, opt.)  excluded ZIPs
``BUY_BOX_EXCLUDE_COMMERCIAL``         (bool, default True)
``BUY_BOX_REQUIRE_DCAD_MATCH``         (bool, default False)
    When True, records that failed DCAD enrichment (no ``dcad_account``)
    are flagged as out-of-buy-box. Useful for "only act on enriched
    leads" workflows. Defaults False so the operator can still see the
    unmatched leads in the dashboard.
``BUY_BOX_FILING_DATE_MAX_AGE_DAYS``   (int, opt.)
    When set, records with a CONFIRMED filing_date older than N
    calendar days from today are excluded.
    Records with missing or unparseable filing_date are NOT excluded --
    parser uncertainty doesn't mean the lead is invalid. Off by default
    (unset).

Integration
-----------
Wire into ``scraper/main.py`` AFTER scoring/enrichment, BEFORE output:

    from scraper import buy_box
    bb = buy_box.BuyBox.from_env()
    summary = buy_box.annotate_records(records, bb)
    logger.info("Buy-box: %s", summary)

The ``annotate_records`` call mutates each record in place to add
``in_buy_box`` and ``buy_box_reasons`` fields. Downstream code (CSV
writer, dashboard) reads those fields to filter.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Env-var parsing helpers
# ----------------------------------------------------------------------------


def _float_env(key: str) -> float | None:
    raw = os.environ.get(key)
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except ValueError:
        logger.warning(
            "Ignoring %s=%r - could not parse as float", key, raw
        )
        return None


def _int_env(key: str) -> int | None:
    raw = os.environ.get(key)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(
            "Ignoring %s=%r - could not parse as int", key, raw
        )
        return None


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _set_env(key: str) -> set[str] | None:
    raw = os.environ.get(key)
    if not raw:
        return None
    items = {item.strip() for item in raw.split(",") if item.strip()}
    return items or None


# ----------------------------------------------------------------------------
# ZIP extraction
# ----------------------------------------------------------------------------


_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def extract_zip(address: str | None) -> str | None:
    """Return the 5-digit ZIP from a US address string, or None.

    Handles ``"123 MAIN ST DALLAS TX 75201"``,
    ``"123 MAIN ST, DALLAS, TX 75201-1234"``, and address fragments
    where the ZIP is the only zip-shaped token.
    """
    if not address:
        return None
    m = _ZIP_RE.search(address)
    return m.group(1) if m else None


# ----------------------------------------------------------------------------
# BuyBox dataclass
# ----------------------------------------------------------------------------


@dataclass
class BuyBox:
    """Operator's acquisition criteria.

    Any field set to None disables that check.
    """

    min_price: float | None = None
    max_price: float | None = None
    zip_allowlist: set[str] | None = None
    zip_denylist: set[str] | None = None
    exclude_commercial: bool = True
    require_dcad_match: bool = False
    filing_date_max_age_days: int | None = None
    # Extension hook: a list of (predicate_name, callable) for custom checks.
    # Each callable takes a record dict and returns either None (pass) or
    # a string reason (fail). Allows operator to add bespoke filters
    # without modifying this module.
    custom_checks: list = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "BuyBox":
        return cls(
            min_price=_float_env("BUY_BOX_MIN_PRICE"),
            max_price=_float_env("BUY_BOX_MAX_PRICE"),
            zip_allowlist=_set_env("BUY_BOX_ZIP_ALLOWLIST"),
            zip_denylist=_set_env("BUY_BOX_ZIP_DENYLIST"),
            exclude_commercial=_bool_env("BUY_BOX_EXCLUDE_COMMERCIAL", True),
            require_dcad_match=_bool_env("BUY_BOX_REQUIRE_DCAD_MATCH", False),
            filing_date_max_age_days=_int_env("BUY_BOX_FILING_DATE_MAX_AGE_DAYS"),
        )

    def describe(self) -> str:
        """Human-readable summary of active criteria.

        Only mentions criteria that are actually enforced today. The
        ``exclude_commercial`` flag is intentionally NOT mentioned -
        commercial detection requires DCAD ``COM_DETAIL`` cross-reference
        which is wired in Phase 0.B, not 0.A. Mentioning it here would
        misrepresent what the filter is doing.

        ASCII-only output (no Unicode comparators) so the log doesn't
        crash on PowerShell's cp1252 default code page.
        """
        bits = []
        if self.min_price is not None:
            bits.append(f"price>=${self.min_price:,.0f}")
        if self.max_price is not None:
            bits.append(f"price<=${self.max_price:,.0f}")
        if self.zip_allowlist:
            bits.append(f"zip-in:{{{len(self.zip_allowlist)}}}")
        if self.zip_denylist:
            bits.append(f"zip-not-in:{{{len(self.zip_denylist)}}}")
        if self.require_dcad_match:
            bits.append("dcad-matched-only")
        if self.filing_date_max_age_days is not None:
            # Phase 3.E semantic: excludes only CONFIRMED-stale records;
            # missing/unparseable filing dates pass through.
            bits.append(f"reject-if-filed>{self.filing_date_max_age_days}d-ago")
        if self.custom_checks:
            bits.append(f"{len(self.custom_checks)} custom checks")
        return ", ".join(bits) if bits else "no filters configured"

    def is_configured(self) -> bool:
        """True if any criterion is set."""
        return any(
            (
                self.min_price is not None,
                self.max_price is not None,
                bool(self.zip_allowlist),
                bool(self.zip_denylist),
                self.require_dcad_match,
                self.filing_date_max_age_days is not None,
                bool(self.custom_checks),
                # exclude_commercial defaults True; only counts if there's
                # also some way to determine commercial-ness (deferred to
                # Phase 0.B when we wire DCAD COM_DETAIL).
            )
        )

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, record: dict, today: date | None = None) -> tuple[bool, list[str]]:
        """Return ``(in_buy_box, reasons_if_excluded)``.

        Reasons are formatted as ``"<category>: <detail>"`` so callers
        can bucket by category (the prefix before ``:``) when aggregating.

        Behavior:
          - If no criteria are configured, every record is in-buy-box.
          - Missing data (None values) does not trigger exclusion EXCEPT
            when ``require_dcad_match`` is True and ``dcad_account`` is None.
          - For ``filing_date_max_age_days`` (Phase 3.E): only records
            with a CONFIRMED parseable filing_date older than the cutoff
            are excluded. Missing/unparseable dates pass through because
            parser uncertainty is not lead invalidity.

        Args:
            record: canonical record dict.
            today: explicit reference date for the filing-date check.
                Defaults to ``date.today()``. Exposed for deterministic
                unit tests.
        """
        reasons: list[str] = []

        # Price band check
        value = record.get("dcad_market_value")
        if value is not None:
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                value_f = None
            if value_f is not None:
                if self.min_price is not None and value_f < self.min_price:
                    reasons.append(
                        f"price below min: ${value_f:,.0f} < ${self.min_price:,.0f}"
                    )
                if self.max_price is not None and value_f > self.max_price:
                    reasons.append(
                        f"price above max: ${value_f:,.0f} > ${self.max_price:,.0f}"
                    )

        # ZIP check (allow/deny). Pull from the canonical record's address
        # or normalized address; whichever has a ZIP.
        zip_code = extract_zip(record.get("address")) or extract_zip(
            record.get("address_normalized")
        )
        if zip_code:
            if self.zip_allowlist and zip_code not in self.zip_allowlist:
                reasons.append(f"zip not in allowlist: {zip_code}")
            if self.zip_denylist and zip_code in self.zip_denylist:
                reasons.append(f"zip in denylist: {zip_code}")

        # DCAD-match requirement
        if self.require_dcad_match and not record.get("dcad_account"):
            reasons.append("no DCAD match: dcad_account is empty")

        # Filing-date age check (Phase 3.E: PERMISSIVE on missing data;
        # only excludes CONFIRMED-stale dates)
        if self.filing_date_max_age_days is not None:
            ref_today = today or date.today()
            cutoff = ref_today - timedelta(days=self.filing_date_max_age_days)
            parsed = _parse_iso_date(record.get("filing_date"))
            # KEY SEMANTIC: only confirmed-old dates get excluded.
            # Missing/unparseable dates pass through silently because
            # they represent parser uncertainty, not lead invalidity.
            if parsed is not None and parsed < cutoff:
                reasons.append(
                    f"filing date too old: {parsed.isoformat()} < {cutoff.isoformat()} "
                    f"({(ref_today - parsed).days}d ago > {self.filing_date_max_age_days}d limit)"
                )

        # Custom checks
        for name, check in self.custom_checks:
            try:
                result = check(record)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Custom check %r raised %s; skipping", name, exc
                )
                continue
            if result:
                reasons.append(f"{name}: {result}")

        return (len(reasons) == 0, reasons)


# ----------------------------------------------------------------------------
# Date parsing helper
# ----------------------------------------------------------------------------


def _parse_iso_date(raw) -> date | None:
    """Parse an ISO-format date string into a ``date`` object. None on failure.

    Accepts ``"YYYY-MM-DD"`` (the canonical record format). Returns None for
    empty strings, None, or any unparseable input. Does NOT raise.
    """
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    try:
        s = str(raw).strip()
        if not s:
            return None
        # Accept YYYY-MM-DD (canonical) or YYYY/MM/DD just in case.
        parts = re.split(r"[-/]", s)
        if len(parts) != 3:
            return None
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Bulk annotation
# ----------------------------------------------------------------------------


def annotate_records(
    records: Iterable[dict],
    buy_box: BuyBox | None = None,
    today: date | None = None,
) -> dict:
    """Annotate each record with ``in_buy_box`` and ``buy_box_reasons``.

    Returns a summary dict for logging / Discord notification:
        {
            "configured":       bool,
            "criteria":         "human-readable string",
            "total":            int,
            "in_buy_box":       int,
            "outside_buy_box":  int,
            "top_exclusion_reasons":  list[tuple[reason, count]],
        }

    If ``buy_box`` is None, uses ``BuyBox.from_env()``. ``today`` is
    passed through to per-record evaluate() for the filing-date age check.
    """
    if buy_box is None:
        buy_box = BuyBox.from_env()

    records_list = list(records)  # we may iterate twice for reason counts

    if not buy_box.is_configured():
        # Mark everything as in-buy-box so downstream consumers always
        # see the field. Saves them from having to handle None.
        for rec in records_list:
            rec["in_buy_box"] = True
            rec["buy_box_reasons"] = []
        return {
            "configured": False,
            "criteria": buy_box.describe(),
            "total": len(records_list),
            "in_buy_box": len(records_list),
            "outside_buy_box": 0,
            "top_exclusion_reasons": [],
        }

    in_count = 0
    out_count = 0
    reason_counts: dict[str, int] = {}

    for rec in records_list:
        in_bb, reasons = buy_box.evaluate(rec, today=today)
        rec["in_buy_box"] = in_bb
        rec["buy_box_reasons"] = reasons
        if in_bb:
            in_count += 1
        else:
            out_count += 1
            # Bucket reasons by their first word so similar reasons
            # collapse (e.g. multiple "value $X below min" entries).
            for reason in reasons:
                bucket = reason.split(":")[0].strip()
                reason_counts[bucket] = reason_counts.get(bucket, 0) + 1

    top_reasons = sorted(
        reason_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:10]

    return {
        "configured": True,
        "criteria": buy_box.describe(),
        "total": len(records_list),
        "in_buy_box": in_count,
        "outside_buy_box": out_count,
        "top_exclusion_reasons": top_reasons,
    }
