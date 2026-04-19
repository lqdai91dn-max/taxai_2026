"""
src/tools/doc_validity_tool.py — Kiểm tra hiệu lực văn bản pháp luật (S-001).

Đọc từ data/law_validity.json — single source of truth về hiệu lực văn bản.
LLM dùng tool này trước khi cite một văn bản để xác nhận:
  - Văn bản có đang có hiệu lực không?
  - Đã bị thay thế bởi văn bản nào chưa?
  - Văn bản có trong database không?
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_LAW_VALIDITY_PATH = Path(__file__).parents[2] / "data/law_validity.json"


def check_doc_validity(doc_id: str) -> dict:
    """
    Kiểm tra hiệu lực của một văn bản pháp luật.

    Args:
        doc_id: ID văn bản theo format hệ thống (underscore thay slash).
                Ví dụ: "68_2026_NDCP", "109_2025_QH15", "111_2013_TTBTC".

    Returns:
        dict với các trường:
            found            : bool — văn bản có trong manifest không
            status           : "active" | "pending" | "active_until_superseded"
                               | "not_in_database" | "unknown"
            is_currently_valid: bool — đang có hiệu lực hôm nay
            effective_from   : str | None — ngày bắt đầu hiệu lực (ISO)
            effective_to     : str | None — ngày kết thúc hiệu lực (ISO)
            name             : str — tên đầy đủ văn bản
            note             : str — ghi chú quan trọng
            superseded_by    : str | None — doc_id văn bản thay thế (nếu có)
    """
    today = date.today()

    try:
        data = json.loads(_LAW_VALIDITY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _unknown(doc_id, "Không đọc được law_validity.json")

    # ── Tra trong "documents" ────────────────────────────────────────────────
    docs = data.get("documents", {})
    if doc_id in docs:
        info   = docs[doc_id]
        status = info.get("status", "unknown")
        eff_from_str = info.get("effective_from")
        eff_to_str   = info.get("effective_to")

        # status là source of truth: "active" → valid, "superseded"/"pending" → invalid
        if status == "active":
            is_valid = True
        elif status == "active_until_superseded":
            eff_to_date = date.fromisoformat(eff_to_str) if eff_to_str else None
            is_valid = eff_to_date is None or today <= eff_to_date
        else:
            is_valid = False  # pending, superseded

        return {
            "found":             True,
            "status":            status,
            "is_currently_valid": is_valid,
            "effective_from":    eff_from_str,
            "effective_to":      eff_to_str,
            "name":              info.get("name", doc_id),
            "note":              info.get("note", ""),
            "superseded_by":     info.get("superseded_by"),
        }

    # ── Tra trong "not_in_database" ──────────────────────────────────────────
    not_in_db = data.get("not_in_database", {})
    if doc_id in not_in_db:
        info = not_in_db[doc_id]
        return {
            "found":             False,
            "status":            "not_in_database",
            "is_currently_valid": False,
            "effective_from":    None,
            "effective_to":      None,
            "name":              info.get("name", doc_id),
            "note":              info.get("note", "Văn bản không có trong cơ sở dữ liệu."),
            "superseded_by":     info.get("superseded_by"),
        }

    # ── Không tìm thấy ───────────────────────────────────────────────────────
    return _unknown(doc_id, "Văn bản không có trong manifest hiệu lực. Không thể xác nhận.")


def _unknown(doc_id: str, note: str) -> dict:
    return {
        "found":             False,
        "status":            "unknown",
        "is_currently_valid": False,
        "effective_from":    None,
        "effective_to":      None,
        "name":              doc_id,
        "note":              note,
        "superseded_by":     None,
    }
