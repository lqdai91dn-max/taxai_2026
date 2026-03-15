"""
Lookup tools — validity, reference resolution, amendment tracking.

Tools:
  check_doc_validity         — kiểm tra hiệu lực văn bản tại một ngày
  resolve_legal_reference    — parse "Điều X NĐ Y/Z" → doc_id + article_id
  get_article_with_amendments — Điều luật + cảnh báo sửa đổi
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

_graph_retriever = None


def _get_grapher():
    global _graph_retriever
    if _graph_retriever is None:
        from src.graph.graph_retriever import GraphRetriever
        _graph_retriever = GraphRetriever()
    return _graph_retriever


def _get_client():
    from src.graph.neo4j_client import Neo4jClient
    return Neo4jClient()


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 5 — check_doc_validity
# ═══════════════════════════════════════════════════════════════════════════════

def check_doc_validity(
    doc_id: str,
    query_date: str | None = None,
) -> dict:
    """
    Kiểm tra hiệu lực pháp lý của một văn bản tại một ngày cụ thể.

    Args:
        doc_id:      ID văn bản. Ví dụ: '109_2025_QH15', '68_2026_NDCP'.
        query_date:  Ngày kiểm tra (ISO format: 'YYYY-MM-DD').
                     Mặc định: hôm nay.

    Returns:
        dict với status (valid/pending/expired/unknown), ngày hiệu lực, amended_by.
    """
    qd_str = query_date or date.today().isoformat()
    try:
        qd = date.fromisoformat(qd_str)
    except ValueError:
        return {"doc_id": doc_id, "error": f"query_date không hợp lệ: {qd_str!r}"}

    client = _get_client()
    try:
        rows = client.run("""
MATCH (d)
WHERE (d:Document OR d:GuidanceDocument) AND d.doc_id = $did
RETURN d.doc_id     AS doc_id,
       d.doc_number AS doc_number,
       d.doc_type   AS doc_type,
       d.title      AS title,
       d.status     AS status,
       d.valid_from AS valid_from,
       d.valid_to   AS valid_to,
       d.hierarchy_rank AS hierarchy_rank
LIMIT 1
""", {"did": doc_id})

        if not rows:
            return {
                "doc_id": doc_id,
                "found":  False,
                "status": "unknown",
                "message": f"Văn bản {doc_id!r} không có trong hệ thống.",
            }

        r = rows[0]
        valid_from = r["valid_from"]
        valid_to   = r["valid_to"]

        # Xác định trạng thái
        if valid_from and date.fromisoformat(valid_from) > qd:
            status = "pending"
            msg = (
                f"Văn bản chưa có hiệu lực tại {qd_str}. "
                f"Hiệu lực từ: {valid_from}."
            )
        elif valid_to and date.fromisoformat(valid_to) <= qd:
            status = "expired"
            msg = (
                f"Văn bản đã hết hiệu lực tại {qd_str}. "
                f"Hết hiệu lực: {valid_to}."
            )
        else:
            status = "valid"
            msg = f"Văn bản đang có hiệu lực tại {qd_str}."

        # Tìm văn bản sửa đổi / thay thế
        amend_rows = client.run("""
MATCH (a:Document)-[r:AMENDS|SUPERSEDES]->(d)
WHERE (d:Document OR d:GuidanceDocument) AND d.doc_id = $did
RETURN a.doc_id AS amender_id, a.doc_number AS amender_number,
       a.title AS amender_title, type(r) AS rel_type,
       r.note AS note, r.effective_date AS eff_date
""", {"did": doc_id})

        amended_by = [
            {
                "doc_id":     ar["amender_id"],
                "doc_number": ar["amender_number"],
                "title":      (ar["amender_title"] or "")[:80],
                "rel_type":   ar["rel_type"],
                "note":       ar["note"],
                "effective_date": ar["eff_date"],
            }
            for ar in amend_rows
        ]

        return {
            "doc_id":      doc_id,
            "found":       True,
            "doc_number":  r["doc_number"],
            "doc_type":    r["doc_type"],
            "title":       (r["title"] or "")[:100],
            "status":      status,
            "valid_from":  valid_from,
            "valid_to":    valid_to,
            "query_date":  qd_str,
            "amended_by":  amended_by,
            "message":     msg,
        }

    except Exception as e:
        logger.error(f"check_doc_validity failed for {doc_id!r}: {e}")
        return {"doc_id": doc_id, "error": str(e)}
    finally:
        client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 8 — resolve_legal_reference
# ═══════════════════════════════════════════════════════════════════════════════

# Pattern bắt số văn bản VN: 68/2026/NĐ-CP, 109/2025/QH15, 1296/CTNVT, v.v.
_DOC_NUMBER_RE = re.compile(
    r'\b(\d{1,4})[/\-](\d{4})[/\-]([A-Za-zÀ-ỹ0-9\-]+)\b'
    r'|'
    r'\b(\d{1,4})[/\-]([A-Za-zÀ-ỹ]+)\b',
    re.UNICODE,
)

# Pattern bắt số Điều: "Điều 5", "điều 10"
_ARTICLE_RE = re.compile(r'[Đđ]iều\s+(\d+)', re.UNICODE)


def resolve_legal_reference(reference_text: str) -> dict:
    """
    Chuyển đổi tham chiếu pháp luật dạng văn bản sang doc_id + article_id.

    Ví dụ:
        "Điều 5 Nghị định 68/2026/NĐ-CP"  → doc_id='68_2026_NDCP', article_id='..._dieu_5'
        "khoản 2 Điều 9 Luật 109/2025/QH15" → doc_id='109_2025_QH15', article_id='..._dieu_9'

    Args:
        reference_text: Chuỗi tham chiếu tự nhiên (từ LLM reasoning hoặc user input).

    Returns:
        dict với doc_id, article_id (nếu tìm được), candidates nếu ambiguous.
    """
    # ── 1. Extract số văn bản + số Điều từ text ──────────────────────────────
    doc_number_matches = _DOC_NUMBER_RE.findall(reference_text)
    article_numbers    = _ARTICLE_RE.findall(reference_text)

    # Reconstruct doc number strings
    candidate_numbers = []
    for m in doc_number_matches:
        if m[0]:  # dạng XX/YYYY/TYPE
            candidate_numbers.append(f"{m[0]}/{m[1]}/{m[2]}")
        elif m[3]:  # dạng XX/TYPE (không có năm)
            candidate_numbers.append(f"{m[3]}/{m[4]}")

    if not candidate_numbers:
        return {
            "reference_text": reference_text,
            "resolved": False,
            "message": "Không tìm thấy số hiệu văn bản trong text.",
        }

    # ── 2. Lookup doc_id từ Neo4j theo doc_number ─────────────────────────────
    client = _get_client()
    try:
        found_docs = []
        for num in candidate_numbers:
            rows = client.run("""
MATCH (d)
WHERE (d:Document OR d:GuidanceDocument)
  AND d.doc_number = $num
RETURN d.doc_id AS doc_id, d.doc_number AS doc_number,
       d.doc_type AS doc_type, d.title AS title, d.status AS status
LIMIT 1
""", {"num": num})
            if rows:
                found_docs.append(dict(rows[0]))

        if not found_docs:
            return {
                "reference_text":    reference_text,
                "resolved":          False,
                "candidate_numbers": candidate_numbers,
                "message": (
                    f"Không tìm thấy văn bản {candidate_numbers} trong hệ thống. "
                    "Kiểm tra lại số hiệu hoặc dùng search_legal_docs."
                ),
            }

        # Lấy doc đầu tiên (thường chỉ có 1)
        doc = found_docs[0]
        doc_id = doc["doc_id"]

        # ── 3. Lookup article_id nếu có số Điều ─────────────────────────────
        article_id = None
        article_title = None
        if article_numbers:
            art_num = article_numbers[0]  # lấy Điều đầu tiên đề cập
            art_rows = client.run("""
MATCH (a:Article {doc_id: $doc_id})
WHERE a.id ENDS WITH ('_dieu_' + $art_num)
   OR a.id CONTAINS ('_dieu_' + $art_num + '_')
RETURN a.id AS article_id, a.title AS title
LIMIT 1
""", {"doc_id": doc_id, "art_num": art_num})

            if art_rows:
                article_id    = art_rows[0]["article_id"]
                article_title = art_rows[0]["title"]

        return {
            "reference_text": reference_text,
            "resolved":       True,
            "doc_id":         doc_id,
            "doc_number":     doc["doc_number"],
            "doc_type":       doc["doc_type"],
            "doc_title":      (doc["title"] or "")[:80],
            "doc_status":     doc["status"],
            "article_number": article_numbers[0] if article_numbers else None,
            "article_id":     article_id,
            "article_title":  article_title,
            "message": (
                f"Đã resolve: {doc['doc_number']} → {doc_id}"
                + (f", Điều {article_numbers[0]} → {article_id}" if article_id else "")
            ),
        }

    except Exception as e:
        logger.error(f"resolve_legal_reference failed: {e}")
        return {"reference_text": reference_text, "resolved": False, "error": str(e)}
    finally:
        client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 9 — get_article_with_amendments
# ═══════════════════════════════════════════════════════════════════════════════

def get_article_with_amendments(article_id: str) -> dict:
    """
    Lấy toàn văn Điều luật + cảnh báo nếu văn bản cha có bị sửa đổi/thay thế.

    Phiên bản simplified: cảnh báo ở mức document (không merge text article).
    Nếu có văn bản AMENDS parent doc → trả warning để LLM tham khảo thêm.

    Args:
        article_id: ID của Article. Ví dụ: 'doc_68_2026_NDCP_chuong_II_dieu_4'

    Returns:
        dict với article text + amendment_warnings + is_fully_resolved.
    """
    from src.tools.retrieval_tools import get_article

    # 1. Lấy article gốc
    article_data = get_article(article_id)

    if not article_data.get("found"):
        return {
            "article_id":        article_id,
            "found":             False,
            "amendment_warnings": [],
            "is_fully_resolved": False,
        }

    doc_id = article_data.get("doc_id", "")

    # 2. Check AMENDS / SUPERSEDES nhắm vào parent doc
    client = _get_client()
    try:
        amend_rows = client.run("""
MATCH (amender:Document)-[r:AMENDS|SUPERSEDES]->(target)
WHERE (target:Document OR target:GuidanceDocument) AND target.doc_id = $did
  AND amender.doc_id <> $did
RETURN amender.doc_id     AS amender_id,
       amender.doc_number AS amender_number,
       amender.doc_type   AS amender_type,
       amender.title      AS amender_title,
       amender.status     AS amender_status,
       amender.valid_from AS amender_valid_from,
       type(r)            AS rel_type,
       r.note             AS note
""", {"did": doc_id})

        amendment_warnings = []
        for ar in amend_rows:
            amendment_warnings.append({
                "amender_doc_id":     ar["amender_id"],
                "amender_doc_number": ar["amender_number"],
                "amender_type":       ar["amender_type"],
                "amender_title":      (ar["amender_title"] or "")[:80],
                "amender_status":     ar["amender_status"],
                "amender_valid_from": ar["amender_valid_from"],
                "rel_type":           ar["rel_type"],
                "note":               ar["note"],
                "warning": (
                    f"{ar['amender_type']} {ar['amender_number'] or ar['amender_id']} "
                    f"{'sửa đổi' if ar['rel_type']=='AMENDS' else 'thay thế'} "
                    f"văn bản {doc_id}. Cần kiểm tra xem Điều này có bị sửa đổi không."
                ),
            })

        # is_fully_resolved = True nếu không có amendments từ doc trong dataset
        # (tức là ta đã có đủ thông tin)
        is_fully_resolved = len(amendment_warnings) == 0

        return {
            **article_data,
            "amendment_warnings":  amendment_warnings,
            "has_amendments":      len(amendment_warnings) > 0,
            "is_fully_resolved":   is_fully_resolved,
            "amendment_summary": (
                "" if is_fully_resolved else
                f"Cảnh báo: {len(amendment_warnings)} văn bản có sửa đổi "
                f"'{doc_id}'. Kiểm tra thêm trước khi kết luận."
            ),
        }

    except Exception as e:
        logger.error(f"get_article_with_amendments failed for {article_id!r}: {e}")
        return {**article_data, "amendment_warnings": [], "error": str(e)}
    finally:
        client.close()
