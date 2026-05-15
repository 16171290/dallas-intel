"""
Regression tests for scraper.dcad_bulk.

No network calls — uses synthetic ZIPs created with tempfile and zipfile.
"""

import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scraper import dcad_bulk


# ═══════════════════════════════════════════════════════════════════════════
# Cache path
# ═══════════════════════════════════════════════════════════════════════════

class TestCachePath:
    def test_cache_path_is_in_cache_dir(self):
        path = dcad_bulk._cache_path(2026)
        from scraper import config
        assert path.parent == config.DCAD_CACHE_DIR

    def test_cache_path_includes_year(self):
        path = dcad_bulk._cache_path(2024)
        assert "2024" in path.name

    def test_cache_path_includes_iso_week(self):
        path = dcad_bulk._cache_path(2026)
        iso = date.today().isocalendar()
        expected = f"{iso.year}{iso.week:02d}"
        assert expected in path.name

    def test_cache_path_zip_extension(self):
        path = dcad_bulk._cache_path(2026)
        assert path.suffix == ".zip"


# ═══════════════════════════════════════════════════════════════════════════
# Data-member filter
# ═══════════════════════════════════════════════════════════════════════════

class TestIsDataMember:
    def test_csv_is_data(self):
        assert dcad_bulk._is_data_member("ACCOUNT_INFO.csv")
        assert dcad_bulk._is_data_member("MULTI_OWNER.CSV")
        assert dcad_bulk._is_data_member("subdir/LAND.csv")

    def test_txt_is_data(self):
        assert dcad_bulk._is_data_member("ACCOUNT_INFO.txt")
        assert dcad_bulk._is_data_member("LAND.TXT")

    def test_readme_is_not_data(self):
        assert not dcad_bulk._is_data_member("README.txt")
        assert not dcad_bulk._is_data_member("readme.csv")
        assert not dcad_bulk._is_data_member("README.pdf")

    def test_schema_docs_not_data(self):
        assert not dcad_bulk._is_data_member("FIELD_LAYOUT.txt")
        assert not dcad_bulk._is_data_member("SCHEMA.csv")
        assert not dcad_bulk._is_data_member("Documentation.txt")
        assert not dcad_bulk._is_data_member("layout_doc.csv")

    def test_pdf_not_data(self):
        assert not dcad_bulk._is_data_member("manual.pdf")
        assert not dcad_bulk._is_data_member("ACCOUNT_INFO.pdf")

    def test_unknown_extension(self):
        assert not dcad_bulk._is_data_member("ACCOUNT_INFO.dat")
        assert not dcad_bulk._is_data_member("data.xml")


# ═══════════════════════════════════════════════════════════════════════════
# ZIP parsing
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def synthetic_zip(tmp_path: Path) -> Path:
    """Build a small ZIP with two data tables and one readme."""
    zip_path = tmp_path / "fake_dcad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "ACCOUNT_INFO.csv",
            "ACCOUNT_NUM,STREET_NUM,STREET_NAME,STREET_SUFFIX\n"
            "10001,100,MAIN,ST\n"
            "10002,200,ELM,AVE\n"
            "10003,300,OAK,DR\n"
        )
        zf.writestr(
            "MULTI_OWNER.csv",
            "ACCOUNT_NUM,OWNER_SEQ_NUM,OWNER_NAME\n"
            "10001,1,JOHN SMITH\n"
            "10002,1,JANE DOE\n"
            "10003,1,ROBERT JONES\n"
        )
        zf.writestr("README.txt", "This is a readme; should be skipped.")
    return zip_path


class TestParseTables:
    def test_returns_dict_keyed_by_uppercased_stem(self, synthetic_zip):
        tables = dcad_bulk.parse_dcad_tables(synthetic_zip)
        assert "ACCOUNT_INFO" in tables
        assert "MULTI_OWNER" in tables

    def test_skips_readme(self, synthetic_zip):
        tables = dcad_bulk.parse_dcad_tables(synthetic_zip)
        assert "README" not in tables

    def test_columns_preserved(self, synthetic_zip):
        tables = dcad_bulk.parse_dcad_tables(synthetic_zip)
        assert set(tables["ACCOUNT_INFO"].columns) == {
            "ACCOUNT_NUM", "STREET_NUM", "STREET_NAME", "STREET_SUFFIX"
        }

    def test_row_counts(self, synthetic_zip):
        tables = dcad_bulk.parse_dcad_tables(synthetic_zip)
        assert len(tables["ACCOUNT_INFO"]) == 3
        assert len(tables["MULTI_OWNER"]) == 3

    def test_data_is_string_typed(self, synthetic_zip):
        """ACCOUNT_NUM should remain a string (no leading-zero loss)."""
        tables = dcad_bulk.parse_dcad_tables(synthetic_zip)
        assert tables["ACCOUNT_INFO"]["ACCOUNT_NUM"].iloc[0] == "10001"


# ═══════════════════════════════════════════════════════════════════════════
# Address index
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildAddressIndex:
    def test_empty_when_no_account_info(self):
        idx = dcad_bulk.build_address_index({})
        assert idx == {}

    def test_empty_when_required_columns_missing(self):
        bad = pd.DataFrame({"WRONG_COL": ["x"]})
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": bad})
        assert idx == {}

    def test_basic_index(self):
        df = pd.DataFrame({
            "ACCOUNT_NUM":      ["10001", "10002"],
            "STREET_NUM":       ["100", "200"],
            "FULL_STREET_NAME": ["MAIN ST", "ELM AVE"],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        assert idx.get("100 MAIN ST") == "10001"
        assert idx.get("200 ELM AVE") == "10002"

    def test_with_directional_prefix(self):
        """FULL_STREET_NAME may contain a directional prefix."""
        df = pd.DataFrame({
            "ACCOUNT_NUM":      ["10001"],
            "STREET_NUM":       ["100"],
            "FULL_STREET_NAME": ["N MAIN ST"],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        assert idx.get("100 N MAIN ST") == "10001"

    def test_with_half_num(self):
        """STREET_HALF_NUM (e.g. '1/2') is included if present."""
        df = pd.DataFrame({
            "ACCOUNT_NUM":      ["10001"],
            "STREET_NUM":       ["100"],
            "STREET_HALF_NUM":  ["1/2"],
            "FULL_STREET_NAME": ["MAIN ST"],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        assert idx.get("100 1/2 MAIN ST") == "10001"

    def test_index_uses_normalized_form(self):
        """Suffix expansion happens in normalize_address."""
        df = pd.DataFrame({
            "ACCOUNT_NUM":      ["10001"],
            "STREET_NUM":       ["100"],
            "FULL_STREET_NAME": ["MAIN STREET"],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        # STREET → ST
        assert idx.get("100 MAIN ST") == "10001"

    def test_skips_blank_addresses(self):
        df = pd.DataFrame({
            "ACCOUNT_NUM":      ["10001", "10002"],
            "STREET_NUM":       ["100", ""],
            "FULL_STREET_NAME": ["MAIN ST", ""],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        assert idx.get("100 MAIN ST") == "10001"
        assert len(idx) == 1


# ═══════════════════════════════════════════════════════════════════════════
# ZIP URL discovery
# ═══════════════════════════════════════════════════════════════════════════

# Fixture HTML modeled on the real DataProducts.aspx structure
# (web-fetched 2026-05-13). The full page lists ~30 ZIPs across five
# categories; this fixture preserves one representative entry per category
# so the deny-list is exercised.
_DATA_PRODUCTS_FIXTURE = """
<html><body>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\DCAD2026_CURRENT.ZIP">
  2026 Data Files (No Values - Most Current Ownership)
</a>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\DCAD2025_CURRENT.ZIP">
  2025 Certified Data Files with Supplemental Changes (Comma Delimited)
</a>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\DCAD2024_CURRENT.ZIP">
  2024 Certified Data Files with Supplemental Changes (Comma Delimited)
</a>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\DCAD2025_BPP_DETAIL_CURRENT.zip">
  2025 BPP Detailed Value Data File with Supplemental Changes (Comma Delimited)
</a>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\2025_REAL_PROPERTY_CERT_APPR_ROLL.zip">
  2025 Real Property Certified Appraisal Roll (Fixed Format 07/24/2025)
</a>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\DCAD2025_CERTIFIED_07242025.zip">
  2025 Certified Data Files at Certification (Comma Delimited 07/24/2025)
</a>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\ARB_CURRENT.zip">
  2021 - 2025 Active Appraisal Review Board Data Files (Comma Delimited)
</a>
<a href="https://www.dallascad.org/ViewPDFs.aspx?type=3&id=\\\\DCAD.ORG\\WEB\\WEBDATA\\WEBFORMS\\DATA PRODUCTS\\MAIL1_APPRAISAL_NOTICE_DATA_2025.zip">
  2025 Mail 1 Appraisal Notice Data (RES and COM)
</a>
</body></html>
"""


class _FakeResp:
    """Mimic requests.Response for unit-testing _discover_zip_url."""
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class TestDiscoverZipUrl:
    """Pin the resolver against a fixture page mirroring real DCAD structure."""

    def _patch_get(self, monkeypatch, html: str = _DATA_PRODUCTS_FIXTURE):
        monkeypatch.setattr(
            dcad_bulk.requests, "get",
            lambda *a, **kw: _FakeResp(html.encode("utf-8")),
        )

    def test_picks_property_data_for_target_year(self, monkeypatch):
        self._patch_get(monkeypatch)
        url = dcad_bulk._discover_zip_url(2025)
        assert "DCAD2025_CURRENT.ZIP" in url

    def test_picks_2026_no_values_zip(self, monkeypatch):
        """For year 2026, the 'No Values - Most Current Ownership' ZIP wins."""
        self._patch_get(monkeypatch)
        url = dcad_bulk._discover_zip_url(2026)
        assert "DCAD2026_CURRENT.ZIP" in url

    def test_rejects_arb_zip(self, monkeypatch):
        self._patch_get(monkeypatch)
        url = dcad_bulk._discover_zip_url(2025)
        assert "ARB" not in url.upper()

    def test_rejects_bpp_zip(self, monkeypatch):
        self._patch_get(monkeypatch)
        url = dcad_bulk._discover_zip_url(2025)
        assert "BPP" not in url.upper()

    def test_rejects_fixed_format_zip(self, monkeypatch):
        self._patch_get(monkeypatch)
        url = dcad_bulk._discover_zip_url(2025)
        assert "REAL_PROPERTY_CERT" not in url.upper()
        assert "APPR_ROLL" not in url.upper()

    def test_rejects_appraisal_notice_zip(self, monkeypatch):
        self._patch_get(monkeypatch)
        url = dcad_bulk._discover_zip_url(2025)
        assert "APPRAISAL_NOTICE" not in url.upper()
        assert "MAIL" not in url.upper()

    def test_rejects_certified_snapshot(self, monkeypatch):
        """The dated _CERTIFIED_<DATE>.zip is a frozen snapshot; we want _CURRENT."""
        self._patch_get(monkeypatch)
        url = dcad_bulk._discover_zip_url(2025)
        assert "CERTIFIED_07" not in url.upper()
        assert "_CURRENT" in url.upper()

    def test_missing_year_raises(self, monkeypatch):
        """Year not present on the page → DCADFetchError with remediation hint."""
        self._patch_get(monkeypatch)
        with pytest.raises(dcad_bulk.DCADFetchError) as exc_info:
            dcad_bulk._discover_zip_url(2099)
        assert "DCAD_ZIP_URL" in str(exc_info.value)

    def test_empty_page_raises(self, monkeypatch):
        """No anchors at all → DCADFetchError."""
        self._patch_get(monkeypatch, html="<html><body><p>nothing</p></body></html>")
        with pytest.raises(dcad_bulk.DCADFetchError):
            dcad_bulk._discover_zip_url(2025)
