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

from scraper import address_variants, resolution_paths
from scraper.resolution import (
    PATH_A_GRANTOR_OWNER_INDEX,
    PATH_B_RAW_EXCERPT,
    PATH_VARIANT_LOOKUP,
    STATUS_GUARDED,
    STATUS_MATCHED,
    STATUS_MULTI_MATCH,
    STATUS_NO_MATCH,
    STATUS_SKIPPED,
    WARN_COMMON_NAME_POLLUTION,
    WARN_LOOKUP_VARIANT_USED,
    WARN_MULTI_ACCOUNT,
    WARN_PATH_B_USED_ALTERNATE,
    WARN_SURNAME_DRIFT,
    WARN_SURNAME_IN_TRUST_FIRST_NAME,
    WARN_SURNAME_ONLY_TIER,
    get_alternate_accounts,
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

    def test_ampersand_is_garbled(self):
        """Cox case: '3031 CREST RDG D &' — ampersand is a garble signal.
        Restored from PR 2 commit cfcf274 (lost in squash merge)."""
        ok, reason = resolution_paths._is_suspect_address("3031 CREST RDG D &")
        assert ok is True and reason == "ocr_garbled"

    def test_trailing_single_letter_is_garbled(self):
        """Trailing single letter alone (e.g. '3031 CREST RDG D') is a
        post-name fragment from OCR cutting off mid-word."""
        ok, reason = resolution_paths._is_suspect_address("3031 CREST RDG D")
        assert ok is True and reason == "ocr_garbled"

    def test_montgomery_run_together_ocr(self):
        """Montgomery case: '2206 BEA OS REEFGRAND PRAIRIE' has 'BEA OS'
        — two short tokens, neither directional nor street-type, one ≤2 chars."""
        ok, reason = resolution_paths._is_suspect_address(
            "2206 BEA OS REEFGRAND PRAIRIE")
        assert ok is True and reason == "ocr_garbled"

    def test_directional_plus_name_not_flagged_as_two_short(self):
        """'S MAIN' has both ≤4 chars but S is directional — not garbled."""
        ok, reason = resolution_paths._is_suspect_address("2639 S MAIN ST")
        assert ok is False

    def test_oak_circle_not_flagged_as_two_short(self):
        """'OAK CIR' both ≤4 but CIR is a street type — not garbled."""
        ok, reason = resolution_paths._is_suspect_address("123 OAK CIR")
        assert ok is False

    def test_big_oak_two_short_no_short_token_not_flagged(self):
        """'BIG OAK' both ≤4, neither directional nor street type, but
        BOTH are ≥3 chars (no ≤2 char short-token signature) — not garbled.
        Prevents FP on legit multi-word names."""
        ok, reason = resolution_paths._is_suspect_address("123 BIG OAK LN")
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


# ═══════════════════════════════════════════════════════════════════════════
# Stage 6.35 — run_variant_lookup (Class 26 family)
# ═══════════════════════════════════════════════════════════════════════════

class TestRunVariantLookup:
    """Stage 6.35 fuzzy-lookup retry for records whose addresses look
    clean but DCAD exact lookup missed."""

    def _build_fixtures(self):
        # Mock DCAD address_index. Note that some entries deliberately
        # have suffix forms that our source addresses lack.
        idx = {
            "4314 HAMILTON AVE":      "acct_hamilton",
            "309 BILLY WICKLIFFE DR": "acct_wickliffe",
            "2828 LAKE VIEW DR":      "acct_lakeview",
            "1234 EXACT MATCH ST":    "acct_exact",
        }
        fi = address_variants.build_fuzzy_index(idx)
        return idx, fi

    def test_meyers_missing_suffix_recovery(self):
        idx, fi = self._build_fixtures()
        records = [{
            "record_id":          "rM",
            "address_normalized": "4314 HAMILTON",   # no suffix
        }]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.matched == 1
        assert records[0]["dcad_account"] == "acct_hamilton"
        assert records[0]["address_normalized"] == "4314 HAMILTON AVE"
        # Warning indicates the address was rewritten
        assert WARN_LOOKUP_VARIANT_USED in get_warnings(records[0])
        # History entry recorded
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_MATCHED
        assert history[0]["path"] == PATH_VARIANT_LOOKUP
        assert history[0]["dcad_account"] == "acct_hamilton"

    def test_acuna_partial_street_recovery(self):
        idx, fi = self._build_fixtures()
        records = [{
            "record_id":          "rA",
            "address_normalized": "309 BILLY WICKLIFFE",
        }]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.matched == 1
        assert records[0]["dcad_account"] == "acct_wickliffe"

    def test_adama_directional_plus_compound_split(self):
        """Real forensic case: source '2828 S LAKEVIEW DR' vs DCAD
        '2828 LAKE VIEW DR'."""
        idx, fi = self._build_fixtures()
        records = [{
            "record_id":          "rAdama",
            "address_normalized": "2828 S LAKEVIEW DR",
        }]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.matched == 1
        assert records[0]["dcad_account"] == "acct_lakeview"
        assert WARN_LOOKUP_VARIANT_USED in get_warnings(records[0])

    def test_exact_match_no_warning(self):
        """If source already matches DCAD exactly, no rewrite warning."""
        idx, fi = self._build_fixtures()
        records = [{
            "record_id":          "rE",
            "address_normalized": "1234 EXACT MATCH ST",
        }]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.matched == 1
        # Address unchanged, so no path_used_alternate warning
        assert WARN_LOOKUP_VARIANT_USED not in get_warnings(records[0])

    def test_skips_already_resolved(self):
        idx, fi = self._build_fixtures()
        records = [{
            "record_id":          "rR",
            "address_normalized": "4314 HAMILTON",
            "dcad_account":       "already_set",
        }]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.skipped_already_resolved == 1
        assert stats.matched == 0
        # Record unchanged
        assert records[0]["dcad_account"] == "already_set"

    def test_skips_suspect_address(self):
        """Suspect addresses are Path B's territory — don't double-process."""
        idx, fi = self._build_fixtures()
        records = [{
            "record_id":          "rS",
            "address_normalized": "DALLAS",   # city-only is suspect
        }]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.skipped_suspect_address == 1
        assert stats.matched == 0

    def test_skips_no_address(self):
        idx, fi = self._build_fixtures()
        records = [{"record_id": "rN"}]  # no address_normalized
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.skipped_no_address == 1
        assert stats.matched == 0

    def test_no_match_writes_history(self):
        """Records where fuzzy lookup found no candidate above threshold
        still get a history entry."""
        idx, fi = self._build_fixtures()
        records = [{
            "record_id":          "rNoMatch",
            "address_normalized": "9999 NONEXISTENT RD",
        }]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.no_match == 1
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_NO_MATCH
        assert history[0]["path"] == PATH_VARIANT_LOOKUP

    def test_full_aggregate_counters(self):
        idx, fi = self._build_fixtures()
        records = [
            # 1. Already resolved
            {"record_id": "r1", "address_normalized": "4314 HAMILTON",
             "dcad_account": "x"},
            # 2. Suspect address — Path B's domain
            {"record_id": "r2", "address_normalized": "DALLAS"},
            # 3. No address
            {"record_id": "r3"},
            # 4. Matches via variant lookup
            {"record_id": "r4", "address_normalized": "4314 HAMILTON"},
            # 5. No match
            {"record_id": "r5", "address_normalized": "9999 NOWHERE RD"},
        ]
        stats = resolution_paths.run_variant_lookup(records, idx, fi)
        assert stats.total_records == 5
        assert stats.skipped_already_resolved == 1
        assert stats.skipped_suspect_address == 1
        assert stats.skipped_no_address == 1
        assert stats.candidates == 2     # r4 + r5
        assert stats.matched == 1        # r4
        assert stats.no_match == 1       # r5


# ═══════════════════════════════════════════════════════════════════════════
# Stage 6.45 — run_path_a (NOF grantor → DCAD owner_index)
# ═══════════════════════════════════════════════════════════════════════════

class TestPathABoilerplateGuard:
    """Class 2 defense: known boilerplate single-token grantors must
    not be looked up in owner_index."""

    def test_liable_boilerplate_skipped(self):
        assert resolution_paths._is_boilerplate_grantor("Liable") is True
        assert resolution_paths._is_boilerplate_grantor("LIABLE") is True
        assert resolution_paths._is_boilerplate_grantor("liable") is True

    def test_other_boilerplate_tokens(self):
        for tok in ("MORTGAGOR", "BORROWER", "TRUSTEE", "DEBTOR",
                    "GRANTOR", "EXECUTOR", "ATTORNEY"):
            assert resolution_paths._is_boilerplate_grantor(tok) is True

    def test_real_name_not_boilerplate(self):
        for name in ("Ousley, Skyler", "Wilson, Robert", "Meyers, Bobbie",
                     "Bobbie Meyers"):
            assert resolution_paths._is_boilerplate_grantor(name) is False

    def test_empty_treated_as_boilerplate(self):
        assert resolution_paths._is_boilerplate_grantor("") is True
        assert resolution_paths._is_boilerplate_grantor(None) is True
        assert resolution_paths._is_boilerplate_grantor("   ") is True


class TestPathAClass19aGuard:
    """Class 19a defense: surname matching first-name-in-trust pattern."""

    def test_heather_bass_pattern_detected(self):
        """Empirical case: grantor='Heather, Linda A.' DCAD owner=
        'Bass Jeffery & Heather Revocable Trust The'.
        'HEATHER' is a first name inside a trust, not a surname."""
        assert resolution_paths._is_trust_first_name_pattern(
            "Heather, Linda A.",
            "Bass Jeffery & Heather Revocable Trust The",
        ) is True

    def test_earls_trust_not_flagged(self):
        """Earls Trust — 'EARLS' IS the trust family name, at the start.
        NOT a first-name-in-trust pattern."""
        assert resolution_paths._is_trust_first_name_pattern(
            "Earls, Richard Lawrence",
            "Earls Trust",
        ) is False

    def test_rhodes_family_trust_not_flagged(self):
        assert resolution_paths._is_trust_first_name_pattern(
            "Rhodes, Margaret",
            "Rhodes Family Trust",
        ) is False

    def test_no_entity_keyword_not_flagged(self):
        """If the DCAD owner isn't a trust/entity at all, even mid-position
        surname matches are likely legit joint-owner cases."""
        assert resolution_paths._is_trust_first_name_pattern(
            "Smith, John",
            "Doe Jane & Smith John",
        ) is False

    def test_no_grantor_returns_false(self):
        assert resolution_paths._is_trust_first_name_pattern(
            "", "Bass Jeffery & Heather Revocable Trust The"
        ) is False

    def test_no_comma_grantor_returns_false(self):
        """Path A's class 19a guard only fires on comma-form grantor names."""
        assert resolution_paths._is_trust_first_name_pattern(
            "Linda Heather",
            "Bass Jeffery & Heather Revocable Trust The",
        ) is False


class TestRunPathA:
    def _build_fixtures(self):
        """owner_index, account_address_index, account_owner_index for tests."""
        owner_index = {
            "OUSLEY SKYLER":  ["acct_ousley"],
            "WILSON ROBERT":  ["acct_wilson_1", "acct_wilson_2"],  # multi-account
            "MEYERS BOBBIE":  ["acct_meyers"],
            "HEATHER":        ["acct_bass_trust"],  # Class 19a candidate
        }
        # Flat dict[str, str] — same shape as legal_resolver._build_account_to_address
        account_address_index = {
            "acct_ousley":     "6840 DART AVE",
            "acct_wilson_1":   "355 CARDINAL CREEK DR",
            "acct_wilson_2":   "3613 BERMUDA DR",
            "acct_meyers":     "4314 HAMILTON AVE",
            "acct_bass_trust": "7018 CLEAR SPRINGS PKWY",
        }
        # Note: dcad owner string for bass_trust contains 'Heather' as a
        # first-name inside a trust — triggers Class 19a guard.
        account_owner_index = {
            "acct_ousley":     "OUSLEY SKYLER",
            "acct_wilson_1":   "WILSON ROBERT",
            "acct_wilson_2":   "WILSON ROBERT",
            "acct_meyers":     "MEYERS BOBBIE",
            "acct_bass_trust": "BASS JEFFERY & HEATHER REVOCABLE TRUST THE",
        }
        return owner_index, account_address_index, account_owner_index

    def test_ousley_single_clean_match(self):
        oi, aai, aoi = self._build_fixtures()
        records = [{
            "record_id": "rO",
            "dallas_code": "NOF",
            "grantor": "Ousley, Skyler",
        }]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.matched == 1
        assert records[0]["dcad_account"] == "acct_ousley"
        assert records[0]["address_normalized"] == "6840 DART AVE"
        # Single-account match — no multi_account warning
        assert WARN_MULTI_ACCOUNT not in get_warnings(records[0])
        # History entry
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_MATCHED
        assert history[0]["path"] == PATH_A_GRANTOR_OWNER_INDEX

    def test_wilson_multi_account_stamps_alternates(self):
        oi, aai, aoi = self._build_fixtures()
        records = [{
            "record_id": "rW",
            "dallas_code": "NOF",
            "grantor": "Wilson, Robert",
        }]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.multi_account_matched == 1
        assert records[0]["dcad_account"] == "acct_wilson_1"  # first picked
        # Alternates stamped
        alternates = get_alternate_accounts(records[0])
        assert "acct_wilson_2" in alternates
        # Multi-account warning
        assert WARN_MULTI_ACCOUNT in get_warnings(records[0])
        # History reflects multi_match
        history = get_history(records[0])
        assert history[0]["status"] == STATUS_MULTI_MATCH

    def test_heather_class_19a_guarded(self):
        """Heather, Linda A. -> surname_only HEATHER -> Bass trust.
        Must be guarded — operator should not be skip-tracing Bass family."""
        oi, aai, aoi = self._build_fixtures()
        records = [{
            "record_id": "rH",
            "dallas_code": "NOF",
            "grantor": "Heather, Linda A.",
        }]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.guarded == 1
        assert stats.matched == 0
        # Critical: dcad_account NOT stamped
        assert records[0].get("dcad_account") is None
        # Warning surfaces why
        assert WARN_SURNAME_IN_TRUST_FIRST_NAME in get_warnings(records[0])
        # History shows GUARDED status
        history = get_history(records[0])
        assert history[0]["status"] == STATUS_GUARDED
        assert history[0]["skip_reason"] == "surname_in_trust_first_name"

    def test_liable_boilerplate_skipped(self):
        oi, aai, aoi = self._build_fixtures()
        records = [{
            "record_id": "rL",
            "dallas_code": "NOF",
            "grantor": "Liable",
        }]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.skipped_boilerplate == 1
        assert records[0].get("dcad_account") is None
        history = get_history(records[0])
        assert history[0]["skip_reason"] == "boilerplate_grantor"

    def test_pb_records_skipped(self):
        """Path A only runs on NOFs. PB records have their own decedent
        fan-out via canonicalize_probate."""
        oi, aai, aoi = self._build_fixtures()
        records = [{
            "record_id": "rPB",
            "dallas_code": "PB",
            "grantor": "Ousley, Skyler",  # matches but path A skips PB
        }]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.skipped_not_nof == 1
        assert records[0].get("dcad_account") is None
        # No history entry — Path A didn't consider this record
        assert get_history(records[0]) == []

    def test_already_resolved_skipped(self):
        oi, aai, aoi = self._build_fixtures()
        records = [{
            "record_id": "rR",
            "dallas_code": "NOF",
            "grantor": "Ousley, Skyler",
            "dcad_account": "already_set",
        }]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.skipped_already_resolved == 1
        # Original dcad_account preserved
        assert records[0]["dcad_account"] == "already_set"

    def test_no_grantor_skipped(self):
        oi, aai, aoi = self._build_fixtures()
        records = [{"record_id": "rN", "dallas_code": "NOF"}]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.skipped_no_grantor == 1

    def test_no_match_records_history(self):
        """Grantor present but no DCAD owner match — must still log
        the attempt in history for audit."""
        oi, aai, aoi = self._build_fixtures()
        records = [{
            "record_id": "rNM",
            "dallas_code": "NOF",
            "grantor": "Nobody, Doesnt Exist",
        }]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.no_match == 1
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_NO_MATCH

    def test_aggregate_counters(self):
        oi, aai, aoi = self._build_fixtures()
        records = [
            {"record_id": "r1", "dallas_code": "NOF",
             "grantor": "Ousley, Skyler"},                # matched
            {"record_id": "r2", "dallas_code": "NOF",
             "grantor": "Wilson, Robert"},                # multi_account
            {"record_id": "r3", "dallas_code": "NOF",
             "grantor": "Liable"},                        # boilerplate
            {"record_id": "r4", "dallas_code": "PB",
             "grantor": "Ousley, Skyler"},                # not NOF -> skip
            {"record_id": "r5", "dallas_code": "NOF",
             "dcad_account": "x",
             "grantor": "Ousley, Skyler"},                # already resolved
            {"record_id": "r6", "dallas_code": "NOF"},    # no grantor
            {"record_id": "r7", "dallas_code": "NOF",
             "grantor": "Heather, Linda A."},             # guarded
            {"record_id": "r8", "dallas_code": "NOF",
             "grantor": "Nobody, NotInIndex"},            # no_match
        ]
        stats = resolution_paths.run_path_a(records, oi, aai, aoi)
        assert stats.total_records == 8
        assert stats.matched == 1
        assert stats.multi_account_matched == 1
        assert stats.skipped_boilerplate == 1
        assert stats.skipped_not_nof == 1
        assert stats.skipped_already_resolved == 1
        assert stats.skipped_no_grantor == 1
        assert stats.guarded == 1
        assert stats.no_match == 1
        # candidates = total - skip_buckets - guarded? actually candidates
        # is incremented after the boilerplate check passes
        assert stats.candidates == 4  # r1, r2, r7 (will be guarded), r8


# ═══════════════════════════════════════════════════════════════════════════
# PR 5 — diagnostic mode (always_run) tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPathADiagnosticMode:
    """When always_run=True, Path A still attempts on already-resolved
    records and records history but does NOT overwrite record fields."""

    def _fixtures(self):
        owner_index = {
            "OUSLEY SKYLER":  ["acct_ousley"],
            "COX LAURA ANNETTE": ["acct_cordova", "acct_crest_ridge"],
        }
        account_address_index = {
            "acct_ousley":      "6840 DART AVE",
            "acct_cordova":     "706 CORDOVA ST",
            "acct_crest_ridge": "3031 CREST RDG DR",
        }
        account_owner_index = {
            "acct_ousley":      "OUSLEY SKYLER",
            "acct_cordova":     "COX LAURA ANNETTE",
            "acct_crest_ridge": "COX LAURA ANNETTE",
        }
        return owner_index, account_address_index, account_owner_index

    def test_default_mode_skips_already_resolved(self):
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "rO",
            "dallas_code": "NOF",
            "grantor": "Ousley, Skyler",
            "dcad_account": "preset_from_path_b",
            "address_normalized": "1234 PRESET ST",
        }
        stats = resolution_paths.run_path_a([rec], oi, aai, aoi)
        assert stats.skipped_already_resolved == 1
        # No history written by Path A
        history = get_history(rec)
        assert all(h["path"] != PATH_A_GRANTOR_OWNER_INDEX for h in history)

    def test_always_run_records_history_but_preserves_stamp(self):
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "rO",
            "dallas_code": "NOF",
            "grantor": "Ousley, Skyler",
            "dcad_account": "preset_from_path_b",
            "address_normalized": "1234 PRESET ST",
        }
        stats = resolution_paths.run_path_a([rec], oi, aai, aoi, always_run=True)
        # Counters still bump
        assert stats.skipped_already_resolved == 1
        assert stats.matched == 1
        # But record fields are preserved verbatim
        assert rec["dcad_account"] == "preset_from_path_b"
        assert rec["address_normalized"] == "1234 PRESET ST"
        # History entry was written so Stage 6.6 can see what Path A picked
        history = get_history(rec)
        path_a_entries = [h for h in history if h["path"] == PATH_A_GRANTOR_OWNER_INDEX]
        assert len(path_a_entries) == 1
        assert path_a_entries[0]["dcad_account"] == "acct_ousley"

    def test_always_run_reveals_disagreement_for_stage_6_6(self):
        """Cox case: upstream stamped acct_crest_ridge; Path A in diagnostic
        mode records its multi-account first-pick of acct_cordova."""
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "rCox",
            "dallas_code": "NOF",
            "grantor": "Cox, Laura Annette",
            "dcad_account": "acct_crest_ridge",   # upstream Path B already resolved
            "address_normalized": "3031 CREST RDG DR",
        }
        stats = resolution_paths.run_path_a([rec], oi, aai, aoi, always_run=True)
        # Path A would pick the multi_account first entry
        assert stats.multi_account_matched == 1
        # Stamp not overwritten
        assert rec["dcad_account"] == "acct_crest_ridge"
        # Record-level WARN_MULTI_ACCOUNT should NOT have been added
        # (that warning belongs to the would-be-stamper, which we suppressed)
        assert WARN_MULTI_ACCOUNT not in get_warnings(rec)
        # But the history entry captures Path A's would-be pick
        history = get_history(rec)
        path_a_entries = [h for h in history if h["path"] == PATH_A_GRANTOR_OWNER_INDEX]
        assert len(path_a_entries) == 1
        # acct_cordova is the would-have-picked account
        assert path_a_entries[0]["dcad_account"] == "acct_cordova"
        assert WARN_MULTI_ACCOUNT in path_a_entries[0]["warnings"]

    def test_always_run_unresolved_records_behave_normally(self):
        """For records that don't have dcad_account yet, always_run=True
        behaves exactly like default mode (stamps + warns + history)."""
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "rO",
            "dallas_code": "NOF",
            "grantor": "Ousley, Skyler",
        }
        stats = resolution_paths.run_path_a([rec], oi, aai, aoi, always_run=True)
        assert stats.matched == 1
        assert rec["dcad_account"] == "acct_ousley"
        assert rec["address_normalized"] == "6840 DART AVE"


class TestPathBDiagnosticMode:
    def test_always_run_preserves_existing_stamp(self):
        address_index = {"3031 CREST RDG DR": "acct_path_b"}
        rec = {
            "record_id": "rB",
            "dcad_account": "acct_already_set",
            "address_normalized": "DALLAS",  # suspect
            "raw_excerpt": "Property: 3031 CREST RDG DR, DALLAS, TX 75228",
        }
        stats = resolution_paths.run_path_b(
            [rec], address_index, always_run=True,
        )
        assert stats.skipped_already_resolved == 1
        assert rec["dcad_account"] == "acct_already_set"  # unchanged
        # But Path B's match still goes to history
        history = get_history(rec)
        path_b_entries = [h for h in history if h["path"] == PATH_B_RAW_EXCERPT]
        assert any(h["status"] == STATUS_MATCHED for h in path_b_entries)


# ═══════════════════════════════════════════════════════════════════════════
# PR 7.7 — Path A multi-account picker uses page text
# ═══════════════════════════════════════════════════════════════════════════
#
# Production audit on 2026-05-27 GHA run found:
#   grantor='Salcedo, Erika'
#   match_decedent_to_dcad returned 2 accounts:
#     - 220955400D03B0000 → 910 GAYNOR AVE, Duncanville (DCAD's first)
#     - 50029500050290000 → 723 HIGH SCHOOL LN, Seagoville
#   Pipeline picked the first one. The publicsearch document was for
#   the Seagoville property. Wrong-person-called risk.
#
# PR 7.7: when Path A returns multi-account AND a page_fetcher is
# available, score each candidate's address against the page text and
# pick the winner if the margin is decisive.


class TestPickAccountByPageText:
    """Direct unit tests for the picker helper."""

    def _idx(self):
        return {
            "acct_seagoville": "723 HIGH SCHOOL LN",
            "acct_duncanville": "910 GAYNOR AVE",
            "acct_irving": "2624 EDINBURGH ST",
            "acct_sanfernando": "8643 SAN FERNANDO WAY",
        }

    def test_clear_winner_returns_account(self):
        # Salcedo scenario: page text matches Seagoville address
        winner = resolution_paths._pick_account_by_page_text(
            ["acct_duncanville", "acct_seagoville"],
            self._idx(),
            "723 HIGH SCHOOL LANE SEAGOVILLE TEXAS 75159",
        )
        assert winner == "acct_seagoville"

    def test_walker_linda_e_irving_scenario(self):
        winner = resolution_paths._pick_account_by_page_text(
            ["acct_sanfernando", "acct_irving"],
            self._idx(),
            "2624 EDINBURGH STREET IRVING TEXAS 75061",
        )
        assert winner == "acct_irving"

    def test_no_page_text_returns_none(self):
        assert resolution_paths._pick_account_by_page_text(
            ["acct_duncanville", "acct_seagoville"], self._idx(), None,
        ) is None
        assert resolution_paths._pick_account_by_page_text(
            ["acct_duncanville", "acct_seagoville"], self._idx(), "",
        ) is None

    def test_single_account_returns_none(self):
        """If only one candidate, no need to tiebreak."""
        assert resolution_paths._pick_account_by_page_text(
            ["acct_seagoville"], self._idx(),
            "723 HIGH SCHOOL LN SEAGOVILLE 75159",
        ) is None

    def test_top_score_below_threshold_returns_none(self):
        """Page text doesn't match any candidate well enough."""
        winner = resolution_paths._pick_account_by_page_text(
            ["acct_duncanville", "acct_seagoville"],
            self._idx(),
            "Random unrelated text about nothing in particular",
        )
        assert winner is None

    def test_margin_below_threshold_returns_none(self):
        """Both candidates score equally — can't decide."""
        idx = {"a": "100 OAK", "b": "100 OAK"}   # identical addresses
        winner = resolution_paths._pick_account_by_page_text(
            ["a", "b"], idx, "100 OAK STREET DALLAS",
        )
        assert winner is None  # tied scores


class TestPathAMultiAccountPickerByPage:
    """Integration tests for the Path A page-tiebreaker behavior."""

    def _fixtures(self):
        # Salcedo Erika: 2 properties in DCAD
        owner_index = {
            "SALCEDO ERIKA": ["acct_duncanville", "acct_seagoville"],
        }
        account_address_index = {
            "acct_duncanville": "910 GAYNOR AVE",
            "acct_seagoville":  "723 HIGH SCHOOL LN",
        }
        account_owner_index = {
            "acct_duncanville": "SALCEDO ERIKA",
            "acct_seagoville":  "SALCEDO ERIKA",
        }
        return owner_index, account_address_index, account_owner_index

    def test_multi_account_page_picks_correct_winner(self):
        """The Salcedo case: page text indicates Seagoville. Path A
        should pick Seagoville as primary, Duncanville as alternate."""
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "315551161",
            "dallas_code": "NOF",
            "grantor": "Salcedo, Erika",
        }

        def fetcher(rid):
            return "723 HIGH SCHOOL LN SEAGOVILLE TEXAS 75159"

        stats = resolution_paths.run_path_a(
            [rec], oi, aai, aoi, page_fetcher=fetcher,
        )
        assert stats.multi_account_matched == 1
        assert stats.multi_account_picked_by_page == 1
        assert rec["dcad_account"] == "acct_seagoville"   # winner
        assert rec["address_normalized"] == "723 HIGH SCHOOL LN"
        # Duncanville (the loser) moved to alternates
        from scraper.resolution import get_alternate_accounts
        assert get_alternate_accounts(rec) == ["acct_duncanville"]
        # Warning flags this happened
        warnings = get_warnings(rec)
        assert "tiebroken_by_page" in warnings
        assert WARN_MULTI_ACCOUNT in warnings

    def test_multi_account_no_page_fetcher_falls_back(self):
        """Backwards-compatible: without page_fetcher kwarg, Path A
        picks the first account exactly as before PR 7.7."""
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "315551161",
            "dallas_code": "NOF",
            "grantor": "Salcedo, Erika",
        }
        stats = resolution_paths.run_path_a([rec], oi, aai, aoi)
        assert stats.multi_account_matched == 1
        assert stats.multi_account_picked_by_page == 0
        # No page picker → first account wins (Duncanville is first in dict)
        assert rec["dcad_account"] == "acct_duncanville"

    def test_multi_account_page_inconclusive_falls_back_to_first(self):
        """Page fetched but text is generic; picker returns None;
        Path A falls back to first-account behavior."""
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "315551161",
            "dallas_code": "NOF",
            "grantor": "Salcedo, Erika",
        }

        def fetcher(rid):
            return "Notice of Foreclosure Sale Dallas County"

        stats = resolution_paths.run_path_a(
            [rec], oi, aai, aoi, page_fetcher=fetcher,
        )
        assert stats.multi_account_picked_by_page == 0
        assert rec["dcad_account"] == "acct_duncanville"   # first
        assert "tiebroken_by_page" not in get_warnings(rec)

    def test_multi_account_page_fetcher_raises_falls_back(self):
        """Page fetch crash doesn't crash Path A — falls back to
        first-account behavior."""
        oi, aai, aoi = self._fixtures()
        rec = {
            "record_id": "315551161",
            "dallas_code": "NOF",
            "grantor": "Salcedo, Erika",
        }

        def crashing_fetcher(rid):
            raise RuntimeError("simulated network failure")

        # Should not propagate
        stats = resolution_paths.run_path_a(
            [rec], oi, aai, aoi, page_fetcher=crashing_fetcher,
        )
        assert stats.multi_account_matched == 1
        assert stats.multi_account_picked_by_page == 0
        assert rec["dcad_account"] == "acct_duncanville"

    def test_single_account_match_unaffected(self):
        """Page fetcher provided but match is unambiguous (1 acct) —
        no page fetch, no tiebreak. Existing single-match path."""
        oi = {"OUSLEY SKYLER": ["acct_ousley"]}
        aai = {"acct_ousley": "6840 DART AVE"}
        aoi = {"acct_ousley": "OUSLEY SKYLER"}
        rec = {
            "record_id": "rO",
            "dallas_code": "NOF",
            "grantor": "Ousley, Skyler",
        }

        fetch_called = [False]
        def fetcher(rid):
            fetch_called[0] = True
            return "ignored"

        stats = resolution_paths.run_path_a(
            [rec], oi, aai, aoi, page_fetcher=fetcher,
        )
        assert stats.matched == 1
        assert stats.multi_account_picked_by_page == 0
        assert not fetch_called[0], "should not fetch for single-account matches"
        assert rec["dcad_account"] == "acct_ousley"


# ═══════════════════════════════════════════════════════════════════════════
# PR 7.8 — joint-grantor name splitting in Path A
# ═══════════════════════════════════════════════════════════════════════════
#
# Production audit on 2026-05-27 GHA run found:
#   grantor='Walker, David & Walker, Linda E.'
#   match_decedent_to_dcad returned only David's 3 accounts (matched
#   on "WALKER DAVID"). Never tried "WALKER LINDA E" independently —
#   so 2624 EDINBURGH ST (Linda E's Irving property, the actual
#   foreclosure target) was never considered.
#
# PR 7.8: when the grantor contains '&', split into individual names,
# match each one through match_decedent_to_dcad, combine the candidate
# accounts. Page-text picker (PR 7.7) then picks the right one.


class TestSplitJointGrantor:
    """Direct unit tests for the split helper."""

    def test_single_name_returns_one_element_list(self):
        assert resolution_paths._split_joint_grantor("Salcedo, Erika") == ["Salcedo, Erika"]

    def test_two_name_joint_splits(self):
        result = resolution_paths._split_joint_grantor(
            "Walker, David & Walker, Linda E."
        )
        assert result == ["Walker, David", "Walker, Linda E."]

    def test_adama_joint_splits(self):
        result = resolution_paths._split_joint_grantor(
            "Adama, Favour & Adama, Bernard"
        )
        assert result == ["Adama, Favour", "Adama, Bernard"]

    def test_three_name_joint_splits(self):
        result = resolution_paths._split_joint_grantor("A, X & B, Y & C, Z")
        assert result == ["A, X", "B, Y", "C, Z"]

    def test_empty_input(self):
        assert resolution_paths._split_joint_grantor("") == []
        assert resolution_paths._split_joint_grantor(None) == []

    def test_whitespace_only_parts_filtered(self):
        """'X &  ' should not produce an empty second segment."""
        result = resolution_paths._split_joint_grantor("Walker, David &   ")
        assert result == ["Walker, David"]

    def test_leading_ampersand_filtered(self):
        result = resolution_paths._split_joint_grantor("& Walker, David")
        assert result == ["Walker, David"]


class TestCombineMatches:
    """Direct unit tests for _combine_matches."""

    def _match(self, accounts, tier="exact", warning=None,
               matched_name="X", key_source="asis"):
        return resolution_paths.ProbateMatchResult(
            matched_name=matched_name, accounts=list(accounts),
            tier=tier, key_source=key_source, warning=warning,
        )

    def test_single_match_returned_unchanged(self):
        m = self._match(["a", "b"])
        result = resolution_paths._combine_matches([("name", m)])
        assert result.accounts == ["a", "b"]
        assert result.tier == "exact"

    def test_two_matches_no_overlap_concatenated(self):
        m1 = self._match(["a", "b"], matched_name="DAVID")
        m2 = self._match(["c", "d"], matched_name="LINDA")
        result = resolution_paths._combine_matches([
            ("Walker, David", m1),
            ("Walker, Linda E.", m2),
        ])
        assert result.accounts == ["a", "b", "c", "d"]

    def test_two_matches_with_overlap_deduplicated(self):
        """Joint owners often own the same property — accounts dedupe
        but preserve first-seen order."""
        m1 = self._match(["shared", "david_only"])
        m2 = self._match(["shared", "linda_only"])
        result = resolution_paths._combine_matches([
            ("D", m1), ("L", m2),
        ])
        assert result.accounts == ["shared", "david_only", "linda_only"]

    def test_best_tier_wins(self):
        m1 = self._match(["a"], tier="surname_only")
        m2 = self._match(["b"], tier="exact")
        result = resolution_paths._combine_matches([("X", m1), ("Y", m2)])
        assert result.tier == "exact"

    def test_pollution_propagates_if_any(self):
        m1 = self._match(["a"], warning=None)
        m2 = self._match(["b"], warning="common_name_pollution")
        result = resolution_paths._combine_matches([("X", m1), ("Y", m2)])
        assert result.warning == "common_name_pollution"

    def test_no_pollution_when_none_present(self):
        m1 = self._match(["a"], warning=None)
        m2 = self._match(["b"], warning=None)
        result = resolution_paths._combine_matches([("X", m1), ("Y", m2)])
        assert result.warning is None


class TestPathAJointGrantorSplit:
    """Integration tests for run_path_a with joint-grantor splitting."""

    def _walker_fixtures(self):
        """Walker case: David has 3 properties, Linda E. has 1 (Irving).
        Production showed David's first property got picked because
        Linda E. was never tried."""
        owner_index = {
            "WALKER DAVID":   ["acct_david_1", "acct_david_2", "acct_david_3"],
            "WALKER LINDA E": ["acct_irving"],
        }
        account_address_index = {
            "acct_david_1":   "8643 SAN FERNANDO WAY",
            "acct_david_2":   "100 SOMEWHERE ST",
            "acct_david_3":   "200 ELSEWHERE DR",
            "acct_irving":    "2624 EDINBURGH ST",
        }
        account_owner_index = {
            "acct_david_1":   "WALKER DAVID",
            "acct_david_2":   "WALKER DAVID",
            "acct_david_3":   "WALKER DAVID",
            "acct_irving":    "WALKER LINDA E",
        }
        return owner_index, account_address_index, account_owner_index

    def test_walker_joint_split_picks_irving_via_page(self):
        """Page text mentions Irving → Linda E's account wins."""
        oi, aai, aoi = self._walker_fixtures()
        rec = {
            "record_id": "315566857",
            "dallas_code": "NOF",
            "grantor": "Walker, David & Walker, Linda E.",
        }

        def fetcher(rid):
            return "2624 EDINBURGH STREET IRVING TEXAS 75061"

        stats = resolution_paths.run_path_a(
            [rec], oi, aai, aoi, page_fetcher=fetcher,
        )
        assert stats.joint_grantor_split_used == 1
        assert stats.multi_account_picked_by_page == 1
        assert rec["dcad_account"] == "acct_irving"
        assert rec["address_normalized"] == "2624 EDINBURGH ST"

    def test_walker_joint_split_no_page_falls_back_to_first(self):
        """Without a page_fetcher, falls back to first combined account
        (David's first, by combination order)."""
        oi, aai, aoi = self._walker_fixtures()
        rec = {
            "record_id": "315566857",
            "dallas_code": "NOF",
            "grantor": "Walker, David & Walker, Linda E.",
        }
        stats = resolution_paths.run_path_a([rec], oi, aai, aoi)
        assert stats.joint_grantor_split_used == 1
        assert stats.multi_account_matched == 1
        # First name's first account wins by combination order
        assert rec["dcad_account"] == "acct_david_1"
        # Linda E's account stamped as alternate (along with David's other 2)
        from scraper.resolution import get_alternate_accounts
        alts = get_alternate_accounts(rec)
        assert "acct_irving" in alts
        assert "acct_david_2" in alts
        assert "acct_david_3" in alts

    def test_adama_joint_split_same_property_deduplicated(self):
        """Adama case: both Favour and Bernard own the same property
        jointly in DCAD. Splitting + combining + dedup gives 1 account."""
        owner_index = {
            "ADAMA FAVOUR":  ["acct_adama_joint"],
            "ADAMA BERNARD": ["acct_adama_joint"],
        }
        account_address_index = {"acct_adama_joint": "2828 LAKE VW DR"}
        account_owner_index = {"acct_adama_joint": "ADAMA FAVOUR & ADAMA BERNARD"}
        rec = {
            "record_id": "315561580",
            "dallas_code": "NOF",
            "grantor": "Adama, Favour & Adama, Bernard",
        }
        stats = resolution_paths.run_path_a(
            [rec], owner_index, account_address_index, account_owner_index,
        )
        assert stats.joint_grantor_split_used == 1
        assert stats.matched == 1  # single account after dedup, not multi
        assert rec["dcad_account"] == "acct_adama_joint"
        from scraper.resolution import get_alternate_accounts
        assert get_alternate_accounts(rec) == []

    def test_single_name_grantor_split_not_used(self):
        """Salcedo: no '&' in grantor → no split → counter not incremented."""
        owner_index = {"SALCEDO ERIKA": ["acct_seagoville"]}
        aai = {"acct_seagoville": "723 HIGH SCHOOL LN"}
        aoi = {"acct_seagoville": "SALCEDO ERIKA"}
        rec = {
            "record_id": "315551161",
            "dallas_code": "NOF",
            "grantor": "Salcedo, Erika",
        }
        stats = resolution_paths.run_path_a([rec], owner_index, aai, aoi)
        assert stats.joint_grantor_split_used == 0
        assert stats.matched == 1

    def test_joint_grantor_one_name_no_match(self):
        """Only David is in owner_index; Linda E. is not. Result: David's
        accounts only, split-used counter still increments."""
        owner_index = {"WALKER DAVID": ["acct_david"]}
        aai = {"acct_david": "8643 SAN FERNANDO WAY"}
        aoi = {"acct_david": "WALKER DAVID"}
        rec = {
            "record_id": "315566857",
            "dallas_code": "NOF",
            "grantor": "Walker, David & Walker, Linda E.",
        }
        stats = resolution_paths.run_path_a([rec], owner_index, aai, aoi)
        assert stats.joint_grantor_split_used == 1
        assert stats.matched == 1
        assert rec["dcad_account"] == "acct_david"

    def test_joint_grantor_both_names_no_match(self):
        """Neither name is in owner_index → no_match, split-used still 1."""
        owner_index = {}
        rec = {
            "record_id": "r1",
            "dallas_code": "NOF",
            "grantor": "Nobody, AtAll & Nope, Either",
        }
        stats = resolution_paths.run_path_a([rec], owner_index, {}, {})
        assert stats.joint_grantor_split_used == 1
        assert stats.no_match == 1
        assert rec.get("dcad_account") is None
