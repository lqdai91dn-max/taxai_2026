"""
tests/eval_runner.py — Evaluation framework cho TaxAI, 200 câu hỏi.

4-tier evaluation:
  Tier 1 (Deterministic): needs_calculation=True → kiểm tra answer correctness (primary).
                           Nếu expected_value có GT numbers/text → PASS/FAIL dựa vào answer.
                           Tool compliance là diagnostic field (không phạt nếu answer đúng).
                           Fallback khi không có GT: vẫn yêu cầu tool.
  Tier 2 (Citation):       Mọi câu → answer phải chứa tham chiếu pháp luật cụ thể.
  Tier 3 (Tool selection): Agent gọi đúng loại tool cho từng topic/loại câu hỏi.
  Tier 4 (Key facts):      Nếu questions.json có key_facts → answer phải chứa
                           các số/từ khóa thiết yếu (N/A nếu chưa annotate).

Scoring:
  - Mỗi tier: PASS=1.0, PARTIAL=0.5, FAIL=0.0, N/A=None
  - overall_score = trung bình các tier áp dụng (bỏ N/A)

Ground truth annotation (trong data/eval/questions.json):
  expected_value:    dict với kết quả tính toán đúng (auto-computed bởi calculator tools)
  key_facts:         list string — số/từ khóa bắt buộc có trong câu trả lời
  expected_docs:     list doc_id phải được cite
  expected_articles: list Điều luật nguồn (điền bằng NotebookLLM)
  annotation_status: "auto" | "pending" | "verified"

Usage:
  python tests/eval_runner.py                           # tất cả 200 câu
  python tests/eval_runner.py --limit 20                # 20 câu đầu
  python tests/eval_runner.py --topic "Thuế HKD"        # theo topic
  python tests/eval_runner.py --difficulty easy         # theo độ khó
  python tests/eval_runner.py --needs-calc              # chỉ câu cần tính
  python tests/eval_runner.py --annotated               # chỉ câu đã annotate
  python tests/eval_runner.py --output results.json     # lưu kết quả
  python tests/eval_runner.py --dry-run                 # kiểm tra setup
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import atexit
import signal
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Setup paths ───────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

QUESTIONS_PATH = ROOT / "data" / "eval" / "questions.json"
RESULTS_DIR    = ROOT / "data" / "eval" / "results"

# Regex để phát hiện tham chiếu pháp luật trong câu trả lời
_LEGAL_REF_RE = re.compile(
    r"(?:"
    r"[Đđ]iều\s+\d+"               # Điều X
    r"|[Kk]hoản\s+\d+"             # Khoản X
    r"|[Nn]ghị\s+[Đđ]ịnh\s+\d+"   # Nghị định XX
    r"|[Ll]uật\s+[A-ZĐẨẸ]"        # Luật TNCN / Luật Thuế...
    r"|\d{2,4}/\d{4}/[A-Z]"        # 68/2026/NĐ-CP dạng số
    r"|NĐ-CP"                       # viết tắt
    r"|QH\d{2}"                     # QH15
    r"|NDCP"                        # biến thể
    r"|Thông\s+tư\s+\d+"           # Thông tư
    r"|[Ss]ổ\s+tay"                # Sổ tay (hướng dẫn HKD — không có Điều/Khoản)
    r"|[Cc]ông\s+văn\s+\d+"        # Công văn hướng dẫn
    r")",
    re.UNICODE,
)

# Refusal keyword list — dùng cho false_refusal detection
_REFUSAL_KEYWORDS = [
    "không đủ cơ sở",
    "không có thông tin",
    "chưa thể xác định",
    "không tìm thấy quy định",
    "không có căn cứ",
    "ngoài phạm vi",
    "chưa có quy định",
    "không thể trả lời",
    "không có căn cứ pháp lý",
]

# Max answer length để coi là refusal (dài hơn thường là full answer có từ khóa bên lề)
_REFUSAL_MAX_LEN = 250


def _detect_false_refusal(result: dict, q_data: dict, t4_score) -> bool:
    """
    False Refusal: Stage 2 tìm đúng doc nhưng Stage 3 từ chối trả lời.

    Phân biệt với True Refusal (Stage 2 tìm sai → LLM đúng khi từ chối):
      retrieved_doc_ids ∩ expected_docs > 0  → Stage 2 OK
      citations_doc_ids ∩ expected_docs = 0  → Stage 3 không cite được
      answer ngắn + chứa từ khóa từ chối     → LLM đầu hàng
      T4 = 0.0                               → Không extract được fact nào
    """
    expected_docs = q_data.get("expected_docs", [])
    if not expected_docs:
        return False

    answer = result.get("answer", "").lower().strip()
    # Guard: answer dài không phải refusal thực sự
    if len(answer) >= _REFUSAL_MAX_LEN:
        return False

    llm_surrendered = any(kw in answer for kw in _REFUSAL_KEYWORDS)
    if not llm_surrendered:
        return False

    # Stage 2 có tìm đúng doc không?
    retrieved = set(result.get("retrieved_doc_ids", []))
    retrieval_success = bool(retrieved & set(expected_docs))
    if not retrieval_success:
        return False  # Stage 2 fail → True Refusal, không phải False

    # T4 = 0.0: không extract được fact dù có data
    return t4_score == 0.0 or t4_score is None


# Mapping topic → expected tools (ít nhất 1 trong số này phải được gọi)
# Keys khớp với topic trong data/eval/questions.json
_TOPIC_TOOL_MAP: dict[str, list[str]] = {
    "Thuế hộ kinh doanh":        ["calculate_tax_hkd", "calculate_tax_hkd_profit",
                                   "evaluate_tax_obligation", "search_legal_docs", "get_article"],
    "Thuế thu nhập cá nhân":     ["calculate_tncn_progressive", "calculate_deduction",
                                   "search_legal_docs", "get_article"],
    "Thuế thương mại điện tử":   ["calculate_tax_hkd", "evaluate_tax_obligation",
                                   "search_legal_docs", "get_article", "get_guidance"],
    "Nghĩa vụ kê khai":          ["evaluate_tax_obligation", "search_legal_docs", "get_article"],
    "Kế toán HKD":               ["search_legal_docs", "get_guidance"],
    "Xử phạt vi phạm":           ["search_legal_docs", "get_article"],
    "Thủ tục hành chính":        ["search_legal_docs", "get_article", "get_guidance"],
    "Thủ tục hoàn thuế":         ["search_legal_docs", "get_article"],
    "Hiệu lực pháp luật":        ["check_doc_validity", "get_article_with_amendments",
                                   "search_legal_docs"],
    "Miễn giảm thuế":            ["evaluate_tax_obligation", "search_legal_docs", "get_article"],
    "Thuế tài sản":              ["search_legal_docs"],
    "Bất khả kháng":             ["search_legal_docs"],
}

# Tất cả calculator/rule-engine tools — phải dùng ít nhất 1 khi needs_calculation=True
_CALC_TOOLS = {
    "calculate_tax_hkd",
    "calculate_tax_hkd_profit",
    "calculate_tncn_progressive",
    "calculate_deduction",
    "evaluate_tax_obligation",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TierResult:
    score: float | None   # 0.0 / 0.5 / 1.0 / None (N/A)
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class EvalResult:
    # Question metadata
    question_id:       str
    question:          str
    topic:             str
    difficulty:        str
    user_type:         str
    needs_calculation: bool
    annotation_status: str   # "auto" | "pending" | "verified"

    # Agent output
    answer:      str
    tool_calls:  list[dict]
    latency_ms:  int
    error:       str | None

    # Tier scores
    tier1: TierResult   # Deterministic — số khớp ground truth (end-to-end calc)
    tier2: TierResult   # Citation structured — citations_doc_ids vs expected_docs (soft F1)
    tier3: TierResult   # DIAGNOSTIC ONLY — không tính vào overall_score
    tier4: TierResult   # Key facts — answer có chứa số/từ khóa thiết yếu

    # Pipeline v2 extended fields
    sources:           list[dict] = field(default_factory=list)
    degrade_level:     int        = 1    # 1=L1 / 2=L2 / 3=L3
    fm_breakdown:      dict       = field(default_factory=dict)
    retrieved_doc_ids: list[str]  = field(default_factory=list)
    false_refusal:     bool       = False  # Stage 2 tìm đúng nhưng Stage 3 từ chối

    @property
    def overall_score(self) -> float:
        # T3 excluded — diagnostic only (synthetic tool_calls không đáng tin)
        scores = [t.score for t in [self.tier1, self.tier2, self.tier4] if t.score is not None]
        if not scores:
            return 0.0
        base = sum(scores) / len(scores)
        # Degrade multiplier: L1=1.0, L2=0.85, L3=0.0
        multiplier = {1: 1.0, 2: 0.85, 3: 0.0}.get(self.degrade_level or 1, 1.0)
        return round(base * multiplier, 3)

    @property
    def passed(self) -> bool:
        # Veto Rule: T1=0.0 trên câu calc → fail toàn tập
        if self.needs_calculation and self.tier1.score == 0.0:
            return False
        return round(self.overall_score * 1000) >= 667


# ═══════════════════════════════════════════════════════════════════════════════
# Tier evaluators
# ═══════════════════════════════════════════════════════════════════════════════

def eval_tier1_deterministic(
    result: dict,
    needs_calculation: bool,
    q_data: dict,
) -> TierResult:
    """
    Tier 1 (redesigned — answer correctness PRIMARY, tool compliance DIAGNOSTIC):

    Layer 1 (primary):  Khi có expected_value → kiểm tra answer trực tiếp với
                        ground truth numbers/text.  PASS/PARTIAL/FAIL chỉ dựa vào
                        answer correctness — không phạt nếu agent không gọi tool
                        nhưng answer vẫn đúng.  tool_compliance field trong details
                        ghi nhận "compliant" / "tool_skipped" (diagnostic only).

    Layer 2 (fallback): Khi không có ground truth → vẫn yêu cầu tool
                        (không có cách nào khác để verify).
                        Nếu không có calc tool → FAIL.
                        Nếu có tool → so sánh answer với tool output.
    """
    if not needs_calculation:
        return TierResult(score=None, reason="N/A — câu hỏi không cần tính toán")

    tool_calls   = result.get("tool_calls", [])
    calc_calls   = [tc for tc in tool_calls if tc.get("tool") in _CALC_TOOLS]
    tool_used    = bool(calc_calls)
    answer       = result.get("answer", "")
    answer_nums  = _extract_numbers_from_text(answer)

    # ── Layer 1: Ground-truth answer correctness (primary) ───────────────────
    expected_value = q_data.get("expected_value")
    if expected_value and isinstance(expected_value, dict):
        _SKIP_KEYS = {"computed_by", "note", "business_category", "is_exempt",
                      "rate_pct", "late_payment_rate_per_day", "tncn_rate_if_500m_3b"}
        gt_numbers = [
            int(round(v)) for k, v in expected_value.items()
            if isinstance(v, (int, float)) and v > 10_000 and k not in _SKIP_KEYS
        ]
        if gt_numbers:
            matched  = [n for n in gt_numbers if _number_in_answer(n, answer_nums)]
            ratio    = len(matched) / len(gt_numbers)
            tc_flag  = "compliant" if tool_used else "tool_skipped"
            tool_lbl = calc_calls[0]["tool"] if calc_calls else "none"

            if ratio >= 0.8:
                suffix = f" [tool={tool_lbl}]" if tool_used else " [tool_skipped — answer đúng, chấp nhận]"
                return TierResult(
                    score=1.0,
                    reason=f"PASS — {len(matched)}/{len(gt_numbers)} ground-truth numbers khớp answer{suffix}",
                    details={"matched": matched, "gt": gt_numbers,
                             "tool": tool_lbl, "tool_compliance": tc_flag},
                )
            elif ratio >= 0.5:
                missing = [n for n in gt_numbers if n not in matched]
                return TierResult(
                    score=0.5,
                    reason=f"PARTIAL — {len(matched)}/{len(gt_numbers)} ground-truth numbers khớp",
                    details={"matched": matched, "missing": missing,
                             "tool": tool_lbl, "tool_compliance": tc_flag},
                )
            else:
                suffix = f" [tool={tool_lbl}]" if tool_used else " [tool_skipped]"
                return TierResult(
                    score=0.2,
                    reason=f"FAIL — ground-truth numbers không xuất hiện trong answer{suffix}",
                    details={"gt_numbers": gt_numbers, "answer_numbers": answer_nums[:5],
                             "tool": tool_lbl, "tool_compliance": tc_flag},
                )

        # ── 1b. Text-match: expected_value có 'text' (câu hỏi về tỷ lệ/%) ───
        ev_text = expected_value.get("text", "")
        if ev_text:
            ev_tokens    = re.findall(r"[\d]+(?:[.,]\d+)?%|miễn thuế|không chịu", ev_text.lower())
            answer_lower = answer.lower()
            matched_tok  = [t for t in ev_tokens if t in answer_lower]
            ratio        = len(matched_tok) / len(ev_tokens) if ev_tokens else 1.0
            tc_flag      = "compliant" if tool_used else "tool_skipped"
            tool_lbl     = calc_calls[0]["tool"] if calc_calls else "none"

            if ratio >= 0.6:
                suffix = "" if tool_used else " [tool_skipped — answer đúng, chấp nhận]"
                return TierResult(
                    score=1.0,
                    reason=f"PASS — answer khớp expected text '{ev_text}'{suffix}",
                    details={"matched_tokens": matched_tok,
                             "tool": tool_lbl, "tool_compliance": tc_flag},
                )
            elif ratio >= 0.3:
                return TierResult(
                    score=0.5,
                    reason=f"PARTIAL — answer khớp {len(matched_tok)}/{len(ev_tokens)} tokens từ '{ev_text}'",
                    details={"matched_tokens": matched_tok,
                             "tool": tool_lbl, "tool_compliance": tc_flag},
                )
            else:
                suffix = "" if tool_used else " [tool_skipped]"
                return TierResult(
                    score=0.2,
                    reason=f"FAIL — answer không khớp expected text '{ev_text}'{suffix}",
                    details={"ev_text": ev_text,
                             "tool": tool_lbl, "tool_compliance": tc_flag},
                )

    # ── Layer 2: Không có ground truth → bắt buộc dùng tool ──────────────────
    if not calc_calls:
        return TierResult(
            score=0.0,
            reason="FAIL — agent không gọi calculator/rule-engine tool nào (không có ground truth để verify)",
            details={"tools_called": [tc.get("tool") for tc in tool_calls],
                     "tool_compliance": "no_tool"},
        )

    tool_name = calc_calls[0]["tool"]

    # ── 2b. Fallback: so sánh answer với tool output ─────────────────────────
    tool_numbers = _extract_numbers_from_tool_calls(calc_calls)
    matched_nums = [n for n in tool_numbers if _number_in_answer(n, answer_nums)]

    has_any_number = bool(re.search(
        r"\d{1,3}(?:[.,]\d{3})*(?:\s*(?:triệu|tỷ|đồng|VND))?", answer
    ))

    if matched_nums:
        return TierResult(
            score=1.0,
            reason=f"PASS — gọi {tool_name}, số từ tool output xuất hiện trong answer",
            details={"tool": tool_name, "matched": matched_nums[:3],
                     "tool_compliance": "compliant"},
        )
    elif has_any_number:
        return TierResult(
            score=0.5,
            reason=f"PARTIAL — gọi {tool_name} nhưng số không khớp chính xác với tool output",
            details={"tool": tool_name, "tool_numbers": tool_numbers[:5],
                     "answer_numbers": answer_nums[:5], "tool_compliance": "compliant"},
        )
    else:
        return TierResult(
            score=0.5,
            reason=f"PARTIAL — gọi {tool_name} nhưng answer không chứa số cụ thể",
            details={"tool": tool_name, "tool_compliance": "compliant"},
        )


def eval_tier2_structured(result: dict, q_data: dict) -> TierResult:
    """
    Tier 2 (v2): Structured citation validation.

    Đo Recall × Soft-Precision dựa trên mảng citations_doc_ids (structured)
    đối chiếu với expected_docs (ground truth).

    Không dùng regex trên answer text — tránh false positive từ hallucinated citations.

    Soft-Precision: penalty = min(1.0, precision / 0.5)
    → Không phạt khi cite ≤ 2× expected (precision ≥ 0.5)
    → Phạt tuyến tính khi cite quá nhiều (noise) (precision < 0.5)
    """
    expected_docs = q_data.get("expected_docs", [])
    if not expected_docs:
        return TierResult(score=None, reason="N/A — không có expected_docs")

    answer = result.get("answer", "")
    if not answer:
        return TierResult(score=0.0, reason="FAIL — answer rỗng")

    # Deduplicate citations
    citations_doc_ids = list(set(result.get("citations_doc_ids", [])))
    expected_set      = set(expected_docs)
    citations_set     = set(citations_doc_ids)

    if not citations_set:
        return TierResult(
            score=0.0,
            reason="FAIL — không có citations (sources rỗng)",
            details={"expected_docs": expected_docs},
        )

    overlap   = len(citations_set & expected_set)
    recall    = overlap / len(expected_set)
    precision = overlap / len(citations_set)

    # Soft precision penalty — linear scaling, cap at 1.0
    penalty = min(1.0, precision / 0.5)
    t2      = round(recall * penalty, 3)

    if t2 >= 0.9:
        return TierResult(
            score=t2,
            reason=f"PASS — overlap {overlap}/{len(expected_set)} docs, precision={precision:.2f}",
            details={"matched": list(citations_set & expected_set),
                     "missing": list(expected_set - citations_set),
                     "extra":   list(citations_set - expected_set)},
        )
    elif t2 >= 0.4:
        return TierResult(
            score=t2,
            reason=f"PARTIAL — overlap {overlap}/{len(expected_set)}, precision={precision:.2f}, penalty={penalty:.2f}",
            details={"matched": list(citations_set & expected_set),
                     "missing": list(expected_set - citations_set)},
        )
    else:
        return TierResult(
            score=t2,
            reason=f"FAIL — overlap {overlap}/{len(expected_set)} docs",
            details={"expected": expected_docs, "got": citations_doc_ids},
        )


def eval_tier3_tool_selection(
    result: dict,
    topic: str,
    needs_calculation: bool,
) -> TierResult:
    """
    Tier 3: Kiểm tra agent gọi đúng loại tool cho câu hỏi.
    """
    tool_calls = result.get("tool_calls", [])
    tools_used = {tc.get("tool") for tc in tool_calls}

    if not tool_calls:
        # Nếu không có tool call mà answer vẫn hợp lý → PARTIAL
        answer = result.get("answer", "")
        if len(answer) > 50:
            return TierResult(
                score=0.3,
                reason="PARTIAL — không gọi tool nào nhưng có answer",
                details={"tools_used": []},
            )
        return TierResult(
            score=0.0,
            reason="FAIL — không gọi tool nào",
            details={"tools_used": []},
        )

    # Kiểm tra calculator tool cho câu hỏi cần tính toán
    if needs_calculation:
        has_calc = bool(tools_used & _CALC_TOOLS)
        if not has_calc:
            return TierResult(
                score=0.0,
                reason="FAIL — câu hỏi cần tính toán nhưng không dùng calculator tool",
                details={"tools_used": list(tools_used)},
            )

    # Kiểm tra topic-appropriate tools
    expected_tools = _TOPIC_TOOL_MAP.get(topic, [])
    if expected_tools:
        matching = tools_used & set(expected_tools)
        if matching:
            return TierResult(
                score=1.0,
                reason=f"PASS — dùng {matching} phù hợp với topic '{topic}'",
                details={"tools_used": list(tools_used), "matching": list(matching)},
            )
        else:
            # Dùng tool khác nhưng vẫn là tool hợp lệ
            return TierResult(
                score=0.5,
                reason=f"PARTIAL — dùng {tools_used} thay vì {set(expected_tools[:2])} cho topic '{topic}'",
                details={"tools_used": list(tools_used), "expected": expected_tools[:3]},
            )
    else:
        # Topic không có mapping → chỉ cần có gọi tool
        return TierResult(
            score=0.8,
            reason=f"PASS — gọi {len(tool_calls)} tools (topic không có mapping cụ thể)",
            details={"tools_used": list(tools_used)},
        )


def eval_tier4_key_facts(result: dict, q_data: dict, llm_judge_client=None) -> TierResult:
    """
    Tier 4: Nếu câu hỏi đã được annotate với key_facts,
    kiểm tra answer có chứa đủ các số/từ khóa thiết yếu không.

    key_facts ví dụ: ["5%", "15%", "50 triệu", "125 triệu", "miễn"]
    Score: tỷ lệ key_facts khớp.  N/A nếu key_facts rỗng.

    llm_judge_client: nếu truyền vào, các key_fact fail T4a sẽ được T4b
                      (Gemini Flash) đánh giá lại theo ngữ nghĩa.
    """
    key_facts = q_data.get("key_facts", [])
    if not key_facts:
        return TierResult(score=None, reason="N/A — chưa có key_facts (cần annotate)")

    answer = result.get("answer", "")
    if not answer:
        return TierResult(score=0.0, reason="FAIL — answer rỗng", details={"key_facts": key_facts})

    matched = [f for f in key_facts if _fact_in_answer(f, answer)]
    missing = [f for f in key_facts if f not in matched]

    # T4b — LLM judge cho các key_fact fail T4a
    t4b_rescued = []
    if llm_judge_client and missing:
        question = q_data.get("question", "")
        still_missing = []
        for kf in missing:
            verdict = _llm_judge_fact(llm_judge_client, question, kf, answer)
            if verdict:
                t4b_rescued.append(kf)
            else:
                still_missing.append(kf)
        matched = matched + t4b_rescued
        missing = still_missing

    ratio = len(matched) / len(key_facts)
    t4b_note = f" (T4b rescued: {t4b_rescued})" if t4b_rescued else ""

    if ratio >= 0.9:
        return TierResult(
            score=1.0,
            reason=f"PASS — {len(matched)}/{len(key_facts)} key facts có trong answer{t4b_note}",
            details={"matched": matched, "t4b_rescued": t4b_rescued},
        )
    elif ratio >= 0.5:
        return TierResult(
            score=0.5,
            reason=f"PARTIAL — {len(matched)}/{len(key_facts)} key facts, thiếu: {missing}{t4b_note}",
            details={"matched": matched, "missing": missing, "t4b_rescued": t4b_rescued},
        )
    else:
        return TierResult(
            score=0.0,
            reason=f"FAIL — chỉ {len(matched)}/{len(key_facts)} key facts, thiếu: {missing}{t4b_note}",
            details={"matched": matched, "missing": missing, "t4b_rescued": t4b_rescued},
        )


_T4B_PROMPT = """\
Bạn là chuyên gia đánh giá câu trả lời về luật thuế Việt Nam.

Câu hỏi người dùng: {question}

Key fact cần kiểm tra: "{key_fact}"

Câu trả lời của AI: {answer}

Nhiệm vụ: Câu trả lời có đề cập ĐÚNG đến nội dung của key fact "{key_fact}" không?

## Quy tắc CHẤP NHẬN (YES):

**1. Đồng nghĩa pháp lý** — chấp nhận nếu câu trả lời dùng cụm từ pháp lý tương đương:
- "miễn thuế" ≡ "không chịu thuế" ≡ "không phải nộp thuế" ≡ "thuộc đối tượng không chịu thuế"
- "không phải khai" ≡ "không cần khai" ≡ "sàn khai thay" ≡ "đã được khai thay"
- "được trừ" ≡ "được tính vào chi phí" ≡ "được khấu trừ"
- "bắt buộc" ≡ "phải" ≡ "bắt buộc phải"

**2. Diễn giải hệ quả tương đương** — chấp nhận nếu câu trả lời nêu hệ quả logic trực tiếp:
- Key fact "sàn chịu trách nhiệm" → PASS nếu câu trả lời nói "người bán không phải khai/nộp"
- Key fact "không phải khai lại" → PASS nếu câu trả lời nói "sàn đã khai thay rồi" hoặc "sàn có trách nhiệm nộp thay"
- Key fact "được miễn" → PASS nếu câu trả lời nói "không phải nộp" hoặc "0 đồng"

**3. Tổ hợp số học** — chấp nhận nếu các số trong câu trả lời cộng lại bằng key fact:
- Key fact "7%" → PASS nếu câu trả lời có "5% + 2%" hoặc "5%...2%"
- Key fact "1,5%" → PASS nếu câu trả lời có "1% + 0,5%" hoặc "0,5%...1%"
- Key fact "tổng X" → PASS nếu câu trả lời liệt kê các thành phần cộng lại bằng X

**4. Diễn đạt khác nhau nhưng nghĩa giống** — chấp nhận nếu nội dung thực chất giống nhau dù dùng từ ngữ khác

## Quy tắc TỪ CHỐI (NO):
- Câu trả lời nói NGƯỢC LẠI key fact (ví dụ: key fact "miễn thuế" nhưng câu trả lời nói "phải nộp thuế")
- Key fact hoàn toàn KHÔNG được đề cập hoặc ám chỉ dưới bất kỳ hình thức nào
- Câu trả lời chỉ hỏi lại / không có nội dung trả lời

Chỉ trả lời: YES hoặc NO (viết hoa), theo sau là dấu phẩy và lý do ngắn gọn (tối đa 15 từ).
Ví dụ: "YES, agent nói sàn khai thay đồng nghĩa không phải khai lại"
Ví dụ: "YES, agent nói 5%+2% tương đương 7%"
Ví dụ: "NO, agent nói phải nộp thuế trong khi key fact là miễn thuế"
"""


def _llm_judge_fact(client, question: str, key_fact: str, answer: str) -> bool:
    """
    Gọi Gemini Flash để đánh giá ngữ nghĩa một key_fact.
    Trả về True nếu LLM đánh giá PASS.
    """
    prompt = _T4B_PROMPT.format(
        question=question,
        key_fact=key_fact,
        answer=answer[:2000],  # giới hạn để tiết kiệm token
    )
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"temperature": 0, "max_output_tokens": 100},
        )
        text = (response.text or "").strip().upper()
        return text.startswith("YES")
    except Exception:
        return False  # fail-safe: không rescue nếu API lỗi


# ── Vietnamese legal paraphrase normalization ─────────────────────────────────
# Chuẩn hóa các mẫu diễn đạt tương đương trong ngôn ngữ pháp lý tiếng Việt.
# Áp dụng cho CẢ fact lẫn answer → so sánh sau normalize.
#
# Nguyên tắc: chỉ normalize khi hai cụm từ có nghĩa pháp lý GIỐNG NHAU.
# Không normalize nếu có sự khác biệt ý nghĩa (vd: "miễn thuế" ≠ "không chịu thuế"
# theo nghĩa kỹ thuật pháp lý, nhưng trong ngữ cảnh T4 user-facing thì tương đương).

_LEGAL_PARAPHRASE_SUBS = [
    # 1. Tax status negation: "không phải chịu thuế X" ≡ "không chịu thuế X"
    #    Ví dụ: "không phải chịu thuế TNCN" → "không chịu thuế tncn"
    (re.compile(r"không\s+phải\s+chịu\s+thuế", re.UNICODE), "không chịu thuế"),
    (re.compile(r"thuộc\s+diện\s+không\s+chịu\s+thuế", re.UNICODE), "không chịu thuế"),
    (re.compile(r"không\s+thuộc\s+diện\s+chịu\s+thuế", re.UNICODE), "không chịu thuế"),

    # 2. Obligation negation: "không cần [V]" ≡ "không phải [V]"
    #    Ví dụ: "không cần khai lại" → "không phải khai lại"
    (re.compile(r"không\s+cần\s+phải\s+", re.UNICODE), "không phải "),
    (re.compile(r"không\s+cần\s+", re.UNICODE),         "không phải "),
    (re.compile(r"không\s+bắt\s+buộc\s+phải\s+", re.UNICODE), "không phải "),
    (re.compile(r"không\s+bắt\s+buộc\s+", re.UNICODE),  "không phải "),

    # 3. Permission: "hoàn toàn có thể [V]" / "có quyền [V]" ≡ "được phép [V]"
    #    Ví dụ: "hoàn toàn có thể ủy quyền" → "được phép ủy quyền"
    (re.compile(r"hoàn\s+toàn\s+có\s+thể\s+", re.UNICODE), "được phép "),
    (re.compile(r"có\s+quyền\s+", re.UNICODE),              "được phép "),
    (re.compile(r"(?:^|(?<=\s))có\s+thể\s+(?!bị\b)", re.UNICODE), "được phép "),

    # 4. Liability/consequence: "có thể bị [V]" ≡ "bị [V]"
    #    Ví dụ: "có thể bị truy thu" → "bị truy thu"
    #    (agent nêu rủi ro = đã cover fact về nghĩa vụ)
    (re.compile(r"có\s+thể\s+bị\s+", re.UNICODE), "bị "),

    # 5. Continuation obligation: "vẫn có thể bị" ≡ "vẫn bị"
    (re.compile(r"vẫn\s+có\s+thể\s+bị\s+", re.UNICODE), "vẫn bị "),
    (re.compile(r"vẫn\s+phải\s+", re.UNICODE), "vẫn "),
]


def _normalize_legal_paraphrase(text: str) -> str:
    """Chuẩn hóa mẫu paraphrase pháp lý tiếng Việt để so sánh ngữ nghĩa."""
    t = text.lower()
    for pattern, replacement in _LEGAL_PARAPHRASE_SUBS:
        t = pattern.sub(replacement, t)
    return t


_NEGATION_GUARD_RE = re.compile(
    r"không\s+\w*\s*$",   # "không [word?]" trước vị trí match, ví dụ "không " hoặc "không được "
    re.UNICODE,
)

def _match_with_negation_guard(fact_str: str, answer_str: str) -> bool:
    """
    Kiểm tra fact_str có trong answer_str, nhưng reject nếu bị đặt trong ngữ cảnh phủ định.
    Ví dụ: fact="được phép ủy quyền" trong "KHÔNG được phép ủy quyền" → False.
             fact="miễn thuế"         trong "không được miễn thuế"     → False.
    Lookback 15 ký tự để bắt cả "không được " (11 ký tự).
    """
    idx = answer_str.find(fact_str)
    if idx < 0:
        return False
    prefix = answer_str[max(0, idx - 15): idx]
    if _NEGATION_GUARD_RE.search(prefix):
        return False
    return True


# Bảng abbreviation bidirectional — expand về dạng đầy đủ trước khi so sánh
_ABBREV_MAP: dict[str, str] = {
    "tncn":   "thu nhập cá nhân",
    "gtgt":   "giá trị gia tăng",
    "hkd":    "hộ kinh doanh",
    "ckd":    "cá nhân kinh doanh",
    "tmđt":   "thương mại điện tử",
    "cccd":   "căn cước công dân",
    "cmnd":   "căn cước công dân",   # CMND ≡ CCCD trong ngữ cảnh này
    "cty":    "công ty",
    "nđ-cp":  "nghị định",
    "tt-btc": "thông tư",
    "tt-btp": "thông tư",
    "ubnd":   "ủy ban nhân dân",
    "mst":    "mã số thuế",
    "hđlđ":  "hợp đồng lao động",
    "bctc":   "báo cáo tài chính",
}

def _normalize_abbrev(text: str) -> str:
    """
    Expand tất cả abbreviation về dạng đầy đủ.
    Dùng lookaround (?<!\\w)/(?!\\w) để tránh replace substring giữa chừng.
    Python 3: \\w match đầy đủ Unicode bao gồm chữ tiếng Việt có dấu.
    """
    t = text.lower()
    for abbrev, full in _ABBREV_MAP.items():
        pattern = r"(?<!\w)" + re.escape(abbrev) + r"(?!\w)"
        t = re.sub(pattern, full, t)
    return t


def _fact_in_answer(fact: str, answer: str) -> bool:
    """
    Kiểm tra một key_fact có xuất hiện trong answer không.
    Xử lý các dạng biểu diễn khác nhau:
      "5%"       → "5%", "5 %", "5 phần trăm"
      "50 triệu" → "50 triệu", "50,000,000", "50000000"
      "miễn"     → "miễn", "không phải nộp"
      "TNCN"     → "thu nhập cá nhân" (bidirectional abbreviation)
    """
    answer_lower = answer.lower()
    fact_lower   = fact.lower().strip()

    # Direct match (case-insensitive)
    if fact_lower in answer_lower:
        return True

    # Markdown-stripped match — loại bỏ ký tự markdown (**bold**, *italic*, __bold__)
    # trước khi so sánh để tránh false miss khi LLM bold keyword trong câu trả lời
    answer_nomd = re.sub(r'\*+|__+', '', answer_lower)
    if fact_lower in answer_nomd:
        return True

    # Abbreviation normalize — expand cả fact + answer về dạng đầy đủ
    fact_norm   = _normalize_abbrev(fact_lower)
    answer_norm = _normalize_abbrev(answer_lower)
    if fact_norm in answer_norm:
        return True

    # Legal paraphrase normalize — chuẩn hóa mẫu diễn đạt pháp lý tương đương
    # Ví dụ: "không phải chịu thuế" ≡ "không chịu thuế"
    #        "không cần khai lại"   ≡ "không phải khai lại"
    #        "hoàn toàn có thể ủy quyền" ≡ "được phép ủy quyền"
    # Negation guard: reject nếu match nằm trong ngữ cảnh "không [fact]"
    fact_para   = _normalize_legal_paraphrase(fact_lower)
    answer_para = _normalize_legal_paraphrase(answer_lower)
    if _match_with_negation_guard(fact_para, answer_para):
        return True
    # Chain: paraphrase + abbrev normalize (bắt "không chịu thuế TNCN" ≡ "không chịu thuế thu nhập cá nhân")
    fact_pa   = _normalize_abbrev(fact_para)
    answer_pa = _normalize_abbrev(answer_para)
    if _match_with_negation_guard(fact_pa, answer_pa):
        return True

    # % variants: "5%" → "5 %"
    if "%" in fact_lower:
        no_space = fact_lower.replace(" ", "")
        with_space = fact_lower.replace("%", " %")
        if no_space in answer_lower or with_space in answer_lower:
            return True

    # Số triệu variants: "50 triệu" → "50,000,000" hoặc "50.000.000"
    m = re.match(r"^([\d,.]+)\s*triệu$", fact_lower)
    if m:
        try:
            val = int(float(m.group(1).replace(",", "").replace(".", "")) * 1_000_000)
            if str(val) in answer or f"{val:,}" in answer:
                return True
        except ValueError:
            pass

    # Số tỷ variants: "3 tỷ" → "3,000,000,000"
    m = re.match(r"^([\d,.]+)\s*tỷ$", fact_lower)
    if m:
        try:
            val = int(float(m.group(1).replace(",", "").replace(".", "")) * 1_000_000_000)
            if str(val) in answer or f"{val:,}" in answer:
                return True
        except ValueError:
            pass

    # Reverse number: "500.000.000 VND" → check "500 triệu" or "500,000,000" in answer
    m = re.match(r"^([\d.]+)\s*(?:vnd|vnđ|đồng|₫)?$", fact_lower.strip())
    if m:
        try:
            val = int(m.group(1).replace(".", ""))
            if val >= 1_000_000:
                trieu = val / 1_000_000
                s = f"{int(trieu)} triệu" if trieu == int(trieu) else f"{trieu:.2f} triệu".replace(".", ",")
                if s in answer_lower:
                    return True
            if val >= 1_000_000_000:
                ty = val / 1_000_000_000
                s = f"{int(ty)} tỷ" if ty == int(ty) else f"{ty:.1f} tỷ".replace(".", ",")
                if s in answer_lower:
                    return True
            # Also check comma-formatted number: 500,000,000
            formatted = f"{val:,}"
            if formatted in answer:
                return True
        except ValueError:
            pass

    # Synonym map cho một số từ khóa phổ biến
    _SYNONYMS: dict[str, list[str]] = {
        "miễn":        ["miễn thuế", "không phải nộp", "không nộp thuế", "được miễn"],
        "0,5%":        ["0.5%", "0,5 %", "0.5 %"],
        "0,03%":       ["0.03%", "0,03 %"],
        "15,5 triệu":  ["15.5 triệu", "15,500,000", "15.500.000"],
        "6,2 triệu":   ["6.2 triệu", "6,200,000", "6.200.000"],
    }
    for synonym in _SYNONYMS.get(fact_lower, []):
        if synonym.lower() in answer_lower:
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_numbers_from_text(text: str) -> list[int]:
    """
    Trích xuất các số > 10 000 từ text, bao gồm dạng rút gọn:
      "50 triệu" → 50_000_000
      "3 tỷ"     → 3_000_000_000
      "325,000,000" → 325_000_000
    """
    result: list[int] = []

    # Dạng "X triệu" hoặc "X,Y triệu"
    for m in re.finditer(r"([\d]+(?:[.,]\d+)?)\s*triệu", text, re.UNICODE):
        try:
            val = int(float(m.group(1).replace(",", ".")) * 1_000_000)
            if val > 10_000:
                result.append(val)
        except ValueError:
            pass

    # Dạng "X tỷ"
    for m in re.finditer(r"([\d]+(?:[.,]\d+)?)\s*tỷ", text, re.UNICODE):
        try:
            val = int(float(m.group(1).replace(",", ".")) * 1_000_000_000)
            if val > 10_000:
                result.append(val)
        except ValueError:
            pass

    # Dạng số đầy đủ có dấu phân cách (325,000,000 hoặc 325.000.000)
    for raw in re.findall(r"\d[\d.,]*", text):
        cleaned = raw.replace(",", "").replace(".", "")
        try:
            n = int(cleaned)
            if n > 10_000:
                result.append(n)
        except ValueError:
            pass

    # Deduplicate giữ thứ tự
    seen: set[int] = set()
    deduped = []
    for n in result:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    return deduped


def _extract_numbers_from_tool_calls(calc_calls: list[dict]) -> list[int]:
    """Trích xuất số quan trọng từ calculator tool results."""
    numbers = []
    for tc in calc_calls:
        result = tc.get("result", {})
        if isinstance(result, dict):
            for key in [
                "total_tax", "gtgt_payable", "tncn_payable", "tax_payable",
                # calculate_deduction output keys
                "total_deduction_annual", "total_deduction_monthly",
                "personal_deduction_monthly", "dependent_deduction_per_person",
                "old_law_annual",
            ]:
                val = result.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    numbers.append(int(round(val)))
    return numbers


def _number_in_answer(num: int, answer_numbers: list[int], tolerance: float = 0.01) -> bool:
    """Kiểm tra số có xuất hiện trong answer (cho phép sai số 1% do làm tròn)."""
    if not answer_numbers:
        return False
    for n in answer_numbers:
        if n == 0:
            continue
        if abs(n - num) / max(abs(num), 1) <= tolerance:
            return True
        # Kiểm tra dạng rút gọn (triệu: num/1e6)
        num_m = int(round(num / 1_000_000))
        if num_m > 0 and abs(n - num_m) / max(abs(num_m), 1) <= tolerance:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_evaluation(
    questions: list[dict],
    agent,
    verbose: bool = False,
    llm_judge_client=None,
    delay_ms: int = 0,
    on_result=None,
    error_wait_ms: int = 15_000,
    max_consecutive_errors: int = 30,
) -> list[EvalResult]:
    """Chạy đánh giá cho một danh sách câu hỏi.

    Args:
        delay_ms:               Delay (ms) giữa các request thành công.
        error_wait_ms:          Thêm delay (ms) sau mỗi request lỗi API (on top of delay_ms).
        max_consecutive_errors: Dừng và báo lỗi nếu lỗi API liên tiếp đạt ngưỡng này.
    """
    results: list[EvalResult] = []
    total = len(questions)
    consecutive_errors = 0   # đếm lỗi API liên tiếp trong phiên

    for i, q in enumerate(questions, 1):
        qid   = q.get("id", f"q{i}")
        qtext = q["question"]
        topic = q.get("topic", "")
        diff  = q.get("difficulty", "")
        utype = q.get("user_type", "")
        needs_calc = q.get("needs_calculation", False)

        print(f"[{i:3d}/{total}] {qid} | {topic} | {diff}", end="", flush=True)

        # ── Gọi agent ────────────────────────────────────────────────────────
        t0 = time.perf_counter()
        error_msg = None
        agent_result: dict = {}

        try:
            agent_result = agent.answer(
                question     = qtext,
                show_sources = True,
            )
        except Exception as e:
            error_msg   = str(e)
            agent_result = {
                "answer": "", "sources": [], "tool_calls": [],
                "model": "error", "iterations": 0,
            }

        latency_ms = int((time.perf_counter() - t0) * 1000)

        # ── Detect API error để tracking consecutive ──────────────────────────
        is_api_error = bool(error_msg and any(
            kw in error_msg for kw in ("RATE_LIMITED", "429", "503", "UNAVAILABLE", "RESOURCE_EXHAUSTED")
        ))

        if is_api_error:
            consecutive_errors += 1
        else:
            consecutive_errors = 0   # reset khi có câu thành công

        # ── Evaluate ─────────────────────────────────────────────────────────
        t1 = eval_tier1_deterministic(agent_result, needs_calc, q)
        t2 = eval_tier2_structured(agent_result, q)
        t3 = eval_tier3_tool_selection(agent_result, topic, needs_calc)  # diagnostic only
        t4 = eval_tier4_key_facts(agent_result, q, llm_judge_client=llm_judge_client)

        false_refusal = _detect_false_refusal(agent_result, q, t4.score)

        eval_res = EvalResult(
            question_id       = qid,
            question          = qtext,
            topic             = topic,
            difficulty        = diff,
            user_type         = utype,
            needs_calculation = needs_calc,
            annotation_status = q.get("annotation_status", "pending"),
            answer            = agent_result.get("answer", ""),
            tool_calls        = agent_result.get("tool_calls", []),
            latency_ms        = latency_ms,
            error             = error_msg,
            tier1             = t1,
            tier2             = t2,
            tier3             = t3,
            tier4             = t4,
            sources           = agent_result.get("sources", []),
            degrade_level     = agent_result.get("degrade_level", 1) or 1,
            fm_breakdown      = agent_result.get("fm_breakdown", {}),
            retrieved_doc_ids = agent_result.get("retrieved_doc_ids", []),
            false_refusal     = false_refusal,
        )

        status = "✅" if eval_res.passed else ("⚠️" if eval_res.overall_score >= 0.4 else "❌")
        err_tag = f" [API_ERR×{consecutive_errors}]" if is_api_error else ""
        print(f" | score={eval_res.overall_score:.2f} {status} | {latency_ms}ms{err_tag}")

        if verbose:
            _print_verbose(eval_res)

        results.append(eval_res)
        if on_result:
            on_result(eval_res)

        # ── Kiểm tra ngưỡng lỗi liên tiếp ───────────────────────────────────
        if consecutive_errors >= max_consecutive_errors:
            print(
                f"\n{'='*60}\n"
                f"⛔ DỪNG: {consecutive_errors} lỗi API liên tiếp (ngưỡng {max_consecutive_errors}).\n"
                f"   Đã chạy {i}/{total} câu, {len(results)} kết quả đã lưu.\n"
                f"   Nguyên nhân có thể: daily quota cạn hoặc API overload.\n"
                f"   Hành động: chạy lại sau khi quota reset (07:00 sáng VN).\n"
                f"{'='*60}\n"
            )
            break   # on_result đã lưu từng câu qua callback → partial results an toàn

        # ── Delay sau request ────────────────────────────────────────────────
        if i < total:
            wait = delay_ms
            if is_api_error and error_wait_ms > 0:
                wait += error_wait_ms   # thêm 15s sau lỗi
            if wait > 0:
                time.sleep(wait / 1000)

    return results


def _print_verbose(r: EvalResult) -> None:
    print(f"  Q: {r.question[:80]}...")
    print(f"  A: {r.answer[:120]}...")
    print(f"  T1={r.tier1.score} — {r.tier1.reason}")
    print(f"  T2={r.tier2.score} — {r.tier2.reason}")
    print(f"  T3={r.tier3.score} — {r.tier3.reason}")
    print(f"  T4={r.tier4.score} — {r.tier4.reason}")
    if r.error:
        print(f"  ERR: {r.error}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════════

def generate_report(results: list[EvalResult]) -> dict:
    """Tạo báo cáo tổng hợp."""
    total = len(results)
    if total == 0:
        return {"error": "No results"}

    passed  = sum(1 for r in results if r.passed)
    errors  = sum(1 for r in results if r.error)

    # Tier scores (bỏ N/A)
    t1_scores = [r.tier1.score for r in results if r.tier1.score is not None]
    t2_scores = [r.tier2.score for r in results if r.tier2.score is not None]
    t3_scores = [r.tier3.score for r in results if r.tier3.score is not None]
    t4_scores = [r.tier4.score for r in results if r.tier4.score is not None]

    # Tool compliance stats (diagnostic — from T1 details)
    t1_applicable = [r for r in results if r.needs_calculation and r.tier1.score is not None]
    t1_compliant  = sum(1 for r in t1_applicable
                        if r.tier1.details.get("tool_compliance") == "compliant")
    t1_skipped    = sum(1 for r in t1_applicable
                        if r.tier1.details.get("tool_compliance") == "tool_skipped")
    t1_no_tool    = sum(1 for r in t1_applicable
                        if r.tier1.details.get("tool_compliance") == "no_tool")

    # Annotation coverage
    annotated  = sum(1 for r in results if r.annotation_status in ("auto", "verified"))
    verified   = sum(1 for r in results if r.annotation_status == "verified")

    overall_scores = [r.overall_score for r in results]

    # Per-topic breakdown
    topics: dict[str, dict] = {}
    for r in results:
        if r.topic not in topics:
            topics[r.topic] = {"total": 0, "passed": 0, "scores": []}
        topics[r.topic]["total"]  += 1
        topics[r.topic]["passed"] += int(r.passed)
        topics[r.topic]["scores"].append(r.overall_score)

    topic_summary = {
        t: {
            "total":  v["total"],
            "passed": v["passed"],
            "pass_rate": round(v["passed"] / v["total"], 3),
            "avg_score": round(sum(v["scores"]) / len(v["scores"]), 3),
        }
        for t, v in sorted(topics.items())
    }

    # Per-difficulty breakdown
    diffs: dict[str, list[float]] = {}
    for r in results:
        diffs.setdefault(r.difficulty, []).append(r.overall_score)

    diff_summary = {
        d: {
            "count":    len(scores),
            "avg_score": round(sum(scores) / len(scores), 3),
        }
        for d, scores in sorted(diffs.items())
    }

    # Latency stats
    latencies = [r.latency_ms for r in results if not r.error]

    def _avg(scores: list) -> float | None:
        return round(sum(scores) / len(scores), 3) if scores else None

    return {
        "summary": {
            "total":           total,
            "passed":          passed,
            "pass_rate":       round(passed / total, 3),
            "avg_score":       round(sum(overall_scores) / total, 3),
            "errors":          errors,
            "annotated":       annotated,
            "verified":        verified,
            "timestamp":       datetime.now().isoformat(),
        },
        "tier_scores": {
            "tier1_deterministic": {
                "applicable": len(t1_scores),
                "avg":        _avg(t1_scores),
                "note":       "Answer correctness primary; tool compliance diagnostic only",
            },
            "tier2_citation": {
                "applicable": len(t2_scores),
                "avg":        _avg(t2_scores),
                "note":       "Answer phải có tham chiếu pháp luật cụ thể",
            },
            "tier3_tool_selection": {
                "applicable": len(t3_scores),
                "avg":        _avg(t3_scores),
                "note":       "Agent gọi đúng loại tool cho topic/loại câu hỏi",
            },
            "tier4_key_facts": {
                "applicable": len(t4_scores),
                "avg":        _avg(t4_scores),
                "note":       f"Key facts check (N/A cho {total - len(t4_scores)} câu chưa annotate)",
            },
        },
        "by_topic":      topic_summary,
        "by_difficulty": diff_summary,
        "latency_ms": {
            "min":  min(latencies) if latencies else None,
            "max":  max(latencies) if latencies else None,
            "avg":  round(sum(latencies) / len(latencies)) if latencies else None,
            "p50":  _percentile(latencies, 50),
            "p95":  _percentile(latencies, 95),
        },
        "tool_compliance": {
            "applicable":      len(t1_applicable),
            "compliant":       t1_compliant,
            "tool_skipped":    t1_skipped,
            "no_tool":         t1_no_tool,
            "compliance_rate": round(t1_compliant / len(t1_applicable), 3) if t1_applicable else None,
            "note":            "Diagnostic only — does not affect T1 score when answer is correct",
        },
        "failed_questions": [
            {
                "id":       r.question_id,
                "question": r.question[:80],
                "score":    r.overall_score,
                "t1":       r.tier1.reason,
                "t2":       r.tier2.reason,
                "t3":       r.tier3.reason,
                "error":    r.error,
            }
            for r in results if not r.passed
        ][:20],  # top 20 failures
    }


def _percentile(data: list[float], pct: int) -> float | None:
    if not data:
        return None
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * pct / 100
    f, c = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return round(sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f))


def print_summary(report: dict) -> None:
    s  = report["summary"]
    ts = report["tier_scores"]
    print("\n" + "═" * 60)
    print("TAXAI EVALUATION REPORT")
    print("═" * 60)
    print(f"  Total questions:     {s['total']}")
    print(f"  Passed (score≥0.667): {s['passed']} ({s['pass_rate']:.1%})")
    print(f"  Average score:       {s['avg_score']:.3f}")
    print(f"  Errors:              {s['errors']}")
    print(f"  Annotated/Verified:  {s['annotated']}/{s['verified']}")
    print()
    print("Tier Scores:")
    t1 = ts["tier1_deterministic"]
    t2 = ts["tier2_citation"]
    t3 = ts["tier3_tool_selection"]
    t4 = ts["tier4_key_facts"]

    def _fmt(label: str, tier: dict) -> str:
        if tier["avg"] is not None:
            return f"  {label}  {tier['avg']:.3f}  (n={tier['applicable']})"
        return f"  {label}  N/A"

    print(_fmt("T1 Deterministic:  ", t1))
    print(_fmt("T2 Citation:       ", t2))
    print(_fmt("T3 Tool Selection: ", t3))
    print(_fmt("T4 Key Facts:      ", t4) + (f"  ← {s['total'] - t4['applicable']} câu chưa annotate" if t4['applicable'] < s['total'] else ""))
    tc = report.get("tool_compliance", {})
    if tc.get("applicable"):
        cr = tc["compliance_rate"]
        line = f"  Tool compliance:     {cr:.1%}  ({tc['compliant']}/{tc['applicable']} calc questions used tool)  [diagnostic]"
        if tc.get("tool_skipped"):
            line += f"  ← {tc['tool_skipped']} correct answer(s) without tool"
        print(line)
    print()
    print("By Topic:")
    for topic, v in report["by_topic"].items():
        bar = "█" * int(v["pass_rate"] * 10) + "░" * (10 - int(v["pass_rate"] * 10))
        print(f"  {topic:25s} {bar} {v['pass_rate']:.0%} ({v['passed']}/{v['total']})")
    print()
    print("By Difficulty:")
    for diff, v in report["by_difficulty"].items():
        print(f"  {diff:10s}  avg={v['avg_score']:.3f}  n={v['count']}")
    lat = report.get("latency_ms", {})
    if lat.get("avg"):
        print()
        print(f"Latency: avg={lat['avg']}ms  p50={lat['p50']}ms  p95={lat['p95']}ms")
    print("═" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TaxAI Evaluation Runner")
    p.add_argument("--limit",       type=int,   default=None,  help="Giới hạn số câu hỏi")
    p.add_argument("--offset",      type=int,   default=0,     help="Bỏ qua N câu đầu sau khi filter (dùng để chia batch)")
    p.add_argument("--topic",       type=str,   default=None,  help="Lọc theo topic")
    p.add_argument("--difficulty",  type=str,   default=None,  choices=["easy", "medium", "hard"])
    p.add_argument("--needs-calc",  action="store_true",       help="Chỉ câu cần tính toán")
    p.add_argument("--annotated",   action="store_true",       help="Chỉ câu đã có annotation (auto/verified)")
    p.add_argument("--output",      type=str,   default=None,  help="Tên file JSON output")
    p.add_argument("--verbose",     action="store_true",       help="In chi tiết từng câu")
    p.add_argument("--dry-run",     action="store_true",       help="Kiểm tra setup, không chạy")
    p.add_argument("--model",       type=str,   default=None,  help="Override Gemini model")
    p.add_argument("--agent",        type=str,   default="planner",
                   choices=["planner", "pipeline", "v4"],      help="Agent backend: planner (R25 default), pipeline (v2), v4 (Pipeline v4)")
    p.add_argument("--ids",          type=str,   default=None,  help="Comma-separated question IDs, e.g. 7,31,36")
    p.add_argument("--llm-judge",    action="store_true",       help="Bật T4b LLM judge (Gemini Flash) cho key_facts fail T4a")
    p.add_argument("--rerun-failed", type=str,   default=None,  help="Path tới file JSON kết quả cũ — chỉ chạy lại câu fail/error, skip câu đã pass")
    p.add_argument("--delay",              type=int, default=15_000, help="Delay (ms) giữa các request thành công (default: 15000 = 15s)")
    p.add_argument("--error-wait",         type=int, default=15_000, help="Thêm delay (ms) sau mỗi request lỗi API, cộng thêm vào --delay (default: 15000)")
    p.add_argument("--max-api-errors",     type=int, default=30,     help="Dừng nếu lỗi API liên tiếp đạt ngưỡng này (default: 30)")
    p.add_argument("--no-delay",     action="store_true",             help="Tắt delay giữa các request (không khuyến khích)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # ── Lock file — chặn nhiều instance chạy song song ────────────────────────
    # Dùng open(..., 'x') để atomic exclusive-create, tránh TOCTOU race condition.
    LOCK_FILE = RESULTS_DIR / ".eval_runner.lock"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    def _pid_alive(pid: int) -> bool:
        try:
            import psutil
            return psutil.pid_exists(pid)
        except ImportError:
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False

    while True:
        try:
            # Atomic create — chỉ 1 process thành công, process khác nhận FileExistsError
            with open(LOCK_FILE, "x") as f:
                f.write(str(os.getpid()))
            break  # lock acquired
        except FileExistsError:
            # Lock file tồn tại — kiểm tra process còn sống không
            try:
                lock_pid = int(LOCK_FILE.read_text().strip())
                if _pid_alive(lock_pid):
                    print(f"❌ eval_runner đang chạy (PID {lock_pid}). Đợi kết thúc hoặc kiểm tra lại.")
                    print(f"   Nếu bị treo, xóa: {LOCK_FILE}")
                    sys.exit(1)
                else:
                    # Process cũ đã chết nhưng không xóa lock → xóa và thử lại
                    LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                LOCK_FILE.unlink(missing_ok=True)  # corrupt → xóa và thử lại

    def _remove_lock():
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    atexit.register(_remove_lock)

    # ── Load questions ────────────────────────────────────────────────────────
    if not QUESTIONS_PATH.exists():
        print(f"❌ Questions file not found: {QUESTIONS_PATH}")
        sys.exit(1)

    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        all_questions: list[dict] = json.load(f)

    print(f"✅ Loaded {len(all_questions)} questions from {QUESTIONS_PATH.name}")

    # ── Filter ────────────────────────────────────────────────────────────────
    questions = all_questions

    if args.ids:
        id_set = {int(x.strip()) for x in args.ids.split(",")}
        questions = [q for q in questions if q.get("id") in id_set]
        print(f"   Filtered by IDs {id_set}: {len(questions)} questions")

    if args.topic:
        questions = [q for q in questions if args.topic.lower() in q.get("topic", "").lower()]
        print(f"   Filtered by topic '{args.topic}': {len(questions)} questions")

    if args.difficulty:
        questions = [q for q in questions if q.get("difficulty") == args.difficulty]
        print(f"   Filtered by difficulty '{args.difficulty}': {len(questions)} questions")

    if args.needs_calc:
        questions = [q for q in questions if q.get("needs_calculation")]
        print(f"   Filtered needs_calculation=True: {len(questions)} questions")

    if args.annotated:
        questions = [q for q in questions if q.get("annotation_status") in ("auto", "verified")]
        print(f"   Filtered annotated only: {len(questions)} questions")

    if args.offset and args.offset > 0:
        questions = questions[args.offset:]
        print(f"   Offset {args.offset}: skipped first {args.offset} questions")

    if args.limit:
        questions = questions[:args.limit]
        print(f"   Limited to first {args.limit} questions")

    # ── Auto-resume: nếu output file đã tồn tại, tự động dùng làm base ──────
    if args.output and not args.rerun_failed:
        _out = Path(args.output)
        _existing = _out if _out.is_absolute() or "/" in args.output or "\\" in args.output else RESULTS_DIR / _out
        if _existing.exists():
            args.rerun_failed = str(_existing)
            print(f"   Auto-resume: output file đã tồn tại — sẽ skip câu đã pass từ {_existing.name}")

    # ── --rerun-failed: load prev results, skip passed questions ─────────────
    prev_results_by_id: dict[int, dict] = {}
    if args.rerun_failed:
        prev_path = Path(args.rerun_failed)
        if not prev_path.exists():
            print(f"❌ --rerun-failed: file not found: {prev_path}")
            sys.exit(1)
        with open(prev_path, encoding="utf-8") as f:
            prev_data = json.load(f)
        prev_results = prev_data.get("results", [])
        for r in prev_results:
            tier_scores = [
                r[t]["score"] for t in ["tier1", "tier2", "tier3", "tier4"]
                if r.get(t) and r[t].get("score") is not None
            ]
            avg = sum(tier_scores) / len(tier_scores) if tier_scores else 0.0
            passed = round(avg * 1000) >= 667
            prev_results_by_id[r["question_id"]] = {"passed": passed, "result": r}

        n_before = len(questions)
        questions = [q for q in questions if not prev_results_by_id.get(q.get("id"), {}).get("passed", False)]
        n_skip = n_before - len(questions)
        print(f"   --rerun-failed: {n_skip} câu đã pass (skip), {len(questions)} câu sẽ chạy lại")

    if not questions:
        print("✅ Tất cả câu đều đã pass — không cần chạy lại")
        sys.exit(0)

    # ── Dry run ───────────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\n✅ Dry run OK — would evaluate {len(questions)} questions")
        print("   Sample questions:")
        for q in questions[:3]:
            print(f"   [{q['id']}] {q['question'][:70]}")
        return

    # ── Load agent ────────────────────────────────────────────────────────────
    agent_label = args.agent if hasattr(args, "agent") else "planner"

    if agent_label == "v4":
        print("\n⏳ Loading Pipeline v4 (V4Adapter)...")
        try:
            from src.agent.pipeline_v4.eval_adapter import V4Adapter
            v4_kwargs: dict = {}
            if args.model:
                v4_kwargs["model"] = args.model
            agent = V4Adapter(**v4_kwargs)
            print(f"✅ Pipeline v4 ready — model: {agent.model}")
        except Exception as e:
            print(f"❌ Failed to load Pipeline v4: {e}")
            sys.exit(1)
    elif agent_label == "pipeline":
        print("\n⏳ Loading Pipeline v2 (PipelineAdapter)...")
        try:
            from src.retrieval.hybrid_search import HybridSearch
            from src.agent.pipeline_adapter import PipelineAdapter
            searcher = HybridSearch()
            pipeline_kwargs: dict = {}
            if args.model:
                pipeline_kwargs["model"] = args.model
            agent = PipelineAdapter(searcher=searcher, **pipeline_kwargs)
            print(f"✅ Pipeline ready — model: {agent.model}")
        except Exception as e:
            print(f"❌ Failed to load pipeline: {e}")
            sys.exit(1)
    else:
        print("\n⏳ Loading TaxAIAgent...")
        try:
            from src.agent.planner import TaxAIAgent
            kwargs: dict = {}
            if args.model:
                kwargs["model"] = args.model
            agent = TaxAIAgent(**kwargs)
            print(f"✅ Agent ready — model: {agent.model}")
        except Exception as e:
            print(f"❌ Failed to load agent: {e}")
            sys.exit(1)

    # ── LLM judge client (T4b) ───────────────────────────────────────────────
    llm_judge_client = None
    if args.llm_judge:
        try:
            from google import genai as _genai
            llm_judge_client = _genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
            print("✅ T4b LLM judge enabled (Gemini Flash)")
        except Exception as e:
            print(f"⚠️  T4b LLM judge disabled — could not init client: {e}")

    # ── Run evaluation ────────────────────────────────────────────────────────
    effective_delay     = 0 if args.no_delay else args.delay
    effective_err_wait  = 0 if args.no_delay else args.error_wait
    effective_max_errs  = args.max_api_errors
    if effective_delay:
        print(f"   Delay between requests: {effective_delay}ms (dùng --no-delay để tắt)")
    if effective_err_wait:
        print(f"   Extra wait on API error: +{effective_err_wait}ms")
    print(f"   Stop threshold: {effective_max_errs} consecutive API errors")
    print(f"\n🚀 Starting evaluation — {len(questions)} questions\n")

    # Checkpoint on Ctrl+C — lưu kết quả partial thay vì mất hết
    _partial_results: list = []
    def _save_checkpoint(signum=None, frame=None):
        if _partial_results and args.output:
            _out = Path(args.output)
            _cp_path = _out if _out.is_absolute() or "/" in args.output or "\\" in args.output else RESULTS_DIR / _out
            _all = list(_partial_results)
            if prev_results_by_id:
                new_ids = {r.question_id for r in _all}
                for qid, entry in prev_results_by_id.items():
                    if entry["passed"] and qid not in new_ids:
                        r = entry["result"]
                        _all.append(EvalResult(
                            question_id=r["question_id"], question=r["question"],
                            topic=r["topic"], difficulty=r["difficulty"],
                            user_type=r.get("user_type", ""), needs_calculation=r.get("needs_calculation", False),
                            annotation_status=r.get("annotation_status", "pending"),
                            answer=r.get("answer", ""), tool_calls=r.get("tool_calls", []),
                            latency_ms=r.get("latency_ms", 0), error=r.get("error"),
                            tier1=TierResult(**r["tier1"]), tier2=TierResult(**r["tier2"]),
                            tier3=TierResult(**r["tier3"]),
                            tier4=TierResult(**r["tier4"]) if r.get("tier4") else TierResult(score=None, reason="N/A"),
                        ))
            _rpt = generate_report(_all)
            _ser = [asdict(r) for r in _all]
            with open(_cp_path, "w", encoding="utf-8") as f:
                json.dump({"report": _rpt, "results": _ser}, f, ensure_ascii=False, indent=2)
            print(f"\n💾 Checkpoint saved ({len(_all)} results) → {_cp_path}")
        if signum is not None:
            sys.exit(0)
    signal.signal(signal.SIGINT, _save_checkpoint)
    signal.signal(signal.SIGTERM, _save_checkpoint)

    try:
        results = run_evaluation(
            questions, agent,
            verbose=args.verbose,
            llm_judge_client=llm_judge_client,
            delay_ms=effective_delay,
            error_wait_ms=effective_err_wait,
            max_consecutive_errors=effective_max_errs,
            on_result=lambda r: _partial_results.append(r),
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ Evaluation error: {e}")
        _save_checkpoint()
        raise

    # ── Merge với prev results nếu dùng --rerun-failed ────────────────────────
    if prev_results_by_id:
        new_ids = {r.question_id for r in results}
        # Reconstruct EvalResult từ stored dict cho các câu đã pass
        for qid, entry in prev_results_by_id.items():
            if entry["passed"] and qid not in new_ids:
                r = entry["result"]
                results.append(EvalResult(
                    question_id       = r["question_id"],
                    question          = r["question"],
                    topic             = r["topic"],
                    difficulty        = r["difficulty"],
                    user_type         = r.get("user_type", ""),
                    needs_calculation = r.get("needs_calculation", False),
                    annotation_status = r.get("annotation_status", "pending"),
                    answer            = r.get("answer", ""),
                    tool_calls        = r.get("tool_calls", []),
                    latency_ms        = r.get("latency_ms", 0),
                    error             = r.get("error"),
                    tier1             = TierResult(**r["tier1"]),
                    tier2             = TierResult(**r["tier2"]),
                    tier3             = TierResult(**r["tier3"]),
                    tier4             = TierResult(**r["tier4"]) if r.get("tier4") else TierResult(score=None, reason="N/A"),
                ))
        results.sort(key=lambda r: r.question_id)
        print(f"\n   (Merged: {len(results)} total = {len(new_ids)} re-run + {len(results) - len(new_ids)} from prev)")

    # ── Report ────────────────────────────────────────────────────────────────
    report = generate_report(results)
    print_summary(report)

    # ── Save results ──────────────────────────────────────────────────────────
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output:
        _out = Path(args.output)
        output_path = _out if _out.is_absolute() or "/" in args.output or "\\" in args.output else RESULTS_DIR / _out
    else:
        output_path = RESULTS_DIR / f"eval_{timestamp}.json"

    # Serialize results
    serializable_results = []
    for r in results:
        d = asdict(r)
        # tier objects: convert to dict with score/reason/details
        for tier_key in ["tier1", "tier2", "tier3"]:
            tier = d[tier_key]
            d[tier_key] = {
                "score":   tier["score"],
                "reason":  tier["reason"],
                "details": tier["details"],
            }
        serializable_results.append(d)

    output_data = {
        "report":  report,
        "results": serializable_results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Results saved to: {output_path}")

    # Exit code
    pass_rate = report["summary"]["pass_rate"]
    sys.exit(0 if pass_rate >= 0.7 else 1)


if __name__ == "__main__":
    main()
