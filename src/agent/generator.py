"""
src/agent/generator.py — Stage 3: Generator

Nhận PipelineState với retrieval chunks + calc result đã sẵn sàng,
gọi Gemini với specialist prompt phù hợp để tạo câu trả lời có citations.

Design:
- Mỗi scope có specialist prompt riêng (~60 dòng) thay vì 700-dòng monolith
- Tất cả prompts chia sẻ BASE_RULES (quy tắc citation, exception scan, format)
- Structured output schema → Gemini trả JSON, parse thành GeneratorOutput
- FM03: injection negative constraint khi regeneration_count > 0
- FM08: đánh dấu conflicting chunks trong context, ưu tiên winner

Failure modes handled:
  FM03  Generator hallucination  → key_facts không có trong chunks
  FM04  Fail x2 sau regenerate   → Level 2 degrade (caller xử lý)
  FM07  Gemini timeout           → propagate TimeoutError
  FM08  Conflicting chunks       → bỏ qua loser, ghi chú trong context
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from google import genai
from google.genai import types

from src.agent.schemas import (
    CalcOutput,
    Citation,
    ConflictPair,
    GeneratorOutput,
    PipelineState,
    QueryType,
    RetrievedChunk,
    RetrievalOutput,
)

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"

# thinkingBudget=0 = không dùng thinking tokens → giảm latency ~3x
_NO_THINKING = types.ThinkingConfig(thinkingBudget=0)


# ── Specialist Prompts ─────────────────────────────────────────────────────────

BASE_RULES = """Bạn là TaxAI — trợ lý tư vấn pháp luật thuế Việt Nam.
Ngày hôm nay: {today}.

## QUY TẮC BẮT BUỘC

### 1. CHỈ DÙNG DỮ LIỆU TỪ CHUNKS
- Mọi con số, tỷ lệ, điều kiện PHẢI xuất phát từ chunks được cung cấp
- KHÔNG dùng kiến thức nền từ training data để điền vào chỗ thiếu
- Nếu chunks không đủ → viết: "Không đủ cơ sở pháp lý để xác định [vấn đề]."

### 2. KIỂM TRA NGOẠI LỆ TRƯỚC KHI KẾT LUẬN (BẮT BUỘC)
Quét toàn bộ chunks trước khi viết câu trả lời — tìm:
- "trừ trường hợp", "trừ khi", "ngoại lệ", "không phải khai", "miễn"
- "bị phạt", "không bị phạt", "tiền chậm nộp", "truy thu"
Nếu có ngoại lệ/hệ quả pháp lý → BẮT BUỘC đưa vào câu trả lời.

### 3. KIỂM TRA HIỆU LỰC
- Luật 109/2025/QH15: hiệu lực 01/07/2026 — KHÔNG áp dụng các quy định này cho ngày hiện tại
- Mức giảm trừ gia cảnh năm 2026 (từ 01/01/2026 theo NQ110/2025/UBTVQH15):
  Bản thân: 15,5 triệu/tháng | Người phụ thuộc: 6,2 triệu/tháng
- Mức cũ (NQ954/2020): 11 triệu + 4,4 triệu — chỉ áp dụng trước 2026
- NĐ68/2026/NĐ-CP: hiệu lực 05/03/2026

### 4. CITATION ĐẦY ĐỦ
- Định dạng: "Theo [tên văn bản số XX/XXXX], Điều X Khoản Y..."
- XÁC MINH TRƯỚC KHI TRÍCH DẪN: nội dung Điều/Khoản phải khớp với điều muốn nói
- Khi kết luận "không tìm thấy" → vẫn cite văn bản đã kiểm tra

### 5. THUẬT NGỮ PHÁP LÝ — KHÔNG PARAPHRASE
- "khai thay, nộp thay" (không phải "sàn khai hộ")
- "miễn kê khai" (không phải "không cần khai")
- "khấu trừ tại nguồn" (không phải "sàn trừ trước")
- "không chịu thuế TNCN" (không phải "được miễn" khi luật dùng từ này)

### 6. PHÂN ĐỊNH CHỦ THỂ TNCN
Trước khi trả lời, xác định rõ câu hỏi về nghĩa vụ của:
- **Cá nhân (người nộp thuế)** — quyền miễn trừ, giảm trừ, nghĩa vụ thực tế
- **Tổ chức chi trả thu nhập** — khấu trừ, kê khai, quyết toán thay
Chỉ trả lời theo đúng chủ thể — không suy diễn hoặc thay thế.

## CẤU TRÚC CÂU TRẢ LỜI
1. Kết luận ngắn (có/không, tỷ lệ, điều kiện chính)
2. Căn cứ pháp lý (tên văn bản + Điều/Khoản cụ thể)
3. Lưu ý khác nếu có (hiệu lực, ngoại lệ, đối tượng áp dụng)
"""

SPECIALIST_PIT = """## TNCN — QUY TẮC BỔ SUNG

### Tính thuế lũy tiến
- Kết quả calculator đã tính sẵn → đọc và trình bày số liệu từ đó
- KHÔNG tự tính lại bậc thuế từ bộ nhớ

### Giảm trừ gia cảnh
- Áp dụng đúng mức theo năm (xem BASE_RULES phần 3 — kiểm tra hiệu lực)
- NPT đăng ký bất kỳ tháng nào trong năm → được giảm trừ từ tháng 1 (hồi tố)

### Thời điểm xác định thu nhập
- Thu nhập tính thuế = thời điểm tổ chức/cá nhân chi trả (không phải thời điểm phát sinh)
- Lương tháng 12/năm N chi trả tháng 1/năm N+1 = thu nhập năm N+1

### Quyết toán thuế
- PHẢI nêu ngoại lệ không bắt buộc QT (d.3): số thuế phải nộp thêm ≤ 50.000đ hoặc nhỏ hơn số đã tạm nộp
- Điều 11 K8 Đb NĐ126 quy định địa điểm nộp hồ sơ — không phải điều kiện phải QT

### Cá nhân không cư trú
- Tỷ lệ khấu trừ theo NĐ117/2025 Điều 5 K2: hàng hóa 1%, dịch vụ 5%, vận tải 2%
- Khác hoàn toàn với cá nhân cư trú — không áp dụng biểu lũy tiến
"""

SPECIALIST_HKD = """## HKD / CÁ NHÂN KINH DOANH — QUY TẮC BỔ SUNG

### Ngưỡng doanh thu 500 triệu
- Dưới 500 triệu/năm → miễn GTGT + TNCN (NĐ68/2026, hiệu lực 05/03/2026)
- Phải thông báo doanh thu thực tế: deadline 31/01 năm tiếp theo

### Phương pháp tính thuế
- PP tỷ lệ % doanh thu: GTGT = doanh_thu × tỷ_lệ_gtgt; TNCN = doanh_thu × tỷ_lệ_tncn
- PP lợi nhuận (doanh thu ≥ 3 tỷ hoặc tự chọn): thu nhập chịu thuế = doanh thu - chi phí

### Sàn TMĐT
- Sàn có chức năng đặt hàng + thanh toán → "khai thay, nộp thay" (NĐ68 Điều 11 K1)
- Người bán KHÔNG phải khai lại phần thuế sàn đã khai thay
- Tự khai khi: sàn không có chức năng thanh toán (K2) HOẶC doanh thu > 3 tỷ + PP lợi nhuận (K3)

### Chi phí được trừ (PP lợi nhuận)
- Đọc cả Khoản 1 (ĐƯỢC trừ) VÀ Khoản 2 (KHÔNG được trừ) Điều 6 NĐ68
- Lãi vay từ người thân/cá nhân: được trừ nhưng ≤ trần Bộ luật Dân sự (20%/năm)

### Chuyển đổi phương pháp từ 2026
- Mẫu 01/BK-HTK: bảng kê hàng tồn kho tại 31/12/2025
- Deadline: cùng tờ khai Q1/2026 hoặc chậm nhất 20/4/2026

### Kết quả calculator
- Đọc và trình bày đầy đủ từ kết quả calculator — không tóm tắt bỏ mất số liệu
"""

SPECIALIST_PENALTY = """## XỬ PHẠT VI PHẠM HÀNH CHÍNH THUẾ — QUY TẮC BỔ SUNG

### Kiểm tra ngoại lệ không bị phạt (BẮT BUỘC)
Sau khi xác định hành vi vi phạm → BẮT BUỘC kiểm tra:
- NĐ125 Điều 9 K3: tự nguyện khai bổ sung trước thanh tra → không bị phạt
- NĐ125 Điều 16 K3: khai sai không dẫn đến thiếu thuế → không bị phạt
- Tiền chậm nộp vẫn phát sinh dù không bị phạt (tính từ kỳ thiếu đến khi nộp bù)

### NĐ310/2025 Điều khoản chuyển tiếp (hiệu lực 16/01/2026)
- Vi phạm đã kết thúc trước 16/01/2026 → áp dụng NĐ125/2020 (luật cũ)
- Vi phạm đang thực hiện + phát hiện sau 16/01/2026 → áp dụng NĐ310
- Đã bị xử phạt trước 16/01/2026 mà còn khiếu nại → áp dụng NĐ125/2020
- Nguyên tắc "luật có lợi hơn" (LXLVPHC Điều 7): quy định mới nhẹ hơn → áp dụng có lợi

### Khai sai vs khai thiếu
- "khai đúng nộp chậm" ≠ "khai sai" → khác nhau về mức phạt
- Khai thiếu thu nhập → khấu trừ thiếu → thiếu thuế → tiền chậm nộp phát sinh
"""

SPECIALIST_GENERAL = """## TRA CỨU QUY ĐỊNH CHUNG — QUY TẮC BỔ SUNG

### Trả lời ngay không hỏi thêm khi câu hỏi về:
- Cơ chế/quy trình: "sàn TMĐT có tự khấu trừ không?", "500 triệu tính gộp hay từng cơ sở?"
- Quy định pháp lý chung, tỷ lệ %, ngưỡng
- Điều kiện/tiêu chí
- Thủ tục/hồ sơ

### Chỉ hỏi thêm khi thực sự cần tính số tiền cụ thể
và người dùng chưa cung cấp doanh thu/thu nhập.

### Câu trả lời không được rỗng
Luôn phải có ít nhất một câu trả lời dựa trên văn bản pháp luật từ chunks.
"""


# ── Schema cho Gemini structured output ────────────────────────────────────────

_GENERATOR_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Câu trả lời đầy đủ theo đúng cấu trúc yêu cầu.",
        },
        "citations": {
            "type": "array",
            "description": "Danh sách trích dẫn nguồn pháp lý xuất hiện trong câu trả lời.",
            "items": {
                "type": "object",
                "properties": {
                    "doc_id":  {"type": "string", "description": "ID văn bản (vd: 109_2025_QH15)"},
                    "article": {"type": "string", "description": "Điều/Khoản cụ thể (vd: Điều 8 Khoản 3)"},
                    "text":    {"type": "string", "description": "Nội dung đoạn trích ngắn (≤120 ký tự)"},
                    "label":   {"type": "string", "description": "Tên hiển thị (vd: Luật 109/2025/QH15)"},
                },
                "required": ["doc_id", "article", "text", "label"],
            },
        },
        "key_facts": {
            "type": "array",
            "description": (
                "Danh sách các fact cụ thể LẤY TỪ chunks — dùng để xác minh sau. "
                "Mỗi fact là substring ngắn có thể tìm thấy trong chunks (≤60 ký tự). "
                "Chỉ include fact về con số/điều kiện cụ thể, không include kết luận diễn giải."
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["answer", "citations", "key_facts"],
}


# ── Context Builder ────────────────────────────────────────────────────────────

# Giới hạn context để tránh attention dilution
_MAX_CONTEXT_CHUNKS = 8          # tối đa 8 chunks (gồm cả exception chunks)
_MAX_CONTEXT_CHARS  = 12_000     # tối đa ~12,000 ký tự (~4,000 tokens)

# C6: Display names cho các doc — dùng làm section header khi group by doc
_DOC_DISPLAY_NAMES: Dict[str, str] = {
    "68_2026_NDCP":      "NĐ 68/2026/NĐ-CP (HKD + TMĐT)",
    "117_2025_NDCP":     "NĐ 117/2025/NĐ-CP (TMĐT)",
    "109_2025_QH15":     "Luật 109/2025/QH15 (Thuế TNCN)",
    "110_2025_UBTVQH15": "NQ 110/2025/UBTVQH15 (Giảm trừ gia cảnh)",
    "149_2025_QH15":     "Luật 149/2025/QH15 (sửa đổi Luật TNCN)",
    "152_2025_TTBTC":    "TT 152/2025/TT-BTC (HKD)",
    "18_2026_TTBTC":     "TT 18/2026/TT-BTC",
    "198_2025_QH15":     "Luật 198/2025/QH15",
    "20_2026_NDCP":      "NĐ 20/2026/NĐ-CP",
    "310_2025_NDCP":     "NĐ 310/2025/NĐ-CP (Xử phạt VPHC)",
    "373_2025_NDCP":     "NĐ 373/2025/NĐ-CP (Quản lý thuế)",
    "125_2020_NDCP":     "NĐ 125/2020/NĐ-CP (Xử phạt cũ)",
    "126_2020_NDCP":     "NĐ 126/2020/NĐ-CP (Quản lý thuế)",
    "1296_CTNVT":        "CV 1296/CTNVT (Hướng dẫn QT TNCN)",
    "So_Tay_HKD":        "Sổ tay HKD",
    "111_2013_TTBTC":    "TT 111/2013/TT-BTC (Hướng dẫn TNCN)",
    "92_2015_TTBTC":     "TT 92/2015/TT-BTC (sửa đổi TT 111)",
    "108_2025_QH15":     "Luật 108/2025/QH15",
}

# Keywords xác định "exception chunk" — cần đặt SAU primary rule chunks
_EXCEPTION_KEYWORDS = (
    "trừ trường hợp", "ngoại lệ", "không phải khai", "không phải nộp",
    "miễn thuế", "được miễn", "không chịu thuế", "không bắt buộc",
    "không phải đăng ký", "không phải xuất", "trừ khi",
)


def _is_exception_chunk(chunk: RetrievedChunk) -> bool:
    """True nếu chunk chứa nội dung ngoại lệ/miễn trừ."""
    text_lower = chunk.text.lower()
    return any(kw in text_lower for kw in _EXCEPTION_KEYWORDS)


def _order_chunks_strategically(chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
    """
    Sắp xếp chunks theo thứ tự logic pháp lý:
      1. Primary rule chunks (cao điểm, không có exception keywords)
      2. Exception/miễn trừ chunks (luôn phải đọc sau rule)
      3. Supporting chunks (điểm thấp hơn, bổ sung)

    Giới hạn: tối đa _MAX_CONTEXT_CHUNKS, ưu tiên giữ ít nhất 1 exception chunk
    nếu có, để không bỏ sót ngoại lệ quan trọng.
    """
    primary    = [c for c in chunks if not _is_exception_chunk(c)]
    exceptions = [c for c in chunks if _is_exception_chunk(c)]

    # Giữ tối đa (MAX-2) primary + 2 exception → đảm bảo exception không bị drop
    max_primary    = max(1, _MAX_CONTEXT_CHUNKS - min(len(exceptions), 2))
    max_exceptions = _MAX_CONTEXT_CHUNKS - min(len(primary), max_primary)

    ordered = primary[:max_primary] + exceptions[:max_exceptions]
    return ordered[:_MAX_CONTEXT_CHUNKS]


def _ensure_doc_coverage(
    selected: List[RetrievedChunk],
    all_chunks: List[RetrievedChunk],
) -> List[RetrievedChunk]:
    """
    E5: Đảm bảo mỗi doc có ít nhất 1 chunk đại diện trong context.

    Nếu một doc xuất hiện trong all_chunks nhưng bị drop khỏi selected
    (do cutoff), swap vào slot cuối để đảm bảo coverage tối thiểu.
    Chỉ swap khi all_chunks có >1 doc — không xáo trộn khi single-doc query.
    """
    if len({c.doc_id for c in all_chunks}) <= 1:
        return selected  # single-doc: không cần coverage check

    result = list(selected)
    selected_doc_ids = {c.doc_id for c in result}
    all_doc_ids_ordered: List[str] = []  # giữ thứ tự xuất hiện
    for c in all_chunks:
        if c.doc_id not in all_doc_ids_ordered:
            all_doc_ids_ordered.append(c.doc_id)

    missing_docs = [d for d in all_doc_ids_ordered if d not in selected_doc_ids]

    for doc_id in missing_docs:
        top_chunk = next((c for c in all_chunks if c.doc_id == doc_id), None)
        if not top_chunk:
            continue
        if len(result) < _MAX_CONTEXT_CHUNKS:
            result.append(top_chunk)
        else:
            # Swap ra chunk cuối (lowest priority) để nhường chỗ cho đại diện doc bị miss
            result[-1] = top_chunk

    return result[:_MAX_CONTEXT_CHUNKS]


def _build_context(
    state: PipelineState,
    correction_instruction: Optional[str] = None,
) -> str:
    """
    Xây dựng context block để inject vào prompt:
    - Chunks (FM08 losers bị loại; strategic ordering; char limit)
    - Kết quả calculator (nếu có)
    - Ambiguous interpretations (nếu AMBIGUOUS)
    - Correction instruction (FM03 — chỉ khi regeneration_count > 0)
    """
    parts: List[str] = []

    # ── Chunks ─────────────────────────────────────────────────────────────────
    ret_out: Optional[RetrievalOutput] = state.retrieval_output
    if ret_out and ret_out.chunks:
        # Xác định winning/losing chunks (FM08)
        losing_ids: Set[str] = set()
        conflict_notes: Dict[str, str] = {}  # chunk_id → note
        if ret_out.has_conflict:
            for cp in ret_out.conflicts:
                loser = cp.chunk_id_a if cp.winner_id == cp.chunk_id_b else cp.chunk_id_b
                losing_ids.add(loser)
                conflict_notes[cp.winner_id] = f"[WINNER — {cp.reason}]"
                conflict_notes[loser] = f"[LOSER — bị thay thế bởi chunk thắng ({cp.reason})]"

        # Lọc losers → sắp xếp chiến lược → đảm bảo mỗi doc có đại diện (E5)
        winner_chunks = [c for c in ret_out.chunks if c.chunk_id not in losing_ids]
        ordered_chunks = _order_chunks_strategically(winner_chunks)
        selected_chunks = _ensure_doc_coverage(ordered_chunks, winner_chunks)

        # C6: Group chunks by doc_id (giữ thứ tự xuất hiện đầu tiên)
        from collections import OrderedDict as _OD
        doc_groups: Dict[str, List[RetrievedChunk]] = _OD()
        for chunk in selected_chunks:
            doc_groups.setdefault(chunk.doc_id, []).append(chunk)

        multi_doc = len(doc_groups) > 1
        parts.append("=== VĂN BẢN PHÁP LUẬT (CHUNKS) ===")
        total_chars = 0
        chunk_number = 1

        for doc_id, doc_chunks in doc_groups.items():
            # C6: Section header per document
            doc_display = _DOC_DISPLAY_NAMES.get(doc_id, doc_id)
            parts.append(f"\n--- {doc_display} ---")

            for chunk in doc_chunks:
                chunk_text = chunk.text.strip()

                if total_chars + len(chunk_text) > _MAX_CONTEXT_CHARS:
                    parts.append("[... bị cắt do giới hạn context]")
                    break

                # doc_id phải có trong header để LLM cite đúng format machine-readable
                header_parts = [f"[{chunk_number}]", f"doc_id={chunk.doc_id}"]
                if chunk.metadata.get("article"):
                    header_parts.append(chunk.metadata["article"])
                if chunk.effective_date:
                    header_parts.append(f"hiệu_lực={chunk.effective_date}")
                if chunk.chunk_id in conflict_notes:
                    header_parts.append(conflict_notes[chunk.chunk_id])
                if _is_exception_chunk(chunk):
                    header_parts.append("[exception]")

                parts.append("---")
                parts.append(" | ".join(header_parts))
                parts.append(chunk_text)
                total_chars += len(chunk_text)
                chunk_number += 1

        # E4: Multi-doc synthesis instruction
        if multi_doc:
            parts.append("")
            parts.append(
                "[Hướng dẫn tổng hợp đa nguồn: Các văn bản trên có thể quy định "
                "những khía cạnh khác nhau của cùng vấn đề. "
                "Xác định từng văn bản quy định điều gì, kết hợp đầy đủ trong câu trả lời. "
                "KHÔNG bỏ qua bất kỳ văn bản nào trong context.]"
            )

        if ret_out.has_conflict:
            parts.append("")
            parts.append(
                f"[Lưu ý FM08: Phát hiện {len(ret_out.conflicts)} cặp chunks mâu thuẫn — "
                "các chunks LOSER đã bị loại khỏi context. Chỉ dùng chunks WINNER ở trên.]"
            )

    # ── Calculator result ──────────────────────────────────────────────────────
    calc_out: Optional[CalcOutput] = state.calc_output
    if calc_out and not calc_out.error and calc_out.formatted:
        parts.append("")
        parts.append("=== KẾT QUẢ TÍNH TOÁN ===")
        parts.append(f"Tool: {calc_out.tool_name}")
        parts.append(calc_out.formatted)

        if calc_out.assumed_params:
            assumed_strs = [f"{k}={v}" for k, v in calc_out.assumed_params.items()]
            parts.append(f"[Tham số mặc định: {', '.join(assumed_strs)} — đề cập trong câu trả lời]")

    elif calc_out and calc_out.error:
        parts.append("")
        parts.append("=== TÍNH TOÁN ===")
        parts.append(f"[Không tính được: {calc_out.error}]")

    # ── Ambiguous interpretations ──────────────────────────────────────────────
    router_out = state.router_output
    if router_out and router_out.query_type == QueryType.AMBIGUOUS and router_out.ambiguous_interpretations:
        parts.append("")
        parts.append("=== CÂU HỎI CÓ NHIỀU CÁCH HIỂU ===")
        for j, interp in enumerate(router_out.ambiguous_interpretations, 1):
            parts.append(f"{j}. {interp}")
        parts.append("[Hãy trả lời cả hai cách hiểu — không bỏ sót cách nào]")

    # ── Scope mismatch warning ─────────────────────────────────────────────────
    if ret_out and ret_out.scope_expanded:
        parts.append("")
        parts.append(
            "[FM01: Không tìm thấy kết quả trong phạm vi ban đầu — đã mở rộng tìm kiếm. "
            "Chunks có thể từ phạm vi rộng hơn.]"
        )

    # ── FM03 Negative constraint (regeneration) ────────────────────────────────
    if correction_instruction:
        parts.append("")
        parts.append("=== RÀNG BUỘC BẮT BUỘC ===")
        parts.append(correction_instruction)

    return "\n".join(parts)


# ── Prompt Builder ─────────────────────────────────────────────────────────────

def _select_specialist(scopes: List[str]) -> str:
    """Chọn specialist prompt dựa trên scopes từ Router."""
    scope_set = {s.lower() for s in scopes}
    if "pit" in scope_set or "tncn" in scope_set:
        return SPECIALIST_PIT
    if "hkd" in scope_set or "tmdt" in scope_set:
        return SPECIALIST_HKD
    if "penalty" in scope_set or "xử_phạt" in scope_set or "xu_phat" in scope_set:
        return SPECIALIST_PENALTY
    # Nếu có nhiều scopes → ghép specialists liên quan
    parts = []
    if any(s in scope_set for s in ("pit", "tncn")):
        parts.append(SPECIALIST_PIT)
    if any(s in scope_set for s in ("hkd", "tmdt")):
        parts.append(SPECIALIST_HKD)
    if any(s in scope_set for s in ("penalty", "xử_phạt", "xu_phat")):
        parts.append(SPECIALIST_PENALTY)
    return "\n".join(parts) if parts else SPECIALIST_GENERAL


# ── Regeneration guard — thêm vào system prompt khi regeneration_count > 0 ──────
_NO_APOLOGY_INSTRUCTION = """
## RÀNG BUỘC TUYỆT ĐỐI CHO LẦN TRẢ LỜI NÀY
BẢN NHÁP TRƯỚC ĐÃ BỊ LOẠI DO VI PHẠM QUY TẮC.
TUYỆT ĐỐI KHÔNG được:
- Xin lỗi ("Xin lỗi vì...", "Rất tiếc...", "Tôi đã nhầm...")
- Giải thích lỗi ở lần trước ("Ở bản trước tôi đã...", "Lần trước sai vì...")
- Nhắc lại hoặc tham chiếu đến câu trả lời cũ
- Dùng kiến thức ngoài chunks được cung cấp

CHỈ được:
- Viết lại câu trả lời hoàn toàn từ đầu, trực tiếp vào vấn đề
- Sử dụng ĐÚNG dữ liệu từ các chunks trong context
- Nếu chunks không đủ cơ sở → viết chính xác: "Không đủ cơ sở pháp lý để xác định [vấn đề]."
"""


def _build_system_prompt(scopes: List[str], today: str, is_regen: bool = False) -> str:
    """Ghép BASE_RULES + specialist prompt phù hợp + no-apology guard nếu là lần regen."""
    base = BASE_RULES.replace("{today}", today)
    specialist = _select_specialist(scopes)
    prompt = f"{base}\n{specialist}"
    if is_regen:
        prompt += _NO_APOLOGY_INSTRUCTION
    return prompt


# ── GeneratorStage ─────────────────────────────────────────────────────────────

class GeneratorStage:
    """
    Stage 3: Tạo câu trả lời từ chunks + calc result.

    Dùng Gemini structured output để đảm bảo JSON schema chuẩn.
    Specialist prompt được chọn dựa trên scopes từ RouterOutput.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = GEMINI_MODEL):
        api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Cần GOOGLE_API_KEY")
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def run(self, state: PipelineState) -> GeneratorOutput:
        """
        Chạy Stage 3 Generator.

        Returns:
            GeneratorOutput với answer_text, citations, key_facts.

        Raises:
            TimeoutError nếu Gemini timeout (FM07 — caller xử lý)
            RuntimeError nếu Gemini trả về invalid JSON
        """
        t0 = time.perf_counter()

        # ── Chuẩn bị dữ liệu ──────────────────────────────────────────────────
        router_out = state.router_output
        scopes = router_out.scopes if router_out else []

        # FM03: lấy correction instruction từ lần fact check trước
        correction_instruction: Optional[str] = None
        if state.regeneration_count > 0 and state.generator_output:
            correction_instruction = state.generator_output.correction_instruction

        from datetime import date
        today = date.today().strftime("%d/%m/%Y")

        # ── Build prompt + context ─────────────────────────────────────────────
        is_regen = state.regeneration_count > 0
        system_prompt = _build_system_prompt(scopes, today, is_regen=is_regen)
        context_block = _build_context(state, correction_instruction)

        user_message = (
            f"Câu hỏi: {state.query}\n\n"
            f"{context_block}\n\n"
            "Hãy trả lời câu hỏi dựa trên các chunks và kết quả tính toán ở trên.\n"
            "Tuân thủ nghiêm ngặt BASE_RULES và specialist rules đã được cung cấp.\n"
            "Trả về JSON với các trường: answer, citations, key_facts.\n"
            "key_facts: 3-6 chuỗi ngắn (≤60 ký tự) CHÍNH XÁC LẤY TỪ chunks — "
            "con số cụ thể, tỷ lệ %, ngưỡng, tên điều khoản. "
            "VD: [\"500 triệu\", \"5%\", \"Điều 3 Khoản 1\", \"miễn GTGT\"]. "
            "KHÔNG đưa kết luận/diễn giải/disclaimer vào key_facts."
        )

        # ── Gọi Gemini structured output ──────────────────────────────────────
        logger.debug("Generator: model=%s, scopes=%s, regen=%d", self.model, scopes, state.regeneration_count)

        response_schema = types.Schema(
            type=types.Type.OBJECT,
            properties={
                "answer": types.Schema(type=types.Type.STRING),
                "citations": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "doc_id":   types.Schema(type=types.Type.STRING),
                            "article":  types.Schema(type=types.Type.STRING),
                            "text":     types.Schema(type=types.Type.STRING),
                            "label":    types.Schema(type=types.Type.STRING),
                            "chunk_id": types.Schema(type=types.Type.STRING),
                        },
                        required=["doc_id", "article", "text", "label"],
                    ),
                ),
                "key_facts": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING),
                ),
            },
            required=["answer", "citations", "key_facts"],
        )

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_text(text=user_message)],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    thinking_config=_NO_THINKING,
                    temperature=0.1,   # low temp → deterministic, fact-grounded
                ),
            )
        except Exception as exc:
            # Propagate timeout / API errors — caller (orchestrator) handles FM07
            logger.error("Generator Gemini call failed: %s", exc)
            raise

        # ── Parse response ─────────────────────────────────────────────────────
        raw_text = response.text
        if not raw_text:
            raise RuntimeError("Generator: Gemini trả về response rỗng")

        try:
            data: Dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error("Generator: JSON parse failed: %s\nraw=%s", e, raw_text[:500])
            raise RuntimeError(f"Generator: Invalid JSON từ Gemini: {e}") from e

        # ── Citation validation + xây dựng ────────────────────────────────────
        # Tập hợp valid doc_ids và chunk_ids từ retrieved (winning) chunks
        valid_doc_ids:   Set[str] = set()
        valid_chunk_ids: Set[str] = set()
        if state.retrieval_output:
            losing_ids_val: Set[str] = set()
            if state.retrieval_output.has_conflict:
                for cp in state.retrieval_output.conflicts:
                    loser = cp.chunk_id_a if cp.winner_id == cp.chunk_id_b else cp.chunk_id_b
                    losing_ids_val.add(loser)
            for ch in state.retrieval_output.chunks:
                if ch.chunk_id not in losing_ids_val:
                    valid_doc_ids.add(ch.doc_id)
                    valid_chunk_ids.add(ch.chunk_id)

        citations: List[Citation] = []
        for c in data.get("citations", []):
            try:
                doc_id   = c["doc_id"]
                chunk_id = c.get("chunk_id", "")

                # Validation: reject citations dari doc không có trong retrieved chunks
                if valid_doc_ids and doc_id not in valid_doc_ids:
                    logger.warning(
                        "Generator: citation rejected (doc not retrieved): doc_id=%s", doc_id
                    )
                    continue

                # Nếu chunk_id được cung cấp, validate thêm ở chunk level
                if chunk_id and valid_chunk_ids and chunk_id not in valid_chunk_ids:
                    logger.warning(
                        "Generator: citation chunk_id mismatch: chunk_id=%s doc_id=%s — "
                        "keeping with doc_id only", chunk_id, doc_id
                    )
                    chunk_id = ""  # reset chunk_id sai nhưng giữ citation

                citations.append(Citation(
                    doc_id=doc_id,
                    article=c.get("article", ""),
                    text=c.get("text", ""),
                    label=c.get("label", doc_id),
                    chunk_id=chunk_id,
                ))
            except (KeyError, TypeError) as e:
                logger.warning("Generator: skip malformed citation: %s", e)

        # Fallback: auto-populate citations từ top retrieved chunks nếu LLM không cite
        # Triggered khi citations=[] (LLM answer from training data, không dùng chunks)
        if not citations and state.retrieval_output and state.retrieval_output.chunks:
            losing_ids_fb: Set[str] = set()
            if state.retrieval_output.has_conflict:
                for cp in state.retrieval_output.conflicts:
                    loser = cp.chunk_id_a if cp.winner_id == cp.chunk_id_b else cp.chunk_id_b
                    losing_ids_fb.add(loser)
            seen_fb: set = set()
            for ch in state.retrieval_output.chunks:
                if ch.chunk_id in losing_ids_fb or ch.doc_id in seen_fb:
                    continue
                seen_fb.add(ch.doc_id)
                citations.append(Citation(
                    doc_id=ch.doc_id,
                    article="",
                    text="",
                    label=ch.doc_id,
                    chunk_id=ch.chunk_id,
                ))
                if len(citations) >= 2:  # cap=2: recall=high, precision=ok
                    break
            if citations:
                logger.info(
                    "Generator: fallback auto-cite %d docs from retrieved chunks (LLM cite=0)",
                    len(citations),
                )

        key_facts: List[str] = [str(f) for f in data.get("key_facts", []) if f]

        latency_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Generator: done in %dms | citations=%d, key_facts=%d",
            latency_ms, len(citations), len(key_facts),
        )

        return GeneratorOutput(
            answer_text=data.get("answer", ""),
            citations=citations,
            key_facts=key_facts,
        )

    def run_degrade_l2(self, state: PipelineState) -> GeneratorOutput:
        """
        FM04: Level 2 degrade — tạo câu trả lời với explicit caveat.

        Gọi khi fact check fail 2 lần. Answer bắt buộc kèm cảnh báo
        'không xác minh được đầy đủ'.
        """
        caveat_instruction = (
            "QUAN TRỌNG: Đây là lần thử thứ hai. Câu trả lời PHẢI kèm theo cảnh báo rõ ràng:\n"
            "- Mở đầu bằng: '[LƯU Ý: Thông tin này chưa được xác minh đầy đủ — "
            "cần kiểm tra lại với cơ quan thuế trước khi áp dụng.]'\n"
            "- Chỉ nêu những điểm có trong chunks — không suy diễn thêm\n"
            "- Ưu tiên an toàn: nếu không chắc → nói không chắc"
        )

        # Thêm caveat vào correction_instruction
        existing = ""
        if state.generator_output and state.generator_output.correction_instruction:
            existing = state.generator_output.correction_instruction + "\n\n"

        # Temporarily patch state để _build_context lấy được correction
        # (không mutate state — tạo mock object)
        class _MockGenOut:
            correction_instruction = existing + caveat_instruction

        original_gen_out = state.generator_output
        state.generator_output = _MockGenOut()  # type: ignore[assignment]
        state.regeneration_count = max(state.regeneration_count, 1)

        try:
            result = self.run(state)
        finally:
            state.generator_output = original_gen_out  # restore

        return result
