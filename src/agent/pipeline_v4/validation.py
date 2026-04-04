"""
src/agent/pipeline_v4/validation.py — Validation Layer (Python, deterministic)

Kiểm tra output của LLM Legal Reasoner (step 6.1) trước khi đưa vào Calculator.

Checks:
1. Template consistency  — template_type hợp lệ với VALID_TEMPLATE_COMBINATIONS
2. Coverage check        — COVERAGE_RULES[template] có đủ params không
3. Citation binding      — source của mỗi param phải trỏ về retrieved_chunks
4. Clarification gate    — nếu LLM đánh dấu clarification_needed → short-circuit

Anti-patterns:
  - KHÔNG dùng LLM để validate (dùng Python only)
  - KHÔNG put required_conditions vào NodeMetadata (coupling sai tầng)
  - Không block nếu chỉ thiếu optional params
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ─── VALID_TEMPLATE_COMBINATIONS ─────────────────────────────────────────────
# Mapping: template_type → điều kiện hợp lệ
# MVP: static dict. Full Production: upgrade to dynamic dependency graph.

VALID_TEMPLATE_COMBINATIONS: Dict[str, dict] = {
    "PIT_progressive": {
        "tax_domain": ["PIT"],
        "who":        ["individual", "employee"],
    },
    "PIT_full": {
        "tax_domain": ["PIT"],
        "who":        ["individual", "employee"],
    },
    "PIT_flat_20": {
        "tax_domain": ["PIT"],
        "who":        ["individual"],
        "requires":   {"is_resident": False},   # non-resident only
    },
    "HKD_percentage": {
        "tax_domain": ["HKD"],
        "who":        ["HKD"],
    },
    "HKD_profit": {
        "tax_domain": ["HKD"],
        "who":        ["HKD"],
        "requires":   {"revenue_gt": 3_000_000_000},  # revenue > 3B → bắt buộc
    },
    "deduction_calc": {
        "tax_domain": ["PIT"],
        "who":        ["individual", "employee"],
    },
}


# ─── COVERAGE_RULES ───────────────────────────────────────────────────────────
# Mapping: template_type → list params bắt buộc phải có trong params_validated
# MVP: static. Full Production: branching conditions (is_resident etc).

COVERAGE_RULES: Dict[str, List[str]] = {
    "PIT_progressive": ["annual_taxable_income"],
    "PIT_full":        ["gross_income"],
    "PIT_flat_20":     ["gross_income"],
    "HKD_percentage":  ["annual_revenue", "business_category"],
    "HKD_profit":      ["annual_revenue", "annual_expenses", "business_category"],
    "deduction_calc":  [],  # dependents + months có defaults
}


# ─── ValidationResult ────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid:    bool
    errors:   List[str]
    warnings: List[str]

    # Shortcut action cho orchestrator
    clarification_needed: bool = False
    clarification_question: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.valid and not self.clarification_needed


# ─── Main validator ───────────────────────────────────────────────────────────

def validate_reasoner_output(
    template_type:    str,
    params_validated: Dict[str, dict],   # {param: {value, source}}
    clarification_needed: bool,
    clarification_question: Optional[str],
    retrieved_chunk_ids: Optional[Set[str]] = None,
) -> ValidationResult:
    """
    Validate output từ LLM Legal Reasoner.

    Args:
        template_type:          Template LLM chọn.
        params_validated:       {param_name: {"value": ..., "source": chunk_id_or_null}}.
        clarification_needed:   LLM flag — nếu True → short-circuit → hỏi user.
        clarification_question: Câu hỏi đề xuất nếu clarification_needed.
        retrieved_chunk_ids:    Set chunk_ids từ retrieval (dùng cho citation binding).

    Returns:
        ValidationResult.
    """
    errors:   List[str] = []
    warnings: List[str] = []

    # ── Check 0: Clarification gate ──────────────────────────────────────────
    if clarification_needed:
        return ValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            clarification_needed=True,
            clarification_question=clarification_question,
        )

    # ── Check 1: Template tồn tại ────────────────────────────────────────────
    if template_type not in VALID_TEMPLATE_COMBINATIONS:
        errors.append(
            f"template_type không hợp lệ: {template_type!r}. "
            f"Hợp lệ: {list(VALID_TEMPLATE_COMBINATIONS)}"
        )

    # ── Check 2: Coverage — params bắt buộc có đủ không ─────────────────────
    required_params = COVERAGE_RULES.get(template_type, [])
    for param in required_params:
        pdata = params_validated.get(param)
        if pdata is None or pdata.get("value") is None:
            errors.append(
                f"Coverage fail: param '{param}' bắt buộc cho {template_type} "
                f"nhưng không có trong params_validated."
            )

    # ── Check 3: Citation binding — source hợp lệ ───────────────────────────
    # Nếu retrieved_chunk_ids được cung cấp, kiểm tra source của mỗi param
    if retrieved_chunk_ids is not None:
        for param, pdata in params_validated.items():
            source = pdata.get("source")
            if source and source not in retrieved_chunk_ids:
                warnings.append(
                    f"Citation binding: source '{source}' của param '{param}' "
                    f"không có trong retrieved chunks — giá trị có thể không có căn cứ."
                )

    # ── Check 4: Null values warning ─────────────────────────────────────────
    for param, pdata in params_validated.items():
        if pdata.get("value") is None and param not in required_params:
            warnings.append(f"Optional param '{param}' là None — sẽ dùng default.")

    valid = len(errors) == 0
    if errors:
        logger.warning("Validation failed for template=%s: %s", template_type, errors)
    return ValidationResult(valid=valid, errors=errors, warnings=warnings)


def extract_calc_params(params_validated: Dict[str, dict]) -> dict:
    """
    Chuyển đổi params_validated (từ LLM) thành flat dict cho Template Registry.

    Input:  {"annual_revenue": {"value": 1200000000, "source": "chunk_123"}, ...}
    Output: {"annual_revenue": 1200000000, ...}

    Chỉ include params có value không None.
    """
    return {
        param: pdata["value"]
        for param, pdata in params_validated.items()
        if pdata.get("value") is not None
    }
