"""
src/agent/template_registry.py — Computation Template Registry (MVP)

Wraps existing calculator functions với:
1. Input schema validation (required fields + sanity constraints)
2. Output normalization (TemplateResult schema chuẩn)
3. Deterministic rounding rules (per law, không dùng Python banker's round)
4. Template versioning (tracing legal source)

Usage (từ Pipeline v4 Python Calculator step):
    result = run_template("HKD_percentage", {
        "annual_revenue":    1_200_000_000,
        "business_category": "goods",   # LLM extracts from RAG
    })
    # result.tax_amount  → int VND (floor_100)
    # result.citations   → list[dict] trỏ về 68/2026/NĐ-CP
    # result.warnings    → ["Doanh thu >3 tỷ..."] nếu có

Anti-patterns:
  - KHÔNG hardcode tax rates tại đây (rates trong calculator_tools.py có citations)
  - KHÔNG gọi LLM từ đây (pure Python, deterministic)
  - KHÔNG dùng template trước khi có legal reasoning output
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.tools.calculator_tools import (
    calculate_deduction,
    calculate_tax_hkd,
    calculate_tax_hkd_profit,
    calculate_tncn_progressive,
)

logger = logging.getLogger(__name__)


# ─── Rounding Rules (theo luật, KHÔNG dùng Python round() mặc định) ─────────
# PIT: làm tròn xuống 1,000 VND — Thông tư 111/2013/TT-BTC, Điều 7
# HKD: làm tròn xuống 100 VND — Nghị định 68/2026/NĐ-CP

def _floor_1000(x: float) -> int:
    return (int(x) // 1000) * 1000

def _floor_100(x: float) -> int:
    return (int(x) // 100) * 100


# ─── Sanity Caps ──────────────────────────────────────────────────────────────
_MAX_REVENUE    = 1_000_000_000_000   # 1 nghìn tỷ VND
_MAX_INCOME     = 100_000_000_000     # 100 tỷ VND (cá nhân)
_MAX_DEPENDENTS = 20


# ─── TemplateResult — normalized output schema ────────────────────────────────

@dataclass
class TemplateResult:
    """
    Output chuẩn từ mọi template.

    tax_amount: đã làm tròn theo luật (floor_1000 cho PIT, floor_100 cho HKD).
    breakdown:  chi tiết từ underlying calculator (GTGT + TNCN, hoặc từng bậc lũy tiến).
    citations:  list các văn bản pháp luật nguồn (doc_id, doc_number, title).
    assumptions: điều kiện giả định (LLM Legal Reasoner điền, Template giữ nguyên).
    raw:        full output của calculator function (dùng cho audit trail).
    """
    tax_amount:     int
    breakdown:      Any                     # list[dict] hoặc dict
    effective_rate: float
    currency:       str                     = "VND"
    template:       str                     = ""
    version:        str                     = ""
    assumptions:    List[str]               = field(default_factory=list)
    citations:      List[dict]              = field(default_factory=list)
    warnings:       List[str]               = field(default_factory=list)
    raw:            dict                    = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tax_amount":     self.tax_amount,
            "breakdown":      self.breakdown,
            "effective_rate": self.effective_rate,
            "currency":       self.currency,
            "template":       self.template,
            "version":        self.version,
            "assumptions":    self.assumptions,
            "citations":      self.citations,
            "warnings":       self.warnings,
        }


# ─── Template class ───────────────────────────────────────────────────────────

class Template:
    """
    Named, versioned computation unit.

    Mỗi template:
    - Khai báo required params (enforcement tại runtime)
    - Chạy validator trước khi execute (sanity + domain rules)
    - Chuẩn hóa output thành TemplateResult
    - Áp dụng rounding rule theo luật
    """

    def __init__(
        self,
        name:      str,
        version:   str,
        required:  List[str],
        executor:  Callable[..., dict],
        validator: Optional[Callable[[dict], None]] = None,
        rounder:   Optional[Callable[[float], int]] = None,
    ):
        self.name      = name
        self.version   = version
        self.required  = required
        self.executor  = executor
        self.validator = validator or (lambda p: None)
        self.rounder   = rounder or (lambda x: int(round(x)))

        # Cache executor param names để lọc kwargs
        sig = inspect.signature(self.executor)
        self._executor_param_names = set(sig.parameters.keys())

    def run(self, params: dict, assumptions: Optional[List[str]] = None) -> TemplateResult:
        """
        Chạy template với params đã qua Validation Layer.

        Args:
            params:      Dict params từ LLM Legal Reasoner.
            assumptions: List giả định từ LLM (được ghi vào TemplateResult).

        Returns:
            TemplateResult với tax_amount, breakdown, citations, warnings.

        Raises:
            ValueError: params không hợp lệ (missing required, out of range).
        """
        # 1. Required fields
        missing = [f for f in self.required if f not in params or params[f] is None]
        if missing:
            raise ValueError(f"[{self.name}] Thiếu params bắt buộc: {missing}")

        # 2. Domain validation
        self.validator(params)

        # 3. Execute (chỉ pass params mà executor nhận)
        filtered = {k: params[k] for k in self._executor_param_names if k in params}
        raw = self.executor(**filtered)

        # 4. Normalize
        result = self._normalize(raw, params)
        result.template    = self.name
        result.version     = self.version
        result.assumptions = assumptions or []
        return result

    def _normalize(self, raw: dict, params: dict) -> TemplateResult:
        # tax_amount: ưu tiên total_tax (HKD), sau đó tax_payable (TNCN progressive)
        raw_total = (
            raw.get("total_tax")
            or raw.get("tax_payable")
            or raw.get("total_deduction_annual")
            or 0.0
        )
        tax_amount = self.rounder(float(raw_total))

        # effective_rate
        eff = raw.get("effective_rate") or 0.0
        if not eff:
            base = params.get("annual_revenue") or params.get("annual_taxable_income") or 1
            eff  = tax_amount / base if base else 0.0

        # citations: thu thập từ "citation" (single) và "breakdown" items
        citations: List[dict] = []
        if "citation" in raw and raw["citation"]:
            c = raw["citation"]
            if isinstance(c, dict):
                citations.append(c)
            elif isinstance(c, list):
                citations.extend(c)
        if "breakdown" in raw:
            for item in (raw["breakdown"] or []):
                c = item.get("citation")
                if c and isinstance(c, dict) and c not in citations:
                    citations.append(c)

        return TemplateResult(
            tax_amount     = tax_amount,
            breakdown      = raw.get("breakdown", []),
            effective_rate = round(eff, 4),
            warnings       = raw.get("warnings", []),
            citations      = citations,
            raw            = raw,
        )


# ─── Validators ───────────────────────────────────────────────────────────────

_VALID_BUSINESS_CATEGORIES = {"goods", "services", "manufacturing", "other", "real_estate"}


def _validate_pit(params: dict) -> None:
    income = float(params.get("annual_taxable_income", 0))
    if income < 0:
        raise ValueError("annual_taxable_income không được âm")
    if income > _MAX_INCOME:
        raise ValueError(
            f"annual_taxable_income {income/1e9:.1f} tỷ vượt sanity cap "
            f"({_MAX_INCOME/1e9:.0f} tỷ) — kiểm tra lại đơn vị nhập"
        )


def _validate_hkd(params: dict) -> None:
    rev = float(params.get("annual_revenue", 0))
    if rev < 0:
        raise ValueError("annual_revenue không được âm")
    if rev > _MAX_REVENUE:
        raise ValueError(
            f"annual_revenue {rev/1e9:.1f} tỷ vượt sanity cap "
            f"({_MAX_REVENUE/1e9:.0f} tỷ) — kiểm tra lại đơn vị nhập"
        )
    cat = params.get("business_category", "")
    if cat not in _VALID_BUSINESS_CATEGORIES:
        raise ValueError(
            f"business_category không hợp lệ: {cat!r}. "
            f"Hợp lệ: {sorted(_VALID_BUSINESS_CATEGORIES)}"
        )


def _validate_hkd_profit(params: dict) -> None:
    _validate_hkd(params)
    rev = float(params.get("annual_revenue", 0))
    exp = float(params.get("annual_expenses", 0))
    if exp < 0:
        raise ValueError("annual_expenses không được âm")
    if exp >= rev:
        raise ValueError(
            f"annual_expenses ({exp:,.0f}) phải nhỏ hơn annual_revenue ({rev:,.0f})"
        )


def _validate_deduction(params: dict) -> None:
    d = int(params.get("dependents", 0))
    m = int(params.get("months", 12))
    if d < 0 or d > _MAX_DEPENDENTS:
        raise ValueError(f"dependents phải từ 0 đến {_MAX_DEPENDENTS}, nhận {d}")
    if not 1 <= m <= 12:
        raise ValueError(f"months phải từ 1 đến 12, nhận {m}")


# ─── PIT Full compound executor ───────────────────────────────────────────────

def _execute_pit_full(
    gross_income: float,
    dependents: int = 0,
    months: int = 12,
) -> dict:
    """
    Compound executor: deduction → taxable_income → progressive tax.

    LLM Legal Reasoner passes (gross_income, dependents) →
    Python Calculator handles full pipeline.
    """
    # Step 1: deduction
    ded_result = calculate_deduction(dependents=dependents, months=months)
    deduction_annual = ded_result["total_deduction_annual"]

    # Step 2: taxable income
    taxable_income = max(0.0, gross_income - deduction_annual)

    # Step 3: progressive
    tax_result = calculate_tncn_progressive(annual_taxable_income=taxable_income)

    # Merge output
    return {
        "gross_income":        gross_income,
        "deduction_annual":    deduction_annual,
        "annual_taxable_income": taxable_income,
        "tax_payable":         tax_result["tax_payable"],
        "effective_rate":      tax_result["effective_rate"],
        "brackets":            tax_result["brackets"],
        "breakdown": [
            {
                "tax_type":    "Giảm trừ",
                "description": ded_result["summary"],
                "value":       deduction_annual,
                "citation":    ded_result["citation"],
            },
            *[
                {
                    "tax_type":    f"Bậc {b['bracket']}",
                    "description": b["income_range"],
                    "formula":     b["formula"],
                    "value":       b["tax_in_bracket"],
                    "citation":    tax_result["citation"],
                }
                for b in tax_result["brackets"]
            ],
        ],
        "citation":  tax_result["citation"],
        "warnings":  [],
        "summary":   tax_result.get("summary", ""),
    }


def _validate_pit_full(params: dict) -> None:
    income = float(params.get("gross_income", 0))
    if income < 0:
        raise ValueError("gross_income không được âm")
    if income > _MAX_INCOME:
        raise ValueError(
            f"gross_income {income/1e9:.1f} tỷ vượt sanity cap "
            f"({_MAX_INCOME/1e9:.0f} tỷ)"
        )
    _validate_deduction(params)


# ─── Template Registry ────────────────────────────────────────────────────────

TEMPLATES: Dict[str, Template] = {
    # ── TNCN lũy tiến — khi đã biết thu nhập tính thuế (sau khấu trừ) ──────
    "PIT_progressive": Template(
        name      = "PIT_progressive",
        version   = "@109_2025_QH15",
        required  = ["annual_taxable_income"],
        executor  = calculate_tncn_progressive,
        validator = _validate_pit,
        rounder   = _floor_1000,
    ),

    # ── TNCN full pipeline — từ gross income + số người phụ thuộc ───────────
    # Compound: tự tính giảm trừ gia cảnh → thu nhập tính thuế → lũy tiến
    "PIT_full": Template(
        name      = "PIT_full",
        version   = "@109_2025_QH15",
        required  = ["gross_income"],
        executor  = _execute_pit_full,
        validator = _validate_pit_full,
        rounder   = _floor_1000,
    ),

    # ── HKD PP doanh thu — doanh thu ≤ 3 tỷ ─────────────────────────────────
    # LLM Legal Reasoner map ngành nghề → business_category
    "HKD_percentage": Template(
        name      = "HKD_percentage",
        version   = "@68_2026_NDCP",
        required  = ["annual_revenue", "business_category"],
        executor  = calculate_tax_hkd,
        validator = _validate_hkd,
        rounder   = _floor_100,
    ),

    # ── HKD PP lợi nhuận — doanh thu > 3 tỷ (bắt buộc) ─────────────────────
    "HKD_profit": Template(
        name      = "HKD_profit",
        version   = "@68_2026_NDCP",
        required  = ["annual_revenue", "annual_expenses", "business_category"],
        executor  = calculate_tax_hkd_profit,
        validator = _validate_hkd_profit,
        rounder   = _floor_100,
    ),

    # ── Giảm trừ gia cảnh standalone — khi chỉ hỏi về giảm trừ ─────────────
    "deduction_calc": Template(
        name      = "deduction_calc",
        version   = "@109_2025_QH15",
        required  = [],   # dependents có default=0, months có default=12
        executor  = calculate_deduction,
        validator = _validate_deduction,
        rounder   = lambda x: int(x),  # deduction: exact, không làm tròn
    ),
}


# ─── Public API ───────────────────────────────────────────────────────────────

def run_template(
    template_name: str,
    params: dict,
    assumptions: Optional[List[str]] = None,
) -> TemplateResult:
    """
    Entry point cho Pipeline v4 Python Calculator step.

    Args:
        template_name: Key trong TEMPLATES (PIT_progressive, PIT_full,
                       HKD_percentage, HKD_profit, deduction_calc).
        params:        Dict params từ LLM Legal Reasoner.
        assumptions:   Giả định được LLM đánh dấu (ghi vào TemplateResult).

    Returns:
        TemplateResult với tax_amount (int VND), breakdown, citations, warnings.

    Raises:
        KeyError:   template_name không tồn tại.
        ValueError: params validation fail (missing required, out of range).
    """
    if template_name not in TEMPLATES:
        available = list(TEMPLATES.keys())
        raise KeyError(
            f"Template không tồn tại: {template_name!r}. "
            f"Available: {available}"
        )
    tmpl = TEMPLATES[template_name]
    logger.debug(
        "run_template: %s v%s | params=%s",
        template_name, tmpl.version, list(params.keys()),
    )
    return tmpl.run(params, assumptions=assumptions)


def list_templates() -> Dict[str, dict]:
    """Trả về metadata của tất cả templates (dùng cho Dynamic Prompt Assembly)."""
    return {
        name: {
            "version":  t.version,
            "required": t.required,
        }
        for name, t in TEMPLATES.items()
    }
