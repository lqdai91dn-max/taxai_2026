"""
src/agent/pipeline_v4/final_validator.py — Final Validator (Python)

Kiểm tra output của LLM Synthesizer (step 6.3) trước khi trả về user.

Checks:
1. VND normalization match — tax_amount (từ locked state) phải xuất hiện trong answer
2. Assumption present     — nếu assumption_risk ≥ medium → answer phải đề cập giả định
3. Citation present       — answer phải có ít nhất 1 citation reference

Anti-pattern: KHÔNG dùng LLM để validate (dùng Python regex only).
Nếu fail: retry 6.3 tối đa 2 lần → Circuit Breaker.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


# ─── VND normalization helpers ────────────────────────────────────────────────
# Patterns cần match trong answer text

_VND_PATTERNS = [
    # 1,980,000 đồng / VNĐ / VND
    r"[\d]{1,3}(?:[,\.]\d{3})+\s*(?:đồng|VNĐ|VND|vnđ|vnd)?",
    # 1.98 triệu / 1,98 triệu
    r"[\d]+[,\.][\d]+\s*triệu",
    # 18 triệu / 18triệu
    r"[\d]+\s*triệu",
    # 1 tỷ
    r"[\d]+[,\.]?[\d]*\s*tỷ",
]
_VND_COMBINED = re.compile("|".join(_VND_PATTERNS), re.IGNORECASE)

# Assumption markers — LLM phải mention nếu có assumption
_ASSUMPTION_MARKERS = [
    r"giả\s+(?:sử|định)",    # "giả sử", "giả định"
    r"giả\s+thiết",           # "giả thiết"
    r"assuming",
    r"lưu\s+ý",               # "lưu ý"
    r"chú\s+ý",
    r"điều\s+kiện",           # "điều kiện"
    r"nếu\s+(?:bạn|anh|chị|ông|bà)",  # "nếu bạn là..."
]
_ASSUMPTION_PATTERN = re.compile("|".join(_ASSUMPTION_MARKERS), re.IGNORECASE)

# Citation markers
_CITATION_MARKERS = [
    r"Nghị\s+định",
    r"Thông\s+tư",
    r"Luật\s+Thuế",
    r"QH\d+",       # "109/2025/QH15"
    r"NĐ-CP",
    r"TT-BTC",
    r"NDCP",
    r"điều\s+\d+",  # "Điều 5"
    r"khoản\s+\d+", # "Khoản 2"
]
_CITATION_PATTERN = re.compile("|".join(_CITATION_MARKERS), re.IGNORECASE)


# ─── FinalValidationResult ────────────────────────────────────────────────────

@dataclass
class FinalValidationResult:
    valid:    bool
    checks:   dict     # {check_name: bool}
    errors:   List[str]
    warnings: List[str]


# ─── VND normalization ────────────────────────────────────────────────────────

def _normalize_vnd(amount: int) -> List[str]:
    """
    Sinh ra các format VND khả dĩ cho amount (dùng để match trong answer text).
    Ví dụ: 1_980_000 → ["1,980,000", "1.980.000", "1980000", "1,98 triệu", "1.98 triệu"]
    """
    formats = []
    # Comma-separated
    formats.append(f"{amount:,}")                    # 1,980,000
    formats.append(str(amount))                       # 1980000
    # Dot-separated (một số văn bản VN dùng)
    formats.append(f"{amount:,}".replace(",", "."))   # 1.980.000

    # Triệu format
    if amount >= 1_000_000:
        trieu = amount / 1_000_000
        formats.append(f"{trieu:.2f} triệu".replace(".", ","))  # 1,98 triệu
        formats.append(f"{trieu:.2f} triệu")                    # 1.98 triệu
        if trieu == int(trieu):
            formats.append(f"{int(trieu)} triệu")               # 18 triệu

    # Tỷ format
    if amount >= 1_000_000_000:
        ty = amount / 1_000_000_000
        formats.append(f"{ty:.2f} tỷ")

    return formats


def _check_vnd_present(answer: str, tax_amount: int) -> bool:
    """
    Kiểm tra tax_amount có xuất hiện trong answer với bất kỳ format VND nào không.
    Tolerant: match nếu bất kỳ format nào xuất hiện.
    """
    candidates = _normalize_vnd(tax_amount)
    for fmt in candidates:
        # Escape đặc biệt cho regex: dấu "." và ","
        escaped = re.escape(fmt)
        if re.search(escaped, answer, re.IGNORECASE):
            return True

    # Fallback: tìm số gần đúng (trong ±1000 VND — rounding tolerance)
    # Tìm tất cả số trong answer, so sánh
    numbers_in_answer = re.findall(r"[\d]{1,3}(?:[,\.]\d{3})+", answer)
    for raw_num in numbers_in_answer:
        try:
            num = int(raw_num.replace(",", "").replace(".", ""))
            if abs(num - tax_amount) <= 1000:
                return True
        except ValueError:
            pass

    return False


# ─── Main validator ───────────────────────────────────────────────────────────

def validate_synthesized_answer(
    answer:          str,
    tax_amount:      Optional[int],
    has_assumptions: bool = False,
    assumption_risk: str  = "low",    # "low" | "medium" | "high"
) -> FinalValidationResult:
    """
    Validate answer text từ LLM Synthesizer.

    Args:
        answer:          Answer text từ LLM (step 6.3).
        tax_amount:      Locked tax amount từ CalcOutput (None nếu không tính thuế).
        has_assumptions: LLM Legal Reasoner có assumptions không.
        assumption_risk: "low" / "medium" / "high" — ảnh hưởng strictness.

    Returns:
        FinalValidationResult.
    """
    errors:   List[str] = []
    warnings: List[str] = []
    checks: dict = {}

    if not answer or not answer.strip():
        return FinalValidationResult(
            valid=False,
            checks={"non_empty": False},
            errors=["Answer rỗng — LLM Synthesizer không trả về gì."],
            warnings=[],
        )

    # ── Check 1: VND amount present ──────────────────────────────────────────
    if tax_amount is not None and tax_amount > 0:
        vnd_ok = _check_vnd_present(answer, tax_amount)
        checks["vnd_present"] = vnd_ok
        if not vnd_ok:
            errors.append(
                f"VND normalization fail: tax_amount={tax_amount:,} không tìm thấy "
                f"trong answer text. LLM có thể đã recompute (vi phạm immutability)."
            )
    else:
        checks["vnd_present"] = True  # N/A

    # ── Check 2: Assumption mention ──────────────────────────────────────────
    if has_assumptions and assumption_risk in ("medium", "high"):
        assumption_ok = bool(_ASSUMPTION_PATTERN.search(answer))
        checks["assumption_mentioned"] = assumption_ok
        if not assumption_ok:
            if assumption_risk == "high":
                errors.append(
                    "Assumption risk=high nhưng answer không đề cập giả định. "
                    "Bắt buộc phải mention để tránh mislead user."
                )
            else:
                warnings.append(
                    "Assumption risk=medium — khuyến nghị mention giả định trong answer."
                )
    else:
        checks["assumption_mentioned"] = True  # N/A

    # ── Check 3: Citation present ────────────────────────────────────────────
    citation_ok = bool(_CITATION_PATTERN.search(answer))
    checks["citation_present"] = citation_ok
    if not citation_ok:
        warnings.append(
            "Không tìm thấy citation nào trong answer. "
            "Khuyến nghị trích dẫn văn bản pháp luật nguồn."
        )

    valid = len(errors) == 0
    return FinalValidationResult(valid=valid, checks=checks, errors=errors, warnings=warnings)
