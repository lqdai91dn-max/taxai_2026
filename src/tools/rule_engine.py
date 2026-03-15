"""
Rule engine — xử lý logic điều kiện (IF/THEN) về nghĩa vụ thuế HKD.

Tool này trả lời các câu hỏi dạng:
  "HKD doanh thu X phải nộp thuế theo phương pháp nào?"
  "Kê khai thuế GTGT theo quý hay tháng?"
  "Có cần dùng hóa đơn điện tử không?"
  "Sàn TMĐT có khấu trừ thuế thay không?"

Nguồn: Nghị định 68/2026/NĐ-CP.
"""

from __future__ import annotations

# ── Constants ─────────────────────────────────────────────────────────────────

_EXEMPT_THRESHOLD         = 500_000_000      # 500M — ngưỡng miễn thuế
_PROFIT_MANDATORY_FROM    = 3_000_000_000    # 3B — bắt buộc PP lợi nhuận
_MONTHLY_FILING_FROM      = 50_000_000_000   # 50B — kê khai theo tháng
_EINVOICE_OPTIONAL_FROM   = 500_000_000      # 500M — khuyến khích HĐĐT
_EINVOICE_MANDATORY_FROM  = 1_000_000_000    # 1B — bắt buộc HĐĐT

_CITE_68 = {
    "doc_id":     "68_2026_NDCP",
    "doc_number": "68/2026/NĐ-CP",
    "effective_date": "2026-03-05",
}


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 5 — evaluate_tax_obligation
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_tax_obligation(
    annual_revenue: float,
    has_online_sales: bool = False,
    platform_has_payment: bool = False,
) -> dict:
    """
    Đánh giá tổng hợp nghĩa vụ thuế và yêu cầu hành chính cho HKD / cá nhân KD.

    Kiểm tra 5 quy tắc:
      1. Miễn thuế (doanh thu ≤ 500 triệu/năm)
      2. Phương pháp tính thuế TNCN (tỷ lệ% doanh thu vs. lợi nhuận)
      3. Kỳ kê khai thuế GTGT (quý / tháng)
      4. Yêu cầu hóa đơn điện tử
      5. Khấu trừ thuế qua sàn TMĐT

    Nguồn: Nghị định 68/2026/NĐ-CP.

    Args:
        annual_revenue:       Tổng doanh thu ước tính trong năm (VND).
        has_online_sales:     HKD có bán hàng trên sàn TMĐT không (default False).
        platform_has_payment: Sàn TMĐT có chức năng đặt hàng + thanh toán không (default False).

    Returns:
        dict với is_exempt, tax_method, filing_frequency, einvoice_required,
        tmdt_withholding, obligations (danh sách quy tắc), notes.
    """
    if annual_revenue < 0:
        raise ValueError("annual_revenue không được âm")

    notes: list[str] = []
    obligations: list[dict] = []

    # ── Rule 1: Miễn thuế ─────────────────────────────────────────────────────
    is_exempt = annual_revenue <= _EXEMPT_THRESHOLD

    if is_exempt:
        obligations.append({
            "rule":      "Miễn thuế GTGT và TNCN",
            "condition": f"Doanh thu {annual_revenue/1e6:.1f} triệu ≤ 500 triệu/năm",
            "result":    "✅ Miễn — không phải nộp thuế GTGT và TNCN",
            "citation":  {**_CITE_68, "note": "Ngưỡng doanh thu miễn thuế"},
        })

    # ── Rule 2: Phương pháp tính thuế TNCN ────────────────────────────────────
    if is_exempt:
        tax_method      = "none"
        tax_method_desc = "Miễn — không áp dụng phương pháp nào"
    elif annual_revenue <= _PROFIT_MANDATORY_FROM:
        tax_method      = "either"
        tax_method_desc = (
            "Được tự chọn: tỷ lệ% doanh thu [TNCN = (DT−500M) × 15%] "
            "HOẶC lợi nhuận [TNCN = (DT−Chi phí) × 15%] — ổn định ≥ 2 năm"
        )
        notes.append(
            "Doanh thu 500M–3B: nếu chọn phương pháp lợi nhuận, "
            "phải duy trì ổn định tối thiểu 2 năm liên tiếp."
        )
        obligations.append({
            "rule":      "Phương pháp tính thuế TNCN",
            "condition": "Doanh thu 500 triệu – 3 tỷ",
            "result":    "⚖️ Tự chọn: PP tỷ lệ% (15% × (DT−500M)) hoặc PP lợi nhuận (15% × (DT−CP))",
            "citation":  {**_CITE_68, "note": "Lựa chọn phương pháp tính thuế TNCN HKD"},
        })
    else:
        rate = "17%" if annual_revenue <= 50_000_000_000 else "20%"
        tax_method      = "profit"
        tax_method_desc = f"Bắt buộc phương pháp lợi nhuận — thuế suất TNCN {rate}"
        notes.append(
            "Doanh thu > 3 tỷ: bắt buộc PP lợi nhuận — "
            "cần sổ sách kế toán đầy đủ, chi phí phải được cơ quan thuế công nhận."
        )
        obligations.append({
            "rule":      "Phương pháp tính thuế TNCN (bắt buộc)",
            "condition": "Doanh thu > 3 tỷ đồng",
            "result":    f"⚠️ Bắt buộc PP lợi nhuận — TNCN = (DT−CP) × {rate}",
            "citation":  {**_CITE_68, "note": "Bắt buộc phương pháp lợi nhuận HKD >3 tỷ"},
        })

    # ── Rule 3: Kỳ kê khai GTGT ──────────────────────────────────────────────
    if is_exempt:
        filing_frequency = "exempt"
        filing_desc      = "Miễn kê khai — doanh thu ≤ 500 triệu"
    elif annual_revenue <= _MONTHLY_FILING_FROM:
        filing_frequency = "quarterly"
        filing_desc      = "Kê khai thuế GTGT theo quý"
    else:
        filing_frequency = "monthly"
        filing_desc      = "Kê khai thuế GTGT theo tháng (doanh thu > 50 tỷ)"

    if not is_exempt:
        obligations.append({
            "rule":      "Kỳ kê khai thuế GTGT",
            "condition": f"Doanh thu {annual_revenue/1e9:.2f} tỷ/năm",
            "result":    f"📅 {filing_desc}",
            "citation":  {**_CITE_68, "note": "Ngưỡng kỳ kê khai thuế GTGT"},
        })

    # ── Rule 4: Hóa đơn điện tử ──────────────────────────────────────────────
    if annual_revenue < _EINVOICE_OPTIONAL_FROM:
        einvoice_required = False
        einvoice_desc     = "Không dùng HĐĐT (doanh thu < 500 triệu)"
    elif annual_revenue < _EINVOICE_MANDATORY_FROM:
        einvoice_required = "optional"
        einvoice_desc     = "Khuyến khích HĐĐT (doanh thu 500M – dưới 1 tỷ)"
    else:
        einvoice_required = True
        einvoice_desc     = "Bắt buộc hóa đơn điện tử (doanh thu ≥ 1 tỷ)"

    obligations.append({
        "rule":      "Hóa đơn điện tử",
        "condition": f"Doanh thu {annual_revenue/1e6:.0f} triệu đồng/năm",
        "result":    f"🧾 {einvoice_desc}",
        "citation":  {**_CITE_68, "note": "Yêu cầu sử dụng hóa đơn điện tử"},
    })

    # ── Rule 5: Khấu trừ TMĐT ────────────────────────────────────────────────
    tmdt_withholding = has_online_sales and platform_has_payment

    if has_online_sales:
        if platform_has_payment:
            tmdt_desc = (
                "Sàn TMĐT CÓ trách nhiệm khấu trừ và nộp thuế thay "
                "(sàn có chức năng đặt hàng trực tuyến + thanh toán)"
            )
            notes.append(
                "Nếu sàn TMĐT đã khấu trừ thuế và doanh thu thực tế năm ≤ 500 triệu → "
                "được bù trừ hoặc hoàn thuế nộp thừa."
            )
        else:
            tmdt_desc = (
                "Sàn TMĐT KHÔNG khấu trừ thuế thay (không có chức năng thanh toán) — "
                "HKD tự kê khai và nộp thuế"
            )

        # TMĐT sàn không xác định được loại giao dịch → áp tỷ lệ cao nhất
        notes.append(
            "Nếu sàn TMĐT không phân biệt được hàng hóa hay dịch vụ → "
            "áp dụng tỷ lệ khấu trừ GTGT 5% và TNCN 2% (tỷ lệ cao nhất)."
        )
        obligations.append({
            "rule":      "Khấu trừ thuế TMĐT",
            "condition": "Kinh doanh trên sàn thương mại điện tử",
            "result":    f"🛒 {tmdt_desc}",
            "citation":  {**_CITE_68, "note": "Quy định khấu trừ thuế qua sàn TMĐT"},
        })

    return {
        "annual_revenue":      annual_revenue,
        "is_exempt":           is_exempt,
        "tax_method":          tax_method,
        "tax_method_desc":     tax_method_desc,
        "filing_frequency":    filing_frequency,
        "filing_desc":         filing_desc,
        "einvoice_required":   einvoice_required,
        "einvoice_desc":       einvoice_desc,
        "tmdt_withholding":    tmdt_withholding,
        "obligations":         obligations,
        "notes":               notes,
        "citation":            {**_CITE_68, "note": "Đánh giá nghĩa vụ thuế tổng hợp"},
        "summary":             _format_obligation_summary(
            annual_revenue, is_exempt, tax_method, filing_frequency,
            einvoice_required, tmdt_withholding,
        ),
    }


def _format_obligation_summary(
    revenue: float,
    is_exempt: bool,
    tax_method: str,
    filing_freq: str,
    einvoice,
    tmdt: bool,
) -> str:
    if is_exempt:
        return f"Doanh thu {revenue/1e6:.0f} triệu ≤ 500 triệu → MIỄN THUẾ GTGT và TNCN."

    method_map   = {"either": "tỷ lệ% hoặc lợi nhuận (tự chọn)", "profit": "lợi nhuận (bắt buộc)"}
    filing_map   = {"quarterly": "theo quý", "monthly": "theo tháng"}
    einvoice_map = {True: "bắt buộc", False: "không áp dụng", "optional": "khuyến khích"}

    parts = [
        f"Doanh thu {revenue/1e6:.0f} triệu/năm:",
        f"• Phương pháp thuế TNCN: {method_map.get(tax_method, tax_method)}",
        f"• Kê khai GTGT: {filing_map.get(filing_freq, filing_freq)}",
        f"• HĐĐT: {einvoice_map.get(einvoice, str(einvoice))}",
    ]
    if tmdt:
        parts.append("• TMĐT: sàn có trách nhiệm khấu trừ thuế thay")
    return "\n".join(parts)
