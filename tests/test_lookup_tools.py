"""
Tests cho Phase B lookup tools.

Dùng Neo4j thực — không mock.
Chạy: pytest tests/test_lookup_tools.py -v
"""

import pytest
from src.tools.doc_validity_tool import check_doc_validity
from src.tools.lookup_tools import (
    resolve_legal_reference,
    get_article_with_amendments,
)


# ═══════════════════════════════════════════════════════════════════════════════
# check_doc_validity
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckDocValidity:

    def test_active_doc(self):
        """68_2026_NDCP hiện đang active."""
        r = check_doc_validity("68_2026_NDCP")
        assert r["found"] is True
        assert r["status"] == "valid"
        assert r["doc_number"] == "68/2026/NĐ-CP"

    def test_active_109(self):
        """109_2025_QH15 status active — luật TNCN chính từ 01/07/2026."""
        r = check_doc_validity("109_2025_QH15")
        assert r["found"] is True
        assert r["status"] == "active"
        assert r["is_currently_valid"] is True

    def test_doc_becomes_valid_after_date(self):
        """109 sẽ valid ở ngày 2026-07-01."""
        r = check_doc_validity("109_2025_QH15", query_date="2026-07-01")
        assert r["status"] == "valid"

    def test_unknown_doc(self):
        """Doc không tồn tại trong hệ thống."""
        r = check_doc_validity("999_FAKE_DOC")
        assert r["found"] is False
        assert r["status"] == "unknown"

    def test_amended_by_field(self):
        """109_2025_QH15 bị sửa đổi bởi 110_2025_UBTVQH15."""
        r = check_doc_validity("109_2025_QH15")
        assert "amended_by" in r
        amenders = [a["doc_id"] for a in r["amended_by"]]
        assert "110_2025_UBTVQH15" in amenders

    def test_invalid_date_format(self):
        r = check_doc_validity("68_2026_NDCP", query_date="not-a-date")
        assert "error" in r

    def test_output_has_required_fields(self):
        r = check_doc_validity("68_2026_NDCP")
        for field in ["doc_id", "found", "status", "valid_from", "amended_by", "message"]:
            assert field in r


# ═══════════════════════════════════════════════════════════════════════════════
# resolve_legal_reference
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveLegalReference:

    def test_resolve_decree_with_article(self):
        """'Điều 4 Nghị định 68/2026/NĐ-CP' → doc_id + article_id."""
        r = resolve_legal_reference("Điều 4 Nghị định 68/2026/NĐ-CP")
        assert r["resolved"] is True
        assert r["doc_id"] == "68_2026_NDCP"
        assert r["article_number"] == "4"
        assert r["article_id"] is not None
        assert "dieu_4" in r["article_id"]

    def test_resolve_law_with_article(self):
        """'Điều 1 Luật 110/2025/UBTVQH15' → 110_2025_UBTVQH15."""
        r = resolve_legal_reference("Điều 1 Nghị quyết 110/2025/UBTVQH15")
        assert r["resolved"] is True
        assert r["doc_id"] == "110_2025_UBTVQH15"
        assert r["article_id"] is not None

    def test_resolve_doc_without_article(self):
        """Chỉ số văn bản, không có Điều → trả doc_id, article_id=None."""
        r = resolve_legal_reference("Nghị định 68/2026/NĐ-CP")
        assert r["resolved"] is True
        assert r["doc_id"] == "68_2026_NDCP"
        assert r["article_number"] is None
        assert r["article_id"] is None

    def test_unknown_doc_number(self):
        """Số văn bản không có trong hệ thống."""
        r = resolve_legal_reference("Điều 5 Thông tư 99/2000/TT-BTC")
        assert r["resolved"] is False
        assert "candidate_numbers" in r

    def test_no_doc_number_in_text(self):
        """Không có số văn bản nào trong text."""
        r = resolve_legal_reference("thuế hộ kinh doanh")
        assert r["resolved"] is False
        assert "Không tìm thấy số hiệu" in r["message"]

    def test_output_message_present(self):
        r = resolve_legal_reference("Điều 4 Nghị định 68/2026/NĐ-CP")
        assert isinstance(r["message"], str)
        assert len(r["message"]) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# get_article_with_amendments
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetArticleWithAmendments:

    def test_article_no_amendments(self):
        """Article của 68_2026_NDCP — không có gì sửa đổi 68."""
        r = get_article_with_amendments("doc_68_2026_NDCP_chuong_II_dieu_4")
        assert r["found"] is True
        assert r["has_amendments"] is False
        assert r["is_fully_resolved"] is True
        assert r["amendment_warnings"] == []

    def test_article_with_amendments(self):
        """Article của 109_2025_QH15 bị sửa đổi bởi 110_2025_UBTVQH15."""
        r = get_article_with_amendments("doc_109_2025_QH15_chuong_I_dieu_1")
        assert r["found"] is True
        assert r["has_amendments"] is True
        assert r["is_fully_resolved"] is False
        assert len(r["amendment_warnings"]) >= 1
        # Warning phải chứa thông tin về amender
        warning = r["amendment_warnings"][0]
        assert "amender_doc_id" in warning
        assert "warning" in warning
        assert isinstance(warning["warning"], str)

    def test_article_not_found(self):
        r = get_article_with_amendments("doc_fake_doc_dieu_99")
        assert r["found"] is False
        assert r["amendment_warnings"] == []

    def test_output_inherits_article_fields(self):
        """Output phải có cả fields của get_article lẫn amendment fields."""
        r = get_article_with_amendments("doc_68_2026_NDCP_chuong_II_dieu_4")
        assert "full_text" in r
        assert "citation" in r
        assert "amendment_warnings" in r
        assert "has_amendments" in r
        assert "is_fully_resolved" in r
