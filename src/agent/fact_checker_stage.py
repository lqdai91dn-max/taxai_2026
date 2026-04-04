"""
src/agent/fact_checker_stage.py — Stage 4: Fact Checker

Kiểm tra tính nhất quán giữa GeneratorOutput và RetrievalOutput.
Hoàn toàn deterministic — 0 API call, ~0ms latency.

Hai lớp kiểm tra:
  Layer A — Key fact presence (substring / token match):
    Mỗi key_fact trong GeneratorOutput.key_facts phải tìm thấy được
    trong ít nhất 1 chunk của RetrievalOutput.
    → Miss = WARNING only (không block, không trigger FM03)
    Lý do: false negatives (paraphrase, unit formatting) gây cascade
    FM03→FM04→L2 degrade→T4=0.0 nhiều hơn là bắt được hallucination thật.

  Layer B — Numeric/polarity consistency (dùng logic từ retrieval/fact_checker.py):
    Comparator không được flip (trên ↔ dưới).
    Polarity không được mâu thuẫn (miễn ↔ phải nộp).
    → Fail = CRITICAL → trigger FM03 regeneration / FM04 degrade

Severity:
  critical  = polarity flip OR comparator flip (Layer B only)
              → trigger FM03 regeneration (nếu regeneration_count == 0)
              → trigger FM04 Level 2 degrade (nếu regeneration_count == 1)
  warning   = key_fact miss (Layer A) hoặc số liệu không verify được
              → log only, không block

Output:
  FactCheckOutput.passed = True  → tiếp tục DONE
  FactCheckOutput.passed = False, has_critical = True  → regenerate / degrade
"""

from __future__ import annotations

import logging
import re
import time
from typing import List

from src.agent.schemas import (
    FactCheckOutput,
    FactIssue,
    GeneratorOutput,
    PipelineState,
    RetrievalOutput,
)
from src.retrieval.fact_checker import check_facts as _legacy_check_facts

logger = logging.getLogger(__name__)


# ── Key fact substring matching ────────────────────────────────────────────────

def _normalize_for_match(text: str) -> str:
    """
    Normalize text cho substring matching:
    - lowercase
    - expand Vietnamese monetary units: "500 triệu" → "500000000"
    - bỏ dấu câu thừa (dấu chấm, phẩy ngăn cách số)
    - collapse whitespace
    """
    text = text.lower()
    # Expand monetary units BEFORE stripping separators
    # "500 triệu" → "500000000", "1,5 tỷ" → "1500000000"
    def _expand_monetary(m: re.Match) -> str:
        num_str = re.sub(r"[.,]", "", m.group(1))  # "1,5" → "15" (temporary)
        decimal_str = m.group(2)                    # "5" nếu có phần thập phân
        unit = m.group(3).strip()
        try:
            if decimal_str:
                # "1,5 triệu" → num=1, decimal=5 → 1.5 million
                base = int(num_str[: -len(decimal_str)])
                frac = int(decimal_str)
                value = float(f"{base}.{frac}")
            else:
                value = float(num_str)
            if unit in ("tỷ",):
                result = int(value * 1_000_000_000)
            else:  # triệu
                result = int(value * 1_000_000)
            return str(result)
        except ValueError:
            return m.group(0)

    text = re.sub(
        r"(\d+)(?:[.,](\d+))?\s*(tỷ|triệu)\b",
        _expand_monetary,
        text,
    )
    # Normalize number separators: "1.000.000" hoặc "1,000,000" → "1000000"
    text = re.sub(r"(\d)[.,](\d{3})", r"\1\2", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _key_fact_found(fact: str, chunk_texts: List[str]) -> bool:
    """
    Kiểm tra key_fact có xuất hiện trong ít nhất 1 chunk không.

    Strategy:
    1. Exact normalized substring match (sau khi expand monetary units)
    2. Nếu fact chứa số (≥3 chữ số) → check số có trong chunks
    3. Nếu fact là cụm từ (không có số) → token-based: 70%+ terms xuất hiện
       trong ít nhất 1 chunk (xử lý paraphrase nhẹ)
    """
    norm_fact = _normalize_for_match(fact)

    # Cache normalized chunks (tránh normalize nhiều lần)
    norm_chunks = [_normalize_for_match(ct) for ct in chunk_texts]

    # Strategy 1: full substring
    for nc in norm_chunks:
        if norm_fact in nc:
            return True

    # Strategy 2: numeric presence — check số ≥3 chữ số
    nums = re.findall(r"\d+(?:[.,]\d+)*", fact)
    if nums:
        all_chunks_joined = " ".join(norm_chunks)
        for n in nums:
            n_norm = re.sub(r"[.,]", "", n)
            if len(n_norm) >= 3:   # bỏ qua số 1-2 chữ số (Điều X, Khoản Y)
                if n_norm in all_chunks_joined:
                    return True

    # Strategy 3: token-based for phrase facts (không có số dài)
    # Nếu ≥70% key terms (len>3) xuất hiện trong ít nhất 1 chunk → found
    key_terms = [w for w in norm_fact.split() if len(w) > 3]
    if len(key_terms) >= 2:
        threshold = 0.7
        for nc in norm_chunks:
            matched = sum(1 for t in key_terms if t in nc)
            if matched / len(key_terms) >= threshold:
                return True

    return False


# ── Layer B wrapper ────────────────────────────────────────────────────────────

def _run_legacy_check(answer: str, chunks: List[str]) -> List[FactIssue]:
    """
    Wrap legacy fact_checker.check_facts() — convert sang FactIssue list.

    Legacy check_facts nhận tool_calls list; ta fake format cho nó.
    """
    # Fake tool_calls format mà legacy code mong đợi
    fake_tool_calls = [
        {
            "tool": "search_legal_docs",
            "result": {
                "results": [{"snippet": c} for c in chunks]
            },
        }
    ]

    result = _legacy_check_facts(answer, fake_tool_calls)

    issues: List[FactIssue] = []
    for issue_str in result.issues:
        # Phân loại severity theo type
        if "Comparator flip" in issue_str or "Polarity" in issue_str:
            severity = "critical"
        else:
            severity = "warning"

        issues.append(FactIssue(
            severity=severity,
            key_fact=issue_str,
            description=issue_str,
        ))

    return issues


# ── FactCheckerStage ───────────────────────────────────────────────────────────

class FactCheckerStage:
    """
    Stage 4: Deterministic fact verification.

    Không cần constructor params — 0 external dependencies.
    """

    def run(self, state: PipelineState) -> FactCheckOutput:
        """
        Chạy Stage 4 Fact Checker.

        Returns:
            FactCheckOutput với passed / issues / has_critical.
        """
        t0 = time.perf_counter()

        gen_out: GeneratorOutput | None = state.generator_output
        ret_out: RetrievalOutput | None = state.retrieval_output

        # Không có gì để check → pass
        if not gen_out or not gen_out.answer_text:
            logger.debug("FactChecker: no generator output — skip")
            return FactCheckOutput(passed=True, issues=[], has_critical=False)

        if not ret_out or not ret_out.chunks:
            logger.debug("FactChecker: no chunks to verify against — skip")
            return FactCheckOutput(passed=True, issues=[], has_critical=False)

        # Lấy text từ winning chunks (bỏ qua losers đã resolve ở Stage 2)
        from src.agent.schemas import ConflictPair
        losing_ids: set[str] = set()
        if ret_out.has_conflict:
            for cp in ret_out.conflicts:
                loser = cp.chunk_id_a if cp.winner_id == cp.chunk_id_b else cp.chunk_id_b
                losing_ids.add(loser)

        chunk_texts = [
            c.text for c in ret_out.chunks
            if c.chunk_id not in losing_ids
        ]

        all_issues: List[FactIssue] = []

        # ── Layer A: Key fact presence ─────────────────────────────────────────
        for fact in gen_out.key_facts:
            if not fact.strip():
                continue
            # Bỏ qua key_facts quá dài — đây là LLM conclusions, không phải chunk substrings
            if len(fact) > 80 and not re.search(r"\d", fact):
                logger.debug("FactChecker: skip long/conclusion key_fact: %s", fact[:60])
                continue
            found = _key_fact_found(fact, chunk_texts)
            if not found:
                # WARNING only — không trigger FM03 regeneration.
                # False negatives (paraphrase/formatting) gây FM03→FM04 cascade
                # nhiều hơn là bắt được hallucination thật. Layer B xử lý
                # numeric contradictions chính xác hơn.
                all_issues.append(FactIssue(
                    severity="warning",
                    key_fact=fact,
                    description=f"Key fact không verify được trong chunks: '{fact}'",
                ))
                logger.debug("FactChecker: key_fact unverified (warning): %s", fact[:80])

        # ── Layer B: Numeric + Polarity consistency ────────────────────────────
        b_issues = _run_legacy_check(gen_out.answer_text, chunk_texts)
        all_issues.extend(b_issues)

        # ── Kết luận ──────────────────────────────────────────────────────────
        has_critical = any(i.severity == "critical" for i in all_issues)
        passed = not has_critical  # Layer A = warning only, không block

        latency_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "FactChecker: %s in %dms | issues=%d (critical=%d, warning=%d)",
            "PASS" if passed else "FAIL",
            latency_ms,
            len(all_issues),
            sum(1 for i in all_issues if i.severity == "critical"),
            sum(1 for i in all_issues if i.severity == "warning"),
        )

        return FactCheckOutput(
            passed=passed,
            issues=all_issues,
            has_critical=has_critical,
        )
