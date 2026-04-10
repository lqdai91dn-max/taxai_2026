"""
scripts/smoke_test_v4.py — Pipeline v4 Smoke Test

Chạy 8 test cases đại diện cho các kịch bản chính:
  1. HKD bán hàng 1.2 tỷ (happy path)
  2. PIT lương 30 triệu 1 người phụ thuộc (happy path)
  3. HKD tiệm vàng 60 tỷ (high-value, goods category)
  4. PIT xổ số 500 triệu (non-progressive template)
  5. Thiếu dữ liệu — clarification trigger (Shopee)
  6. Domain mơ hồ (freelancer)
  7. Threshold query (ngưỡng chịu thuế)
  8. Math hallucination trap (LLM tự tính → phải detect)

Usage:
    python scripts/smoke_test_v4.py
    python scripts/smoke_test_v4.py --limit 3
    python scripts/smoke_test_v4.py --log results/smoke_v4.json
    python scripts/smoke_test_v4.py --query "câu hỏi tuỳ ý"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root on path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ─── Test cases ───────────────────────────────────────────────────────────────

TEST_CASES: List[Dict[str, Any]] = [
    {
        "id": "TC01",
        "scenario": "HKD bán hàng hoá 1.2 tỷ — happy path",
        "query": "Tôi kinh doanh hàng hoá, doanh thu năm 2026 là 1.2 tỷ đồng. Thuế khoán phải nộp bao nhiêu?",
        "expect_template": "HKD_percentage",
        "expect_clarification": False,
        "expect_tax_present": True,
        "expect_no_compute": True,
    },
    {
        "id": "TC02",
        "scenario": "PIT lương 30 triệu 1 người phụ thuộc — happy path",
        "query": "Lương tháng của tôi 30 triệu đồng, có 1 người phụ thuộc. Tôi phải đóng thuế TNCN bao nhiêu?",
        "expect_template": "PIT_full",
        "expect_clarification": False,
        "expect_tax_present": True,
        "expect_no_compute": True,
    },
    {
        "id": "TC03",
        "scenario": "HKD tiệm vàng 60 tỷ — high-value goods",
        "query": "Tiệm vàng của tôi doanh thu 60 tỷ năm 2026, tính thuế thế nào?",
        "expect_template": "HKD_percentage",
        "expect_clarification": False,
        "expect_tax_present": True,
        "expect_no_compute": True,
    },
    {
        "id": "TC04",
        "scenario": "PIT xổ số 500 triệu — withholding tax",
        "query": "Tôi trúng xổ số 500 triệu đồng. Thuế TNCN phải nộp là bao nhiêu?",
        "expect_template": None,          # template có thể là WITHHOLDING hoặc explain
        "expect_clarification": False,
        "expect_tax_present": False,      # có thể chỉ giải thích tỷ lệ
        "expect_no_compute": True,
    },
    {
        "id": "TC05",
        "scenario": "Thiếu dữ liệu — clarification trigger (Shopee)",
        "query": "Tôi bán hàng trên Shopee thì phải đóng thuế bao nhiêu?",
        "expect_template": None,
        "expect_clarification": True,     # phải hỏi lại doanh thu
        "expect_tax_present": False,
        "expect_no_compute": True,
    },
    {
        "id": "TC06",
        "scenario": "Domain mơ hồ — freelancer thiết kế",
        "query": "Tôi làm freelance thiết kế đồ hoạ, thu nhập 15 triệu/tháng. Thuế tôi phải nộp là gì?",
        "expect_template": None,         # PIT_full hoặc HKD_percentage đều hợp lệ cho freelancer
        "expect_clarification": False,
        "expect_tax_present": True,
        "expect_no_compute": True,
    },
    {
        "id": "TC07",
        "scenario": "Threshold query — ngưỡng không phải nộp thuế",
        "query": "Doanh thu bao nhiêu thì hộ kinh doanh không phải nộp thuế?",
        "expect_template": None,          # explain query, no calculation
        "expect_clarification": False,
        "expect_tax_present": False,
        "expect_no_compute": True,
    },
    {
        "id": "TC08",
        "scenario": "Math hallucination trap — LLM tự tính",
        "query": (
            "Lương 30 triệu, giảm trừ bản thân 11 triệu, 1 người phụ thuộc 4.4 triệu. "
            "Tôi tự tính ra phải đóng 2 triệu tiền thuế đúng không?"
        ),
        "expect_template": "PIT_full",
        "expect_clarification": None,     # LLM có thể hỏi luật cũ/mới — cả 2 đều ok
        "expect_tax_present": False,      # Nếu hỏi lại thì chưa có thuế; nếu tính thì có
        "expect_no_compute": True,        # LLM phải KHÔNG xác nhận số 2 triệu
    },
]


# ─── Result checker ───────────────────────────────────────────────────────────

def _check_result(
    tc: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    So sánh kết quả pipeline với expectation của test case.
    Returns dict với pass/fail per check.
    """
    checks: Dict[str, Any] = {}
    error = result.get("error")

    # Check 1: No crash
    checks["no_crash"] = error is None

    if error:
        return checks

    # Check 2: Template match (if expected)
    if tc["expect_template"] is not None:
        actual_template = result.get("template")
        checks["template_match"] = actual_template == tc["expect_template"]
    else:
        checks["template_match"] = True  # skip

    # Check 3: Clarification (None = không check)
    actual_clarif = result.get("clarification", False)
    if tc["expect_clarification"] is None:
        checks["clarification_triggered"] = True   # skip
    elif tc["expect_clarification"]:
        checks["clarification_triggered"] = actual_clarif is True
    else:
        checks["clarification_triggered"] = actual_clarif is False or actual_clarif is None

    # Check 4: Tax amount present in answer (if expected)
    answer = result.get("final_answer", "") or ""
    if tc["expect_tax_present"]:
        import re
        # "VND", "VNĐ", "đồng", "triệu", "tỷ" — Gemini đôi khi viết VNĐ thay VND
        tax_present = bool(re.search(r"\d[\d,\.]*\s*(?:VNĐ|VND|đồng|triệu|tỷ)", answer, re.IGNORECASE))
        checks["tax_amount_in_answer"] = tax_present
    else:
        checks["tax_amount_in_answer"] = True  # skip

    # Check 5: No LLM computation in reasoner output
    if tc["expect_no_compute"]:
        violations = result.get("compute_violations", [])
        checks["no_llm_compute"] = len(violations) == 0
    else:
        checks["no_llm_compute"] = True

    # Check 6: JSON parse succeeded (meta)
    meta_reasoner = result.get("meta_reasoner", {})
    checks["json_parse_ok"] = meta_reasoner.get("parse_ok", False) is True

    return checks


# ─── Run single query via pipeline ───────────────────────────────────────────

def _get_searcher():
    """Lazy-init hybrid searcher từ existing pipeline."""
    from src.retrieval.hybrid_search import HybridSearch
    return HybridSearch()


def run_query(query: str, session_id: str) -> Dict[str, Any]:
    """
    Gọi Pipeline v4 và trả về structured result dict.
    """
    from src.agent.pipeline_v4.orchestrator import PipelineV4

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return {"error": "GOOGLE_API_KEY not set", "query": query}

    try:
        searcher = _get_searcher()
        pipeline = PipelineV4(searcher=searcher, api_key=api_key)
        output = pipeline.run(question=query, session_id=session_id)

        return {
            "query":                  query,
            "final_answer":           output.get("answer"),
            "template":               output.get("template"),
            "tax_amount":             output.get("tax_amount"),
            "clarification":          output.get("clarification_needed", False),
            "clarification_question": output.get("clarification_question"),
            "compute_violations":     output.get("compute_violations", []),
            "meta_reasoner":          output.get("meta_reasoner", {}),
            "intent_summary":         output.get("intent_summary"),
            "top_chunks":             output.get("top_chunks", []),
            "degrade_level":          output.get("degrade_level"),
            "latency_ms":             output.get("latency_ms", 0),
            "error":                  output.get("error"),
        }

    except Exception as exc:
        import traceback
        return {
            "query":      query,
            "error":      str(exc),
            "traceback":  traceback.format_exc(),
            "latency_ms": 0,
        }


# ─── Pretty print ─────────────────────────────────────────────────────────────

_GREEN = "\033[92m"
_RED   = "\033[91m"
_YELLOW = "\033[93m"
_RESET = "\033[0m"
_BOLD  = "\033[1m"


def _color(text: str, color: str) -> str:
    if sys.stdout.isatty():
        return f"{color}{text}{_RESET}"
    return text


def print_result(tc: Dict[str, Any], result: Dict[str, Any], checks: Dict[str, Any]) -> None:
    all_pass = all(v for v in checks.values())
    status = _color("PASS", _GREEN) if all_pass else _color("FAIL", _RED)
    latency = result.get("latency_ms", 0)

    print(f"\n{'─'*70}")
    print(f"[{tc['id']}] {tc['scenario']}")
    print(f"  Status : {status}  ({latency}ms)")

    if result.get("error"):
        print(f"  Error  : {_color(result['error'], _RED)}")
        return

    print(f"  Template  : {result.get('template', '—')}")
    print(f"  Tax amount: {result.get('tax_amount', '—')}")
    if result.get("clarification"):
        print(f"  Clarif Q  : {_color(result.get('clarification_question', ''), _YELLOW)}")

    # Checks
    for check_name, passed in checks.items():
        icon = _color("✓", _GREEN) if passed else _color("✗", _RED)
        print(f"  {icon} {check_name}")

    # Show truncated answer
    answer = result.get("final_answer", "") or ""
    if answer:
        preview = answer[:200].replace("\n", " ")
        if len(answer) > 200:
            preview += "..."
        print(f"  Answer : {preview}")

    # Compute violations
    violations = result.get("compute_violations", [])
    if violations:
        print(f"  {_color('COMPUTE VIOLATIONS:', _RED)} {violations}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline v4 Smoke Test")
    parser.add_argument("--limit", type=int, default=None, help="Chạy N test cases đầu tiên")
    parser.add_argument("--log", type=str, default=None, help="Ghi kết quả JSON vào file")
    parser.add_argument("--query", type=str, default=None, help="Chạy 1 query tùy ý")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG logs")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Check API key
    if not os.environ.get("GOOGLE_API_KEY"):
        print(_color("\nERROR: GOOGLE_API_KEY không được set.", _RED))
        print("Hãy chạy: export GOOGLE_API_KEY=your_key_here")
        sys.exit(1)

    # Single ad-hoc query mode
    if args.query:
        print(f"\nRunning ad-hoc query: {args.query}")
        result = run_query(args.query, session_id="adhoc_001")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Batch test mode
    cases = TEST_CASES[:args.limit] if args.limit else TEST_CASES
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(_color(f"\n{'='*70}", _BOLD))
    print(_color(f"  Pipeline v4 Smoke Test — {len(cases)} test cases", _BOLD))
    print(_color(f"  {timestamp}", _BOLD))
    print(_color(f"{'='*70}", _BOLD))

    all_results = []
    total_pass = 0
    total_checks = 0
    pass_checks = 0

    for i, tc in enumerate(cases, 1):
        print(f"\nRunning [{tc['id']}] {tc['scenario']}...")
        session_id = f"smoke_{timestamp}_{tc['id']}"

        result = run_query(tc["query"], session_id)
        checks = _check_result(tc, result)
        print_result(tc, result, checks)

        tc_pass = all(v for v in checks.values())
        if tc_pass:
            total_pass += 1

        pass_checks += sum(1 for v in checks.values() if v)
        total_checks += len(checks)

        all_results.append({
            "tc": tc,
            "result": result,
            "checks": checks,
            "pass": tc_pass,
        })

    # Summary
    print(f"\n{'='*70}")
    print(f"Summary: {total_pass}/{len(cases)} test cases PASS")
    print(f"         {pass_checks}/{total_checks} individual checks PASS")

    if total_pass == len(cases):
        print(_color("  ALL PASS ✓", _GREEN))
    else:
        failed = [r["tc"]["id"] for r in all_results if not r["pass"]]
        print(_color(f"  FAILED: {', '.join(failed)}", _RED))

    # Write log
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": timestamp,
                "total_cases": len(cases),
                "total_pass": total_pass,
                "results": all_results,
            }, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nLog saved: {log_path}")


if __name__ == "__main__":
    main()
