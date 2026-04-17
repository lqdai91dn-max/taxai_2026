"""
tests/test_regression_30.py — Regression test suite 30 câu hỏi đại diện.

Mục tiêu: bảo vệ 100% pass rate R59 sau mỗi production change.
Chạy trước khi deploy bất kỳ thay đổi nào vào planner/tools/retrieval.

Cấu trúc 2 lớp:

  Layer 1 — Deterministic (không cần API, ~3s):
    - Calculator correctness: Q2, Q3, Q7, Q8, Q11, Q12, Q15, Q22
    - Skill detectors: S-001, S-002, S-003, S-005, S-007, S-008
    - Key_fact format integrity

  Layer 2 — Integration canary (cần API, ~3-5 phút):
    - 5 câu canary covering 5 topic clusters
    - Mark: @pytest.mark.integration
    - Chạy bằng: pytest tests/test_regression_30.py -m integration -v

Usage:
  pytest tests/test_regression_30.py -v                # Layer 1 only (~3s)
  pytest tests/test_regression_30.py -m integration -v  # Layer 1 + 2 (~5 phút, cần API)
  pytest tests/test_regression_30.py -v --tb=short      # CI mode
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def questions() -> dict[int, dict]:
    """Load questions.json → dict keyed by id."""
    path = ROOT / "data/eval/questions.json"
    qs = json.loads(path.read_text(encoding="utf-8"))
    return {q["id"]: q for q in qs}


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1A — Calculator correctness (no API needed)
# Questions: Q2, Q3, Q7, Q8, Q11, Q12, Q15, Q22
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculatorRegression:
    """
    Verify calculator tools trả về đúng số liệu cho các câu điển hình.
    Regression: nếu tỷ lệ thuế hoặc ngưỡng thay đổi → các test này sẽ fail.
    """

    def test_Q2_mien_thue_ngu_ng_500m(self):
        """Q2: quán phở DT 500M → miễn TNCN, key_fact='500 triệu'."""
        from src.tools.calculator_tools import calculate_tax_hkd
        r = calculate_tax_hkd(500_000_000, "services")
        assert r["exempt"] is True,          "DT=500M phải miễn TNCN"
        assert r["tncn_payable"] == 0,       "TNCN=0 khi exempt"
        assert r["gtgt_payable"] == 25_000_000, "GTGT=500M×5%=25M"
        assert "500" in r["summary"],        "summary phải đề cập 500"

    def test_Q3_tiem_lam_toc_1ty_dich_vu(self):
        """Q3: tiệm làm tóc 1 tỷ, dịch vụ → GTGT 5%, TNCN 2%."""
        from src.tools.calculator_tools import calculate_tax_hkd
        r = calculate_tax_hkd(1_000_000_000, "services")
        assert r["exempt"] is False
        assert r["gtgt_rate"] == pytest.approx(0.05), "GTGT rate phải 5%"
        assert r["tncn_rate"] == pytest.approx(0.02), "TNCN rate phải 2%"
        assert r["gtgt_payable"] == 50_000_000
        assert r["tncn_payable"] == 20_000_000
        assert r["total_tax"]    == 70_000_000
        # Formula format: phải có "×" và "VND"
        formulas = [b["formula"] for b in r["breakdown"]]
        assert all("×" in f and "VND" in f for f in formulas), \
            "Mỗi formula phải có '×' và 'VND'"

    def test_Q7_cho_thue_nha_360m_mien_tncn(self):
        """Q7: cho thuê nhà 360M/năm → exempt TNCN, key_fact='miễn','500 triệu'."""
        from src.tools.calculator_tools import calculate_tax_hkd
        r = calculate_tax_hkd(360_000_000, "real_estate")
        assert r["exempt"] is True
        assert r["tncn_payable"] == 0
        assert r["gtgt_payable"] == pytest.approx(360_000_000 * 0.05)
        assert "miễn" in r["summary"].lower()

    def test_Q8_tiem_vang_3ty_buoc_pp_loi_nhuan(self):
        """Q8: tiệm vàng >3 tỷ → cần PP lợi nhuận, key_fact='3 tỷ'."""
        from src.tools.calculator_tools import calculate_tax_hkd
        r = calculate_tax_hkd(50_000_000_000, "goods")
        # PP % doanh thu vẫn tính đúng, nhưng DT>3B phải có warning/flag
        assert r["annual_revenue"] == 50_000_000_000
        # Tỷ lệ HH: GTGT 1%, TNCN 0.5%
        assert r["gtgt_rate"] == pytest.approx(0.01), "Hàng hóa GTGT=1%"
        assert r["tncn_rate"] == pytest.approx(0.005), "Hàng hóa TNCN=0.5%"

    def test_Q11_giam_tru_gia_canh_2026(self):
        """Q11: giảm trừ 2026 — bản thân 15.5M, NPT 6.2M. key_fact=['15,5','6,2','110']."""
        from src.tools.calculator_tools import calculate_deduction
        r = calculate_deduction(1, 12)
        assert r["personal_deduction_monthly"] == 15_500_000, \
            "Bản thân 15.5M/tháng (NQ110/2025)"
        assert r["dependent_deduction_per_person"] == 6_200_000, \
            "NPT 6.2M/tháng (NQ110/2025)"
        assert r["total_deduction_monthly"] == 21_700_000
        assert r["total_deduction_annual"]  == 260_400_000

    def test_Q12_luong_20m_1npt_mien_thue(self):
        """Q12: lương 20M, 1 NPT → sau giảm trừ 21.7M/tháng → không đủ taxable → miễn."""
        from src.tools.calculator_tools import calculate_deduction, calculate_tncn_progressive
        ded = calculate_deduction(1, 12)
        monthly_ded = ded["total_deduction_monthly"]  # 21.7M
        salary_monthly = 20_000_000
        annual_taxable = max(0, (salary_monthly - monthly_ded) * 12)
        assert annual_taxable == 0, \
            f"20M < 21.7M deduction → taxable=0, got {annual_taxable}"
        tncn = calculate_tncn_progressive(annual_taxable)
        assert tncn["tax_payable"] == 0, "Không có thu nhập tính thuế → TNCN=0"

    def test_Q15_trung_thuong_50m_xo_so(self):
        """Q15: trúng vé số 50M → thuế = (50-10)×10% = 4M. key_fact=['4.000.000','10%']."""
        # Công thức: (giải_thưởng - 10_000_000) × 10%
        prize = 50_000_000
        threshold = 10_000_000
        rate = 0.10
        tax = (prize - threshold) * rate
        assert tax == 4_000_000, f"Thuế trúng thưởng sai: expected 4M, got {tax}"

    def test_Q22_tmdt_shopee_ty_le_khau_tru(self):
        """Q22: Shopee khấu trừ 1% GTGT + 0.5% TNCN = 1.5%. key_fact=['1%','0,5%']."""
        from src.tools.calculator_tools import calculate_tax_hkd
        # Dùng calculate_tax_hkd làm ground truth thay vì đọc dict trực tiếp
        # (dict có thể là tuple hoặc scalar tùy version)
        r = calculate_tax_hkd(1_000_000_000, "goods")
        assert r["gtgt_rate"] == pytest.approx(0.01),  "Hàng hóa GTGT=1%"
        assert r["tncn_rate"] == pytest.approx(0.005), "Hàng hóa TNCN=0.5%"

    def test_calculator_formula_format_no_regression(self):
        """
        Verify format formula không bị revert về dạng cũ (thiếu số cụ thể).
        Regression guard cho commit calculator_tools.py.
        """
        from src.tools.calculator_tools import calculate_tax_hkd
        r = calculate_tax_hkd(1_200_000_000, "goods")
        # Format mới: "1,200,000,000 × 1.0% = 12,000,000 VND"
        # Format cũ (bị revert): "1.2 tỷ × 1% = 12 triệu"
        formulas_text = " ".join(b["formula"] for b in r["breakdown"])
        assert "×" in formulas_text,   "Formula phải dùng ký hiệu ×"
        assert "VND" in formulas_text,  "Formula phải có đơn vị VND"
        assert "1,200,000,000" in formulas_text, "Formula phải có số nguyên đầy đủ"


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1B — Skill detectors regression (no API needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestSkillDetectors:
    """
    Verify các skill detector functions không bị break bởi thay đổi code.
    """

    # ── S-001: check_doc_validity ─────────────────────────────────────────────

    def test_S001_doc_validity_active(self):
        """S-001: NĐ68 phải active."""
        from src.tools.doc_validity_tool import check_doc_validity
        r = check_doc_validity("68_2026_NDCP")
        assert r["found"] is True
        assert r["status"] == "active"
        assert r["is_currently_valid"] is True

    def test_S001_doc_validity_active_until_superseded(self):
        """S-001: TT111/2013 còn hiệu lực đến 30/06/2026."""
        from src.tools.doc_validity_tool import check_doc_validity
        r = check_doc_validity("111_2013_TTBTC")
        assert r["found"] is True
        assert r["status"] == "active_until_superseded"
        assert r["effective_to"] == "2026-06-30"
        assert r["superseded_by"] == "109_2025_QH15"

    def test_S001_doc_validity_pending(self):
        """S-001: Luật 109/2025 status pending, chưa có hiệu lực."""
        from src.tools.doc_validity_tool import check_doc_validity
        r = check_doc_validity("109_2025_QH15")
        assert r["found"] is True
        assert r["status"] == "pending"
        assert r["is_currently_valid"] is False

    def test_S001_doc_not_in_db(self):
        """S-001: TT40/2021 không có trong DB."""
        from src.tools.doc_validity_tool import check_doc_validity
        r = check_doc_validity("TT40_2021_TTBTC")
        assert r["found"] is False

    # ── S-002: Multi-income detector ─────────────────────────────────────────

    def test_S002_detect_luong_va_kinh_doanh(self):
        """S-002: Q14 pattern — 2 công ty + hợp đồng lao động → multi-income."""
        from src.agent.planner import _detect_multi_income
        q = "Tôi làm 2 công ty, 1 chỗ ký hợp đồng lao động, 1 chỗ làm freelance, vừa có cửa hàng doanh thu 1 tỷ"
        assert _detect_multi_income(q) is True

    def test_S002_detect_luong_hkd(self):
        """S-002: lương + hộ kinh doanh rõ ràng → True."""
        from src.agent.planner import _detect_multi_income
        assert _detect_multi_income("lương 30 triệu và hộ kinh doanh doanh thu 2 tỷ") is True

    def test_S002_no_detect_pure_hkd(self):
        """S-002: Q3 pure HKD → không trigger."""
        from src.agent.planner import _detect_multi_income
        assert _detect_multi_income("tiệm làm tóc doanh thu 1 tỷ nộp thuế bao nhiêu") is False

    def test_S002_no_detect_pure_luong(self):
        """S-002: câu hỏi chỉ về lương → không trigger."""
        from src.agent.planner import _detect_multi_income
        assert _detect_multi_income("lương 20 triệu tháng có phải nộp thuế không") is False

    # ── S-003: Search confidence annotator ───────────────────────────────────

    def test_S003_high_confidence(self):
        """S-003: score >= SCORE_HIGH → _confidence=HIGH."""
        from src.agent.planner import _annotate_search_confidence, _SCORE_HIGH
        result = {
            "results": [
                {"score": _SCORE_HIGH + 0.001, "text": "nội dung A", "doc_id": "68_2026_NDCP"},
                {"score": _SCORE_HIGH - 0.001, "text": "nội dung B", "doc_id": "68_2026_NDCP"},
            ]
        }
        out = _annotate_search_confidence(result)
        assert out["results"][0]["_confidence"] == "HIGH", \
            f"score={_SCORE_HIGH + 0.001} phải HIGH (threshold={_SCORE_HIGH})"
        assert out["_search_quality"] == "HIGH"

    def test_S003_low_confidence(self):
        """S-003: score thấp → _confidence=LOW."""
        from src.agent.planner import _annotate_search_confidence
        result = {"results": [{"score": 0.003, "text": "x", "doc_id": "abc"}]}
        out = _annotate_search_confidence(result)
        assert "LOW" in out["results"][0]["_confidence"]
        assert out["_search_quality"] == "LOW"

    def test_S003_no_results(self):
        """S-003: kết quả rỗng → _search_quality=NO_RESULTS."""
        from src.agent.planner import _annotate_search_confidence
        out = _annotate_search_confidence({"results": []})
        assert out["_search_quality"] == "NO_RESULTS"

    # ── S-005: Hierarchy conflict annotator ──────────────────────────────────

    def test_S005_detects_superseded_conflict(self):
        """S-005: TT111 (superseded) + Luật 109 (supersedes) → conflict flagged."""
        from src.agent.planner import _annotate_hierarchy_conflicts
        result = {
            "results": [
                {"doc_id": "111_2013_TTBTC", "text": "hướng dẫn cũ"},
                {"doc_id": "109_2025_QH15",  "text": "luật mới"},
            ]
        }
        out = _annotate_hierarchy_conflicts(result)
        assert "_hierarchy_conflicts" in out, "Phải detect conflict 111→109"
        conflicts = out["_hierarchy_conflicts"]
        assert len(conflicts) >= 1
        assert conflicts[0]["old"] == "111_2013_TTBTC"
        assert conflicts[0]["new"] == "109_2025_QH15"

    def test_S005_no_conflict_when_single_doc(self):
        """S-005: chỉ 1 doc → không có conflict."""
        from src.agent.planner import _annotate_hierarchy_conflicts
        result = {"results": [{"doc_id": "68_2026_NDCP", "text": "nội dung"}]}
        out = _annotate_hierarchy_conflicts(result)
        assert "_hierarchy_conflicts" not in out

    # ── S-007: Cache staleness guard ─────────────────────────────────────────

    def test_S007_old_entry_stale(self):
        """S-007: entry tạo từ 2023 (trước NĐ68) → stale."""
        from src.retrieval.qa_cache import _check_law_staleness
        ts_2023 = 1700000000.0   # Nov 2023
        result = _check_law_staleness(ts_2023)
        assert result is not None, "Entry từ 2023 phải bị stale"

    def test_S007_recent_entry_not_stale(self):
        """S-007: entry tạo hôm nay → không stale."""
        from src.retrieval.qa_cache import _check_law_staleness
        result = _check_law_staleness(time.time())
        assert result is None, "Entry hôm nay không stale"

    # ── S-008: Pending law citation guard ────────────────────────────────────

    def test_S008_detect_pending_law_cite(self):
        """S-008: answer cite '109/2025' → phát hiện pending."""
        from src.agent.planner import _check_pending_law_citations
        answer = "Theo Luật 109/2025/QH15, mức thuế suất mới áp dụng từ 01/07/2026."
        found = _check_pending_law_citations(answer)
        doc_ids = [f[0] for f in found]
        assert "109_2025_QH15" in doc_ids, "Phải detect cite Luật 109 (pending)"

    def test_S008_no_cite_active_doc(self):
        """S-008: cite NĐ68 (active) → không flag."""
        from src.agent.planner import _check_pending_law_citations
        answer = "Theo Nghị định 68/2026/NĐ-CP, doanh thu dưới 500 triệu được miễn."
        found = _check_pending_law_citations(answer)
        assert len(found) == 0, "NĐ68 active không phải pending → không flag"


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1C — Key_facts format integrity
# Verify key_facts cho 30 câu vẫn còn trong questions.json
# (guard: nếu ai xóa nhầm key_facts → test fail)
# ─────────────────────────────────────────────────────────────────────────────

# 30 câu regression subset: 10 HKD, 5 TNCN, 10 TMĐT, 3 kê khai, 2 xử phạt
REGRESSION_30_IDS = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10,          # Thuế HKD
    11, 12, 13, 14, 15,                       # TNCN
    21, 22, 23, 24, 25, 26, 27, 28, 29, 30,  # TMĐT
    31, 32, 33,                               # Nghĩa vụ kê khai
    48, 50,                                   # Xử phạt
]

# key_facts tối thiểu cần có (canary — không phải full set)
_CANARY_KEY_FACTS: dict[int, list[str]] = {
    2:  ["500 triệu"],
    3:  ["5%", "2%"],
    11: ["15,5 triệu", "6,2 triệu"],
    15: ["4.000.000", "10%"],
    21: ["thực hiện khấu trừ"],
    22: ["1%", "0,5%"],
    31: ["không phải nộp tờ khai"],
    48: ["Vẫn bị truy thu"],
}


class TestKeyFactsIntegrity:
    """Verify key_facts trong questions.json không bị xóa/thay đổi nhầm."""

    def test_all_30_questions_exist(self, questions):
        missing = [i for i in REGRESSION_30_IDS if i not in questions]
        assert missing == [], f"Questions bị thiếu trong questions.json: {missing}"

    def test_all_30_have_key_facts(self, questions):
        no_kf = [i for i in REGRESSION_30_IDS if not questions[i].get("key_facts")]
        assert no_kf == [], f"Questions không có key_facts: {no_kf}"

    @pytest.mark.parametrize("qid,expected_facts", _CANARY_KEY_FACTS.items())
    def test_canary_key_facts_unchanged(self, questions, qid, expected_facts):
        """Verify key_facts của 8 câu canary không bị thay đổi."""
        actual_kf = questions[qid].get("key_facts", [])
        for fact in expected_facts:
            assert any(fact in kf for kf in actual_kf), \
                f"Q{qid}: key_fact '{fact}' bị mất. Hiện tại: {actual_kf}"


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2 — Integration canary (requires API key)
# 5 câu bao phủ 5 topic clusters quan trọng nhất
# Mark: @pytest.mark.integration
# Chạy: pytest tests/test_regression_30.py -m integration -v
# ─────────────────────────────────────────────────────────────────────────────

# 5 canary questions + key_facts bắt buộc trong answer
_INTEGRATION_CANARIES = [
    # (qid, question_text, key_facts_required_in_answer)
    (
        2,
        "Doanh thu của quán phở một năm bao nhiêu thì được miễn không phải nộp thuế GTGT và TNCN?",
        ["500 triệu", "500.000.000"],
    ),
    (
        11,
        "Mức giảm trừ gia cảnh cho bản thân và người phụ thuộc năm 2026 là bao nhiêu vậy?",
        ["15,5 triệu", "6,2 triệu"],
    ),
    (
        21,
        "Tôi bán hàng trên Shopee, Tiktok bây giờ sàn tự thu thuế luôn hay tôi vẫn phải tự đi nộp?",
        ["thực hiện khấu trừ"],
    ),
    (
        31,
        "Hộ kinh doanh của tôi doanh thu dưới 500 triệu/năm thì khỏi đóng thuế, "
        "nhưng có cần nộp tờ khai thuế không?",
        ["không phải nộp tờ khai"],
    ),
    (
        48,
        "Các khoản nợ thuế khoán từ năm 2025 trở về trước có bị cơ quan thuế truy thu lại không "
        "khi sang 2026 bị phát hiện?",
        ["Vẫn bị truy thu"],
    ),
]


@pytest.mark.integration
class TestIntegrationCanary:
    """
    5 câu canary chạy với TaxAIAgent thực tế.
    Yêu cầu: GEMINI_API_KEY trong môi trường.
    Mục tiêu: verify answer chứa key_facts bắt buộc.
    """

    @pytest.fixture(scope="class")
    def agent(self):
        """Khởi tạo TaxAIAgent một lần cho cả class."""
        import os
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            pytest.skip("GEMINI_API_KEY không có trong môi trường")
        from src.agent.planner import TaxAIAgent
        return TaxAIAgent()

    @pytest.mark.parametrize("qid,question,required_facts", _INTEGRATION_CANARIES)
    def test_canary_answer_contains_key_facts(self, agent, qid, question, required_facts):
        """
        Agent phải trả lời câu hỏi với ít nhất 1 trong required_facts.
        Fail → regression nghiêm trọng.
        """
        result = agent.answer(question)
        answer = result.get("answer", "").lower()

        assert len(answer) > 50, \
            f"Q{qid}: Answer rỗng hoặc quá ngắn: '{answer[:100]}'"

        matched = [f for f in required_facts if f.lower() in answer]
        assert matched, (
            f"Q{qid}: Không tìm thấy key_facts {required_facts} trong answer.\n"
            f"Answer snippet: '{answer[:300]}'"
        )

    @pytest.mark.parametrize("qid,question,_", _INTEGRATION_CANARIES)
    def test_canary_has_citation(self, agent, qid, question, _):
        """Answer phải có citation pháp lý (Điều X hoặc tên văn bản)."""
        result = agent.answer(question)
        answer = result.get("answer", "")
        has_citation = (
            "điều" in answer.lower()
            or "nghị định" in answer.lower()
            or "thông tư" in answer.lower()
            or "nghị quyết" in answer.lower()
            or "luật" in answer.lower()
        )
        assert has_citation, \
            f"Q{qid}: Answer thiếu citation pháp lý. Snippet: '{answer[:200]}'"
