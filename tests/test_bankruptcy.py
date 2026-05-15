"""Tests for ``scraper.bankruptcy``."""

from __future__ import annotations

import io
from unittest import mock

import pytest

from scraper import bankruptcy


# ============================================================================
# _parse_title
# ============================================================================


class TestParseTitle:
    def test_bare_chapter_suffix(self):
        # No judge code, just chapter number after the dash.
        result = bankruptcy._parse_title("26-32140-7 Eddie C. Watkins")
        assert result == ("26-32140", "26-32140-7", "Eddie C. Watkins")

    def test_judge_code_plus_chapter(self):
        result = bankruptcy._parse_title(
            "26-32138-mvl13 Jose Angel Rivera Hernandez"
        )
        assert result == (
            "26-32138",
            "26-32138-mvl13",
            "Jose Angel Rivera Hernandez",
        )

    def test_judge_code_plus_chapter_7(self):
        result = bankruptcy._parse_title("26-20164-bwo7 Battery Joe-John LLC")
        assert result == ("26-20164", "26-20164-bwo7", "Battery Joe-John LLC")

    def test_joint_filers_in_title(self):
        result = bankruptcy._parse_title(
            "26-32136-swe7 Michael Earl Watts and Jan M Watts"
        )
        assert result == (
            "26-32136",
            "26-32136-swe7",
            "Michael Earl Watts and Jan M Watts",
        )

    def test_leading_whitespace_tolerated(self):
        result = bankruptcy._parse_title("   26-32140-7   Eddie C. Watkins")
        assert result == ("26-32140", "26-32140-7", "Eddie C. Watkins")

    def test_returns_none_for_empty(self):
        assert bankruptcy._parse_title("") is None
        assert bankruptcy._parse_title("   ") is None

    def test_returns_none_for_no_case_number(self):
        # Title that doesn't match the pattern at all.
        assert bankruptcy._parse_title("Just some random text") is None

    def test_returns_none_for_missing_debtor(self):
        # Case number with no name after it.
        assert bankruptcy._parse_title("26-32140-7") is None
        assert bankruptcy._parse_title("26-32140-7   ") is None

    def test_4_digit_sequence(self):
        # Smaller case numbers (older years) work too.
        result = bankruptcy._parse_title("21-1234-7 Old Case")
        assert result == ("21-1234", "21-1234-7", "Old Case")

    def test_6_digit_sequence(self):
        # Larger sequence numbers (high-volume years).
        result = bankruptcy._parse_title("26-123456-mvl13 Some Debtor")
        assert result == ("26-123456", "26-123456-mvl13", "Some Debtor")


# ============================================================================
# _split_joint_filers
# ============================================================================


class TestSplitJointFilers:
    def test_single_filer(self):
        assert bankruptcy._split_joint_filers("Eddie C. Watkins") == [
            "Eddie C. Watkins"
        ]

    def test_two_filers(self):
        assert bankruptcy._split_joint_filers(
            "Michael Earl Watts and Jan M Watts"
        ) == ["Michael Earl Watts", "Jan M Watts"]

    def test_three_filers(self):
        # Rare but possible (joint spousal + dependent filing).
        result = bankruptcy._split_joint_filers("A and B and C")
        assert result == ["A", "B", "C"]

    def test_case_insensitive_and(self):
        result = bankruptcy._split_joint_filers("John AND Jane")
        assert result == ["John", "Jane"]

    def test_no_split_when_no_and(self):
        assert bankruptcy._split_joint_filers("McAndrews John") == [
            "McAndrews John"
        ]

    def test_no_split_when_and_is_substring(self):
        # 'and' inside another word should not split.
        assert bankruptcy._split_joint_filers("Andrew Johnson") == [
            "Andrew Johnson"
        ]
        assert bankruptcy._split_joint_filers("Sandra Land") == [
            "Sandra Land"
        ]

    def test_empty(self):
        assert bankruptcy._split_joint_filers("") == []

    def test_whitespace_only(self):
        assert bankruptcy._split_joint_filers("   ") == []


# ============================================================================
# _is_business_name
# ============================================================================


class TestIsBusinessName:
    def test_llc(self):
        assert bankruptcy._is_business_name("Battery Joe-John LLC")
        assert bankruptcy._is_business_name("Battery Joe-John L.L.C.")
        assert bankruptcy._is_business_name("Battery Joe-John L.L.C")

    def test_inc(self):
        assert bankruptcy._is_business_name("Acme Holdings Inc")
        assert bankruptcy._is_business_name("Acme Holdings Inc.")
        assert bankruptcy._is_business_name("Acme Holdings INCORPORATED")

    def test_corp(self):
        assert bankruptcy._is_business_name("Widgets Corp")
        assert bankruptcy._is_business_name("Widgets Corporation")

    def test_lp_partnership(self):
        assert bankruptcy._is_business_name("Smith Holdings LP")
        assert bankruptcy._is_business_name("Smith Holdings L.P.")

    def test_pllc(self):
        assert bankruptcy._is_business_name("Law Firm PLLC")

    def test_llp(self):
        assert bankruptcy._is_business_name("Big Law LLP")

    def test_pa(self):
        assert bankruptcy._is_business_name("Smith Medical PA")
        assert bankruptcy._is_business_name("Smith Medical P.A.")

    def test_individual_name_not_matched(self):
        assert not bankruptcy._is_business_name("Eddie C. Watkins")
        assert not bankruptcy._is_business_name("Jose Angel Rivera Hernandez")
        assert not bankruptcy._is_business_name("Mary Smith")

    def test_joint_individuals_not_matched(self):
        assert not bankruptcy._is_business_name(
            "Michael Earl Watts and Jan M Watts"
        )

    def test_empty(self):
        assert not bankruptcy._is_business_name("")
        assert not bankruptcy._is_business_name(None)

    def test_name_containing_corp_word_inside_not_at_end(self):
        # Only the *suffix* triggers business detection.
        assert not bankruptcy._is_business_name(
            "Corporal John Smith"  # 'Corp' at start, person name
        )


# ============================================================================
# _is_voluntary_petition
# ============================================================================


class TestIsVoluntaryPetition:
    def test_basic_voluntary_petition(self):
        desc = (
            "Type: bk Office: 3 Chapter: 7  "
            "[Voluntary petition chapter 7 (attorney filer)]"
        )
        assert bankruptcy._is_voluntary_petition(desc)

    def test_mixed_case(self):
        desc = "Type: bk Chapter: 13  [Voluntary Petition Chapter 13]"
        assert bankruptcy._is_voluntary_petition(desc)

    def test_chapter_in_parens(self):
        desc = "Type: bk Office: 3 Chapter: 7 [Voluntary petition (chapter 7)]"
        assert bankruptcy._is_voluntary_petition(desc)

    def test_motion_not_voluntary_petition(self):
        desc = "Type: bk Chapter: 7  [Motion for Relief from Stay]"
        assert not bankruptcy._is_voluntary_petition(desc)

    def test_discharge_order_not_voluntary_petition(self):
        desc = "Type: bk Chapter: 7  [Discharge of Debtor]"
        assert not bankruptcy._is_voluntary_petition(desc)

    def test_does_not_match_phrase_in_narrative(self):
        # Even if 'voluntary petition' appears unbracketed in body text,
        # we require the bracket anchor.
        desc = "Order denying motion to file voluntary petition late"
        assert not bankruptcy._is_voluntary_petition(desc)

    def test_empty(self):
        assert not bankruptcy._is_voluntary_petition("")
        assert not bankruptcy._is_voluntary_petition(None)


# ============================================================================
# _extract_chapter / _extract_office / _extract_trustee
# ============================================================================


class TestExtractFields:
    def test_chapter_7(self):
        desc = "Type: bk Office: 3 Chapter: 7  [Voluntary petition]"
        assert bankruptcy._extract_chapter(desc) == "7"

    def test_chapter_13(self):
        desc = "Type: bk Chapter: 13 Trustee: Powers, Thomas  [Voluntary Petition]"
        assert bankruptcy._extract_chapter(desc) == "13"

    def test_chapter_11(self):
        desc = "Type: bk Chapter: 11  [Voluntary petition]"
        assert bankruptcy._extract_chapter(desc) == "11"

    def test_chapter_missing(self):
        assert bankruptcy._extract_chapter("Type: bk  [Some event]") is None
        assert bankruptcy._extract_chapter("") is None
        assert bankruptcy._extract_chapter(None) is None

    def test_office_extraction(self):
        desc = "Type: bk Office: 3 Chapter: 7  [Voluntary petition]"
        assert bankruptcy._extract_office(desc) == "3"

    def test_office_missing(self):
        assert bankruptcy._extract_office("Type: bk Chapter: 7") is None

    def test_trustee_present(self):
        desc = (
            "Type: bk Office: 3 Chapter: 13 Trustee: Powers, Thomas  "
            "[Voluntary Petition Chapter 13]"
        )
        assert bankruptcy._extract_trustee(desc) == "Powers, Thomas"

    def test_trustee_with_middle_initial(self):
        desc = "Trustee: Ries, Kent David [Voluntary Petition]"
        assert bankruptcy._extract_trustee(desc) == "Ries, Kent David"

    def test_trustee_missing(self):
        # Ch.7 cases often have no Trustee at filing time.
        desc = "Type: bk Office: 3 Chapter: 7  [Voluntary petition chapter 7]"
        assert bankruptcy._extract_trustee(desc) is None

    def test_trustee_does_not_swallow_bracket(self):
        # Make sure the trustee regex stops before the [event] portion.
        desc = "Trustee: Lopez, Aldo [Voluntary petition chapter 7]"
        assert bankruptcy._extract_trustee(desc) == "Lopez, Aldo"


# ============================================================================
# parse_feed (XML structural parsing)
# ============================================================================


_SAMPLE_RSS = """<?xml version="1.0" encoding="ISO-8859-1"?>
<rss version="2.0">
  <channel>
    <title>Northern District of Texas Bankruptcy - Recent Entries</title>
    <link>https://ecf.txnb.uscourts.gov</link>
    <description>Docket entries</description>
    <item>
      <title>26-32140-7 Eddie C. Watkins</title>
      <link>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12345</link>
      <description>Type: bk Office: 3 Chapter: 7  [Voluntary petition chapter 7 (attorney filer)]</description>
      <pubDate>Wed, 14 May 2026 21:30:00 GMT</pubDate>
      <guid>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12345</guid>
    </item>
    <item>
      <title>26-32138-mvl13 Jose Angel Rivera Hernandez</title>
      <link>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12346</link>
      <description>Type: bk Office: 3 Chapter: 13 Trustee: Powers, Thomas  [Voluntary Petition Chapter 13 (case upload)]</description>
      <pubDate>Wed, 14 May 2026 21:15:00 GMT</pubDate>
      <guid>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12346</guid>
    </item>
    <item>
      <title>26-32136-swe7 Michael Earl Watts and Jan M Watts</title>
      <link>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12347</link>
      <description>Type: bk Office: 3 Chapter: 7 Trustee: Yaquinto, Robert  [Voluntary petition (chapter 7)]</description>
      <pubDate>Wed, 14 May 2026 21:00:00 GMT</pubDate>
      <guid>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12347</guid>
    </item>
    <item>
      <title>26-20164-bwo7 Battery Joe-John LLC</title>
      <link>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12348</link>
      <description>Type: bk Office: 2 Chapter: 7 Trustee: Ries, Kent David [Voluntary petition chapter 7 (attorney filer)]</description>
      <pubDate>Wed, 14 May 2026 20:45:00 GMT</pubDate>
      <guid>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?12348</guid>
    </item>
    <item>
      <title>26-12345-7 Some Old Case</title>
      <link>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?99999</link>
      <description>Type: bk Office: 3 Chapter: 7  [Motion for Relief from Stay]</description>
      <pubDate>Wed, 14 May 2026 20:30:00 GMT</pubDate>
      <guid>https://ecf.txnb.uscourts.gov/cgi-bin/DktRpt.pl?99999</guid>
    </item>
  </channel>
</rss>
"""


class TestParseFeed:
    def test_parses_all_items(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        assert len(items) == 5

    def test_item_fields_populated(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        first = items[0]
        assert first["title"] == "26-32140-7 Eddie C. Watkins"
        assert "Voluntary petition" in first["description"]
        assert first["pubDate"] == "Wed, 14 May 2026 21:30:00 GMT"
        assert first["link"].startswith("https://")

    def test_empty_feed(self):
        empty_rss = """<?xml version="1.0"?>
            <rss version="2.0"><channel><title>Empty</title></channel></rss>"""
        items = bankruptcy.parse_feed(empty_rss.encode("utf-8"))
        assert items == []

    def test_invalid_xml_raises(self):
        with pytest.raises(bankruptcy.BankruptcyParseError):
            bankruptcy.parse_feed(b"<not valid xml")

    def test_handles_missing_optional_fields(self):
        minimal = """<?xml version="1.0"?>
            <rss version="2.0"><channel><item>
                <title>26-12345-7 Test Person</title>
            </item></channel></rss>"""
        items = bankruptcy.parse_feed(minimal.encode("utf-8"))
        assert len(items) == 1
        assert items[0]["title"] == "26-12345-7 Test Person"
        assert items[0]["link"] == ""
        assert items[0]["description"] == ""


# ============================================================================
# extract_voluntary_petitions
# ============================================================================


class TestExtractVoluntaryPetitions:
    def test_filters_to_voluntary_petitions_only(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        # 4 voluntary petitions + 1 motion = 4 records.
        assert len(records) == 4

    def test_single_filer_record(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        watkins = next(r for r in records if r.case_number == "26-32140")
        assert watkins.case_number_raw == "26-32140-7"
        assert watkins.chapter == "7"
        assert watkins.office == "3"
        assert watkins.debtor_names == ["Eddie C. Watkins"]
        assert watkins.is_business is False
        assert watkins.trustee is None  # Ch.7, no trustee assigned yet

    def test_ch13_record_with_trustee(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        rivera = next(r for r in records if r.case_number == "26-32138")
        assert rivera.chapter == "13"
        assert rivera.trustee == "Powers, Thomas"
        assert rivera.debtor_names == ["Jose Angel Rivera Hernandez"]

    def test_joint_filer_split(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        watts = next(r for r in records if r.case_number == "26-32136")
        assert watts.debtor_names == [
            "Michael Earl Watts",
            "Jan M Watts",
        ]
        assert watts.is_business is False

    def test_business_filer_flagged(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        biz = next(r for r in records if r.case_number == "26-20164")
        assert biz.is_business is True
        # Business name kept as a single entry, NOT joint-split.
        assert biz.debtor_names == ["Battery Joe-John LLC"]

    def test_motion_not_extracted(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        assert all(r.case_number != "26-12345" for r in records)

    def test_empty_items(self):
        assert bankruptcy.extract_voluntary_petitions([]) == []

    def test_records_preserve_pub_date_and_link(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        watkins = next(r for r in records if r.case_number == "26-32140")
        assert watkins.pub_date == "Wed, 14 May 2026 21:30:00 GMT"
        assert watkins.link.startswith("https://ecf.txnb.uscourts.gov")

    def test_record_to_dict(self):
        items = bankruptcy.parse_feed(_SAMPLE_RSS.encode("utf-8"))
        records = bankruptcy.extract_voluntary_petitions(items)
        d = records[0].to_dict()
        assert d["case_number"] == records[0].case_number
        assert d["debtor_names"] == records[0].debtor_names
        assert isinstance(d, dict)


# ============================================================================
# fetch_feed (HTTP layer)
# ============================================================================


class TestFetchFeed:
    def test_successful_fetch(self):
        # Mock urlopen to return our sample.
        body = _SAMPLE_RSS.encode("utf-8")
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__.return_value = mock_resp
        with mock.patch.object(
            bankruptcy.urllib.request, "urlopen", return_value=mock_resp
        ):
            result = bankruptcy.fetch_feed("https://example.test/feed")
        assert result == body

    def test_http_error_raises(self):
        from urllib.error import HTTPError

        def raise_http_error(*args, **kwargs):
            raise HTTPError(
                url="https://example.test/feed",
                code=403,
                msg="Forbidden",
                hdrs=None,
                fp=io.BytesIO(b"access denied"),
            )

        with mock.patch.object(
            bankruptcy.urllib.request, "urlopen", side_effect=raise_http_error
        ):
            with pytest.raises(bankruptcy.BankruptcyFeedError) as exc_info:
                bankruptcy.fetch_feed("https://example.test/feed")
        assert "403" in str(exc_info.value)

    def test_url_error_raises(self):
        from urllib.error import URLError

        with mock.patch.object(
            bankruptcy.urllib.request,
            "urlopen",
            side_effect=URLError("Connection refused"),
        ):
            with pytest.raises(bankruptcy.BankruptcyFeedError) as exc_info:
                bankruptcy.fetch_feed("https://example.test/feed")
        assert "Network error" in str(exc_info.value)


# ============================================================================
# fetch_voluntary_petitions (end-to-end integration)
# ============================================================================


class TestFetchVoluntaryPetitions:
    def test_end_to_end_with_mock(self):
        body = _SAMPLE_RSS.encode("utf-8")
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__.return_value = mock_resp
        with mock.patch.object(
            bankruptcy.urllib.request, "urlopen", return_value=mock_resp
        ):
            records = bankruptcy.fetch_voluntary_petitions(
                "https://example.test/feed"
            )
        assert len(records) == 4
        case_numbers = {r.case_number for r in records}
        assert case_numbers == {"26-32140", "26-32138", "26-32136", "26-20164"}
