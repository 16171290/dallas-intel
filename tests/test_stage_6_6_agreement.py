"""Unit tests for scraper.stage_6_6_agreement."""
from __future__ import annotations

import pytest

from scraper import stage_6_6_agreement
from scraper.resolution import (
    PATH_A_GRANTOR_OWNER_INDEX,
    PATH_B_RAW_EXCERPT,
    PATH_C_LEGAL_RESOLVER,
    PATH_STAGE_6_6,
    STATUS_MATCHED,
    STATUS_MULTI_MATCH,
    WARN_PAGE_TIEBREAK_INCONCLUSIVE,
    WARN_PATH_AGREEMENT,
    WARN_PATH_DISAGREEMENT,
    WARN_TIEBROKEN_BY_PAGE,
    ResolutionHistoryEntry,
    append_history,
    get_alternate_accounts,
    get_history,
    get_warnings,
)
from scraper.stage_6_6_agreement import (
    AgreementState,
    Stage66Stats,
    audit_agreement,
    run_stage_6_6,
    score_address_match,
)


# ═══════════════════════════════════════════════════════════════════════════
# score_address_match
# ═══════════════════════════════════════════════════════════════════════════


class TestScoreAddressMatch:
    def test_full_overlap_scores_1(self):
        # Every significant candidate token appears in page text
        s = score_address_match("3031 CREST RIDGE DR",
                                "3031 CREST RIDGE DRIVE DALLAS TEXAS 75228")
        assert s == 1.0

    def test_no_overlap_scores_0(self):
        s = score_address_match("706 CORDOVA ST",
                                "3031 CREST RIDGE DRIVE DALLAS TEXAS 75228")
        # Possible tokens: 706 vs 3031, CORDOVA vs CREST RIDGE — zero overlap
        assert s == 0.0

    def test_partial_overlap(self):
        # Candidate "2206 BEAUMONT ST"; page "BEAUMONT DALLAS"
        # Candidate sig tokens: {2206, BEAUMONT}; common: {BEAUMONT}; score = 1/2
        s = score_address_match("2206 BEAUMONT ST", "BEAUMONT DALLAS")
        assert s == pytest.approx(0.5)

    def test_noise_tokens_ignored(self):
        # ST, DR, DALLAS, TX are all noise tokens — should not bias the score
        s_with_noise = score_address_match("100 OAK ST", "100 OAK ST DALLAS TX")
        s_without    = score_address_match("100 OAK",    "100 OAK")
        assert s_with_noise == s_without == 1.0

    def test_empty_candidate_scores_0(self):
        assert score_address_match("", "anything") == 0.0
        assert score_address_match(None, "anything") == 0.0

    def test_empty_page_scores_0(self):
        assert score_address_match("100 OAK ST", "") == 0.0
        assert score_address_match("100 OAK ST", None) == 0.0

    def test_only_noise_tokens_scores_0(self):
        # Candidate = "ST DR" only noise → no significant tokens → score 0
        assert score_address_match("ST DR", "any") == 0.0

    def test_cox_real_data(self):
        # The actual case from production
        page = "3031 CREST RIDGE DRIVE DALLAS TEXAS 75228"
        s_correct = score_address_match("3031 CREST RDG DR", page)
        s_wrong   = score_address_match("706 CORDOVA ST", page)
        # 3031 CREST RDG DR -> sig tokens {3031, CREST, RDG}; page has {3031, CREST, RIDGE}
        # common = {3031, CREST}; score = 2/3 ≈ 0.667
        assert s_correct == pytest.approx(2/3)
        assert s_wrong == 0.0
        # Margin between correct and wrong should be well above the default 0.15
        assert (s_correct - s_wrong) > 0.30

    def test_montgomery_real_data(self):
        page = "2206 BEAUMONT STREET GRAND PRAIRIE TEXAS 75051"
        s_correct = score_address_match("2206 BEAUMONT ST", page)
        s_wrong   = score_address_match("203 HIGH CANYON CT", page)
        # 2206, BEAUMONT both in page → 2/2 = 1.0
        assert s_correct == 1.0
        assert s_wrong == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# audit_agreement
# ═══════════════════════════════════════════════════════════════════════════


def _hist(path: str, account: str | None,
          status: str = STATUS_MATCHED) -> ResolutionHistoryEntry:
    return ResolutionHistoryEntry(
        path=path, stage="6.x", input=None, status=status,
        dcad_account=account,
    )


class TestAuditAgreement:
    def test_no_matches(self):
        rec = {"record_id": "r1"}
        s = audit_agreement(rec)
        assert s.kind == "no_match"
        assert s.picks == {}

    def test_single_path_single_match(self):
        rec = {"record_id": "r1", "dcad_account": "A1"}
        append_history(rec, _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        s = audit_agreement(rec)
        assert s.kind == "single"
        assert s.picks == {"A1": [PATH_A_GRANTOR_OWNER_INDEX]}
        assert s.primary_acct == "A1"

    def test_two_paths_agree(self):
        rec = {"record_id": "r1", "dcad_account": "A1"}
        append_history(rec, _hist(PATH_B_RAW_EXCERPT, "A1"))
        append_history(rec, _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        s = audit_agreement(rec)
        assert s.kind == "agreement"
        assert set(s.picks["A1"]) == {PATH_A_GRANTOR_OWNER_INDEX, PATH_B_RAW_EXCERPT}

    def test_two_paths_disagree(self):
        rec = {"record_id": "r1", "dcad_account": "A1"}
        append_history(rec, _hist(PATH_B_RAW_EXCERPT, "A1"))
        append_history(rec, _hist(PATH_A_GRANTOR_OWNER_INDEX, "A2"))
        s = audit_agreement(rec)
        assert s.kind == "disagreement"
        assert set(s.picks.keys()) == {"A1", "A2"}

    def test_three_paths_two_agree_one_disagrees(self):
        rec = {"record_id": "r1", "dcad_account": "A1"}
        append_history(rec, _hist(PATH_B_RAW_EXCERPT, "A1"))
        append_history(rec, _hist(PATH_C_LEGAL_RESOLVER, "A1"))
        append_history(rec, _hist(PATH_A_GRANTOR_OWNER_INDEX, "A2"))
        # 2 picks total: A1 (2 paths) vs A2 (1 path) → disagreement
        s = audit_agreement(rec)
        assert s.kind == "disagreement"

    def test_history_entries_without_dcad_account_ignored(self):
        # An entry with status=MATCHED but no dcad_account shouldn't count
        rec = {"record_id": "r1", "dcad_account": "A1"}
        append_history(rec, _hist(PATH_B_RAW_EXCERPT, None))
        append_history(rec, _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        s = audit_agreement(rec)
        assert s.kind == "single"

    def test_no_match_status_ignored(self):
        # Status=NO_MATCH entries don't count toward agreement
        rec = {"record_id": "r1", "dcad_account": "A1"}
        append_history(rec, _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1", status=STATUS_MATCHED))
        append_history(rec, _hist(PATH_C_LEGAL_RESOLVER, "A2", status="no_match"))
        s = audit_agreement(rec)
        assert s.kind == "single"

    def test_multi_match_status_counts(self):
        rec = {"record_id": "r1", "dcad_account": "A1"}
        append_history(rec, _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1", status=STATUS_MULTI_MATCH))
        append_history(rec, _hist(PATH_B_RAW_EXCERPT, "A1"))
        s = audit_agreement(rec)
        assert s.kind == "agreement"


# ═══════════════════════════════════════════════════════════════════════════
# run_stage_6_6
# ═══════════════════════════════════════════════════════════════════════════


class TestRunStage66:
    def _acct_to_addr(self):
        # Indices used across the disagreement tests
        return {
            "A1": "3031 CREST RIDGE DR",
            "A2": "706 CORDOVA ST",
            "B1": "2206 BEAUMONT ST",
            "B2": "203 HIGH CANYON CT",
            "C1": "1842 SHADY ELM DR",
        }

    def test_no_match_records_counted(self):
        records = [{"record_id": "r1"}]  # no history
        stats = run_stage_6_6(records, self._acct_to_addr())
        assert stats.no_match_records == 1
        assert get_warnings(records[0]) == []

    def test_single_path_no_warning(self):
        records = [{"record_id": "r1", "dcad_account": "A1"}]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        stats = run_stage_6_6(records, self._acct_to_addr())
        assert stats.single_path_records == 1
        assert WARN_PATH_AGREEMENT not in get_warnings(records[0])
        assert WARN_PATH_DISAGREEMENT not in get_warnings(records[0])

    def test_agreement_stamps_warning_and_history(self):
        records = [{"record_id": "r1", "dcad_account": "A1"}]
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "A1"))
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        stats = run_stage_6_6(records, self._acct_to_addr())
        assert stats.agreement_records == 1
        assert WARN_PATH_AGREEMENT in get_warnings(records[0])
        # Stage 6.6 history entry stamped
        s6_entries = [
            h for h in get_history(records[0]) if h["path"] == PATH_STAGE_6_6
        ]
        assert len(s6_entries) == 1
        assert s6_entries[0]["dcad_account"] == "A1"

    def test_disagreement_no_fetcher_records_warning_only(self):
        # No page fetcher; we record disagreement but don't change the stamp
        records = [{"record_id": "r1", "dcad_account": "A1"}]
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "A1"))
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A2"))
        stats = run_stage_6_6(records, self._acct_to_addr())
        assert stats.disagreement_records == 1
        assert stats.pages_fetched == 0
        assert WARN_PATH_DISAGREEMENT in get_warnings(records[0])
        assert records[0]["dcad_account"] == "A1"  # unchanged

    def test_disagreement_resolved_by_page_text(self):
        # Cox scenario: Path A picked 706 CORDOVA (A2), Path B picked 3031 CREST
        # RIDGE (A1). Page text says CREST RIDGE → A1 wins.
        records = [{"record_id": "315561585", "dcad_account": "A2"}]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A2"))
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "A1"))

        def fake_fetcher(rid):
            return "3031 CREST RIDGE DRIVE DALLAS TEXAS 75228"

        stats = run_stage_6_6(records, self._acct_to_addr(),
                              page_fetcher=fake_fetcher)
        assert stats.disagreement_records == 1
        assert stats.pages_fetched == 1
        assert stats.tiebroken == 1
        assert records[0]["dcad_account"] == "A1"  # winner
        assert records[0]["address_normalized"] == "3031 CREST RIDGE DR"
        assert "A2" in get_alternate_accounts(records[0])
        assert WARN_TIEBROKEN_BY_PAGE in get_warnings(records[0])
        # disagreement warning also recorded
        assert WARN_PATH_DISAGREEMENT in get_warnings(records[0])

    def test_disagreement_inconclusive_when_page_blank(self):
        records = [{"record_id": "r1", "dcad_account": "A2"}]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A2"))
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "A1"))

        def empty_fetcher(rid):
            return None

        stats = run_stage_6_6(records, self._acct_to_addr(),
                              page_fetcher=empty_fetcher)
        assert stats.inconclusive == 1
        assert stats.tiebroken == 0
        assert records[0]["dcad_account"] == "A2"  # unchanged
        assert WARN_PAGE_TIEBREAK_INCONCLUSIVE in get_warnings(records[0])

    def test_disagreement_inconclusive_when_page_too_generic(self):
        # Page says just "DALLAS" — noise-stripped to nothing.
        # Both candidates' addresses score 0.0 against empty significant tokens.
        records = [{"record_id": "r1", "dcad_account": "A2"}]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A2"))
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "A1"))

        def generic_fetcher(rid):
            return "DALLAS TX"

        stats = run_stage_6_6(records, self._acct_to_addr(),
                              page_fetcher=generic_fetcher)
        assert stats.inconclusive == 1
        assert records[0]["dcad_account"] == "A2"
        assert WARN_PAGE_TIEBREAK_INCONCLUSIVE in get_warnings(records[0])

    def test_disagreement_inconclusive_when_margin_too_small(self):
        # Both candidates appear partially — top score isn't decisive
        records = [{"record_id": "r1", "dcad_account": "A1"}]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "A2"))

        # Page text contains tokens from BOTH candidate addresses
        def ambiguous_fetcher(rid):
            return "CREST CORDOVA"  # Both A1 and A2 partly match

        stats = run_stage_6_6(records, self._acct_to_addr(),
                              page_fetcher=ambiguous_fetcher,
                              tiebreak_min_margin=0.50)
        # Margin too small → inconclusive
        assert stats.inconclusive == 1
        assert records[0]["dcad_account"] == "A1"

    def test_montgomery_scenario(self):
        # Production case: Path A picked HIGH CANYON, Path B picked BEAUMONT.
        records = [{"record_id": "315561589", "dcad_account": "B2"}]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "B2"))
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "B1"))

        def fetcher(rid):
            return "2206 BEAUMONT STREET GRAND PRAIRIE TEXAS 75051"

        stats = run_stage_6_6(records, self._acct_to_addr(),
                              page_fetcher=fetcher)
        assert stats.tiebroken == 1
        assert records[0]["dcad_account"] == "B1"
        assert records[0]["address_normalized"] == "2206 BEAUMONT ST"
        assert WARN_TIEBROKEN_BY_PAGE in get_warnings(records[0])

    def test_fetcher_exception_handled_gracefully(self):
        records = [{"record_id": "r1", "dcad_account": "A1"}]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        append_history(records[0], _hist(PATH_B_RAW_EXCERPT, "A2"))

        def crashing_fetcher(rid):
            raise RuntimeError("simulated network failure")

        stats = run_stage_6_6(records, self._acct_to_addr(),
                              page_fetcher=crashing_fetcher)
        # Stage 6.6 should not propagate the exception; falls into inconclusive
        assert stats.disagreement_records == 1
        assert stats.inconclusive == 1
        assert records[0]["dcad_account"] == "A1"

    def test_multi_record_mixed_outcomes(self):
        a_to_a = self._acct_to_addr()
        records = [
            # Record 1: single-path
            {"record_id": "single", "dcad_account": "A1"},
            # Record 2: agreement
            {"record_id": "agree", "dcad_account": "A1"},
            # Record 3: disagreement resolvable
            {"record_id": "disagree", "dcad_account": "A2"},
            # Record 4: no matches at all
            {"record_id": "miss"},
        ]
        append_history(records[0], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        append_history(records[1], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A1"))
        append_history(records[1], _hist(PATH_B_RAW_EXCERPT, "A1"))
        append_history(records[2], _hist(PATH_A_GRANTOR_OWNER_INDEX, "A2"))
        append_history(records[2], _hist(PATH_B_RAW_EXCERPT, "A1"))

        def fetcher(rid):
            return "3031 CREST RIDGE DR" if rid == "disagree" else None

        stats = run_stage_6_6(records, a_to_a, page_fetcher=fetcher)
        assert stats.single_path_records == 1
        assert stats.agreement_records == 1
        assert stats.disagreement_records == 1
        assert stats.no_match_records == 1
        assert stats.tiebroken == 1
        # Verify the tiebreak moved the disagreement record to A1
        assert records[2]["dcad_account"] == "A1"
