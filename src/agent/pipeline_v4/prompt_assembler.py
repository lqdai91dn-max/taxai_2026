"""
src/agent/pipeline_v4/prompt_assembler.py — P6 Dynamic Prompt Assembly

Mục tiêu: thay thế monolithic prompt bằng prompt được lắp ghép tất định (deterministic)
từ QueryIntent + retrieved chunks. Mỗi lần chỉ load blocks liên quan.

Nguyên tắc thiết kế:
  [P6.1] Prompt ngắn gọn — chỉ load domain rules liên quan đến query
  [P6.2] Enforce JSON schema cứng — LLM KHÔNG được tự ý thay đổi format
  [P6.3] Cấm computation — LLM chỉ extract params, Python tính toán
  [P6.4] Source bắt buộc per param — mỗi param phải có chunk_id nguồn

Usage:
    from src.agent.pipeline_v4.prompt_assembler import assemble_reasoner_prompt
    system_prompt = assemble_reasoner_prompt(query_intent, retrieved_chunks)
    # Dùng system_prompt trong LLM Legal Reasoner (step 6.1)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# BLOCK 0 — BASE INSTRUCTION (LUÔN CÓ MẶT)
# Chứa: JSON schema cứng, cấm computation, yêu cầu source per param
# ═══════════════════════════════════════════════════════════════════════════════

BASE_INSTRUCTION = """\
Bạn là chuyên gia pháp lý thuế Việt Nam. Nhiệm vụ: đọc tài liệu pháp lý và trích xuất tham số.

══ QUY TẮC BẮT BUỘC ══
[1] KHÔNG tự tính số tiền thuế. Python Calculator sẽ tính — bạn chỉ cần trích xuất đúng tham số.
[2] KHÔNG bịa đặt thông tin không có trong tài liệu hoặc câu hỏi.
[3] MỌI tham số phải có "source": chunk_id nếu lấy từ tài liệu, null nếu từ câu hỏi.
[4] Nếu thiếu tham số bắt buộc → đặt clarification_needed=true, KHÔNG đoán.
[5] Output là JSON hợp lệ DUY NHẤT — không thêm text, markdown, giải thích ngoài JSON.
[6] Trường "assumptions": chỉ ghi điều kiện pháp lý bằng văn xuôi ngắn gọn.
    - ĐÚNG: "Cá nhân cư trú đủ 183 ngày", "Năm tính thuế 2026", "Áp dụng Luật 109/2025/QH15"
    - SAI:  "Giảm trừ = 11M + 4.4M = 15.4M", "Thu nhập tính thuế = 360M - 132M"
    → Không viết công thức, phép tính, dấu bằng trong assumptions.

══ QUY TẮC CHUYỂN ĐỔI ĐƠN VỊ (được phép) ══
  Nếu user nói "tháng" → params cần "năm": convert thầm lặng, ghi vào assumptions.
  Ví dụ: "lương 30 triệu/tháng" → gross_income = 360000000, assumptions = ["Thu nhập đã annualize từ 30M/tháng × 12"]
  Lưu ý: assumptions phải là câu văn, KHÔNG phải công thức ("30M × 12 = 360M" là SAI).

══ JSON SCHEMA BẮT BUỘC (copy chính xác, không thay đổi key) ══
{
  "template_type": "<PIT_full | PIT_progressive | HKD_percentage | HKD_profit | deduction_calc | explain>",
  "params_validated": {
    "<tên_tham_số>": {
      "value": <số nguyên hoặc chuỗi category hoặc null — KHÔNG phải công thức>,
      "source": "<chunk_id hoặc null>"
    }
  },
  "assumptions": ["<điều kiện pháp lý bằng văn xuôi — KHÔNG có công thức toán>"],
  "clarification_needed": <true | false>,
  "clarification_question": "<câu hỏi nếu clarification_needed=true, null nếu không>",
  "scenarios": []
}

══ TEMPLATE VÀ THAM SỐ TƯƠNG ỨNG ══
  PIT_full        → gross_income (VND/năm), dependents (số người, mặc định 0), months (1-12, mặc định 12)
  PIT_progressive → annual_taxable_income (VND/năm, sau khi đã trừ giảm trừ)
  HKD_percentage  → annual_revenue (VND/năm), business_category (goods|services|manufacturing|other|real_estate)
  HKD_profit      → annual_revenue (VND/năm), annual_expenses (VND/năm), business_category
  deduction_calc  → dependents (số người), months (1-12)
  explain         → params_validated: {} (không cần tham số — dùng khi câu hỏi là tra cứu/giải thích)

══ MAPPING BUSINESS_CATEGORY ══
  goods         — phân phối, cung cấp hàng hóa (tiệm vàng, cửa hàng tạp hóa, ...)
  services      — dịch vụ không gắn hàng hóa (salon tóc, tư vấn, ...)
  manufacturing — sản xuất, vận tải, xây dựng bao thầu NVL, ăn uống (nhà hàng, café, ...)
  other         — hoạt động kinh doanh khác
  real_estate   — cho thuê bất động sản\
"""


# ═══════════════════════════════════════════════════════════════════════════════
# BLOCK 1 — DOMAIN RULES
# Load theo tax_domain từ QueryIntent (chỉ load domain liên quan)
# ═══════════════════════════════════════════════════════════════════════════════

DOMAIN_RULES: Dict[str, str] = {

    "HKD": """\
══ DOMAIN: HỘ KINH DOANH (HKD) ══
Nguồn: Nghị định 68/2026/NĐ-CP

Ngưỡng miễn thuế TNCN:
  • Doanh thu ≤ 500 triệu/năm → miễn TNCN (vẫn nộp GTGT)
  • Doanh thu > 500 triệu/năm → nộp cả GTGT + TNCN

Phương pháp áp dụng:
  • Doanh thu ≤ 3 tỷ/năm  → PP tỷ lệ % doanh thu → template: HKD_percentage
  • Doanh thu > 3 tỷ/năm  → BẮT BUỘC PP lợi nhuận → template: HKD_profit
    (PP lợi nhuận cần thêm annual_expenses — hỏi nếu không có)

Lưu ý trích xuất:
  • "doanh thu" → annual_revenue (tổng năm, đơn vị VND)
  • "chi phí hợp lệ / chi phí được trừ" → annual_expenses (chỉ dùng cho HKD_profit)
  • Ngành nghề → business_category (xem MAPPING ở trên)\
""",

    "PIT": """\
══ DOMAIN: THUẾ THU NHẬP CÁ NHÂN (TNCN) ══
Nguồn: Luật Thuế TNCN 109/2025/QH15 (hiệu lực 01/07/2026)

Biểu thuế lũy tiến 5 bậc (áp dụng cho thu nhập từ lương/công):
  Bậc 1: ≤ 120 triệu/năm          → 5%
  Bậc 2: 120 – 360 triệu/năm      → 10%
  Bậc 3: 360 – 720 triệu/năm      → 20%
  Bậc 4: 720 triệu – 1,2 tỷ/năm  → 30%
  Bậc 5: > 1,2 tỷ/năm             → 35%

Giảm trừ gia cảnh (áp dụng từ 01/07/2026):
  • Bản thân: 15,5 triệu/tháng
  • Mỗi người phụ thuộc: 6,2 triệu/tháng

Lựa chọn template:
  • Có gross_income + cần tính từ đầu → PIT_full
  • Đã biết thu nhập tính thuế (sau giảm trừ) → PIT_progressive
  • Chỉ hỏi về giảm trừ → deduction_calc

Lưu ý trích xuất:
  • "lương / thu nhập" → gross_income (tổng năm = lương tháng × 12)
  • "người phụ thuộc / con" → dependents (số nguyên ≥ 0)
  • Thu nhập tháng → nhân 12 để ra năm, ghi assumption\
""",

    "VAT": """\
══ DOMAIN: THUẾ GIÁ TRỊ GIA TĂNG (GTGT) ══
Lưu ý: GTGT của HKD thường được tính cùng với TNCN trong HKD_percentage.
Nếu câu hỏi chỉ về GTGT độc lập → clarification_needed=true,
hỏi thêm: đối tượng là HKD hay doanh nghiệp?\
""",

    "TMDT": """\
══ DOMAIN: THƯƠNG MẠI ĐIỆN TỬ (TMĐT) ══
Nguồn: Nghị định 68/2026/NĐ-CP, Điều 8

Sàn TMĐT khấu trừ thay:
  • Sàn có trách nhiệm khai và nộp thuế thay cho người bán
  • Tỷ lệ khấu trừ: theo ngành nghề của người bán (goods/services/manufacturing)
  • Người bán trên sàn → template: HKD_percentage, business_category dựa theo ngành

Lưu ý: nếu câu hỏi từ góc độ sàn (platform) → who=enterprise, clarify thêm.\
""",
}


# ═══════════════════════════════════════════════════════════════════════════════
# BLOCK 2 — SPECIAL CONTEXT RULES
# Load có điều kiện theo who/intent từ QueryIntent
# ═══════════════════════════════════════════════════════════════════════════════

SPECIAL_CONTEXT_RULES: Dict[str, str] = {

    "WITHHOLDING_TAX": """\
══ CONTEXT ĐẶC BIỆT: KHẤU TRỪ TẠI NGUỒN ══
Nguồn: Luật Thuế TNCN 109/2025/QH15, Điều 25

Đơn vị chi trả thu nhập (công ty, tổ chức) có nghĩa vụ:
  • Khấu trừ thuế TNCN trước khi trả lương
  • Khai và nộp thuế thay cho người lao động

Trích xuất:
  • Góc độ người lao động → template: PIT_full (gross_income, dependents)
  • Góc độ người sử dụng lao động → tính tương tự PIT_full nhưng ghi assumption:
    "Tính từ góc độ khấu trừ tại nguồn"\
""",

    "REAL_ESTATE": """\
══ CONTEXT ĐẶC BIỆT: CHUYỂN NHƯỢNG BẤT ĐỘNG SẢN ══
Nguồn: Luật Thuế TNCN 109/2025/QH15

Thuế TNCN từ chuyển nhượng BĐS:
  • Thuế suất: 2% trên giá chuyển nhượng (hoặc giá do Nhà nước quy định nếu cao hơn)
  • Đây là thuế đơn giản, KHÔNG dùng biểu lũy tiến
  • Template: PIT_progressive với annual_taxable_income = giá chuyển nhượng × 2%
    HOẶC clarify nếu chưa rõ giá

Lưu ý: nếu là cho thuê BĐS → đây là HKD real_estate, không phải PIT chuyển nhượng.\
""",

    "LOTTERY": """\
══ CONTEXT ĐẶC BIỆT: XỔ SỐ / TRÚNG THƯỞNG ══
Nguồn: Luật Thuế TNCN 109/2025/QH15

  • Thuế suất: 10% trên phần thu nhập vượt 10 triệu đồng/lần
  • Ví dụ: trúng 500 triệu → thuế = (500M - 10M) × 10% = 49 triệu
  • Template: PIT_progressive, annual_taxable_income = (prize_value - 10_000_000)
  • Ghi assumption: "Áp dụng mức miễn 10 triệu/lần theo Luật 109/2025/QH15"\
""",

    "THRESHOLD_QUERY": """\
══ CONTEXT ĐẶC BIỆT: CÂU HỎI VỀ NGƯỠNG / ĐIỀU KIỆN ══
Người dùng hỏi về điều kiện, ngưỡng miễn thuế — không cần tính toán số tiền.

Nhiệm vụ: đối chiếu điều kiện từ tài liệu, trả lời Có/Không và nêu điều kiện cụ thể.
→ clarification_needed=false nếu đủ thông tin
→ template_type: "explain" (dùng thống nhất cho mọi câu hỏi tra cứu/giải thích)
→ params_validated: {} (không cần tham số tính toán)\
""",
}


# ═══════════════════════════════════════════════════════════════════════════════
# INTENT TASK BLOCKS
# Ép nhiệm vụ cụ thể theo intent.primary
# ═══════════════════════════════════════════════════════════════════════════════

_INTENT_TASK: Dict[str, str] = {
    "calculate": """\
══ NHIỆM VỤ: TRÍCH XUẤT THAM SỐ ĐỂ TÍNH THUẾ ══
Trích xuất ĐẦY ĐỦ các tham số cần thiết (xem TEMPLATE TƯƠNG ỨNG ở trên).
KHÔNG tự tính số tiền thuế — Python calculator sẽ tính sau.
Nếu thiếu tham số bắt buộc → clarification_needed=true.\
""",
    "explain": """\
══ NHIỆM VỤ: TRA CỨU ĐIỀU KIỆN / QUY ĐỊNH ══
Đối chiếu thông tin người dùng cung cấp với văn bản pháp lý.
params_validated có thể để {} nếu không cần tính toán.
Nếu câu hỏi có thể trả lời trực tiếp từ tài liệu → clarification_needed=false.\
""",
}


# ═══════════════════════════════════════════════════════════════════════════════
# ASSEMBLY FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def assemble_reasoner_prompt(
    query_intent,                       # QueryIntent object từ P5.1
    retrieved_chunks: List[Dict],       # Top chunks từ retrieval + reranker
    raw_query: str = "",               # Câu hỏi gốc của user
) -> str:
    """
    Lắp ghép system prompt cho LLM Legal Reasoner (step 6.1).

    Deterministic assembly:
      1. BASE_INSTRUCTION (luôn có)
      2. DOMAIN_RULES[domain] cho mỗi tax_domain trong QueryIntent
      3. SPECIAL_CONTEXT_RULES theo who/activity_group/flags
      4. INTENT_TASK theo intent.primary
      5. RAG context (top chunks)
      6. Câu hỏi user

    Args:
        query_intent:     QueryIntent object (P5.1). None → dùng prompt tối giản.
        retrieved_chunks: List hits từ hybrid search (đã rerank P5.3).
        raw_query:        Câu hỏi gốc.

    Returns:
        Assembled system prompt string.
    """
    parts: List[str] = [BASE_INSTRUCTION]

    if query_intent is not None:
        # ── 1. Domain rules ───────────────────────────────────────────────
        domains = _get_fv_list(query_intent, "tax_domain")
        for domain in domains:
            rule = DOMAIN_RULES.get(domain)
            if rule:
                parts.append(rule)

        # ── 2. Special context rules ──────────────────────────────────────
        who_list       = _get_fv_list(query_intent, "who")
        activity_list  = _get_fv_list(query_intent, "activity_group")
        intent_primary = _get_intent_primary(query_intent)
        flags          = _get_flags(query_intent)

        # Employer/enterprise → withholding tax context
        if any(w in who_list for w in ("employer", "enterprise")):
            parts.append(SPECIAL_CONTEXT_RULES["WITHHOLDING_TAX"])

        # Real estate transfer
        if "real_estate_transfer" in activity_list:
            parts.append(SPECIAL_CONTEXT_RULES["REAL_ESTATE"])

        # Lottery/prizes
        if "lottery_prizes" in activity_list:
            parts.append(SPECIAL_CONTEXT_RULES["LOTTERY"])

        # Explain/condition query
        if intent_primary == "explain":
            parts.append(SPECIAL_CONTEXT_RULES["THRESHOLD_QUERY"])

        # ── 3. Intent task ────────────────────────────────────────────────
        task_block = _INTENT_TASK.get(intent_primary)
        if task_block:
            parts.append(task_block)

    # ── 4. RAG context ────────────────────────────────────────────────────
    if retrieved_chunks:
        rag_text = _format_rag_context(retrieved_chunks)
        parts.append(f"══ TÀI LIỆU PHÁP LÝ TRUY XUẤT ══\n{rag_text}")

    # ── 5. Câu hỏi user ───────────────────────────────────────────────────
    if raw_query:
        parts.append(f"══ CÂU HỎI NGƯỜI DÙNG ══\n{raw_query}")

    parts.append("Trả lời bằng JSON hợp lệ theo schema đã quy định ở trên.")

    return "\n\n".join(parts)


def estimate_token_count(prompt: str) -> int:
    """Ước tính số tokens (~4 chars/token cho tiếng Việt)."""
    return len(prompt) // 4


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_fv_list(query_intent, field: str) -> List[str]:
    """Đọc list value từ FieldValue (graceful)."""
    try:
        fv = getattr(query_intent, field, None)
        if fv is None:
            return []
        val = fv.value if hasattr(fv, "value") else fv
        if isinstance(val, list):
            return [str(v) for v in val if v and str(v) not in ("UNSPECIFIED", "")]
        if isinstance(val, str) and val not in ("UNSPECIFIED", ""):
            return [val]
        return []
    except Exception:
        return []


def _get_intent_primary(query_intent) -> str:
    """Lấy intent.primary từ QueryIntent."""
    try:
        fv = getattr(query_intent, "intent", None)
        if fv is None:
            return ""
        val = fv.value if hasattr(fv, "value") else fv
        if isinstance(val, dict):
            return val.get("primary", "")
        return ""
    except Exception:
        return ""


def _get_flags(query_intent) -> Dict[str, Any]:
    """Lấy flags dict từ QueryIntent."""
    try:
        fv = getattr(query_intent, "flags", None)
        if fv is None:
            return {}
        val = fv.value if hasattr(fv, "value") else fv
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def _format_rag_context(chunks: List[Dict], max_chunks: int = 12, max_chars_per_chunk: int = 400) -> str:
    """
    Format retrieved chunks thành context block cho LLM.

    Giới hạn:
      - Top 12 chunks (ưu tiên chunks đã rerank cao nhất)
      - 400 chars/chunk (tránh prompt quá dài)
    """
    lines: List[str] = []
    for chunk in chunks[:max_chunks]:
        chunk_id   = chunk.get("chunk_id", "")
        doc_id     = chunk.get("metadata", {}).get("doc_id", "")
        breadcrumb = chunk.get("metadata", {}).get("breadcrumb", "")
        text       = (chunk.get("text") or chunk.get("snippet") or "")[:max_chars_per_chunk]
        score      = chunk.get("final_score") or chunk.get("rrf_score") or 0.0

        # Format: [chunk_id | doc_id | breadcrumb]
        header = f"[{chunk_id} | {doc_id}"
        if breadcrumb:
            header += f" | {breadcrumb}"
        header += f" | score={score:.4f}]"

        lines.append(f"{header}\n{text}")

    return "\n\n---\n\n".join(lines)
