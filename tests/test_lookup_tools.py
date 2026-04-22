"""
Tests cho doc_validity_tool.check_doc_validity.

Không dùng mock — đọc trực tiếp từ data/law_validity.json.
Chạy: pytest tests/test_lookup_tools.py -v
"""

import pytest
from src.tools.doc_validity_tool import check_doc_validity


# ═══════════════════════════════════════════════════════════════════════════════
# check_doc_validity — match actual API
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckDocValidity:

    def test_active_doc(self):
        """68_2026_NDCP hiện đang active."""
        r = check_doc_validity("68_2026_NDCP")
        assert r["found"] is True
        assert r["status"] == "active"
        assert r["is_currently_valid"] is True

    def test_active_109(self):
        """109_2025_QH15 status active — luật TNCN chính từ 01/07/2026."""
        r = check_doc_validity("109_2025_QH15")
        assert r["found"] is True
        assert r["status"] == "active"
        assert r["is_currently_valid"] is True

    def test_superseded_doc(self):
        """111_2013_TTBTC đã bị thay thế — is_currently_valid phải False."""
        r = check_doc_validity("111_2013_TTBTC")
        assert r["found"] is True
        assert r["status"] == "superseded"
        assert r["is_currently_valid"] is False
        assert r["superseded_by"] == "109_2025_QH15"

    def test_superseded_92(self):
        """92_2015_TTBTC đã bị thay thế."""
        r = check_doc_validity("92_2015_TTBTC")
        assert r["found"] is True
        assert r["status"] == "superseded"
        assert r["is_currently_valid"] is False

    def test_unknown_doc(self):
        """Doc không tồn tại trong hệ thống."""
        r = check_doc_validity("999_FAKE_DOC")
        assert r["found"] is False
        assert r["status"] == "unknown"

    def test_not_in_database(self):
        """TT40_2021_TTBTC — có trong not_in_database."""
        r = check_doc_validity("TT40_2021_TTBTC")
        assert r["found"] is False
        assert r["status"] == "not_in_database"

    def test_output_has_required_fields(self):
        r = check_doc_validity("68_2026_NDCP")
        required = ["doc_id", "found", "status", "is_currently_valid",
                    "effective_from", "effective_to", "name", "note", "superseded_by"]
        for field in required:
            assert field in r, f"Missing field: {field}"

    def test_doc_id_in_output(self):
        """doc_id phải khớp với input."""
        r = check_doc_validity("110_2025_UBTVQH15")
        assert r["doc_id"] == "110_2025_UBTVQH15"

    def test_name_present(self):
        """name phải là chuỗi không rỗng."""
        r = check_doc_validity("68_2026_NDCP")
        assert isinstance(r["name"], str)
        assert len(r["name"]) > 0

    def test_effective_from_format(self):
        """effective_from phải là ISO date string."""
        r = check_doc_validity("68_2026_NDCP")
        assert r["effective_from"] == "2026-04-02"

    def test_note_present(self):
        """note phải có nội dung (không phải None/rỗng) cho doc tồn tại."""
        r = check_doc_validity("109_2025_QH15")
        assert r["note"] and len(r["note"]) > 0
