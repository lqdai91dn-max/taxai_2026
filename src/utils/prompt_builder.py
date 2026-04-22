"""
Auto-generate Section 3 (LUẬT ÁP DỤNG) of the agent system prompt from law_validity.json.
When adding a new law: update law_validity.json only — no prompt edits needed.
"""
from __future__ import annotations

from src.utils.law_registry import get_active_documents, get_superseded_documents, get_exception_docs


def build_law_section() -> str:
    active = get_active_documents()
    superseded = get_superseded_documents()
    exceptions = get_exception_docs()

    lines: list[str] = ["### 3. LUẬT ÁP DỤNG — VĂN BẢN HIỆN HÀNH"]

    # --- Primary active documents ---
    lines.append("**Văn bản đang có hiệu lực:**")
    for doc_id, doc in active.items():
        doc_num = doc.get("doc_number", doc_id)
        eff = doc.get("effective_from", "?")
        note = doc.get("note", "")
        lines.append(f"- **{doc_num}** (hiệu lực từ {eff}): {note}")

    lines.append("")

    # --- Superseded documents ---
    if superseded:
        lines.append("**Văn bản đã bị thay thế (KHÔNG dùng làm căn cứ chính):**")
        for doc_id, doc in superseded.items():
            doc_num = doc.get("doc_number", doc_id)
            superseded_by_id = doc.get("superseded_by", "")
            superseded_by_doc = get_active_documents().get(superseded_by_id, {})
            superseded_by_num = superseded_by_doc.get("doc_number", superseded_by_id)
            eff_to = doc.get("effective_to", "?")
            lines.append(f"- {doc_num}: hết hiệu lực {eff_to}, thay bởi **{superseded_by_num}**")
        lines.append("")

    # --- Exception use notice ---
    if exceptions:
        lines.append("**Ngoại lệ — văn bản cũ vẫn được tham khảo trong trường hợp cụ thể:**")
        for ex_doc in exceptions:
            doc_num = ex_doc.get("doc_number", ex_doc["doc_id"])
            ex = ex_doc["exception_use"]
            reason = ex.get("reason", "")
            expires = ex.get("expires_condition", "")
            lines.append(f"- **{doc_num}**: {reason}")
            if expires:
                lines.append(f"  - Điều kiện hết ngoại lệ: {expires}")
        lines.append("")

    lines.append(
        "**Nguyên tắc:** Khi câu hỏi liên quan đến giai đoạn TRƯỚC ngày hiệu lực của luật mới "
        "hoặc có implementation detail chưa được hướng dẫn → tham khảo văn bản cũ tương ứng, "
        "nhưng phải nêu rõ đây là quy định cũ."
    )

    return "\n".join(lines)


# Computed once at import time — no overhead per request
_LAW_SECTION: str = build_law_section()
