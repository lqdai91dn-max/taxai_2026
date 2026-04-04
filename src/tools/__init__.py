"""
Tool registry cho Tax AI — 8 tools active.

Phase A (deterministic calculators):
  1. calculate_tax_hkd              — GTGT + TNCN cho HKD (phương pháp doanh thu)
  2. calculate_tncn_progressive     — TNCN lũy tiến 5 bậc (cá nhân tiền lương)
  3. calculate_deduction            — Giảm trừ gia cảnh TNCN
  4. calculate_tax_hkd_profit       — GTGT + TNCN cho HKD (phương pháp lợi nhuận)

Phase B (retrieval + lookup wrappers):
  5. search_legal_docs              — hybrid BM25 + vector search
  6. check_doc_validity             — hiệu lực văn bản tại một ngày
  7. resolve_legal_reference        — parse citation text → doc_id + article_id

Phase B+ (rule engine):
  8. evaluate_tax_obligation        — Rule engine: miễn thuế, kỳ kê khai, HĐĐT, TMĐT

DISABLED (Neo4j offline — silent fail, burns iterations):
  - get_article                    → TOOL_REGISTRY only, NOT in TOOL_DEFINITIONS
  - get_article_with_amendments    → TOOL_REGISTRY only, NOT in TOOL_DEFINITIONS
  - get_impl_chain                 → TOOL_REGISTRY only, NOT in TOOL_DEFINITIONS
  - get_guidance                   → TOOL_REGISTRY only, NOT in TOOL_DEFINITIONS

Usage:
    from src.tools import TOOL_DEFINITIONS, TOOL_REGISTRY
    result = TOOL_REGISTRY["calculate_tax_hkd"](annual_revenue=2e9, business_category="services")
"""

from src.tools.calculator_tools import (
    calculate_tax_hkd,
    calculate_tncn_progressive,
    calculate_deduction,
    calculate_tax_hkd_profit,
    TAX_HKD_GTGT_RATES,
    TAX_HKD_TNCN_REVENUE_RATES,
    TAX_HKD_TNCN_PROFIT_BRACKETS,
    TAX_TNCN_PROGRESSIVE_BRACKETS,
)
from src.tools.retrieval_tools import (
    search_legal_docs,
    get_article,
    get_guidance,
    get_impl_chain,
)
from src.tools.lookup_tools import (
    check_doc_validity,
    resolve_legal_reference,
    get_article_with_amendments,
)
from src.tools.rule_engine import evaluate_tax_obligation

# ── Tool registry — dùng trong Phase C planner ───────────────────────────────

TOOL_REGISTRY: dict = {
    # Phase A — calculators
    "calculate_tax_hkd":           calculate_tax_hkd,
    "calculate_tncn_progressive":  calculate_tncn_progressive,
    "calculate_deduction":         calculate_deduction,
    "calculate_tax_hkd_profit":    calculate_tax_hkd_profit,
    # Phase B — retrieval
    "search_legal_docs":           search_legal_docs,
    "get_article":                 get_article,
    "check_doc_validity":          check_doc_validity,
    "get_guidance":                get_guidance,
    "get_impl_chain":              get_impl_chain,
    "resolve_legal_reference":     resolve_legal_reference,
    "get_article_with_amendments": get_article_with_amendments,
    # Phase B+ — rule engine
    "evaluate_tax_obligation":     evaluate_tax_obligation,
}

# ── Gemini function calling definitions ──────────────────────────────────────

TOOL_DEFINITIONS = [
    # ── Phase A ───────────────────────────────────────────────────────────────
    {
        "name": "calculate_tax_hkd",
        "description": (
            "Tính thuế GTGT và thuế TNCN cho hộ kinh doanh / cá nhân kinh doanh "
            "theo Nghị định 68/2026/NĐ-CP. "
            "Trả về breakdown từng loại thuế kèm citation điều luật. "
            "Dùng khi user hỏi về số tiền thuế phải đóng của HKD."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "annual_revenue": {
                    "type": "number",
                    "description": "Doanh thu năm (VND). Ví dụ: 2000000000 cho 2 tỷ.",
                },
                "business_category": {
                    "type": "string",
                    "enum": ["goods", "services", "manufacturing", "other", "real_estate"],
                    "description": (
                        "Nhóm ngành: goods=hàng hóa, services=dịch vụ, "
                        "manufacturing=sản xuất/vận tải, other=khác, real_estate=BĐS."
                    ),
                },
            },
            "required": ["annual_revenue", "business_category"],
        },
    },
    {
        "name": "calculate_tncn_progressive",
        "description": (
            "Tính thuế TNCN theo biểu lũy tiến 5 bậc "
            "(Luật 109/2025/QH15, hiệu lực 01/07/2026). "
            "Dùng cho cá nhân có thu nhập từ lương/tiền công. "
            "KHÔNG dùng cho hộ kinh doanh."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "annual_taxable_income": {
                    "type": "number",
                    "description": (
                        "Thu nhập tính thuế năm (VND) — "
                        "sau khi đã trừ giảm trừ gia cảnh, bảo hiểm, v.v."
                    ),
                },
            },
            "required": ["annual_taxable_income"],
        },
    },

    {
        "name": "calculate_deduction",
        "description": (
            "Tính giảm trừ gia cảnh TNCN cho cá nhân có thu nhập từ tiền lương/công. "
            "Mức mới theo Luật 109/2025/QH15: 15.5 triệu/tháng (bản thân) "
            "+ 6.2 triệu/tháng × số người phụ thuộc. "
            "Dùng để xác định thu nhập tính thuế TNCN lũy tiến."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dependents": {
                    "type": "integer",
                    "description": "Số người phụ thuộc đã đăng ký (≥0, mặc định 0).",
                },
                "months": {
                    "type": "integer",
                    "description": "Số tháng tính giảm trừ (1–12, mặc định 12 = cả năm).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "calculate_tax_hkd_profit",
        "description": (
            "Tính thuế GTGT + TNCN cho HKD theo phương pháp lợi nhuận. "
            "Dùng cho HKD doanh thu > 3 tỷ (bắt buộc) hoặc 500M–3B (tự chọn). "
            "Công thức TNCN: (Doanh thu − Chi phí hợp lý) × Thuế suất. "
            "KHÁC với calculate_tax_hkd (phương pháp doanh thu: DT − 500M)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "annual_revenue": {
                    "type": "number",
                    "description": "Tổng doanh thu năm (VND). Phải > 500 triệu.",
                },
                "annual_expenses": {
                    "type": "number",
                    "description": "Tổng chi phí hợp lý, hợp lệ năm (VND). Phải ≥ 0 và < doanh thu.",
                },
                "business_category": {
                    "type": "string",
                    "enum": ["goods", "services", "manufacturing", "other", "real_estate"],
                    "description": "Nhóm ngành: goods/services/manufacturing/other/real_estate.",
                },
            },
            "required": ["annual_revenue", "annual_expenses", "business_category"],
        },
    },

    # ── Phase B ───────────────────────────────────────────────────────────────
    {
        "name": "search_legal_docs",
        "description": (
            "Tìm kiếm điều khoản pháp luật liên quan bằng hybrid search. "
            "Dùng khi cần tìm quy định chung hoặc không biết Điều cụ thể nào."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Câu hỏi hoặc từ khóa pháp luật cần tìm.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Số kết quả (1-10, mặc định 5).",
                },
                "doc_filter": {
                    "type": "string",
                    "description": "doc_id cụ thể để giới hạn phạm vi (optional).",
                },
                "exclude_doc_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Danh sách doc_id cần loại trừ khỏi kết quả. Dùng khi biết chắc văn bản đó không liên quan để tránh false positive. Ví dụ: ['310_2025_NDCP'] khi câu hỏi không liên quan đến xử phạt vi phạm.",
                },
            },
            "required": ["query"],
        },
    },
    # get_article — DISABLED: Neo4j offline, silent fail burns iterations
    {
        "name": "check_doc_validity",
        "description": (
            "Kiểm tra hiệu lực pháp lý của một văn bản tại một ngày. "
            "Trả về status: valid / pending / expired và danh sách văn bản sửa đổi nó. "
            "Dùng trước khi trích dẫn luật để đảm bảo văn bản còn hiệu lực."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "ID văn bản. Ví dụ: '109_2025_QH15', '68_2026_NDCP'.",
                },
                "query_date": {
                    "type": "string",
                    "description": "Ngày kiểm tra (YYYY-MM-DD). Mặc định: hôm nay.",
                },
            },
            "required": ["doc_id"],
        },
    },
    # get_guidance  — DISABLED: Neo4j offline, silent fail burns iterations
    # get_impl_chain — DISABLED: Neo4j offline, silent fail burns iterations
    {
        "name": "resolve_legal_reference",
        "description": (
            "Chuyển đổi tham chiếu pháp luật dạng text sang doc_id và article_id. "
            "Ví dụ: 'Điều 5 Nghị định 68/2026/NĐ-CP' → doc_id + article_id. "
            "Dùng khi user hoặc LLM đề cập đến một điều luật cụ thể bằng tên."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference_text": {
                    "type": "string",
                    "description": (
                        "Chuỗi tham chiếu tự nhiên. "
                        "Ví dụ: 'Điều 5 NĐ 68/2026/NĐ-CP', 'khoản 2 Điều 9 Luật 109/2025/QH15'."
                    ),
                },
            },
            "required": ["reference_text"],
        },
    },
    # get_article_with_amendments — DISABLED: Neo4j offline, silent fail burns iterations

    # ── Phase B+ — Rule engine ────────────────────────────────────────────────
    {
        "name": "evaluate_tax_obligation",
        "description": (
            "Đánh giá tổng hợp nghĩa vụ thuế và yêu cầu hành chính cho HKD. "
            "Trả lời: miễn thuế không, phương pháp tính thuế nào, kê khai quý hay tháng, "
            "có cần hóa đơn điện tử không, sàn TMĐT có khấu trừ thuế không. "
            "Dùng khi user hỏi về quy trình, thủ tục, hoặc 'phải làm gì'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "annual_revenue": {
                    "type": "number",
                    "description": "Tổng doanh thu ước tính trong năm (VND).",
                },
                "has_online_sales": {
                    "type": "boolean",
                    "description": "HKD có bán hàng trên sàn TMĐT không (default false).",
                },
                "platform_has_payment": {
                    "type": "boolean",
                    "description": (
                        "Sàn TMĐT có chức năng đặt hàng trực tuyến và thanh toán không "
                        "(Shopee/Lazada/TikTok Shop = true). Default false."
                    ),
                },
            },
            "required": ["annual_revenue"],
        },
    },
]

__all__ = [
    # Phase A
    "calculate_tax_hkd",
    "calculate_tncn_progressive",
    "calculate_deduction",
    "calculate_tax_hkd_profit",
    # Phase B
    "search_legal_docs",
    "get_article",
    "check_doc_validity",
    "get_guidance",
    "get_impl_chain",
    "resolve_legal_reference",
    "get_article_with_amendments",
    # Phase B+
    "evaluate_tax_obligation",
    # Registry
    "TOOL_REGISTRY",
    "TOOL_DEFINITIONS",
    # Tax tables
    "TAX_HKD_GTGT_RATES",
    "TAX_HKD_TNCN_REVENUE_RATES",
    "TAX_HKD_TNCN_PROFIT_BRACKETS",
    "TAX_TNCN_PROGRESSIVE_BRACKETS",
]
