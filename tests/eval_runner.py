"""
tests/eval_runner.py — Evaluation framework cho TaxAI, 200 câu hỏi.

4-tier evaluation:
  Tier 1 (Deterministic): needs_calculation=True → agent PHẢI gọi calculator tool.
                           Nếu questions.json có expected_value → so sánh với ground truth.
                           Fallback: so sánh với số trong tool output.
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
import re
import sys
import time
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
    r")",
    re.UNICODE,
)

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
    tier1: TierResult   # Deterministic — gọi đúng tool + số khớp ground truth
    tier2: TierResult   # Citation — answer có trích dẫn pháp luật
    tier3: TierResult   # Tool selection — dùng đúng loại tool cho topic
    tier4: TierResult   # Key facts — answer có chứa số/từ khóa thiết yếu (N/A nếu chưa annotate)

    @property
    def overall_score(self) -> float:
        scores = [
            t.score for t in [self.tier1, self.tier2, self.tier3, self.tier4]
            if t.score is not None
        ]
        return round(sum(scores) / len(scores), 3) if scores else 0.0

    @property
    def passed(self) -> bool:
        return self.overall_score >= 0.67


# ═══════════════════════════════════════════════════════════════════════════════
# Tier evaluators
# ═══════════════════════════════════════════════════════════════════════════════

def eval_tier1_deterministic(
    result: dict,
    needs_calculation: bool,
    q_data: dict,
) -> TierResult:
    """
    Tier 1: Với câu hỏi cần tính toán:
      1. Agent PHẢI gọi ít nhất 1 calculator/rule-engine tool.
      2a. Nếu q_data có expected_value → so sánh answer với ground truth numbers.
      2b. Fallback → so sánh answer với số từ tool output.
    """
    if not needs_calculation:
        return TierResult(score=None, reason="N/A — câu hỏi không cần tính toán")

    tool_calls   = result.get("tool_calls", [])
    calc_calls   = [tc for tc in tool_calls if tc.get("tool") in _CALC_TOOLS]
    answer       = result.get("answer", "")
    answer_nums  = _extract_numbers_from_text(answer)

    if not calc_calls:
        return TierResult(
            score=0.0,
            reason="FAIL — agent không gọi calculator/rule-engine tool nào",
            details={"tools_called": [tc.get("tool") for tc in tool_calls]},
        )

    tool_name = calc_calls[0]["tool"]

    # ── 2a. Ground-truth check khi có expected_value ──────────────────────────
    expected_value = q_data.get("expected_value")
    if expected_value and isinstance(expected_value, dict):
        _SKIP_KEYS = {"computed_by", "note", "business_category", "is_exempt",
                      "rate_pct", "late_payment_rate_per_day", "tncn_rate_if_500m_3b"}
        gt_numbers = [
            int(round(v)) for k, v in expected_value.items()
            if isinstance(v, (int, float)) and v > 10_000 and k not in _SKIP_KEYS
        ]
        if gt_numbers:
            matched = [n for n in gt_numbers if _number_in_answer(n, answer_nums)]
            ratio   = len(matched) / len(gt_numbers)
            if ratio >= 0.8:
                return TierResult(
                    score=1.0,
                    reason=f"PASS — gọi {tool_name}, {len(matched)}/{len(gt_numbers)} ground-truth numbers khớp",
                    details={"tool": tool_name, "matched": matched, "gt": gt_numbers},
                )
            elif ratio >= 0.5:
                missing = [n for n in gt_numbers if n not in matched]
                return TierResult(
                    score=0.5,
                    reason=f"PARTIAL — gọi {tool_name}, chỉ {len(matched)}/{len(gt_numbers)} ground-truth numbers khớp",
                    details={"tool": tool_name, "matched": matched, "missing": missing},
                )
            else:
                return TierResult(
                    score=0.2,
                    reason=f"FAIL — gọi {tool_name} nhưng ground-truth numbers không xuất hiện trong answer",
                    details={"tool": tool_name, "gt_numbers": gt_numbers, "answer_numbers": answer_nums[:5]},
                )

    # ── 2b. Fallback: so sánh với tool output ────────────────────────────────
    tool_numbers = _extract_numbers_from_tool_calls(calc_calls)
    matched_nums = [n for n in tool_numbers if _number_in_answer(n, answer_nums)]

    has_any_number = bool(re.search(
        r"\d{1,3}(?:[.,]\d{3})*(?:\s*(?:triệu|tỷ|đồng|VND))?", answer
    ))

    if matched_nums:
        return TierResult(
            score=1.0,
            reason=f"PASS — gọi {tool_name}, số từ tool output xuất hiện trong answer",
            details={"tool": tool_name, "matched": matched_nums[:3]},
        )
    elif has_any_number:
        return TierResult(
            score=0.5,
            reason=f"PARTIAL — gọi {tool_name} nhưng số không khớp chính xác với tool output",
            details={"tool": tool_name, "tool_numbers": tool_numbers[:5], "answer_numbers": answer_nums[:5]},
        )
    else:
        return TierResult(
            score=0.5,
            reason=f"PARTIAL — gọi {tool_name} nhưng answer không chứa số cụ thể",
            details={"tool": tool_name},
        )


def eval_tier2_citation(result: dict, question: str) -> TierResult:
    """
    Tier 2: Kiểm tra answer có chứa tham chiếu pháp luật cụ thể không.
    """
    answer = result.get("answer", "")

    if not answer:
        return TierResult(score=0.0, reason="FAIL — answer rỗng")

    # Kiểm tra regex patterns
    legal_refs = _LEGAL_REF_RE.findall(answer)

    # Kiểm tra sources từ tool calls
    sources = result.get("sources", [])
    has_sources = len(sources) > 0

    if legal_refs and has_sources:
        return TierResult(
            score=1.0,
            reason=f"PASS — {len(legal_refs)} tham chiếu pháp luật + {len(sources)} sources",
            details={"legal_refs": legal_refs[:5], "source_count": len(sources)},
        )
    elif legal_refs:
        return TierResult(
            score=0.8,
            reason=f"PASS — {len(legal_refs)} tham chiếu pháp luật trong text",
            details={"legal_refs": legal_refs[:5]},
        )
    elif has_sources:
        return TierResult(
            score=0.5,
            reason=f"PARTIAL — có {len(sources)} sources nhưng answer không trích dẫn rõ ràng",
            details={"source_count": len(sources)},
        )
    else:
        # Kiểm tra xem có từ khóa pháp lý nào không
        legal_keywords = ["thuế", "quy định", "theo", "căn cứ", "điều", "khoản"]
        keyword_count = sum(1 for kw in legal_keywords if kw.lower() in answer.lower())
        if keyword_count >= 2:
            return TierResult(
                score=0.3,
                reason="PARTIAL — answer có từ khóa pháp lý nhưng thiếu citation cụ thể",
                details={"keyword_count": keyword_count},
            )
        return TierResult(
            score=0.0,
            reason="FAIL — answer không có tham chiếu pháp luật nào",
            details={"answer_preview": answer[:100]},
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


def eval_tier4_key_facts(result: dict, q_data: dict) -> TierResult:
    """
    Tier 4: Nếu câu hỏi đã được annotate với key_facts,
    kiểm tra answer có chứa đủ các số/từ khóa thiết yếu không.

    key_facts ví dụ: ["5%", "15%", "50 triệu", "125 triệu", "miễn"]
    Score: tỷ lệ key_facts khớp.  N/A nếu key_facts rỗng.
    """
    key_facts = q_data.get("key_facts", [])
    if not key_facts:
        return TierResult(score=None, reason="N/A — chưa có key_facts (cần annotate)")

    answer = result.get("answer", "")
    if not answer:
        return TierResult(score=0.0, reason="FAIL — answer rỗng", details={"key_facts": key_facts})

    matched  = [f for f in key_facts if _fact_in_answer(f, answer)]
    missing  = [f for f in key_facts if f not in matched]
    ratio    = len(matched) / len(key_facts)

    if ratio >= 0.9:
        return TierResult(
            score=1.0,
            reason=f"PASS — {len(matched)}/{len(key_facts)} key facts có trong answer",
            details={"matched": matched},
        )
    elif ratio >= 0.5:
        return TierResult(
            score=0.5,
            reason=f"PARTIAL — {len(matched)}/{len(key_facts)} key facts, thiếu: {missing}",
            details={"matched": matched, "missing": missing},
        )
    else:
        return TierResult(
            score=0.0,
            reason=f"FAIL — chỉ {len(matched)}/{len(key_facts)} key facts, thiếu: {missing}",
            details={"matched": matched, "missing": missing},
        )


def _fact_in_answer(fact: str, answer: str) -> bool:
    """
    Kiểm tra một key_fact có xuất hiện trong answer không.
    Xử lý các dạng biểu diễn khác nhau:
      "5%"       → "5%", "5 %", "5 phần trăm"
      "50 triệu" → "50 triệu", "50,000,000", "50000000"
      "miễn"     → "miễn", "không phải nộp"
    """
    answer_lower = answer.lower()
    fact_lower   = fact.lower().strip()

    # Direct match (case-insensitive)
    if fact_lower in answer_lower:
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
            for key in ["total_tax", "gtgt_payable", "tncn_payable", "tax_payable"]:
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
) -> list[EvalResult]:
    """Chạy đánh giá cho một danh sách câu hỏi."""
    results: list[EvalResult] = []
    total = len(questions)

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

        # ── Evaluate ─────────────────────────────────────────────────────────
        t1 = eval_tier1_deterministic(agent_result, needs_calc, q)
        t2 = eval_tier2_citation(agent_result, qtext)
        t3 = eval_tier3_tool_selection(agent_result, topic, needs_calc)
        t4 = eval_tier4_key_facts(agent_result, q)

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
        )

        status = "✅" if eval_res.passed else ("⚠️" if eval_res.overall_score >= 0.4 else "❌")
        print(f" | score={eval_res.overall_score:.2f} {status} | {latency_ms}ms")

        if verbose:
            _print_verbose(eval_res)

        results.append(eval_res)

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
                "note":       "Ground-truth check khi có expected_value; fallback tool-output check",
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
    print(f"  Passed (score≥0.67): {s['passed']} ({s['pass_rate']:.1%})")
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
    p.add_argument("--topic",       type=str,   default=None,  help="Lọc theo topic")
    p.add_argument("--difficulty",  type=str,   default=None,  choices=["easy", "medium", "hard"])
    p.add_argument("--needs-calc",  action="store_true",       help="Chỉ câu cần tính toán")
    p.add_argument("--annotated",   action="store_true",       help="Chỉ câu đã có annotation (auto/verified)")
    p.add_argument("--output",      type=str,   default=None,  help="Tên file JSON output")
    p.add_argument("--verbose",     action="store_true",       help="In chi tiết từng câu")
    p.add_argument("--dry-run",     action="store_true",       help="Kiểm tra setup, không chạy")
    p.add_argument("--model",       type=str,   default=None,  help="Override Gemini model")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # ── Load questions ────────────────────────────────────────────────────────
    if not QUESTIONS_PATH.exists():
        print(f"❌ Questions file not found: {QUESTIONS_PATH}")
        sys.exit(1)

    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        all_questions: list[dict] = json.load(f)

    print(f"✅ Loaded {len(all_questions)} questions from {QUESTIONS_PATH.name}")

    # ── Filter ────────────────────────────────────────────────────────────────
    questions = all_questions

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

    if args.limit:
        questions = questions[:args.limit]
        print(f"   Limited to first {args.limit} questions")

    if not questions:
        print("❌ No questions after filtering")
        sys.exit(1)

    # ── Dry run ───────────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\n✅ Dry run OK — would evaluate {len(questions)} questions")
        print("   Sample questions:")
        for q in questions[:3]:
            print(f"   [{q['id']}] {q['question'][:70]}")
        return

    # ── Load agent ────────────────────────────────────────────────────────────
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

    # ── Run evaluation ────────────────────────────────────────────────────────
    print(f"\n🚀 Starting evaluation — {len(questions)} questions\n")
    results = run_evaluation(questions, agent, verbose=args.verbose)

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
