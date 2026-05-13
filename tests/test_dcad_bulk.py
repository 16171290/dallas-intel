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
            "ACCOUNT_NUM":   ["10001", "10002"],
            "STREET_NUM":    ["100", "200"],
            "STREET_NAME":   ["MAIN", "ELM"],
            "STREET_SUFFIX": ["ST", "AVE"],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        # normalize.normalize_address keeps "100 MAIN ST"
        assert idx.get("100 MAIN ST") == "10001"
        assert idx.get("200 ELM AVE") == "10002"

    def test_optional_directionals(self):
        df = pd.DataFrame({
            "ACCOUNT_NUM":   ["10001"],
            "STREET_NUM":    ["100"],
            "PREFIX_DIR":    ["N"],
            "STREET_NAME":   ["MAIN"],
            "STREET_SUFFIX": ["ST"],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        assert idx.get("100 N MAIN ST") == "10001"

    def test_index_uses_normalized_form(self):
        """An address spelled as 'STREET' should normalize to 'ST' in the key."""
        df = pd.DataFrame({
            "ACCOUNT_NUM":   ["10001"],
            "STREET_NUM":    ["100"],
            "STREET_NAME":   ["MAIN"],
            "STREET_SUFFIX": ["STREET"],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        # The build_address_index sends to normalize_address which expands
        # STREET → ST in the lookup; key should be "100 MAIN ST".
        assert idx.get("100 MAIN ST") == "10001"

    def test_skips_blank_addresses(self):
        df = pd.DataFrame({
            "ACCOUNT_NUM":   ["10001", "10002"],
            "STREET_NUM":    ["100", ""],
            "STREET_NAME":   ["MAIN", ""],
            "STREET_SUFFIX": ["ST", ""],
        })
        idx = dcad_bulk.build_address_index({"ACCOUNT_INFO": df})
        assert idx.get("100 MAIN ST") == "10001"
        assert len(idx) == 1
