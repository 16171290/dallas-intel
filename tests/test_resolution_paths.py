"""Tests for scraper.resolution_paths — Path B (raw_excerpt fallback).

Covers:
  - Address extraction regex (positives + negatives + venue/trustee rejects)
  - _is_suspect_address heuristic (which addresses trigger Path B)
  - run_path_b end-to-end against a fake address_index
  - Stats counters
  - Resolution history written for every considered record (audit trail)
"""

from __future__ import annotations

import pytest

from scraper import resolution_paths
from scraper.resolution import (
    PATH_B_RAW_EXCERPT,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    STATUS_SKIPPED,
    WARN_PATH_B_USED_ALTERNATE,
    get_history,
    get_warnings,
)


# ═══════════════════════════════════════════════════════════════════════════
# extract_clean_address_from_raw_excerpt — regex coverage
# ═══════════════════════════════════════════════════════════════════════════

class TestAddressExtraction:
    """Regex must extract real property addresses and reject venue/trustee."""

    def test_clean_address_with_zip_and_state(self):
        snip = "NOTICE OF FORECLOSURE | 43 VANGUARD WAY, DALLAS, TEXAS, 75243"
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is not None
        assert "VANGUARD" in result.upper()
        assert "75243" in result

    def test_clean_address_with_zip_no_state(self):
        snip = "NOTICE OF FORECLOSURE | 3332 Dilworth Drive, Grand Prairie, 75050"
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is not None
        assert "Dilworth" in result

    def test_clean_address_with_state_abbreviation(self):
        snip = "NOTICE | 1442 MARLENE PLACE, DESOTO, TX 75115 | BEING LOT 26"
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is not None
        assert "MARLENE" in result.upper()

    def test_clean_address_mid_string_lifted(self):
        snip = ("NOTICE OF FORECLOSURE | 2828 SOUTH LAKEVIEW DRIVE, CEDAR HILL, "
                "TEXAS, 75104 | other text after")
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is not None
        assert "LAKEVIEW" in result.upper()

    def test_rejects_courthouse_venue(self):
        """600 Commerce is the George Allen Courts Building (Class 25)."""
        snip = "Sale will occur at 600 Commerce Street, Dallas, TX 75202"
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is None

    def test_rejects_trustee_office_houston(self):
        """20405 State Hwy is Codilis & Moody (Class 1)."""
        snip = "Trustee: 20405 State Highway, Houston, TX 77070"
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is None

    def test_rejects_trustee_office_addison(self):
        snip = "Trustee mailing 15851 N Dallas Parkway, Addison, 75001"
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is None

    def test_returns_first_valid_match_when_multiple(self):
        """Property mentioned first, then a venue — first non-venue wins."""
        snip = ("3031 CREST RIDGE DRIVE, DALLAS, TEXAS, 75228 | "
                "Sale at 600 Commerce Street, Dallas, TX 75202")
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is not None
        assert "CREST RIDGE" in result.upper()

    def test_skips_venue_when_first_falls_back_to_real(self):
        """Venue listed FIRST, then real property — venue rejected, real returned."""
        snip = ("Sale at 600 Commerce Street, Dallas, TX 75202 | "
                "Property: 3031 CREST RIDGE DRIVE, DALLAS, TEXAS, 75228")
        result = resolution_paths.extract_clean_address_from_raw_excerpt(snip)
        assert result is not None
        assert "CREST RIDGE" in result.upper()

    def test_none_input(self):
        assert resolution_paths.extract_clean_address_from_raw_excerpt(None) is None

    def test_empty_string(self):
        assert resolution_paths.extract_clean_address_from_raw_excerpt("") is None

    def test_no_address_pattern(self):
        snip = "NOTICE OF FORECLOSURE | Subdivision - Name: PLEASANTWOOD Lot: 8"
        assert resolution_paths.extract_clean_address_from_raw_excerpt(snip) is None

    def test_address_without_zip_not_matched(self):
        """Pattern requires a 5-digit zip anchor to reduce FP."""
        snip = "4314 HAMILTON, DALLAS, TEXAS | other text"
        # No zip -> no match (acceptable conservative behavior for PR 2)
        assert resolution_paths.extract_clean_address_from_raw_excerpt(snip) is None


# ═══════════════════════════════════════════════════════════════════════════
# _is_suspect_address — trigger heuristic
# ═══════════════════════════════════════════════════════════════════════════

class TestSuspectAddressHeuristic:
    def test_empty_is_suspect(self):
        ok, reason = resolution_paths._is_suspect_address(None)
        assert ok is True and reason == "empty"
        ok, reason = resolution_paths._is_suspect_address("")
        assert ok is True and reason == "empty"

    def test_city_only_is_suspect(self):
        ok, reason = resolution_paths._is_suspect_address("DALLAS")
        assert ok is True and reason == "city_only"
        ok, reason = resolution_paths._is_suspect_address("IRVING")
        assert ok is True and reason == "city_only"

    def test_venue_address_is_suspect(self):
        ok, reason = resolution_paths._is_suspect_address("600 COMMERCE ST")
        assert ok is True and reason == "venue_or_trustee"

    def test_trustee_address_is_suspect(self):
        ok, reason = resolution_paths._is_suspect_address("20405 STATE HWY")
        assert ok is True and reason == "venue_or_trustee"

    def test_ocr_garbled_is_suspect(self):
        ok, reason = resolution_paths._is_suspect_address("43 VANGUARD OWE AY TALLY NTY")
        assert ok is True and reason == "ocr_garbled"

    def test_clean_address_not_suspect(self):
        ok, reason = resolution_paths._is_suspect_address("4314 HAMILTON")
        assert ok is False
        ok, reason = resolution_paths._is_suspect_address("2639 LENWAY ST")
        assert ok is False
        ok, reason = resolution_paths._is_suspect_address("9915 LINGO LANE")
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# run_path_b — end-to-end with fake address_index
# ═══════════════════════════════════════════════════════════════════════════

class TestRunPathB:
    def _build_address_index(self) -> dict[str, str]:
        """Mock DCAD address_index matching the normalized-address format
        normalize.normalize_address produces."""
        return {
            "43 VANGUARD WAY":        "acct_vanguard_43",
            "1442 MARLENE PL":        "acct_marlene_1442",
            "2828 S LAKEVIEW DR":     "acct_lakeview_2828",
            "3031 CREST RIDGE DR":    "acct_crestridge_3031",
            "6840 DART AVE":          "acct_dart_6840",
            # Note: '4314 HAMILTON' deliberately absent — should be no_match
        }

    def test_skips_already_resolved(self):
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",  # would be suspect
            "raw_excerpt": "43 VANGUARD WAY, DALLAS, TX 75243",
            "dcad_account": "already_set",
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.skipped_already_resolved == 1
        assert stats.matched == 0
        # No history entry written for already-resolved records
        assert get_history(records[0]) == []

    def test_skips_clean_address(self):
        """Records with clean address_normalized that just didn't match DCAD
        should NOT be re-attempted by Path B (different problem)."""
        records = [{
            "record_id": "r1",
            "address_normalized": "4314 HAMILTON",
            "raw_excerpt": "43 VANGUARD WAY, DALLAS, TX 75243",
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.skipped_clean_address == 1
        assert stats.matched == 0

    def test_matches_when_city_only_and_raw_excerpt_has_address(self):
        """Class 24 case: address_normalized = 'DALLAS', raw_excerpt has
        the real address."""
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",
            "raw_excerpt": "NOTICE | 43 VANGUARD WAY, DALLAS, TEXAS, 75243",
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.matched == 1
        assert records[0]["dcad_account"] == "acct_vanguard_43"
        assert records[0]["address_normalized"] == "43 VANGUARD WAY"
        # History entry exists with matched status
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_MATCHED
        assert history[0]["path"] == PATH_B_RAW_EXCERPT
        assert history[0]["dcad_account"] == "acct_vanguard_43"
        # Warning: we overwrote a non-empty address
        assert WARN_PATH_B_USED_ALTERNATE in get_warnings(records[0])

    def test_matches_trustee_address_class_1_case(self):
        """Class 1 case: address_normalized = trustee office, raw_excerpt
        has real property."""
        records = [{
            "record_id": "r1",
            "address_normalized": "20405 STATE HWY",
            "raw_excerpt": ("NOTICE OF FORECLOSURE | 2828 SOUTH LAKEVIEW DRIVE, "
                            "CEDAR HILL, TEXAS, 75104"),
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.matched == 1
        assert records[0]["dcad_account"] == "acct_lakeview_2828"
        assert records[0]["address_normalized"] == "2828 S LAKEVIEW DR"
        assert WARN_PATH_B_USED_ALTERNATE in get_warnings(records[0])

    def test_no_warning_when_overwriting_empty_address(self):
        """If address_normalized was empty/None to begin with, Path B
        stamping shouldn't add the 'used_alternate' warning."""
        records = [{
            "record_id": "r1",
            "address_normalized": None,
            "raw_excerpt": "43 VANGUARD WAY, DALLAS, TEXAS, 75243",
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.matched == 1
        assert get_warnings(records[0]) == []   # no swap warning

    def test_no_match_when_extracted_address_not_in_dcad(self):
        """Raw_excerpt has a clean address but it's not in DCAD's index.
        Status should be no_match (not skipped)."""
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",
            "raw_excerpt": "9999 NONEXISTENT ROAD, DALLAS, TEXAS, 75999",
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.no_match == 1
        assert stats.matched == 0
        # Record's dcad_account still None
        assert records[0].get("dcad_account") is None
        # History entry exists with no_match status
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_NO_MATCH

    def test_skipped_when_no_raw_excerpt(self):
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",
            # No raw_excerpt
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.skipped_no_raw_excerpt == 1
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_SKIPPED
        assert history[0]["skip_reason"] == "no_raw_excerpt"

    def test_skipped_when_raw_excerpt_has_no_clean_address(self):
        """raw_excerpt populated but doesn't contain extractable address."""
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",
            "raw_excerpt": "NOTICE | Subdivision - Name: PLEASANTWOOD Lot: 8",
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        assert stats.skipped_no_clean_address == 1
        history = get_history(records[0])
        assert history[0]["skip_reason"] == "no_clean_address_in_raw_excerpt"

    def test_rejects_venue_in_raw_excerpt(self):
        """raw_excerpt contains ONLY a venue/trustee address.
        Path B must not match that — treated as no_clean_address."""
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",
            "raw_excerpt": "Sale at 600 Commerce Street, Dallas, TX 75202",
        }]
        stats = resolution_paths.run_path_b(records, self._build_address_index())
        # The venue address was rejected → extract returns None →
        # skipped_no_clean_address
        assert stats.skipped_no_clean_address == 1
        assert stats.matched == 0

    def test_mixed_batch_counter_accuracy(self):
        """Aggregate across multiple records — counters must add up."""
        idx = self._build_address_index()
        records = [
            # 1. Already resolved
            {"record_id": "r1", "dcad_account": "x", "address_normalized": "DALLAS"},
            # 2. Clean address (not suspect)
            {"record_id": "r2", "address_normalized": "4314 HAMILTON"},
            # 3. Matches via Path B
            {"record_id": "r3", "address_normalized": "DALLAS",
             "raw_excerpt": "1442 MARLENE PLACE, DESOTO, TEXAS, 75115"},
            # 4. No raw_excerpt
            {"record_id": "r4", "address_normalized": "DALLAS"},
            # 5. Raw_excerpt has no clean addr
            {"record_id": "r5", "address_normalized": "DALLAS",
             "raw_excerpt": "Subdivision PLEASANTWOOD Lot 8"},
            # 6. No match in fake index
            {"record_id": "r6", "address_normalized": "DALLAS",
             "raw_excerpt": "9999 NONEXISTENT RD, DALLAS, TEXAS, 75999"},
        ]
        stats = resolution_paths.run_path_b(records, idx)
        assert stats.total_records == 6
        assert stats.skipped_already_resolved == 1
        assert stats.skipped_clean_address == 1
        assert stats.candidates == 3   # r3, r5, r6 (r4 has no excerpt → not a candidate)
        # Actually re-check: r4 has no raw_excerpt — IS it a candidate?
        # Looking at the code: candidate counter only increments AFTER
        # raw_excerpt check. So r4 doesn't count as a candidate.
        # r3, r5, r6 = 3 candidates.
        assert stats.matched == 1
        assert stats.no_match == 1
        assert stats.skipped_no_raw_excerpt == 1
        assert stats.skipped_no_clean_address == 1

    def test_run_on_empty_records_list(self):
        stats = resolution_paths.run_path_b([], self._build_address_index())
        assert stats.total_records == 0
        assert stats.matched == 0

    def test_run_with_empty_address_index(self):
        """Empty DCAD index → all candidates produce no_match."""
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",
            "raw_excerpt": "43 VANGUARD WAY, DALLAS, TEXAS, 75243",
        }]
        stats = resolution_paths.run_path_b(records, {})
        assert stats.no_match == 1
        assert stats.matched == 0


# ═══════════════════════════════════════════════════════════════════════════
# Audit-trail completeness
# ═══════════════════════════════════════════════════════════════════════════

class TestAuditTrail:
    """Every record Path B considers (other than already_resolved + clean)
    must have a resolution_history entry. The forensic audit relies on this."""

    def test_skipped_no_raw_excerpt_writes_history(self):
        rec = {"record_id": "r1", "address_normalized": "DALLAS"}
        resolution_paths.run_path_b([rec], {})
        history = get_history(rec)
        assert len(history) == 1
        assert history[0]["path"] == PATH_B_RAW_EXCERPT
        assert history[0]["status"] == STATUS_SKIPPED

    def test_already_resolved_does_NOT_write_history(self):
        """Records that Path B doesn't process (already_resolved) should
        not generate noise in resolution_history."""
        rec = {"record_id": "r1", "dcad_account": "x", "address_normalized": "DALLAS"}
        resolution_paths.run_path_b([rec], {})
        assert get_history(rec) == []

    def test_clean_address_does_NOT_write_history(self):
        """Records with a clean (non-suspect) address aren't Path B's concern
        — leave them alone, no history entry."""
        rec = {"record_id": "r1", "address_normalized": "4314 HAMILTON"}
        resolution_paths.run_path_b([rec], {})
        assert get_history(rec) == []
