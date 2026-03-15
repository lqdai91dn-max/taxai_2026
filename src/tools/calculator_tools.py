"""
Calculator tools — deterministic tax computation.

Rules:
  - Pure functions, no I/O, no LLM calls.
  - Config-driven tax tables (update here when law changes).
  - Every output includes citation trỏ tới điều luật nguồn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ═══════════════════════════════════════════════════════════════════════════════
# TAX TABLES — config-driven, update khi luật thay đổi
# ═══════════════════════════════════════════════════════════════════════════════

# Nguồn: Nghị định 68/2026/NĐ-CP + Sổ tay HKD (Bảng tỷ lệ % GTGT)
TAX_HKD_GTGT_RATES: dict[str, tuple[float, str]] = {
    # category: (rate, mô tả ngành)
    "goods":         (0.01, "Phân phối, cung cấp hàng hóa"),
    "services":      (0.05, "Dịch vụ, xây dựng không bao thầu nguyên vật liệu"),
    "manufacturing": (0.03, "Sản xuất, vận tải, dịch vụ gắn hàng hóa, xây dựng bao thầu NVL"),
    "other":         (0.02, "Hoạt động kinh doanh khác"),
    "real_estate":   (0.05, "Cho thuê bất động sản"),
}

# Nguồn: Nghị định 68/2026/NĐ-CP — Phương pháp tỷ lệ % doanh thu (PP doanh thu)
# TNCN = tổng doanh thu × tỷ lệ % theo ngành (áp dụng khi DT > 500M, tính trên TOÀN BỘ DT)
TAX_HKD_TNCN_REVENUE_RATES: dict[str, tuple[float, str]] = {
    # category: (tncn_rate, mô tả)
    "goods":         (0.005, "Phân phối, cung cấp hàng hóa — TNCN 0.5%"),
    "services":      (0.02,  "Dịch vụ — TNCN 2%"),
    "manufacturing": (0.015, "Sản xuất, vận tải, xây dựng — TNCN 1.5%"),
    "other":         (0.01,  "Hoạt động kinh doanh khác — TNCN 1%"),
    "real_estate":   (0.05,  "Cho thuê bất động sản — TNCN 5%"),
}

# Nguồn: Nghị định 68/2026/NĐ-CP — Phương pháp lợi nhuận (PP lợi nhuận)
# Thuế suất TNCN = (DT − Chi phí) × thuế suất bậc theo mức doanh thu
# Chỉ dùng cho calculate_tax_hkd_profit
TAX_HKD_TNCN_PROFIT_BRACKETS: list[tuple[float, float, float, str]] = [
    (0,              500_000_000,  0.00, "Doanh thu ≤500 triệu — miễn thuế TNCN"),
    (500_000_000,  3_000_000_000,  0.15, "Doanh thu 500 triệu – 3 tỷ — thuế suất 15%"),
    (3_000_000_000, 50_000_000_000, 0.17, "Doanh thu 3 tỷ – 50 tỷ — thuế suất 17%"),
    (50_000_000_000, float("inf"),  0.20, "Doanh thu >50 tỷ — thuế suất 20%"),
]

# Nguồn: Luật Thuế TNCN 109/2025/QH15 (hiệu lực 01/07/2026), biểu lũy tiến 5 bậc
# Format: (income_from, income_to, rate)  — thu nhập tính theo NĂM
TAX_TNCN_PROGRESSIVE_BRACKETS: list[tuple[float, float, float]] = [
    (0,               120_000_000,  0.05),
    (120_000_000,     360_000_000,  0.10),
    (360_000_000,     720_000_000,  0.20),
    (720_000_000,   1_200_000_000,  0.30),
    (1_200_000_000, float("inf"),   0.35),
]

# ── Citations ──────────────────────────────────────────────────────────────────

_CITE_68 = {
    "doc_id":  "68_2026_NDCP",
    "doc_number": "68/2026/NĐ-CP",
    "title":   "Nghị định về chính sách thuế và quản lý thuế đối với hộ kinh doanh, cá nhân kinh doanh",
    "effective_date": "2026-03-05",
}

_CITE_109 = {
    "doc_id":  "109_2025_QH15",
    "doc_number": "109/2025/QH15",
    "title":   "Luật Thuế Thu nhập cá nhân",
    "effective_date": "2026-07-01",
}

# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TaxBreakdownItem:
    tax_type: str          # "GTGT" | "TNCN"
    description: str
    formula: str
    value: float           # VND
    citation: dict


@dataclass
class TaxHKDResult:
    annual_revenue: float
    business_category: str
    exempt: bool           # True nếu doanh thu ≤500M (miễn toàn bộ)

    gtgt_rate: float
    gtgt_payable: float

    tncn_rate: float
    tncn_base: float       # phần doanh thu tính thuế TNCN (revenue - 500M nếu có)
    tncn_payable: float

    total_tax: float
    breakdown: list[TaxBreakdownItem]
    warnings: list[str]


@dataclass
class ProgressiveBracketItem:
    bracket: int           # 1..5
    income_range: str      # mô tả bậc
    rate: float
    taxable_in_bracket: float
    tax_in_bracket: float


@dataclass
class TNCNProgressiveResult:
    annual_taxable_income: float
    tax_payable: float
    effective_rate: float  # tax_payable / annual_taxable_income
    brackets: list[ProgressiveBracketItem]
    citation: dict


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 1 — calculate_tax_hkd
# ═══════════════════════════════════════════════════════════════════════════════

BusinessCategory = Literal["goods", "services", "manufacturing", "other", "real_estate"]

HKD_TNCN_EXEMPT_THRESHOLD = 500_000_000  # 500 triệu VND/năm


def calculate_tax_hkd(
    annual_revenue: float,
    business_category: BusinessCategory,
) -> dict:
    """
    Tính thuế GTGT + TNCN cho hộ kinh doanh / cá nhân kinh doanh.

    Nguồn luật: Nghị định 68/2026/NĐ-CP.

    Args:
        annual_revenue:     Tổng doanh thu năm (VND).
        business_category:  Nhóm ngành nghề (goods/services/manufacturing/other/real_estate).

    Returns:
        dict với gtgt_payable, tncn_payable, total_tax, breakdown, warnings.
    """
    if annual_revenue < 0:
        raise ValueError("annual_revenue không được âm")
    if business_category not in TAX_HKD_GTGT_RATES:
        raise ValueError(f"business_category không hợp lệ: {business_category!r}")

    gtgt_rate, category_desc = TAX_HKD_GTGT_RATES[business_category]
    tncn_rate_rev, tncn_desc = TAX_HKD_TNCN_REVENUE_RATES[business_category]
    warnings: list[str] = []
    breakdown: list[TaxBreakdownItem] = []

    # ── GTGT ──────────────────────────────────────────────────────────────────
    # GTGT áp dụng trên toàn bộ doanh thu, không có ngưỡng miễn
    gtgt_payable = annual_revenue * gtgt_rate
    breakdown.append(TaxBreakdownItem(
        tax_type="GTGT",
        description=f"Thuế GTGT — {category_desc}",
        formula=f"{annual_revenue:,.0f} × {gtgt_rate:.1%} = {gtgt_payable:,.0f} VND",
        value=gtgt_payable,
        citation={**_CITE_68, "note": "Tỷ lệ % tính thuế GTGT theo nhóm ngành nghề"},
    ))

    # ── TNCN — phương pháp tỷ lệ % doanh thu ─────────────────────────────────
    # Ngưỡng miễn: DT ≤ 500M → không nộp TNCN
    # Khi DT > 500M: TNCN tính trên TOÀN BỘ doanh thu (không trừ 500M)
    exempt = annual_revenue <= HKD_TNCN_EXEMPT_THRESHOLD

    if exempt:
        tncn_payable = 0.0
        tncn_base = 0.0
        tncn_rate = 0.0
        breakdown.append(TaxBreakdownItem(
            tax_type="TNCN",
            description="Thuế TNCN — miễn vì doanh thu ≤500 triệu/năm",
            formula=f"Doanh thu {annual_revenue:,.0f} ≤ 500,000,000 → miễn",
            value=0.0,
            citation={**_CITE_68, "note": "Ngưỡng doanh thu miễn thuế TNCN: 500 triệu đồng/năm"},
        ))
    else:
        # PP doanh thu: TNCN = tổng DT × tỷ lệ % theo ngành
        tncn_rate = tncn_rate_rev
        tncn_base = annual_revenue   # tính trên toàn bộ doanh thu
        tncn_payable = annual_revenue * tncn_rate

        breakdown.append(TaxBreakdownItem(
            tax_type="TNCN",
            description=f"Thuế TNCN — PP tỷ lệ % doanh thu | {tncn_desc}",
            formula=(
                f"{annual_revenue:,.0f} × {tncn_rate:.1%} = {tncn_payable:,.0f} VND"
            ),
            value=tncn_payable,
            citation={**_CITE_68, "note": "Tỷ lệ % TNCN theo nhóm ngành nghề (PP doanh thu)"},
        ))

    # ── Cảnh báo đặc biệt ─────────────────────────────────────────────────────
    if annual_revenue > 3_000_000_000:
        warnings.append(
            "Doanh thu >3 tỷ đồng: bắt buộc chuyển sang phương pháp lợi nhuận "
            "— dùng calculate_tax_hkd_profit (cần sổ sách kế toán đầy đủ)."
        )

    total_tax = gtgt_payable + tncn_payable

    result = TaxHKDResult(
        annual_revenue=annual_revenue,
        business_category=business_category,
        exempt=exempt,
        gtgt_rate=gtgt_rate,
        gtgt_payable=gtgt_payable,
        tncn_rate=tncn_rate,
        tncn_base=tncn_base,
        tncn_payable=tncn_payable,
        total_tax=total_tax,
        breakdown=breakdown,
        warnings=warnings,
    )
    return _hkd_result_to_dict(result)


def _get_hkd_tncn_profit_rate(annual_revenue: float) -> tuple[float, str]:
    """Tra cứu thuế suất TNCN HKD theo mức doanh thu (chỉ dùng cho PP lợi nhuận)."""
    for rev_from, rev_to, rate, desc in TAX_HKD_TNCN_PROFIT_BRACKETS:
        if rev_from < annual_revenue <= rev_to:
            return rate, desc
    # fallback: bậc cao nhất
    return TAX_HKD_TNCN_PROFIT_BRACKETS[-1][2], TAX_HKD_TNCN_PROFIT_BRACKETS[-1][3]


def _hkd_result_to_dict(r: TaxHKDResult) -> dict:
    return {
        "annual_revenue": r.annual_revenue,
        "business_category": r.business_category,
        "exempt": r.exempt,
        "gtgt_rate": r.gtgt_rate,
        "gtgt_payable": round(r.gtgt_payable),
        "tncn_rate": r.tncn_rate,
        "tncn_base": round(r.tncn_base),
        "tncn_payable": round(r.tncn_payable),
        "total_tax": round(r.total_tax),
        "breakdown": [
            {
                "tax_type": item.tax_type,
                "description": item.description,
                "formula": item.formula,
                "value": round(item.value),
                "citation": item.citation,
            }
            for item in r.breakdown
        ],
        "warnings": r.warnings,
        "summary": _format_hkd_summary(r),
    }


def _format_hkd_summary(r: TaxHKDResult) -> str:
    if r.exempt:
        return (
            f"Doanh thu {r.annual_revenue/1e6:.0f} triệu đồng ≤ 500 triệu → "
            f"miễn thuế TNCN. Chỉ nộp GTGT: {r.gtgt_payable/1e6:.2f} triệu đồng "
            f"({r.gtgt_rate:.1%} × doanh thu)."
        )
    return (
        f"Doanh thu {r.annual_revenue/1e6:.0f} triệu đồng | "
        f"GTGT: {r.gtgt_payable/1e6:.2f} triệu ({r.gtgt_rate:.1%}) | "
        f"TNCN: {r.tncn_payable/1e6:.2f} triệu ({r.tncn_rate:.1%} × toàn bộ doanh thu) | "
        f"Tổng: {r.total_tax/1e6:.2f} triệu đồng."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 2 — calculate_tncn_progressive
# ═══════════════════════════════════════════════════════════════════════════════

_BRACKET_LABELS = [
    "Bậc 1: ≤120 triệu/năm",
    "Bậc 2: 120–360 triệu/năm",
    "Bậc 3: 360–720 triệu/năm",
    "Bậc 4: 720 triệu–1,2 tỷ/năm",
    "Bậc 5: >1,2 tỷ/năm",
]


def calculate_tncn_progressive(annual_taxable_income: float) -> dict:
    """
    Tính thuế TNCN lũy tiến từng phần cho cá nhân (lương, tiền công).

    Nguồn luật: Luật TNCN 109/2025/QH15 (hiệu lực 01/07/2026), biểu 5 bậc.

    Args:
        annual_taxable_income: Thu nhập tính thuế năm (VND),
                               SAU khi đã trừ giảm trừ gia cảnh và các khoản khác.

    Returns:
        dict với tax_payable, effective_rate, brackets breakdown.
    """
    if annual_taxable_income < 0:
        raise ValueError("annual_taxable_income không được âm")

    total_tax = 0.0
    brackets: list[ProgressiveBracketItem] = []

    for i, (inc_from, inc_to, rate) in enumerate(TAX_TNCN_PROGRESSIVE_BRACKETS):
        if annual_taxable_income <= inc_from:
            break

        taxable_in_bracket = min(annual_taxable_income, inc_to) - inc_from
        if taxable_in_bracket <= 0:
            continue

        tax_in_bracket = taxable_in_bracket * rate
        total_tax += tax_in_bracket

        brackets.append(ProgressiveBracketItem(
            bracket=i + 1,
            income_range=_BRACKET_LABELS[i],
            rate=rate,
            taxable_in_bracket=taxable_in_bracket,
            tax_in_bracket=tax_in_bracket,
        ))

    effective_rate = total_tax / annual_taxable_income if annual_taxable_income > 0 else 0.0

    result = TNCNProgressiveResult(
        annual_taxable_income=annual_taxable_income,
        tax_payable=total_tax,
        effective_rate=effective_rate,
        brackets=brackets,
        citation=_CITE_109,
    )
    return _progressive_result_to_dict(result)


def _progressive_result_to_dict(r: TNCNProgressiveResult) -> dict:
    return {
        "annual_taxable_income": r.annual_taxable_income,
        "tax_payable": round(r.tax_payable),
        "effective_rate": round(r.effective_rate, 4),
        "effective_rate_pct": f"{r.effective_rate:.2%}",
        "brackets": [
            {
                "bracket": b.bracket,
                "income_range": b.income_range,
                "rate": b.rate,
                "rate_pct": f"{b.rate:.0%}",
                "taxable_in_bracket": round(b.taxable_in_bracket),
                "tax_in_bracket": round(b.tax_in_bracket),
                "formula": (
                    f"{b.taxable_in_bracket:,.0f} × {b.rate:.0%} "
                    f"= {b.tax_in_bracket:,.0f} VND"
                ),
            }
            for b in r.brackets
        ],
        "citation": r.citation,
        "summary": _format_progressive_summary(r),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 3 — calculate_deduction
# ═══════════════════════════════════════════════════════════════════════════════

# Nguồn: Luật TNCN 109/2025/QH15 (hiệu lực 01/07/2026)
_PERSONAL_DEDUCTION_MONTHLY   = 15_500_000  # 15.5 triệu/tháng — bản thân
_DEPENDENT_DEDUCTION_MONTHLY  =  6_200_000  # 6.2 triệu/tháng — mỗi người phụ thuộc

# Mức cũ (trước 01/07/2026 — Luật TNCN 2007 sửa đổi)
_PERSONAL_DEDUCTION_OLD   = 11_000_000  # 11 triệu/tháng
_DEPENDENT_DEDUCTION_OLD  =  4_400_000  # 4.4 triệu/tháng


def calculate_deduction(
    dependents: int = 0,
    months: int = 12,
) -> dict:
    """
    Tính giảm trừ gia cảnh TNCN cho cá nhân có thu nhập từ tiền lương/công.

    Nguồn: Luật TNCN 109/2025/QH15 (hiệu lực 01/07/2026).
    Mức giảm trừ mới: 15.5 triệu/tháng (bản thân) + 6.2 triệu/tháng × người phụ thuộc.

    Args:
        dependents: Số người phụ thuộc đã đăng ký (≥0).
        months:     Số tháng tính giảm trừ trong năm (1–12, mặc định 12).

    Returns:
        dict với personal_monthly, dependent_monthly, total_monthly, total_annual.
    """
    if dependents < 0:
        raise ValueError("dependents không được âm")
    if not 1 <= months <= 12:
        raise ValueError("months phải từ 1 đến 12")

    dependent_total_monthly = _DEPENDENT_DEDUCTION_MONTHLY * dependents
    total_monthly           = _PERSONAL_DEDUCTION_MONTHLY + dependent_total_monthly
    total_annual            = total_monthly * months

    # Mức cũ để so sánh
    old_total_annual = (_PERSONAL_DEDUCTION_OLD + _DEPENDENT_DEDUCTION_OLD * dependents) * months
    increase = total_annual - old_total_annual

    return {
        "dependents":                    dependents,
        "months":                        months,
        "personal_deduction_monthly":    _PERSONAL_DEDUCTION_MONTHLY,
        "dependent_deduction_per_person": _DEPENDENT_DEDUCTION_MONTHLY,
        "dependent_deduction_total_monthly": dependent_total_monthly,
        "total_deduction_monthly":       total_monthly,
        "total_deduction_annual":        total_annual,
        "old_law_annual":                old_total_annual,
        "increase_vs_old_law":           increase,
        "citation": {
            **_CITE_109,
            "note": "Mức giảm trừ gia cảnh mới theo Luật 109/2025/QH15",
        },
        "warning": (
            "Mức 15.5 triệu/tháng (bản thân) và 6.2 triệu/tháng (người phụ thuộc) "
            "áp dụng từ 01/07/2026 theo Luật 109/2025/QH15. "
            "Trước ngày này áp dụng mức cũ: 11 triệu/tháng và 4.4 triệu/tháng."
        ),
        "summary": (
            f"{months} tháng | Bản thân: {_PERSONAL_DEDUCTION_MONTHLY/1e6:.1f}M/tháng"
            + (f" + {dependents} người phụ thuộc × {_DEPENDENT_DEDUCTION_MONTHLY/1e6:.1f}M"
               if dependents else "")
            + f" | Tổng giảm trừ năm: {total_annual/1e6:.1f} triệu đồng"
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 4 — calculate_tax_hkd_profit
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_tax_hkd_profit(
    annual_revenue: float,
    annual_expenses: float,
    business_category: str,
) -> dict:
    """
    Tính thuế GTGT + TNCN cho HKD theo phương pháp lợi nhuận (thu nhập tính thuế).

    Áp dụng cho:
      - HKD doanh thu > 3 tỷ đồng: BẮT BUỘC dùng phương pháp lợi nhuận.
      - HKD doanh thu 500 triệu – 3 tỷ: Tự nguyện chọn (phải ổn định ≥ 2 năm).

    Công thức TNCN: (Doanh thu − Chi phí hợp lý, hợp lệ) × Thuế suất
    Công thức GTGT: Doanh thu × Tỷ lệ % GTGT  (không thay đổi theo phương pháp)

    Nguồn: Nghị định 68/2026/NĐ-CP.

    Args:
        annual_revenue:   Tổng doanh thu năm (VND). Phải > 500 triệu.
        annual_expenses:  Chi phí hợp lý, hợp lệ năm (VND). Phải ≥ 0 và < doanh thu.
        business_category: Nhóm ngành nghề (goods/services/manufacturing/other/real_estate).

    Returns:
        dict với taxable_income, tncn_payable, gtgt_payable, total_tax, breakdown, warnings.
    """
    if annual_revenue <= HKD_TNCN_EXEMPT_THRESHOLD:
        raise ValueError(
            f"Phương pháp lợi nhuận chỉ áp dụng cho doanh thu > "
            f"{HKD_TNCN_EXEMPT_THRESHOLD/1e6:.0f} triệu. "
            f"Doanh thu {annual_revenue/1e6:.0f} triệu → dùng calculate_tax_hkd."
        )
    if annual_expenses < 0:
        raise ValueError("annual_expenses không được âm")
    if annual_expenses >= annual_revenue:
        raise ValueError("Chi phí phải nhỏ hơn doanh thu")
    if business_category not in TAX_HKD_GTGT_RATES:
        raise ValueError(f"business_category không hợp lệ: {business_category!r}")

    gtgt_rate, category_desc = TAX_HKD_GTGT_RATES[business_category]
    warnings: list[str] = []
    breakdown: list[dict] = []

    # ── GTGT — vẫn tính trên toàn bộ doanh thu ───────────────────────────────
    gtgt_payable = annual_revenue * gtgt_rate
    breakdown.append({
        "tax_type":    "GTGT",
        "description": f"Thuế GTGT — {category_desc}",
        "formula":     f"{annual_revenue:,.0f} × {gtgt_rate:.0%} = {gtgt_payable:,.0f} VND",
        "value":       round(gtgt_payable),
        "citation":    {**_CITE_68, "note": "Tỷ lệ % GTGT (PP lợi nhuận — GTGT tính trên doanh thu)"},
    })

    # ── TNCN — phương pháp lợi nhuận ─────────────────────────────────────────
    taxable_income = annual_revenue - annual_expenses
    tncn_rate, bracket_desc = _get_hkd_tncn_profit_rate(annual_revenue)  # rate theo DT

    if taxable_income <= 0:
        tncn_payable = 0.0
        breakdown.append({
            "tax_type":    "TNCN",
            "description": "Thuế TNCN = 0 (lợi nhuận ≤ 0)",
            "formula":     f"({annual_revenue:,.0f} − {annual_expenses:,.0f}) = {taxable_income:,.0f} ≤ 0",
            "value":       0,
            "citation":    {**_CITE_68, "note": "PP lợi nhuận: thu nhập tính thuế ≤ 0"},
        })
        warnings.append("Lợi nhuận âm hoặc bằng 0 — không phát sinh thuế TNCN.")
    else:
        tncn_payable = taxable_income * tncn_rate
        breakdown.append({
            "tax_type":    "TNCN",
            "description": f"Thuế TNCN — PP lợi nhuận | {bracket_desc}",
            "formula":     (
                f"({annual_revenue:,.0f} − {annual_expenses:,.0f}) × {tncn_rate:.0%} "
                f"= {taxable_income:,.0f} × {tncn_rate:.0%} = {tncn_payable:,.0f} VND"
            ),
            "value":       round(tncn_payable),
            "citation":    {**_CITE_68, "note": "PP lợi nhuận: Thuế suất TNCN theo mức doanh thu năm"},
        })

    if annual_revenue <= 3_000_000_000:
        warnings.append(
            "Doanh thu 500M–3B: chọn phương pháp lợi nhuận → phải duy trì ổn định "
            "tối thiểu 2 năm liên tiếp."
        )
    else:
        warnings.append(
            "Doanh thu > 3 tỷ: bắt buộc PP lợi nhuận — cần sổ sách kế toán đầy đủ, "
            "chi phí hợp lý được cơ quan thuế công nhận."
        )

    total_tax      = round(gtgt_payable) + round(tncn_payable)
    profit_margin  = taxable_income / annual_revenue if annual_revenue > 0 else 0.0

    return {
        "annual_revenue":    annual_revenue,
        "annual_expenses":   annual_expenses,
        "taxable_income":    round(taxable_income),
        "profit_margin":     round(profit_margin, 4),
        "business_category": business_category,
        "method":            "profit",
        "gtgt_rate":         gtgt_rate,
        "gtgt_payable":      round(gtgt_payable),
        "tncn_rate":         tncn_rate,
        "tncn_payable":      round(tncn_payable),
        "total_tax":         total_tax,
        "breakdown":         breakdown,
        "warnings":          warnings,
        "summary": (
            f"PP lợi nhuận | DT {annual_revenue/1e6:.0f}M − CP {annual_expenses/1e6:.0f}M "
            f"= TNTT {taxable_income/1e6:.0f}M | "
            f"GTGT {round(gtgt_payable)/1e6:.2f}M + TNCN {round(tncn_payable)/1e6:.2f}M "
            f"= Tổng {total_tax/1e6:.2f}M đồng"
        ),
    }


def _format_progressive_summary(r: TNCNProgressiveResult) -> str:
    if r.tax_payable == 0:
        return (
            f"Thu nhập tính thuế {r.annual_taxable_income/1e6:.1f} triệu đồng/năm → "
            f"thuế TNCN = 0 (chưa đến bậc 1)."
        )
    lines = [
        f"Thu nhập tính thuế: {r.annual_taxable_income/1e6:.1f} triệu đồng/năm",
        f"Thuế phải nộp: {r.tax_payable/1e6:.3f} triệu đồng ({r.effective_rate:.2%} thuế suất hiệu dụng)",
        "Chi tiết từng bậc:",
    ]
    for b in r.brackets:
        lines.append(
            f"  {b.income_range} ({b.rate:.0%}): "
            f"{b.taxable_in_bracket/1e6:.1f} triệu × {b.rate:.0%} "
            f"= {b.tax_in_bracket/1e6:.3f} triệu"
        )
    return "\n".join(lines)
