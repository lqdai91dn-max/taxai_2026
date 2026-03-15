"""
Unit tests cho Phase A calculator tools.

Tất cả test dùng số thực từ luật — không mock, không roundtrip LLM.
Chạy: pytest tests/test_calculator_tools.py -v
"""

import pytest
from src.tools.calculator_tools import calculate_tax_hkd, calculate_tncn_progressive


# ═══════════════════════════════════════════════════════════════════════════════
# calculate_tax_hkd
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateTaxHKD:

    # ── Trường hợp miễn thuế TNCN ──────────────────────────────────────────────

    def test_exempt_tncn_300m_goods(self):
        """Doanh thu 300 triệu hàng hóa → miễn TNCN, chỉ có GTGT."""
        result = calculate_tax_hkd(300_000_000, "goods")
        assert result["exempt"] is True
        assert result["tncn_payable"] == 0
        assert result["gtgt_rate"] == 0.01
        assert result["gtgt_payable"] == 3_000_000   # 300M × 1%
        assert result["total_tax"] == 3_000_000

    def test_exempt_tncn_500m_boundary(self):
        """Đúng 500 triệu → vẫn miễn TNCN (≤500M)."""
        result = calculate_tax_hkd(500_000_000, "services")
        assert result["exempt"] is True
        assert result["tncn_payable"] == 0
        assert result["gtgt_payable"] == 25_000_000  # 500M × 5%

    # ── PP doanh thu: tỷ lệ % theo ngành trên toàn bộ DT ──────────────────────

    def test_tncn_goods_rate_1b(self):
        """Doanh thu 1 tỷ hàng hóa → TNCN 0.5% trên toàn bộ doanh thu."""
        result = calculate_tax_hkd(1_000_000_000, "goods")
        assert result["exempt"] is False
        assert result["tncn_rate"] == 0.005
        assert result["tncn_base"] == 1_000_000_000  # toàn bộ DT
        assert result["tncn_payable"] == 5_000_000   # 1B × 0.5%
        assert result["gtgt_payable"] == 10_000_000  # 1B × 1%
        assert result["total_tax"] == 15_000_000

    def test_tncn_services_rate_2b(self):
        """Doanh thu 2 tỷ dịch vụ → TNCN 2% trên toàn bộ DT."""
        result = calculate_tax_hkd(2_000_000_000, "services")
        assert result["tncn_rate"] == 0.02
        # TNCN = 2B × 2% = 40M
        assert result["tncn_payable"] == 40_000_000
        # GTGT = 2B × 5% = 100M
        assert result["gtgt_payable"] == 100_000_000
        assert result["total_tax"] == 140_000_000

    def test_tncn_services_1b_annotation(self):
        """Xác nhận annotation Q3: DT 1B dịch vụ → 70M tổng thuế."""
        result = calculate_tax_hkd(1_000_000_000, "services")
        assert result["tncn_rate"] == 0.02
        assert result["gtgt_payable"] == 50_000_000   # 1B × 5%
        assert result["tncn_payable"] == 20_000_000   # 1B × 2%
        assert result["total_tax"] == 70_000_000      # khớp annotation Q3

    def test_tncn_manufacturing_5b(self):
        """Doanh thu 5 tỷ sản xuất → TNCN 1.5% trên toàn bộ DT."""
        result = calculate_tax_hkd(5_000_000_000, "manufacturing")
        assert result["tncn_rate"] == 0.015
        # TNCN = 5B × 1.5% = 75M
        assert result["tncn_payable"] == 75_000_000
        # GTGT = 5B × 3% = 150M
        assert result["gtgt_payable"] == 150_000_000
        assert result["total_tax"] == 225_000_000
        # Warning về >3B (bắt buộc PP lợi nhuận)
        assert len(result["warnings"]) >= 1

    def test_tncn_other_100b(self):
        """Doanh thu 100 tỷ other → TNCN 1%."""
        result = calculate_tax_hkd(100_000_000_000, "other")
        assert result["tncn_rate"] == 0.01
        # TNCN = 100B × 1% = 1B
        assert result["tncn_payable"] == 1_000_000_000
        # GTGT = 100B × 2% = 2B
        assert result["gtgt_payable"] == 2_000_000_000
        assert result["total_tax"] == 3_000_000_000

    # ── Real estate ──────────────────────────────────────────────────────────────

    def test_real_estate_600m(self):
        """Cho thuê BĐS 600 triệu → GTGT 5%, TNCN 5% (annotation Q7: 10% = 5%+5%)."""
        result = calculate_tax_hkd(600_000_000, "real_estate")
        assert result["gtgt_rate"] == 0.05
        assert result["gtgt_payable"] == 30_000_000   # 600M × 5%
        assert result["tncn_rate"] == 0.05
        assert result["tncn_payable"] == 30_000_000   # 600M × 5%
        assert result["total_tax"] == 60_000_000      # 10% tổng

    # ── Output structure ─────────────────────────────────────────────────────────

    def test_output_has_breakdown_and_citations(self):
        """Output phải có breakdown items với citation."""
        result = calculate_tax_hkd(2_000_000_000, "goods")
        assert len(result["breakdown"]) == 2
        for item in result["breakdown"]:
            assert "citation" in item
            assert item["citation"]["doc_id"] == "68_2026_NDCP"
            assert "formula" in item

    def test_output_has_summary_string(self):
        result = calculate_tax_hkd(1_000_000_000, "services")
        assert isinstance(result["summary"], str)
        assert len(result["summary"]) > 10

    # ── Validation ────────────────────────────────────────────────────────────────

    def test_invalid_category_raises(self):
        with pytest.raises(ValueError, match="business_category"):
            calculate_tax_hkd(1_000_000_000, "invalid_type")

    def test_negative_revenue_raises(self):
        with pytest.raises(ValueError):
            calculate_tax_hkd(-1, "goods")

    def test_zero_revenue(self):
        """Doanh thu 0 → không nộp gì."""
        result = calculate_tax_hkd(0, "goods")
        assert result["total_tax"] == 0
        assert result["exempt"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# calculate_tncn_progressive
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateTNCNProgressive:

    def test_zero_income(self):
        result = calculate_tncn_progressive(0)
        assert result["tax_payable"] == 0
        assert result["brackets"] == []

    def test_below_bracket1(self):
        """100 triệu/năm — trong bậc 1 (5%)."""
        result = calculate_tncn_progressive(100_000_000)
        assert result["tax_payable"] == 5_000_000      # 100M × 5%
        assert len(result["brackets"]) == 1
        assert result["brackets"][0]["rate"] == 0.05

    def test_exactly_bracket1_ceiling(self):
        """120 triệu — đúng trần bậc 1."""
        result = calculate_tncn_progressive(120_000_000)
        assert result["tax_payable"] == 6_000_000      # 120M × 5%
        assert len(result["brackets"]) == 1

    def test_spans_two_brackets(self):
        """200 triệu — bậc 1 (120M) + bậc 2 (80M)."""
        result = calculate_tncn_progressive(200_000_000)
        # Bậc 1: 120M × 5% = 6M
        # Bậc 2: 80M × 10% = 8M
        # Tổng: 14M
        assert result["tax_payable"] == 14_000_000
        assert len(result["brackets"]) == 2

    def test_spans_all_5_brackets(self):
        """2 tỷ/năm — đủ 5 bậc."""
        result = calculate_tncn_progressive(2_000_000_000)
        assert len(result["brackets"]) == 5
        # Tính thủ công:
        # Bậc 1: 120M × 5%  = 6,000,000
        # Bậc 2: 240M × 10% = 24,000,000
        # Bậc 3: 360M × 20% = 72,000,000
        # Bậc 4: 480M × 30% = 144,000,000
        # Bậc 5: 800M × 35% = 280,000,000
        # Tổng:               526,000,000
        assert result["tax_payable"] == 526_000_000

    def test_effective_rate_calculation(self):
        """Effective rate phải = tax / income."""
        result = calculate_tncn_progressive(200_000_000)
        expected_rate = 14_000_000 / 200_000_000
        assert abs(result["effective_rate"] - expected_rate) < 1e-6

    def test_bracket_3_only_partial(self):
        """500 triệu — bậc 3 chỉ lấy 1 phần."""
        result = calculate_tncn_progressive(500_000_000)
        # Bậc 1: 120M × 5%  = 6M
        # Bậc 2: 240M × 10% = 24M
        # Bậc 3: 140M × 20% = 28M
        # Tổng: 58M
        assert result["tax_payable"] == 58_000_000
        assert len(result["brackets"]) == 3

    def test_citation_is_law_109(self):
        """Citation phải trỏ về Luật 109/2025/QH15."""
        result = calculate_tncn_progressive(200_000_000)
        assert result["citation"]["doc_id"] == "109_2025_QH15"
        assert "109/2025/QH15" in result["citation"]["doc_number"]

    def test_output_has_summary(self):
        result = calculate_tncn_progressive(300_000_000)
        assert isinstance(result["summary"], str)
        assert "triệu" in result["summary"]

    def test_negative_income_raises(self):
        with pytest.raises(ValueError):
            calculate_tncn_progressive(-1)

    def test_bracket_formula_strings_present(self):
        """Mỗi bracket phải có formula string."""
        result = calculate_tncn_progressive(500_000_000)
        for b in result["brackets"]:
            assert "formula" in b
            assert "×" in b["formula"]
