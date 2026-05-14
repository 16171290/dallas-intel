"""Tests for ``scraper.governmental_grantor``."""

from __future__ import annotations

import pytest

from scraper import governmental_grantor as gg


# ─────────────────────────────────────────────────────────────────────────────
# is_governmental_grantor — exact denylist
# ─────────────────────────────────────────────────────────────────────────────


class TestExactDenylist:
    def test_trinity_river_authority(self):
        assert gg.is_governmental_grantor("TRINITY RIVER AUTHORITY OF TEXAS")
        assert gg.is_governmental_grantor("TRINITY RIVER AUTHORITY")

    def test_case_insensitive(self):
        assert gg.is_governmental_grantor("trinity river authority of texas")
        assert gg.is_governmental_grantor("Trinity River Authority of Texas")

    def test_whitespace_tolerant(self):
        assert gg.is_governmental_grantor("  TRINITY RIVER AUTHORITY OF TEXAS  ")
        assert gg.is_governmental_grantor("TRINITY  RIVER   AUTHORITY OF TEXAS")

    def test_dallas_county(self):
        assert gg.is_governmental_grantor("DALLAS COUNTY")
        assert gg.is_governmental_grantor("COUNTY OF DALLAS")
        assert gg.is_governmental_grantor("DALLAS COUNTY TEXAS")

    def test_state_of_texas(self):
        assert gg.is_governmental_grantor("STATE OF TEXAS")
        assert gg.is_governmental_grantor("THE STATE OF TEXAS")

    def test_irs(self):
        assert gg.is_governmental_grantor("INTERNAL REVENUE SERVICE")
        assert gg.is_governmental_grantor("IRS")

    def test_hud(self):
        assert gg.is_governmental_grantor("HUD")
        assert gg.is_governmental_grantor(
            "DEPARTMENT OF HOUSING AND URBAN DEVELOPMENT"
        )

    def test_fannie_freddie(self):
        assert gg.is_governmental_grantor("FANNIE MAE")
        assert gg.is_governmental_grantor("FREDDIE MAC")
        assert gg.is_governmental_grantor("FEDERAL NATIONAL MORTGAGE ASSOCIATION")


# ─────────────────────────────────────────────────────────────────────────────
# is_governmental_grantor — pattern matching
# ─────────────────────────────────────────────────────────────────────────────


class TestPatternMatching:
    def test_city_of_known_texas_cities(self):
        assert gg.is_governmental_grantor("CITY OF DALLAS")
        assert gg.is_governmental_grantor("City of Garland")
        assert gg.is_governmental_grantor("CITY OF MESQUITE TX")
        assert gg.is_governmental_grantor("CITY OF GRAND PRAIRIE")
        assert gg.is_governmental_grantor("CITY OF FORT WORTH")

    def test_town_of_known_cities(self):
        assert gg.is_governmental_grantor("TOWN OF ADDISON")
        assert gg.is_governmental_grantor("TOWN OF HIGHLAND PARK")

    def test_school_districts(self):
        assert gg.is_governmental_grantor(
            "DALLAS INDEPENDENT SCHOOL DISTRICT"
        )
        assert gg.is_governmental_grantor("RICHARDSON ISD")
        assert gg.is_governmental_grantor("GARLAND ISD")
        assert gg.is_governmental_grantor(
            "Plano Independent School District"
        )

    def test_water_districts(self):
        assert gg.is_governmental_grantor(
            "NORTH TEXAS MUNICIPAL WATER DISTRICT"
        )
        assert gg.is_governmental_grantor(
            "DALLAS COUNTY WATER CONTROL AND IMPROVEMENT DISTRICT"
        )

    def test_river_authority(self):
        assert gg.is_governmental_grantor("TRINITY RIVER AUTHORITY OF TEXAS")
        # Pattern is `\w+\s+RIVER\s+AUTHORITY` — should match other names too
        assert gg.is_governmental_grantor("BRAZOS RIVER AUTHORITY")
        assert gg.is_governmental_grantor("COLORADO RIVER AUTHORITY")

    def test_tollway_authority(self):
        assert gg.is_governmental_grantor("NORTH TEXAS TOLLWAY AUTHORITY")
        assert gg.is_governmental_grantor("HARRIS COUNTY TOLLWAY AUTHORITY")

    def test_us_department(self):
        assert gg.is_governmental_grantor("U.S. DEPARTMENT OF HOUSING")
        assert gg.is_governmental_grantor("US DEPARTMENT OF VETERANS AFFAIRS")
        assert gg.is_governmental_grantor("DEPARTMENT OF TRANSPORTATION")

    def test_secretary_of(self):
        assert gg.is_governmental_grantor("SECRETARY OF HOUSING")
        assert gg.is_governmental_grantor("SECRETARY OF VETERANS AFFAIRS")

    def test_tax_collection_law_firms(self):
        """Tax-collection law firms acting as agents are noise from LP/AT
        search — the actual lead is the taxpayer, which is sourced from the
        Linebarger tax-sale source directly in Phase 4."""
        assert gg.is_governmental_grantor("LINEBARGER GOGGAN BLAIR SAMPSON")
        assert gg.is_governmental_grantor(
            "PERDUE BRANDON FIELDER COLLINS AND MOTT"
        )


# ─────────────────────────────────────────────────────────────────────────────
# False-positive avoidance
# ─────────────────────────────────────────────────────────────────────────────


class TestFalsePositives:
    def test_normal_individual_name(self):
        assert not gg.is_governmental_grantor("JOHN SMITH")
        assert not gg.is_governmental_grantor("MARY JONES")
        assert not gg.is_governmental_grantor("SMITH JOHN A")

    def test_normal_business_name(self):
        assert not gg.is_governmental_grantor("ACME PROPERTY INVESTMENTS LLC")
        assert not gg.is_governmental_grantor("DALLAS HOMES LLC")
        assert not gg.is_governmental_grantor("MAPLE STREET HOLDINGS")

    def test_business_with_authority_in_name(self):
        """``AUTHORITY`` alone must not trigger — must be qualified by RIVER
        or TOLLWAY etc."""
        assert not gg.is_governmental_grantor("SMITH AUTHORITY HOLDINGS LLC")
        assert not gg.is_governmental_grantor("AUTHORITY CAPITAL PARTNERS")

    def test_business_with_state_in_name(self):
        """``STATE`` alone must not match — only ``STATE OF TEXAS``."""
        assert not gg.is_governmental_grantor("LONE STAR STATE PROPERTIES")
        # A surname STATE — extremely rare but should not match.
        assert not gg.is_governmental_grantor("STATE FAMILY TRUST")

    def test_business_with_district_in_name(self):
        """``DISTRICT`` alone must not match — must be SCHOOL DISTRICT,
        WATER DISTRICT, MUNICIPAL UTILITY DISTRICT, etc."""
        assert not gg.is_governmental_grantor("DOWNTOWN DISTRICT LOFTS")
        assert not gg.is_governmental_grantor("DISTRICT INVESTMENTS LLC")

    def test_city_followed_by_non_tx_city(self):
        """``CITY OF`` followed by a non-TX city name should not match."""
        assert not gg.is_governmental_grantor("CITY OF GOD INVESTMENTS")
        assert not gg.is_governmental_grantor("CITY OF ANGELS HOLDINGS")

    def test_empty_and_none(self):
        assert not gg.is_governmental_grantor("")
        assert not gg.is_governmental_grantor(None)
        assert not gg.is_governmental_grantor("   ")


# ─────────────────────────────────────────────────────────────────────────────
# Filter partition helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestFilterGovernmentalRecords:
    def test_partitions_correctly(self):
        records = [
            {"grantor": "JOHN SMITH", "record_id": "1"},
            {"grantor": "TRINITY RIVER AUTHORITY OF TEXAS", "record_id": "2"},
            {"grantor": "MARY JONES", "record_id": "3"},
            {"grantor": "CITY OF DALLAS", "record_id": "4"},
        ]
        kept, removed = gg.filter_governmental_records(records)
        kept_ids = {r["record_id"] for r in kept}
        removed_ids = {r["record_id"] for r in removed}
        assert kept_ids == {"1", "3"}
        assert removed_ids == {"2", "4"}

    def test_keeps_records_with_no_grantor(self):
        """Missing grantor = don't conclude governmental. Keep for review."""
        records = [
            {"record_id": "1"},
            {"grantor": None, "record_id": "2"},
            {"grantor": "", "record_id": "3"},
        ]
        kept, removed = gg.filter_governmental_records(records)
        assert len(kept) == 3
        assert len(removed) == 0

    def test_empty_input(self):
        kept, removed = gg.filter_governmental_records([])
        assert kept == []
        assert removed == []

    def test_dual_check_grantee_option(self):
        """When ``also_check_grantee=True`` records with governmental
        grantees are also removed."""
        records = [
            {"grantor": "JOHN SMITH", "grantee": "STATE OF TEXAS", "record_id": "1"},
            {"grantor": "JOHN SMITH", "grantee": "MARY JONES", "record_id": "2"},
        ]
        # Default: only grantor checked, both pass through.
        kept_default, removed_default = gg.filter_governmental_records(records)
        assert len(kept_default) == 2
        # With also_check_grantee, record 1 is removed.
        kept_dual, removed_dual = gg.filter_governmental_records_dual(
            records, also_check_grantee=True
        )
        kept_ids = {r["record_id"] for r in kept_dual}
        removed_ids = {r["record_id"] for r in removed_dual}
        assert kept_ids == {"2"}
        assert removed_ids == {"1"}


# ─────────────────────────────────────────────────────────────────────────────
# Realistic Dallas scenarios (regression tests for observed patterns)
# ─────────────────────────────────────────────────────────────────────────────


class TestRealDallasPatterns:
    """Regression cases based on patterns observed in Dallas filings."""

    def test_trinity_river_lp_filings(self):
        """Multiple LPs filed by Trinity River Authority on the same date —
        these polluted the LP results during validation."""
        for variant in [
            "TRINITY RIVER AUTHORITY OF TEXAS",
            "Trinity River Authority of Texas",
            "TRINITY RIVER AUTHORITY",
        ]:
            assert gg.is_governmental_grantor(variant), variant

    def test_dallas_isd_tax_sale(self):
        assert gg.is_governmental_grantor("DALLAS INDEPENDENT SCHOOL DISTRICT")
        assert gg.is_governmental_grantor("DISD")

    def test_dcad_appraisal_district(self):
        assert gg.is_governmental_grantor("DALLAS CENTRAL APPRAISAL DISTRICT")
        assert gg.is_governmental_grantor("DCAD")

    def test_dart(self):
        assert gg.is_governmental_grantor("DALLAS AREA RAPID TRANSIT")
        assert gg.is_governmental_grantor("DART")
