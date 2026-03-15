"""
Tests cho 3 tools mới: calculate_deduction, calculate_tax_hkd_profit, evaluate_tax_obligation.

Chạy: pytest tests/test_new_tools.py -v
"""

from __future__ import annotations

import pytest
from src.tools.calculator_tools import calculate_deduction, calculate_tax_hkd_profit
from src.tools.rule_engine import evaluate_tax_obligation


# ═══════════════════════════════════════════════════════════════════════════════
# calculate_deduction
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateDeduction:

    def test_no_dependents_full_year(self):
        r = calculate_deduction(dependents=0, months=12)
        # 15.5M × 12 = 186M
        assert r["total_deduction_annual"] == 186_000_000
        assert r["total_deduction_monthly"] == 15_500_000
        assert r["dependents"] == 0

    def test_two_dependents_full_year(self):
        r = calculate_deduction(dependents=2, months=12)
        # (15.5M + 2 × 6.2M) × 12 = 27.9M × 12 = 334.8M
        assert r["total_deduction_monthly"] == 27_900_000
        assert r["total_deduction_annual"] == 334_800_000

    def test_one_dependent_six_months(self):
        r = calculate_deduction(dependents=1, months=6)
        # (15.5 + 6.2) × 6 = 21.7M × 6 = 130.2M
        assert r["total_deduction_annual"] == 130_200_000
        assert r["months"] == 6

    def test_citation_is_109(self):
        r = calculate_deduction()
        assert r["citation"]["doc_id"] == "109_2025_QH15"

    def test_warning_about_effective_date(self):
        r = calculate_deduction()
        assert "01/07/2026" in r["warning"]

    def test_increase_vs_old_law(self):
        """Mức mới > mức cũ."""
        r = calculate_deduction(dependents=0, months=12)
        assert r["increase_vs_old_law"] > 0
        # (15.5 - 11) × 12 = 4.5M × 12 = 54M
        assert r["increase_vs_old_law"] == 54_000_000

    def test_negative_dependents_raises(self):
        with pytest.raises(ValueError, match="âm"):
            calculate_deduction(dependents=-1)

    def test_invalid_months_raises(self):
        with pytest.raises(ValueError, match="months"):
            calculate_deduction(months=13)

    def test_zero_months_raises(self):
        with pytest.raises(ValueError):
            calculate_deduction(months=0)

    def test_summary_contains_total(self):
        r = calculate_deduction(dependents=1, months=12)
        assert "triệu" in r["summary"]


# ═══════════════════════════════════════════════════════════════════════════════
# calculate_tax_hkd_profit
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateTaxHKDProfit:

    def test_basic_services_3b(self):
        """HKD dịch vụ DT 5 tỷ, CP 3 tỷ → bắt buộc PP lợi nhuận (17%)."""
        r = calculate_tax_hkd_profit(
            annual_revenue=5_000_000_000,
            annual_expenses=3_000_000_000,
            business_category="services",
        )
        # GTGT = 5B × 5% = 250M
        assert r["gtgt_payable"] == 250_000_000
        # TNCN = (5B - 3B) × 17% = 2B × 17% = 340M
        assert r["tncn_payable"] == 340_000_000
        assert r["total_tax"] == 590_000_000
        assert r["taxable_income"] == 2_000_000_000
        assert r["tncn_rate"] == 0.17
        assert r["method"] == "profit"

    def test_goods_optional_range(self):
        """HKD hàng hóa DT 1 tỷ, tự chọn PP lợi nhuận (15%)."""
        r = calculate_tax_hkd_profit(
            annual_revenue=1_000_000_000,
            annual_expenses=400_000_000,
            business_category="goods",
        )
        # GTGT = 1B × 1% = 10M
        assert r["gtgt_payable"] == 10_000_000
        # TNCN = (1B - 400M) × 15% = 600M × 15% = 90M
        assert r["tncn_payable"] == 90_000_000
        assert r["tncn_rate"] == 0.15

    def test_very_high_revenue_20pct(self):
        """DT > 50B → thuế suất TNCN 20%."""
        r = calculate_tax_hkd_profit(
            annual_revenue=60_000_000_000,
            annual_expenses=40_000_000_000,
            business_category="manufacturing",
        )
        assert r["tncn_rate"] == 0.20
        # TNCN = (60B - 40B) × 20% = 20B × 20% = 4B
        assert r["tncn_payable"] == 4_000_000_000

    def test_zero_profit_no_tncn(self):
        """Chi phí rất cao → lợi nhuận 0 → không có thuế TNCN."""
        r = calculate_tax_hkd_profit(
            annual_revenue=1_000_000_000,
            annual_expenses=999_999_999,
            business_category="services",
        )
        assert r["tncn_payable"] == 0
        assert r["taxable_income"] == 1

    def test_exempt_threshold_raises(self):
        """DT ≤ 500M không được dùng PP lợi nhuận."""
        with pytest.raises(ValueError, match="500"):
            calculate_tax_hkd_profit(
                annual_revenue=400_000_000,
                annual_expenses=100_000_000,
                business_category="goods",
            )

    def test_expenses_gte_revenue_raises(self):
        with pytest.raises(ValueError, match="Chi phí"):
            calculate_tax_hkd_profit(
                annual_revenue=1_000_000_000,
                annual_expenses=1_000_000_000,
                business_category="goods",
            )

    def test_negative_expenses_raises(self):
        with pytest.raises(ValueError, match="âm"):
            calculate_tax_hkd_profit(
                annual_revenue=1_000_000_000,
                annual_expenses=-1,
                business_category="goods",
            )

    def test_profit_margin_correct(self):
        r = calculate_tax_hkd_profit(
            annual_revenue=2_000_000_000,
            annual_expenses=1_500_000_000,
            business_category="services",
        )
        # margin = 500M / 2B = 0.25
        assert r["profit_margin"] == 0.25

    def test_warning_mandatory_for_high_revenue(self):
        """DT > 3B phải có warning về bắt buộc PP lợi nhuận."""
        r = calculate_tax_hkd_profit(
            annual_revenue=4_000_000_000,
            annual_expenses=1_000_000_000,
            business_category="services",
        )
        assert any("bắt buộc" in w for w in r["warnings"])

    def test_warning_optional_for_mid_revenue(self):
        """DT 500M-3B phải có warning về ổn định 2 năm."""
        r = calculate_tax_hkd_profit(
            annual_revenue=1_000_000_000,
            annual_expenses=300_000_000,
            business_category="goods",
        )
        assert any("2 năm" in w for w in r["warnings"])

    def test_citation_is_68(self):
        r = calculate_tax_hkd_profit(
            annual_revenue=2_000_000_000,
            annual_expenses=500_000_000,
            business_category="services",
        )
        assert r["breakdown"][0]["citation"]["doc_id"] == "68_2026_NDCP"

    def test_gtgt_same_as_revenue_method(self):
        """GTGT PP lợi nhuận = GTGT PP doanh thu (vẫn tính trên DT)."""
        from src.tools.calculator_tools import calculate_tax_hkd
        rev = calculate_tax_hkd(1_500_000_000, "services")
        prof = calculate_tax_hkd_profit(1_500_000_000, 500_000_000, "services")
        assert rev["gtgt_payable"] == prof["gtgt_payable"]


# ═══════════════════════════════════════════════════════════════════════════════
# evaluate_tax_obligation
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluateTaxObligation:

    def test_exempt_below_500m(self):
        r = evaluate_tax_obligation(400_000_000)
        assert r["is_exempt"] is True
        assert r["tax_method"] == "none"
        assert r["filing_frequency"] == "exempt"
        assert "MIỄN" in r["summary"]

    def test_exactly_500m_is_exempt(self):
        r = evaluate_tax_obligation(500_000_000)
        assert r["is_exempt"] is True

    def test_above_500m_not_exempt(self):
        r = evaluate_tax_obligation(600_000_000)
        assert r["is_exempt"] is False
        assert r["tax_method"] == "either"

    def test_above_3b_mandatory_profit(self):
        r = evaluate_tax_obligation(4_000_000_000)
        assert r["tax_method"] == "profit"
        assert r["is_exempt"] is False

    def test_filing_quarterly_under_50b(self):
        r = evaluate_tax_obligation(2_000_000_000)
        assert r["filing_frequency"] == "quarterly"

    def test_filing_monthly_over_50b(self):
        r = evaluate_tax_obligation(60_000_000_000)
        assert r["filing_frequency"] == "monthly"

    def test_einvoice_not_required_under_500m(self):
        r = evaluate_tax_obligation(300_000_000)
        assert r["einvoice_required"] is False

    def test_einvoice_optional_500m_to_1b(self):
        r = evaluate_tax_obligation(700_000_000)
        assert r["einvoice_required"] == "optional"

    def test_einvoice_mandatory_over_1b(self):
        r = evaluate_tax_obligation(2_000_000_000)
        assert r["einvoice_required"] is True

    def test_tmdt_withholding_true(self):
        r = evaluate_tax_obligation(
            1_000_000_000,
            has_online_sales=True,
            platform_has_payment=True,
        )
        assert r["tmdt_withholding"] is True

    def test_tmdt_no_payment_not_withheld(self):
        r = evaluate_tax_obligation(
            1_000_000_000,
            has_online_sales=True,
            platform_has_payment=False,
        )
        assert r["tmdt_withholding"] is False

    def test_obligations_list_non_empty(self):
        r = evaluate_tax_obligation(2_000_000_000)
        assert len(r["obligations"]) >= 3  # method + filing + einvoice

    def test_notes_for_profit_method_stability(self):
        """DT 500M-3B → ghi chú ổn định phương pháp."""
        r = evaluate_tax_obligation(1_000_000_000)
        assert any("2 năm" in n for n in r["notes"])

    def test_negative_revenue_raises(self):
        with pytest.raises(ValueError):
            evaluate_tax_obligation(-1)

    def test_citation_in_obligations(self):
        r = evaluate_tax_obligation(1_000_000_000)
        for ob in r["obligations"]:
            assert "doc_id" in ob["citation"]
            assert ob["citation"]["doc_id"] == "68_2026_NDCP"

    def test_tmdt_note_about_higher_rate(self):
        """Ghi chú về tỷ lệ cao nhất khi không rõ hàng hóa/dịch vụ."""
        r = evaluate_tax_obligation(1_000_000_000, has_online_sales=True)
        assert any("5%" in n for n in r["notes"])
