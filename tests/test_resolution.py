"""Tests for scraper.resolution — the PR 1 schema scaffolding.

Foundation tests for the multi-path lead-resolution framework.
PRs 2-7 depend on these helpers being correct, so this suite must be
exhaustive about:

  - Constants are spelled correctly and not silently reassigned
  - Lazy-safe behavior on records that lack signal_metadata
  - Validation rejects unknown enum values
  - Idempotency where the design guarantees it
  - Read helpers never raise on malformed records
"""

from __future__ import annotations

import pytest

from scraper import resolution
from scraper.resolution import (
    # Path identifiers
    PATH_STAGE_7_ADDRESS_INDEX, PATH_B_RAW_EXCERPT,
    PATH_A_GRANTOR_OWNER_INDEX, PATH_C_LEGAL_RESOLVER, PATH_C_APN,
    PATH_PB_DECEDENT_OWNER_INDEX, PATH_PB_APPLICANT_OWNER_INDEX,
    VALID_PATHS,
    # Statuses
    STATUS_MATCHED, STATUS_NO_MATCH, STATUS_MULTI_MATCH,
    STATUS_SKIPPED, STATUS_GUARDED, STATUS_ERROR,
    VALID_STATUSES,
    # Tiers
    TIER_EXACT, TIER_NO_MIDDLE, TIER_SURNAME_ONLY, TIER_FUZZY_SUBDIVISION,
    VALID_TIERS,
    # Confidence
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_NONE,
    VALID_CONFIDENCES,
    # Warnings
    WARN_MULTI_ACCOUNT, WARN_SURNAME_ONLY_TIER, WARN_SURNAME_DRIFT,
    WARN_LOW_MARKET_VALUE, WARN_PATH_DISAGREEMENT,
    CANONICAL_WARNINGS,
    # Blocklists
    GRANTOR_BOILERPLATE_BLOCKLIST, VENUE_TRUSTEE_SIGS,
    # Dataclass
    ResolutionHistoryEntry,
    # Helpers
    append_history, get_history,
    set_primary_resolution, get_primary_resolution,
    set_confidence, get_confidence,
    add_warning, get_warnings,
    set_alternate_accounts, get_alternate_accounts,
    get_matched_entries, get_accounts_produced, get_paths_attempted,
)


# ═══════════════════════════════════════════════════════════════════════════
# Constants — verify spelling + membership
# ═══════════════════════════════════════════════════════════════════════════

class TestConstants:
    def test_all_path_constants_in_valid_paths(self):
        for p in (PATH_STAGE_7_ADDRESS_INDEX, PATH_B_RAW_EXCERPT,
                  PATH_A_GRANTOR_OWNER_INDEX, PATH_C_LEGAL_RESOLVER, PATH_C_APN,
                  PATH_PB_DECEDENT_OWNER_INDEX, PATH_PB_APPLICANT_OWNER_INDEX):
            assert p in VALID_PATHS, f"Path constant {p!r} missing from VALID_PATHS"

    def test_all_status_constants_in_valid_statuses(self):
        for s in (STATUS_MATCHED, STATUS_NO_MATCH, STATUS_MULTI_MATCH,
                  STATUS_SKIPPED, STATUS_GUARDED, STATUS_ERROR):
            assert s in VALID_STATUSES

    def test_all_tier_constants_in_valid_tiers(self):
        for t in (TIER_EXACT, TIER_NO_MIDDLE, TIER_SURNAME_ONLY,
                  TIER_FUZZY_SUBDIVISION):
            assert t in VALID_TIERS

    def test_all_confidence_constants_in_valid_confidences(self):
        for c in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_NONE):
            assert c in VALID_CONFIDENCES

    def test_all_warning_constants_in_canonical_warnings(self):
        for w in (WARN_MULTI_ACCOUNT, WARN_SURNAME_ONLY_TIER, WARN_SURNAME_DRIFT,
                  WARN_LOW_MARKET_VALUE, WARN_PATH_DISAGREEMENT):
            assert w in CANONICAL_WARNINGS

    def test_constants_are_frozensets(self):
        # frozenset prevents accidental mutation in test helpers
        for name in ("VALID_PATHS", "VALID_STATUSES", "VALID_TIERS",
                     "VALID_CONFIDENCES", "CANONICAL_WARNINGS",
                     "GRANTOR_BOILERPLATE_BLOCKLIST"):
            assert isinstance(getattr(resolution, name), frozenset), \
                f"{name} must be frozenset to prevent mutation"

    def test_venue_trustee_sigs_uppercase(self):
        # Comparison is via .upper() containment — all sigs must already be upper
        for sig in VENUE_TRUSTEE_SIGS:
            assert sig == sig.upper(), f"VENUE_TRUSTEE_SIGS entry {sig!r} must be uppercase"

    def test_grantor_blocklist_uppercase(self):
        for tok in GRANTOR_BOILERPLATE_BLOCKLIST:
            assert tok == tok.upper(), f"Blocklist entry {tok!r} must be uppercase"


# ═══════════════════════════════════════════════════════════════════════════
# ResolutionHistoryEntry — validation + serialization
# ═══════════════════════════════════════════════════════════════════════════

class TestHistoryEntry:
    def test_minimal_construct(self):
        e = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX,
            stage="6.4",
        )
        assert e.path == PATH_A_GRANTOR_OWNER_INDEX
        assert e.stage == "6.4"
        assert e.status == STATUS_NO_MATCH  # default
        assert e.dcad_account is None
        assert e.alternates == []
        assert e.warnings == []

    def test_full_construct(self):
        e = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX,
            stage="6.4",
            input="OUSLEY SKYLER",
            status=STATUS_MATCHED,
            tier=TIER_EXACT,
            dcad_account="00000555358000000",
            alternates=[],
            warnings=[],
        )
        assert e.dcad_account == "00000555358000000"

    def test_multi_match_with_alternates(self):
        e = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX,
            stage="6.4",
            input="WILSON ROBERT",
            status=STATUS_MULTI_MATCH,
            tier=TIER_EXACT,
            dcad_account="22137400110070000",
            alternates=["440162400J0050000", "00000416917000000"],
            warnings=[WARN_MULTI_ACCOUNT, WARN_COMMON_NAME_POLLUTION := "common_name_pollution"],
        )
        assert len(e.alternates) == 2
        assert WARN_MULTI_ACCOUNT in e.warnings

    def test_invalid_path_rejected(self):
        with pytest.raises(ValueError, match="not in VALID_PATHS"):
            ResolutionHistoryEntry(path="bogus_path", stage="6.4")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError, match="not in VALID_STATUSES"):
            ResolutionHistoryEntry(
                path=PATH_A_GRANTOR_OWNER_INDEX,
                stage="6.4",
                status="bogus_status",
            )

    def test_invalid_tier_rejected(self):
        with pytest.raises(ValueError, match="not in VALID_TIERS"):
            ResolutionHistoryEntry(
                path=PATH_A_GRANTOR_OWNER_INDEX,
                stage="6.4",
                tier="bogus_tier",
            )

    def test_tier_none_allowed(self):
        # Path B + Stage 7 use no tier; None is valid
        e = ResolutionHistoryEntry(
            path=PATH_B_RAW_EXCERPT,
            stage="6.3",
            tier=None,
        )
        assert e.tier is None

    def test_unknown_warning_rejected(self):
        with pytest.raises(ValueError, match="non-canonical values"):
            ResolutionHistoryEntry(
                path=PATH_A_GRANTOR_OWNER_INDEX,
                stage="6.4",
                warnings=["i_made_this_up"],
            )

    def test_to_dict_matches_design_doc_schema(self):
        e = ResolutionHistoryEntry(
            path=PATH_C_LEGAL_RESOLVER,
            stage="6.5",
            input="PLEASANTWOOD ADDITION NO Lot 8 Block 9/6262",
            status=STATUS_MATCHED,
            tier=TIER_EXACT,
            dcad_account="00000555358000000",
        )
        d = e.to_dict()
        # Must match the exact keys defined in docs/RESOLUTION_PATHS_DESIGN.md §3
        assert set(d.keys()) == {
            "path", "stage", "input", "status", "skip_reason",
            "tier", "dcad_account", "alternates", "warnings",
        }

    def test_to_dict_is_serializable(self):
        # Round-trip through json to guarantee records.json compatibility
        import json
        e = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX,
            stage="6.4",
            input="OUSLEY SKYLER",
            status=STATUS_MATCHED,
            tier=TIER_EXACT,
            dcad_account="00000555358000000",
        )
        s = json.dumps(e.to_dict())
        parsed = json.loads(s)
        assert parsed["path"] == PATH_A_GRANTOR_OWNER_INDEX


# ═══════════════════════════════════════════════════════════════════════════
# Lazy-safe append_history / get_history
# ═══════════════════════════════════════════════════════════════════════════

class TestAppendHistory:
    def test_append_to_record_with_no_signal_metadata(self):
        # Probate-source records often have signal_metadata absent entirely.
        rec: dict = {"record_id": "r1"}
        entry = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4")
        append_history(rec, entry)
        assert "signal_metadata" in rec
        assert rec["signal_metadata"]["resolution_history"] == [entry.to_dict()]

    def test_append_to_record_with_signal_metadata_none(self):
        # enrichment.canonicalize_probate sets signal_metadata: None.
        rec: dict = {"record_id": "r1", "signal_metadata": None}
        entry = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4")
        append_history(rec, entry)
        # Should materialize, not crash
        assert isinstance(rec["signal_metadata"], dict)
        assert rec["signal_metadata"]["resolution_history"] == [entry.to_dict()]

    def test_append_to_record_with_existing_signal_metadata(self):
        rec: dict = {
            "record_id": "r1",
            "signal_metadata": {"existing_field": "preserved"},
        }
        entry = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4")
        append_history(rec, entry)
        # Existing fields must be preserved
        assert rec["signal_metadata"]["existing_field"] == "preserved"
        assert rec["signal_metadata"]["resolution_history"] == [entry.to_dict()]

    def test_append_multiple_entries_in_order(self):
        rec: dict = {"record_id": "r1"}
        e1 = ResolutionHistoryEntry(
            path=PATH_B_RAW_EXCERPT, stage="6.3", status=STATUS_NO_MATCH)
        e2 = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4",
            status=STATUS_MATCHED, tier=TIER_EXACT, dcad_account="X")
        e3 = ResolutionHistoryEntry(
            path=PATH_C_LEGAL_RESOLVER, stage="6.5",
            status=STATUS_MATCHED, tier=TIER_EXACT, dcad_account="X")
        append_history(rec, e1)
        append_history(rec, e2)
        append_history(rec, e3)
        history = rec["signal_metadata"]["resolution_history"]
        assert len(history) == 3
        assert [h["path"] for h in history] == [
            PATH_B_RAW_EXCERPT,
            PATH_A_GRANTOR_OWNER_INDEX,
            PATH_C_LEGAL_RESOLVER,
        ]

    def test_get_history_empty_when_absent(self):
        rec: dict = {"record_id": "r1"}
        assert get_history(rec) == []

    def test_get_history_empty_when_signal_metadata_none(self):
        rec: dict = {"record_id": "r1", "signal_metadata": None}
        assert get_history(rec) == []

    def test_get_history_empty_when_field_absent(self):
        rec: dict = {"record_id": "r1", "signal_metadata": {"other": "field"}}
        assert get_history(rec) == []

    def test_get_history_tolerates_malformed_history_field(self):
        # If someone wrote a non-list value, don't crash on read.
        rec: dict = {"record_id": "r1",
                     "signal_metadata": {"resolution_history": "not_a_list"}}
        assert get_history(rec) == []

    def test_signal_metadata_non_dict_replaced_with_warning(self):
        # Some malformed source sets signal_metadata to a list/str/etc.
        # The helper must replace with empty dict and write entry.
        rec: dict = {"record_id": "r1", "signal_metadata": ["bogus"]}
        entry = ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4")
        append_history(rec, entry)
        assert isinstance(rec["signal_metadata"], dict)
        assert rec["signal_metadata"]["resolution_history"] == [entry.to_dict()]


# ═══════════════════════════════════════════════════════════════════════════
# Primary resolution + confidence
# ═══════════════════════════════════════════════════════════════════════════

class TestPrimaryResolution:
    def test_set_then_get(self):
        rec: dict = {"record_id": "r1"}
        set_primary_resolution(rec, PATH_A_GRANTOR_OWNER_INDEX)
        assert get_primary_resolution(rec) == PATH_A_GRANTOR_OWNER_INDEX

    def test_set_validates_path(self):
        rec: dict = {"record_id": "r1"}
        with pytest.raises(ValueError, match="not in VALID_PATHS"):
            set_primary_resolution(rec, "bogus_path")

    def test_get_returns_none_when_absent(self):
        rec: dict = {"record_id": "r1"}
        assert get_primary_resolution(rec) is None

    def test_get_returns_none_when_signal_metadata_missing(self):
        assert get_primary_resolution({}) is None


class TestConfidence:
    def test_set_then_get(self):
        rec: dict = {"record_id": "r1"}
        set_confidence(rec, CONFIDENCE_HIGH)
        assert get_confidence(rec) == CONFIDENCE_HIGH

    def test_set_validates_level(self):
        rec: dict = {"record_id": "r1"}
        with pytest.raises(ValueError, match="not in VALID_CONFIDENCES"):
            set_confidence(rec, "really_sure")

    def test_get_defaults_to_none_for_legacy_records(self):
        """Per design doc §14 Q5: lazy migration. Legacy records that don't
        have the field should report CONFIDENCE_NONE."""
        rec: dict = {"record_id": "legacy"}
        assert get_confidence(rec) == CONFIDENCE_NONE

    def test_get_defaults_when_value_is_garbage(self):
        rec: dict = {"record_id": "r1",
                     "signal_metadata": {"confidence": "extremely_high"}}
        # Garbage value falls back to NONE; don't crash, don't lie.
        assert get_confidence(rec) == CONFIDENCE_NONE


# ═══════════════════════════════════════════════════════════════════════════
# Warnings
# ═══════════════════════════════════════════════════════════════════════════

class TestWarnings:
    def test_add_then_get(self):
        rec: dict = {"record_id": "r1"}
        add_warning(rec, WARN_MULTI_ACCOUNT)
        assert WARN_MULTI_ACCOUNT in get_warnings(rec)

    def test_add_idempotent(self):
        rec: dict = {"record_id": "r1"}
        add_warning(rec, WARN_MULTI_ACCOUNT)
        add_warning(rec, WARN_MULTI_ACCOUNT)
        add_warning(rec, WARN_MULTI_ACCOUNT)
        assert get_warnings(rec) == [WARN_MULTI_ACCOUNT]  # only once

    def test_add_multiple_distinct(self):
        rec: dict = {"record_id": "r1"}
        add_warning(rec, WARN_MULTI_ACCOUNT)
        add_warning(rec, WARN_SURNAME_DRIFT)
        add_warning(rec, WARN_LOW_MARKET_VALUE)
        warnings = get_warnings(rec)
        assert set(warnings) == {
            WARN_MULTI_ACCOUNT, WARN_SURNAME_DRIFT, WARN_LOW_MARKET_VALUE,
        }

    def test_add_validates_canonical(self):
        rec: dict = {"record_id": "r1"}
        with pytest.raises(ValueError, match="not in CANONICAL_WARNINGS"):
            add_warning(rec, "operator_intuition")

    def test_get_returns_empty_for_legacy(self):
        assert get_warnings({"record_id": "legacy"}) == []
        assert get_warnings({"record_id": "r", "signal_metadata": None}) == []

    def test_get_returns_copy(self):
        # Mutating the return value must not mutate the record's stored list
        rec: dict = {"record_id": "r1"}
        add_warning(rec, WARN_MULTI_ACCOUNT)
        returned = get_warnings(rec)
        returned.append("rogue_value")
        assert get_warnings(rec) == [WARN_MULTI_ACCOUNT]


# ═══════════════════════════════════════════════════════════════════════════
# Alternate accounts
# ═══════════════════════════════════════════════════════════════════════════

class TestAlternateAccounts:
    def test_set_then_get(self):
        rec: dict = {"record_id": "r1"}
        accts = ["acct1", "acct2", "acct3"]
        set_alternate_accounts(rec, accts)
        assert get_alternate_accounts(rec) == accts

    def test_set_filters_empty_values(self):
        rec: dict = {"record_id": "r1"}
        set_alternate_accounts(rec, ["acct1", "", None, "acct2"])
        assert get_alternate_accounts(rec) == ["acct1", "acct2"]

    def test_set_overwrites_previous(self):
        rec: dict = {"record_id": "r1"}
        set_alternate_accounts(rec, ["a", "b"])
        set_alternate_accounts(rec, ["c", "d"])
        assert get_alternate_accounts(rec) == ["c", "d"]

    def test_set_empty_clears(self):
        rec: dict = {"record_id": "r1"}
        set_alternate_accounts(rec, ["a", "b"])
        set_alternate_accounts(rec, [])
        assert get_alternate_accounts(rec) == []

    def test_get_returns_empty_for_legacy(self):
        assert get_alternate_accounts({"record_id": "legacy"}) == []


# ═══════════════════════════════════════════════════════════════════════════
# Read-only convenience accessors — Stage 6.6 + audits
# ═══════════════════════════════════════════════════════════════════════════

class TestConvenienceAccessors:
    def _build_record_with_history(self) -> dict:
        rec: dict = {"record_id": "r1"}
        # Path B tried, no match
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_B_RAW_EXCERPT, stage="6.3",
            input=None, status=STATUS_SKIPPED,
            skip_reason="no_clean_address_in_raw_excerpt"))
        # Path A matched
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4",
            input="OUSLEY SKYLER", status=STATUS_MATCHED,
            tier=TIER_EXACT, dcad_account="acct_X"))
        # Path C matched the SAME account (agreement)
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_C_LEGAL_RESOLVER, stage="6.5",
            input="PLEASANTWOOD Lot 8 Block 9/6262", status=STATUS_MATCHED,
            tier=TIER_EXACT, dcad_account="acct_X"))
        return rec

    def test_get_matched_entries_excludes_misses_and_skips(self):
        rec = self._build_record_with_history()
        matched = get_matched_entries(rec)
        assert len(matched) == 2
        assert all(m["status"] == STATUS_MATCHED for m in matched)

    def test_get_matched_entries_excludes_entries_without_account(self):
        rec: dict = {"record_id": "r1"}
        # A status=matched entry but missing dcad_account — exclude.
        # (Theoretically shouldn't happen in production but defensive.)
        rec.setdefault("signal_metadata", {})["resolution_history"] = [
            {"path": PATH_A_GRANTOR_OWNER_INDEX, "status": STATUS_MATCHED,
             "dcad_account": None},
        ]
        assert get_matched_entries(rec) == []

    def test_get_accounts_produced_collects_distinct(self):
        rec = self._build_record_with_history()
        accts = get_accounts_produced(rec)
        assert accts == {"acct_X"}  # both paths agreed

    def test_get_accounts_produced_handles_disagreement(self):
        rec: dict = {"record_id": "r1"}
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4",
            status=STATUS_MATCHED, tier=TIER_EXACT, dcad_account="acct_X"))
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_C_LEGAL_RESOLVER, stage="6.5",
            status=STATUS_MATCHED, tier=TIER_EXACT, dcad_account="acct_Y"))
        assert get_accounts_produced(rec) == {"acct_X", "acct_Y"}

    def test_get_paths_attempted_includes_skips_and_misses(self):
        rec = self._build_record_with_history()
        paths = get_paths_attempted(rec)
        assert paths == {
            PATH_B_RAW_EXCERPT,
            PATH_A_GRANTOR_OWNER_INDEX,
            PATH_C_LEGAL_RESOLVER,
        }

    def test_accessors_on_legacy_record_return_empty(self):
        legacy: dict = {"record_id": "old", "signal_metadata": None}
        assert get_matched_entries(legacy) == []
        assert get_accounts_produced(legacy) == set()
        assert get_paths_attempted(legacy) == set()


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end — simulate what a downstream PR would do
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEndUsage:
    def test_full_resolution_lifecycle_for_one_record(self):
        """Simulate what Path B + Path A + Stage 6.6 would write end-to-end
        for the user's reference record (Ousley Skyler)."""
        rec: dict = {
            "record_id": "315562561",
            "dallas_code": "NOF",
            "grantor": "Ousley, Skyler",
            "address_normalized": "DALLAS",  # city-only suspect
            "dcad_account": None,
            "raw_excerpt": "NOTICE OF FORECLOSURE | Subdivision - Name: "
                           "PLEASANTWOOD ADDITION NO Lot: 8 Block: 9/6262",
        }

        # Path B (PR 2): no clean address in raw_excerpt -> skipped
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_B_RAW_EXCERPT, stage="6.3",
            input=None, status=STATUS_SKIPPED,
            skip_reason="no_clean_address_in_raw_excerpt"))

        # Path A (PR 3): grantor matches owner_index -> matched
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4",
            input="OUSLEY SKYLER", status=STATUS_MATCHED,
            tier=TIER_EXACT, dcad_account="00000555358000000"))
        # Stage 6.4 stamps dcad_account on the record
        rec["dcad_account"] = "00000555358000000"
        rec["address_normalized"] = "6840 DART AVE"
        set_primary_resolution(rec, PATH_A_GRANTOR_OWNER_INDEX)

        # Path C (PR 4): legal-desc matches same account -> agreement
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_C_LEGAL_RESOLVER, stage="6.5",
            input="PLEASANTWOOD ADDITION NO Lot 8 Block 9/6262",
            status=STATUS_MATCHED, tier=TIER_EXACT,
            dcad_account="00000555358000000"))

        # Stage 6.6 (PR 5): cross-path audit detects agreement
        assert get_accounts_produced(rec) == {"00000555358000000"}
        assert get_paths_attempted(rec) == {
            PATH_B_RAW_EXCERPT, PATH_A_GRANTOR_OWNER_INDEX, PATH_C_LEGAL_RESOLVER,
        }
        set_confidence(rec, CONFIDENCE_HIGH)

        # Verify final shape matches what downstream consumers will see
        sm = rec["signal_metadata"]
        assert len(sm["resolution_history"]) == 3
        assert sm["primary_resolution"] == PATH_A_GRANTOR_OWNER_INDEX
        assert sm["confidence"] == CONFIDENCE_HIGH

    def test_wrong_person_scenario_emits_low_confidence(self):
        """Simulate the Heather/Bass false-positive: Path A surname_only
        guard fires, no match stamped. Confidence = NONE."""
        rec: dict = {
            "record_id": "rH",
            "dallas_code": "PB",
            "grantor": "Heather, Linda A.",
        }
        # Path A guarded due to surname_in_trust_first_name
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4",
            input="HEATHER LINDA A", status=STATUS_GUARDED,
            skip_reason="surname_in_trust_first_name",
            tier=TIER_SURNAME_ONLY))

        # No match. Confidence should remain NONE (default).
        assert get_confidence(rec) == CONFIDENCE_NONE
        assert get_accounts_produced(rec) == set()
        # The guard fired — the operator can see WHY in the history
        matched = get_matched_entries(rec)
        assert len(matched) == 0
        history = get_history(rec)
        assert history[0]["status"] == STATUS_GUARDED
        assert history[0]["skip_reason"] == "surname_in_trust_first_name"

    def test_multi_account_scenario_warning_workflow(self):
        """Wilson, Robert case: 8 accounts at exact tier. First-pick policy
        + multi_account warning."""
        rec: dict = {"record_id": "rW", "grantor": "Wilson, Robert"}
        eight_accts = [f"acct_{i}" for i in range(8)]
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX, stage="6.4",
            input="WILSON ROBERT", status=STATUS_MULTI_MATCH,
            tier=TIER_EXACT, dcad_account=eight_accts[0],
            alternates=eight_accts[1:],
            warnings=[WARN_MULTI_ACCOUNT, "common_name_pollution"]))

        # PR 5 (Stage 6.6) would also stamp record-level warnings
        add_warning(rec, WARN_MULTI_ACCOUNT)
        add_warning(rec, "common_name_pollution")  # canonical constant value
        set_alternate_accounts(rec, eight_accts[1:])
        set_confidence(rec, CONFIDENCE_LOW)

        # Verify alternates land in the right place for dashboard render
        assert len(get_alternate_accounts(rec)) == 7
        assert WARN_MULTI_ACCOUNT in get_warnings(rec)
        assert get_confidence(rec) == CONFIDENCE_LOW
