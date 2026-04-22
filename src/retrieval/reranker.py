"""
src/retrieval/reranker.py — NodeMetadata Reranker (P5.3)

Stage 2 của Two-Stage Retrieval: sau RRF Fusion, dùng NodeMetadata
để boost score cho chunks có metadata khớp với QueryIntent.

Thiết kế:
  - Additive bonus (không drop bất kỳ chunk nào — chỉ reorder)
  - Bonus dựa trên intersection giữa QueryIntent fields và NodeMetadata nm_* fields
  - Graceful degradation: nếu NodeMetadata chưa annotate → bonus=0, không crash

Bonus weights (tổng tối đa = 0.85):
  tax_domain match:         +0.30  (quan trọng nhất — PIT vs HKD)
  activity_group match:     +0.20  (ngành nghề)
  who match:                +0.10  (đối tượng nộp thuế)
  content_type bonus:       +0.20  (tax_rate cho calculate query)
  temporal validity:        +0.05  (văn bản còn hiệu lực)

final_score = rrf_score × (1 + sum_of_applicable_bonuses)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Bonus weights ────────────────────────────────────────────────────────────
# R31: giảm domain bonus (quá coarse), tăng content_type (fine-grained signal)
# _MAX_BONUS: hard ceiling chống stacking bug (domain+activity+who+content ≤ 0.15)

_W_TAX_DOMAIN    = 0.05   # R31: 0.30 → 0.05 (domain quá broad, gây annotated-doc bias)
_W_ACTIVITY      = 0.10   # R31: 0.20 → 0.10
_W_WHO           = 0.05   # R31: 0.10 → 0.05
_W_CONTENT_TYPE  = 0.15   # R31: fine-grained signal — tăng nhẹ
_W_TEMPORAL      = 0.05   # giữ nguyên

_MAX_BONUS       = 0.15   # R31: hard ceiling — ngăn stacking override base retrieval

_TODAY = date.today().isoformat()   # "2026-03-26"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_nm_list(meta: dict, key: str) -> List[str]:
    """
    Đọc nm_* field từ ChromaDB metadata.
    ChromaDB lưu list thành comma-separated string: "HKD,PIT"
    """
    raw = meta.get(key, "")
    if not raw or raw == "UNSPECIFIED":
        return []
    return [v.strip() for v in str(raw).split(",") if v.strip()]


def _intersects(a: List[str], b: List[str]) -> bool:
    """True nếu 2 list có ít nhất 1 phần tử chung (case-insensitive)."""
    if not a or not b:
        return False
    a_lower = {x.lower() for x in a}
    return any(x.lower() in a_lower for x in b)


def _is_temporally_valid(meta: dict) -> bool:
    """
    Kiểm tra văn bản còn hiệu lực (nm_effective_from <= today, nm_effective_to null/future).
    Nếu không có metadata → assume valid (True).
    """
    eff_from = meta.get("nm_effective_from", "")
    eff_to   = meta.get("nm_effective_to", "")
    if eff_from and eff_from > _TODAY:
        return False   # chưa có hiệu lực
    if eff_to and eff_to < _TODAY:
        return False   # hết hiệu lực
    return True


# ─── Core rerank function ─────────────────────────────────────────────────────

def rerank_with_intent(
    results: List[Dict[str, Any]],
    query_intent,               # QueryIntent object (từ query_intent.py)
) -> List[Dict[str, Any]]:
    """
    Rerank search results dựa trên QueryIntent × NodeMetadata match.

    Args:
        results:       List hits từ RRF fusion (mỗi hit có rrf_score + metadata).
        query_intent:  QueryIntent object từ P5.1 QueryIntent Builder.

    Returns:
        Same list, reordered theo final_score = rrf_score × (1 + bonus).
        Thêm field 'nm_bonus' và 'final_score' vào mỗi hit để debug.
    """
    if not results:
        return results

    # Extract QueryIntent fields (graceful — FieldValue có .value)
    qi_tax_domain    = _get_fv_list(query_intent, "tax_domain")
    qi_activity      = _get_fv_list(query_intent, "activity_group")
    qi_who           = _get_fv_list(query_intent, "who")
    qi_requires_calc = _get_intent_flag(query_intent, "requires_calculation")

    annotated_count = 0
    boosted_count   = 0

    for hit in results:
        meta  = hit.get("metadata", {})
        bonus = 0.0

        # Kiểm tra có NodeMetadata không (nm_annotated = True)
        if not meta.get("nm_annotated"):
            hit["nm_bonus"]    = 0.0
            hit["final_score"] = hit.get("rrf_score", 0.0)
            continue

        annotated_count += 1

        # ── tax_domain match ─────────────────────────────────────────────
        nm_domain = _parse_nm_list(meta, "nm_tax_domain")
        if nm_domain and _intersects(qi_tax_domain, nm_domain):
            bonus += _W_TAX_DOMAIN

        # ── activity_group match ─────────────────────────────────────────
        nm_activity = _parse_nm_list(meta, "nm_activity_group")
        if nm_activity and _intersects(qi_activity, nm_activity):
            bonus += _W_ACTIVITY

        # ── who match ────────────────────────────────────────────────────
        nm_who = _parse_nm_list(meta, "nm_who")
        if nm_who and _intersects(qi_who, nm_who):
            bonus += _W_WHO

        # ── content_type bonus (chỉ khi query cần tính toán) ────────────
        if qi_requires_calc:
            nm_content = meta.get("nm_content_type", "")
            if nm_content == "tax_rate":
                bonus += _W_CONTENT_TYPE
            elif nm_content == "threshold":
                bonus += _W_CONTENT_TYPE * 0.5  # threshold cũng useful

        # ── temporal validity ────────────────────────────────────────────
        if _is_temporally_valid(meta):
            bonus += _W_TEMPORAL

        # ── Compute final score (hard ceiling chống stacking) ───────────
        bonus = min(bonus, _MAX_BONUS)
        rrf = hit.get("rrf_score", 0.0)
        final = round(rrf * (1.0 + bonus), 6)

        hit["nm_bonus"]    = round(bonus, 3)
        hit["final_score"] = final

        if bonus > 0:
            boosted_count += 1

    # Reorder theo final_score
    results.sort(key=lambda h: h.get("final_score", h.get("rrf_score", 0)), reverse=True)

    if annotated_count > 0:
        logger.debug(
            "[Reranker] annotated=%d boosted=%d/%d",
            annotated_count, boosted_count, len(results),
        )

    return results


def rerank_with_exception_penalty(
    results: List[Dict[str, Any]],
    query_intent,
    query: str,
) -> List[Dict[str, Any]]:
    """
    Full reranking pipeline:
      1. NodeMetadata bonus pass (rerank_with_intent)
      2. ExceptionRouter penalty pass (superseded doc penalty)

    Dùng hàm này thay vì gọi trực tiếp rerank_with_intent.
    """
    results = rerank_with_intent(results, query_intent)

    try:
        from src.retrieval.exception_router import get_router
        router = get_router()
        results = router.apply_penalty(results, query)
    except Exception as exc:
        logger.warning("[Reranker] ExceptionRouter failed (non-fatal): %s", exc)

    return results


# ─── Helpers để đọc QueryIntent fields ────────────────────────────────────────

def _get_fv_list(query_intent, field: str) -> List[str]:
    """Lấy giá trị list từ FieldValue của QueryIntent (graceful)."""
    try:
        fv = getattr(query_intent, field, None)
        if fv is None:
            return []
        val = fv.value if hasattr(fv, "value") else fv
        if val is None:
            return []
        if isinstance(val, list):
            return [str(v) for v in val if v and str(v) != "UNSPECIFIED"]
        if isinstance(val, str) and val != "UNSPECIFIED":
            return [val]
        return []
    except Exception:
        return []


def _get_intent_flag(query_intent, flag: str) -> bool:
    """Lấy boolean flag từ intent.value dict của QueryIntent."""
    try:
        fv = getattr(query_intent, "intent", None)
        if fv is None:
            return False
        val = fv.value if hasattr(fv, "value") else fv
        if isinstance(val, dict):
            return bool(val.get(flag, False))
        return False
    except Exception:
        return False
