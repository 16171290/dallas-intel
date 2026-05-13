"""Regression tests for scraper.output."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from scraper import output


# ═══════════════════════════════════════════════════════════════════════════
# JSON archive
# ═══════════════════════════════════════════════════════════════════════════

class TestRecordsJson:
    def test_round_trip(self, tmp_path: Path):
        records = [
            {"record_id": "A", "score": 90, "filing_date": "2026-05-13"},
            {"record_id": "B", "score": 50, "filing_date": "2026-05-12"},
        ]
        path = tmp_path / "records.json"
        output.write_records_json(records, path)
        loaded = output.read_records_json(path)
        assert len(loaded) == 2
        # Sorted by score descending.
        assert loaded[0]["score"] >= loaded[1]["score"]

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "deep" / "nested" / "records.json"
        output.write_records_json([], path)
        assert path.exists()

    def test_read_missing_returns_empty(self, tmp_path: Path):
        path = tmp_path / "absent.json"
        assert output.read_records_json(path) == []

    def test_read_malformed_returns_empty(self, tmp_path: Path):
        path = tmp_path / "bad.json"
        path.write_text("this is not json {")
        assert output.read_records_json(path) == []

    def test_archive_metadata(self, tmp_path: Path):
        path = tmp_path / "records.json"
        output.write_records_json([{"record_id": "A"}], path)
        data = json.loads(path.read_text())
        assert "generated_at" in data
        assert data["record_count"] == 1

    def test_atomic_write(self, tmp_path: Path):
        """A failed write should not leave a .tmp file behind."""
        path = tmp_path / "records.json"
        output.write_records_json([{"x": 1}], path)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []


# ═══════════════════════════════════════════════════════════════════════════
# Daily CSV
# ═══════════════════════════════════════════════════════════════════════════

class TestDailyCsv:
    def test_writes_csv_with_expected_filename(self, tmp_path: Path):
        path = output.write_daily_csv([], date(2026, 5, 13), tmp_path)
        assert path.name == "ghl_export_20260513.csv"
        assert path.exists()

    def test_header_present(self, tmp_path: Path):
        path = output.write_daily_csv([], date(2026, 5, 13), tmp_path)
        with path.open() as f:
            reader = csv.reader(f)
            header = next(reader)
        assert "First Name" in header
        assert "Address"    in header
        assert "Score"      in header
        assert "DCAD Account" in header

    def test_basic_record(self, tmp_path: Path):
        rec = {
            "record_id":     "A",
            "active":        True,
            "grantee":       "John Smith",
            "address":       "100 Main St, Dallas, TX 75201",
            "score":         85,
            "dcad_account":  "10001",
            "filing_date":   "2026-05-13",
            "instrument_num": "2026000123",
            "category":      "L/P",
            "dallas_code":   "LP",
            "source":        "publicsearch.us",
            "dcad_market_value": 250_000.0,
        }
        path = output.write_daily_csv([rec], date(2026, 5, 13), tmp_path)
        with path.open() as f:
            reader = csv.DictReader(f)
            row = next(reader)
        assert row["First Name"]  == "John"
        assert row["Last Name"]   == "Smith"
        assert row["Address"]     == "100 Main St"
        assert row["City"]        == "Dallas"
        assert row["State"]       == "TX"
        assert row["Zip"]         == "75201"
        assert row["Score"]       == "85"
        assert row["DCAD Account"] == "10001"
        assert "category:L/P" in row["Tags"]

    def test_inactive_records_excluded(self, tmp_path: Path):
        records = [
            {"record_id": "A", "active": True,  "grantee": "John Smith", "address": "100 Main"},
            {"record_id": "B", "active": False, "grantee": "Jane Doe",   "address": "200 Elm"},
        ]
        path = output.write_daily_csv(records, date(2026, 5, 13), tmp_path)
        with path.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["Last Name"] == "Smith"

    def test_homestead_in_tags(self, tmp_path: Path):
        rec = {
            "active": True, "grantee": "John Smith", "address": "100 Main",
            "category": "LIEN", "dcad_homestead": True,
            "dcad_account": "10001", "source": "publicsearch.us",
        }
        path = output.write_daily_csv([rec], date(2026, 5, 13), tmp_path)
        with path.open() as f:
            row = next(csv.DictReader(f))
        assert "homestead" in row["Tags"]


# ═══════════════════════════════════════════════════════════════════════════
# Name splitting
# ═══════════════════════════════════════════════════════════════════════════

class TestSplitName:
    def test_two_word(self):
        assert output._split_name("John Smith") == ("John", "Smith")

    def test_three_word(self):
        assert output._split_name("John Q Smith") == ("John Q", "Smith")

    def test_one_word_goes_to_last(self):
        assert output._split_name("Smith") == ("", "Smith")

    def test_empty(self):
        assert output._split_name("") == ("", "")
        assert output._split_name("   ") == ("", "")


# ═══════════════════════════════════════════════════════════════════════════
# Address splitting
# ═══════════════════════════════════════════════════════════════════════════

class TestSplitAddress:
    def test_comma_form(self):
        out = output._split_address("1004 Seago Drive, Seagoville, TX 75159")
        assert out["street"] == "1004 Seago Drive"
        assert out["city"]   == "Seagoville"
        assert out["state"]  == "TX"
        assert out["zip"]    == "75159"

    def test_space_form(self):
        out = output._split_address("200 N Main St Dallas TX 75201")
        # Whole string falls into "street" when no commas; zip extracted.
        assert out["zip"]   == "75201"
        assert out["state"] == "TX"

    def test_no_zip(self):
        out = output._split_address("100 Main St, Dallas, TX")
        assert out["state"] == "TX"
        assert out["zip"]   == ""

    def test_empty(self):
        out = output._split_address("")
        assert out["street"] == ""
        assert out["city"]   == ""
        assert out["state"]  == ""
        assert out["zip"]    == ""


# ═══════════════════════════════════════════════════════════════════════════
# Run summary
# ═══════════════════════════════════════════════════════════════════════════

class TestSummarizeRun:
    def test_empty(self):
        summary = output.summarize_run([])
        assert summary["total"]                    == 0
        assert summary["active"]                   == 0
        assert summary["by_category"]              == {}
        assert summary["address_resolution_rate"]  == 0.0

    def test_counts_by_category(self):
        records = [
            {"category": "L/P",    "active": True, "dcad_account": "10001", "score": 80},
            {"category": "L/P",    "active": True, "dcad_account": "10002", "score": 60},
            {"category": "NOTICE", "active": True, "dcad_account": "10003", "score": 90},
            {"category": "DEED",   "active": False, "score": 30},
        ]
        summary = output.summarize_run(records)
        assert summary["by_category"]["L/P"]      == 2
        assert summary["by_category"]["NOTICE"]   == 1
        assert summary["total"]                   == 4
        assert summary["active"]                  == 3
        # 3 of 4 have dcad_account → 0.75
        assert summary["address_resolution_rate"] == 0.75

    def test_score_bands(self):
        records = [
            {"score": 95, "active": True},  # EXTREME
            {"score": 80, "active": True},  # HIGH
            {"score": 65, "active": True},  # MEDIUM
            {"score": 45, "active": True},  # LOW
            {"score": 20, "active": True},  # NONE
        ]
        summary = output.summarize_run(records)
        assert summary["by_score_band"]["EXTREME"] == 1
        assert summary["by_score_band"]["HIGH"]    == 1
        assert summary["by_score_band"]["MEDIUM"]  == 1
        assert summary["by_score_band"]["LOW"]     == 1
        assert summary["by_score_band"]["NONE"]    == 1

    def test_extra_merged(self):
        summary = output.summarize_run([], extra={"custom_metric": 42})
        assert summary["custom_metric"] == 42
