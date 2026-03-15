"""
Tests cho eval_runner — kiểm tra logic 4 tier không cần API.

Chạy: pytest tests/test_eval_framework.py -v
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tests.eval_runner import (
    eval_tier1_deterministic,
    eval_tier2_citation,
    eval_tier3_tool_selection,
    eval_tier4_key_facts,
    generate_report,
    EvalResult,
    TierResult,
    _extract_numbers_from_text,
    _extract_numbers_from_tool_calls,
    _number_in_answer,
    _fact_in_answer,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 1 — Deterministic
# ═══════════════════════════════════════════════════════════════════════════════

class TestTier1:

    def test_na_when_no_calculation_needed(self):
        result = eval_tier1_deterministic({}, needs_calculation=False, q_data={})
        assert result.score is None
        assert "N/A" in result.reason

    def test_fail_when_no_calc_tool_called(self):
        agent_result = {
            "answer": "Thuế là 100 triệu",
            "tool_calls": [{"tool": "search_legal_docs", "args": {}, "result": {}}],
        }
        result = eval_tier1_deterministic(agent_result, needs_calculation=True, q_data={})
        assert result.score == 0.0
        assert "FAIL" in result.reason

    def test_pass_when_calc_tool_called_and_number_matches(self):
        agent_result = {
            "answer": "Thuế phải nộp là 325,000,000 đồng",
            "tool_calls": [{
                "tool": "calculate_tax_hkd",
                "args": {"annual_revenue": 2_000_000_000, "business_category": "services"},
                "result": {"total_tax": 325_000_000, "gtgt_payable": 100_000_000},
            }],
        }
        result = eval_tier1_deterministic(agent_result, needs_calculation=True, q_data={})
        assert result.score == 1.0
        assert "PASS" in result.reason

    def test_pass_with_ground_truth_expected_value(self):
        """Khi có expected_value → so sánh với ground truth thay vì tool output."""
        agent_result = {
            "answer": "GTGT phải nộp 50 triệu, TNCN 75 triệu, tổng 125 triệu đồng.",
            "tool_calls": [{"tool": "calculate_tax_hkd", "args": {}, "result": {}}],
        }
        q_data = {
            "expected_value": {
                "gtgt_payable": 50_000_000,
                "tncn_payable": 75_000_000,
                "total_tax":    125_000_000,
            }
        }
        result = eval_tier1_deterministic(agent_result, needs_calculation=True, q_data=q_data)
        assert result.score == 1.0
        assert "ground-truth" in result.reason

    def test_fail_ground_truth_numbers_missing(self):
        """Answer không chứa số đúng → fail dù có gọi tool."""
        agent_result = {
            "answer": "Bạn sẽ phải nộp một khoản thuế nhất định theo quy định.",
            "tool_calls": [{"tool": "calculate_tax_hkd", "args": {}, "result": {}}],
        }
        q_data = {
            "expected_value": {
                "gtgt_payable": 50_000_000,
                "tncn_payable": 75_000_000,
                "total_tax":    125_000_000,
            }
        }
        result = eval_tier1_deterministic(agent_result, needs_calculation=True, q_data=q_data)
        assert result.score <= 0.2

    def test_partial_when_calc_tool_called_but_number_approximate(self):
        agent_result = {
            "answer": "Thuế khoảng 325 triệu đồng",
            "tool_calls": [{
                "tool": "calculate_tax_hkd",
                "args": {},
                "result": {"total_tax": 325_000_000},
            }],
        }
        result = eval_tier1_deterministic(agent_result, needs_calculation=True, q_data={})
        # 325 triệu → số 325 xuất hiện → score = 1.0 hoặc 0.5
        assert result.score is not None
        assert result.score > 0.0

    def test_new_tools_accepted_as_calc_tools(self):
        """calculate_deduction và evaluate_tax_obligation cũng được chấp nhận."""
        for tool_name in ("calculate_deduction", "calculate_tax_hkd_profit",
                          "evaluate_tax_obligation"):
            agent_result = {
                "answer": "Kết quả tính toán: 50 triệu đồng.",
                "tool_calls": [{"tool": tool_name, "args": {}, "result": {}}],
            }
            result = eval_tier1_deterministic(agent_result, needs_calculation=True, q_data={})
            assert result.score != 0.0, f"{tool_name} nên được chấp nhận là calc tool"


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 2 — Citation
# ═══════════════════════════════════════════════════════════════════════════════

class TestTier2:

    def test_fail_empty_answer(self):
        result = eval_tier2_citation({"answer": ""}, question="test")
        assert result.score == 0.0

    def test_pass_with_dieu_reference(self):
        result = eval_tier2_citation(
            {"answer": "Theo Điều 5 Nghị định 68/2026/NĐ-CP, thuế GTGT là 5%.", "sources": []},
            question="test",
        )
        assert result.score >= 0.8

    def test_pass_with_doc_number(self):
        result = eval_tier2_citation(
            {"answer": "Căn cứ 68/2026/NĐ-CP, hộ kinh doanh nộp thuế theo tỷ lệ %.", "sources": []},
            question="test",
        )
        assert result.score >= 0.5

    def test_partial_with_sources_but_no_explicit_ref(self):
        result = eval_tier2_citation(
            {
                "answer": "Hộ kinh doanh phải nộp thuế theo quy định hiện hành.",
                "sources": [{"doc_id": "68_2026_NDCP", "type": "search"}],
            },
            question="test",
        )
        # Có sources nhưng không trích dẫn rõ → partial
        assert 0.0 < result.score < 1.0

    def test_pass_with_qh15_reference(self):
        result = eval_tier2_citation(
            {"answer": "Theo Luật 109/2025/QH15, biểu thuế lũy tiến 5 bậc.", "sources": []},
            question="test",
        )
        assert result.score >= 0.8

    def test_pass_nghidinh_abbreviation(self):
        result = eval_tier2_citation(
            {"answer": "NĐ-CP quy định rõ về thuế GTGT 5%.", "sources": []},
            question="test",
        )
        assert result.score >= 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 3 — Tool Selection
# ═══════════════════════════════════════════════════════════════════════════════

class TestTier3:

    def test_fail_no_tools_no_answer(self):
        result = eval_tier3_tool_selection(
            {"tool_calls": [], "answer": ""},
            topic="Thuế hộ kinh doanh",
            needs_calculation=False,
        )
        assert result.score == 0.0

    def test_partial_no_tools_but_has_answer(self):
        result = eval_tier3_tool_selection(
            {"tool_calls": [], "answer": "Hộ kinh doanh phải nộp thuế GTGT và TNCN theo quy định tại Nghị định 68/2026/NĐ-CP của Chính phủ."},
            topic="Thuế hộ kinh doanh",
            needs_calculation=False,
        )
        assert 0.0 < result.score <= 0.5

    def test_fail_calc_needed_but_no_calc_tool(self):
        result = eval_tier3_tool_selection(
            {"tool_calls": [{"tool": "search_legal_docs", "args": {}, "result": {}}], "answer": "test"},
            topic="Thuế hộ kinh doanh",
            needs_calculation=True,
        )
        assert result.score == 0.0

    def test_pass_calc_tool_used_for_calc_question(self):
        result = eval_tier3_tool_selection(
            {"tool_calls": [{"tool": "calculate_tax_hkd", "args": {}, "result": {}}], "answer": "test"},
            topic="Thuế hộ kinh doanh",
            needs_calculation=True,
        )
        assert result.score == 1.0

    def test_pass_check_validity_for_hieu_luc_topic(self):
        result = eval_tier3_tool_selection(
            {"tool_calls": [{"tool": "check_doc_validity", "args": {}, "result": {}}], "answer": "test"},
            topic="Hiệu lực pháp luật",
            needs_calculation=False,
        )
        assert result.score == 1.0

    def test_partial_wrong_but_valid_tool_for_topic(self):
        result = eval_tier3_tool_selection(
            {"tool_calls": [{"tool": "get_impl_chain", "args": {}, "result": {}}], "answer": "test"},
            topic="Thuế hộ kinh doanh",
            needs_calculation=False,
        )
        # get_impl_chain không trong expected list → partial
        assert 0.0 < result.score <= 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 4 — Key Facts
# ═══════════════════════════════════════════════════════════════════════════════

class TestTier4:

    def test_na_when_no_key_facts(self):
        result = eval_tier4_key_facts({"answer": "test"}, q_data={})
        assert result.score is None
        assert "N/A" in result.reason

    def test_na_empty_key_facts_list(self):
        result = eval_tier4_key_facts({"answer": "test"}, q_data={"key_facts": []})
        assert result.score is None

    def test_pass_all_facts_present(self):
        result = eval_tier4_key_facts(
            {"answer": "Thuế GTGT 5%, TNCN 15%, tổng 125 triệu đồng."},
            q_data={"key_facts": ["5%", "15%", "125 triệu"]},
        )
        assert result.score == 1.0

    def test_fail_no_facts_present(self):
        result = eval_tier4_key_facts(
            {"answer": "Bạn phải nộp một khoản thuế nhất định."},
            q_data={"key_facts": ["5%", "125 triệu"]},
        )
        assert result.score == 0.0

    def test_partial_half_facts_present(self):
        # "5%" và "15%" khớp, "125 triệu" và "50 triệu" không khớp → 2/4 = 0.5 → PARTIAL
        result = eval_tier4_key_facts(
            {"answer": "Thuế GTGT là 5%, TNCN là 15%, phải nộp đúng hạn."},
            q_data={"key_facts": ["5%", "15%", "125 triệu", "50 triệu"]},
        )
        assert 0.0 < result.score < 1.0

    def test_mien_keyword_match(self):
        result = eval_tier4_key_facts(
            {"answer": "Doanh thu 300 triệu → được miễn thuế GTGT và TNCN."},
            q_data={"key_facts": ["miễn", "500 triệu"]},
        )
        # "miễn" → khớp, "500 triệu" → không khớp → partial
        assert result.score > 0.0

    def test_fail_empty_answer(self):
        result = eval_tier4_key_facts(
            {"answer": ""},
            q_data={"key_facts": ["5%", "miễn"]},
        )
        assert result.score == 0.0

    def test_so_trien_viet_match(self):
        """125 triệu → "125,000,000" hoặc "125 triệu" đều khớp."""
        assert _fact_in_answer("125 triệu", "Tổng thuế: 125,000,000 đồng")
        assert _fact_in_answer("125 triệu", "Tổng thuế: 125 triệu đồng")

    def test_percent_variants(self):
        assert _fact_in_answer("5%", "Tỷ lệ 5% áp dụng cho dịch vụ")
        assert _fact_in_answer("5%", "Tỷ lệ 5 % áp dụng cho dịch vụ")

    def test_decimal_percent_variants(self):
        assert _fact_in_answer("0,5%", "tỷ lệ 0.5% trên doanh thu")
        assert _fact_in_answer("15,5 triệu", "giảm trừ 15.5 triệu/tháng")


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestHelpers:

    def test_extract_numbers_from_text(self):
        nums = _extract_numbers_from_text("Thuế là 325,000,000 đồng, tức 325 triệu")
        assert 325000000 in nums or 325 in nums

    def test_extract_numbers_ignores_small(self):
        nums = _extract_numbers_from_text("Điều 5 khoản 2 mức 1000 đồng")
        assert 5 not in nums
        assert 2 not in nums

    def test_number_in_answer_exact_match(self):
        assert _number_in_answer(325_000_000, [325_000_000])

    def test_number_in_answer_million_form(self):
        # 325_000_000 → 325 triệu
        assert _number_in_answer(325_000_000, [325])

    def test_number_in_answer_tolerance(self):
        # Làm tròn 1%
        assert _number_in_answer(100_000_000, [100_100_000])

    def test_extract_numbers_from_calc_tool(self):
        calls = [{
            "tool": "calculate_tax_hkd",
            "args": {},
            "result": {"total_tax": 325_000_000, "gtgt_payable": 100_000_000},
        }]
        nums = _extract_numbers_from_tool_calls(calls)
        assert 325_000_000 in nums
        assert 100_000_000 in nums


# ═══════════════════════════════════════════════════════════════════════════════
# Report generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestReport:

    def _make_result(self, score: float, topic: str = "Thuế hộ kinh doanh") -> EvalResult:
        t = TierResult(score=score, reason="test")
        return EvalResult(
            question_id="q1", question="test", topic=topic,
            difficulty="easy", user_type="household_business",
            needs_calculation=False, annotation_status="pending",
            answer="test answer", tool_calls=[], latency_ms=500, error=None,
            tier1=TierResult(score=None, reason="N/A"),
            tier2=t, tier3=t,
            tier4=TierResult(score=None, reason="N/A"),
        )

    def test_report_pass_rate(self):
        results = [self._make_result(1.0) for _ in range(8)] + \
                  [self._make_result(0.0) for _ in range(2)]
        report = generate_report(results)
        assert report["summary"]["total"] == 10
        # score 1.0 → passed, score 0.0 → not passed
        assert report["summary"]["passed"] == 8

    def test_report_by_topic(self):
        results = [
            self._make_result(1.0, "Thuế hộ kinh doanh"),
            self._make_result(0.5, "Thuế hộ kinh doanh"),
            self._make_result(1.0, "Kế toán HKD"),
        ]
        report = generate_report(results)
        assert "Thuế hộ kinh doanh" in report["by_topic"]
        assert report["by_topic"]["Thuế hộ kinh doanh"]["total"] == 2

    def test_report_empty_results(self):
        report = generate_report([])
        assert "error" in report

    def test_overall_score_calculation(self):
        r = self._make_result(0.8)
        assert r.overall_score == 0.8

    def test_passed_threshold(self):
        r_pass   = self._make_result(0.8)
        r_border = self._make_result(0.67)
        r_fail   = self._make_result(0.5)
        assert r_pass.passed is True
        assert r_border.passed is True
        assert r_fail.passed is False
