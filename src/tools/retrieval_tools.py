"""
Retrieval tools — wrap HybridSearch + GraphRetriever.

Tools:
  search_legal_docs  — hybrid BM25 + vector search trên ChromaDB
  get_article        — lấy toàn văn Điều từ Neo4j
  get_guidance       — lấy GuidanceChunks (Sổ tay / Công văn) liên quan
  get_impl_chain     — chuỗi IMPLEMENTS/AMENDS/SUPERSEDES của văn bản
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Lazy-load để không block import khi chưa cần Neo4j / ChromaDB
_hybrid_search = None
_graph_retriever = None


def _get_searcher():
    global _hybrid_search
    if _hybrid_search is None:
        from src.retrieval.hybrid_search import HybridSearch
        _hybrid_search = HybridSearch()
    return _hybrid_search


def _get_grapher():
    global _graph_retriever
    if _graph_retriever is None:
        from src.graph.graph_retriever import GraphRetriever
        _graph_retriever = GraphRetriever()
    return _graph_retriever


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 3 — search_legal_docs
# ═══════════════════════════════════════════════════════════════════════════════

# P0: 310_2025_NDCP — gate logic
#
# NĐ310 là NĐ sửa đổi NĐ125 về xử phạt vi phạm hành chính thuế/hóa đơn.
# Nội dung chứa nhiều từ khóa chung (thuế, khai, nộp) → BM25 false positive
# cho queries về thủ tục, kế toán, miễn giảm, cho thuê, hoàn thuế...
#
# Quy tắc:
#   1. General search (no doc_filter): luôn exclude 310
#   2. Explicit doc_filter='310_2025_NDCP': chỉ cho phép khi query có penalty keywords
#      → không có penalty keywords → trả về empty (agent sẽ dùng kết quả general search)

_GENERAL_SEARCH_EXCLUDE = frozenset(["310_2025_NDCP"])

_PENALTY_KEYWORDS = frozenset([
    "phạt", "xử phạt", "vi phạm", "mức phạt", "bị phạt",
    "tiền phạt", "chế tài", "vi phạm hành chính",
    "310", "nd310", "nđ310",
    "trốn thuế", "khai sai bị",
    # B4: tiền chậm nộp / lãi chậm nộp (Q115 type — wrong tax period → surcharge)
    "chậm nộp", "0,03%", "0.03%", "lãi chậm",
])

# B2: Accounting book keywords → co-retrieval 68 + 152
# Khi query liên quan đến sổ kế toán HKD → inject 152_2025_TTBTC vào kết quả
_ACCOUNTING_KEYWORDS = frozenset([
    "sổ kế toán", "sổ doanh thu", "sổ chi tiết",
    "s1a", "s2a", "s2b", "s2c", "s2d", "s2e", "s3a",
    "s1a-hkd", "s2a-hkd", "s2b-hkd", "s2c-hkd", "s2d-hkd", "s2e-hkd",
    "mẫu sổ", "phương pháp ghi sổ", "ghi sổ",
    "152/2025", "thông tư 152",
    "kế toán hộ kinh doanh", "kế toán cá nhân kinh doanh",
    "lưu trữ sổ", "bảo quản sổ",
])


def _is_accounting_query(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _ACCOUNTING_KEYWORDS)


# B4/Q115: Tiền chậm nộp rate queries → co-retrieve 108_2025_QH15
# 108 = Luật Quản lý thuế, chứa mức 0,03%/ngày tại Điều 16 K2
_CHAM_NOP_KEYWORDS = frozenset([
    "tiền chậm nộp", "lãi chậm nộp", "mức chậm nộp",
    "chậm nộp thuế", "tính tiền chậm",
    "0,03%", "0.03%",
])


def _is_cham_nop_query(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _CHAM_NOP_KEYWORDS)


def _has_penalty_keywords(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _PENALTY_KEYWORDS)


def _normalize_doc_filter(doc_filter: str | None, searcher) -> str | None:
    """Normalize doc_filter to correct case using the BM25 index.

    Builds a lowercase → actual_doc_id map on first call so that
    agent calls like doc_filter='so_tay_hkd' resolve to 'So_Tay_HKD'.
    Returns None if doc_filter doesn't match any known doc_id (to avoid
    filtering by a bogus value like 'Nghị định').
    """
    if not doc_filter:
        return None

    # Build lowercase map from BM25 metadata
    doc_id_map: dict[str, str] = {}
    for meta in getattr(searcher, "bm25_metas", []):
        did = meta.get("doc_id", "")
        if did:
            doc_id_map[did.lower()] = did

    lower = doc_filter.lower()
    if lower in doc_id_map:
        normalized = doc_id_map[lower]
        if normalized != doc_filter:
            logger.info(f"[doc_filter] Normalized '{doc_filter}' → '{normalized}'")
        return normalized

    # No match — log warning and ignore the filter
    logger.warning(f"[doc_filter] Unknown doc_id '{doc_filter}' — ignoring filter")
    return None


def search_legal_docs(
    query: str,
    top_k: int = 5,
    doc_filter: str | None = None,
    exclude_doc_ids: list[str] | None = None,
) -> dict:
    """
    Tìm kiếm điều khoản pháp luật liên quan bằng hybrid search (BM25 + vector).

    Args:
        query:           Câu hỏi hoặc từ khóa cần tìm.
        top_k:           Số kết quả trả về (mặc định 5, tối đa 10).
        doc_filter:      doc_id cụ thể để giới hạn phạm vi tìm kiếm (optional).
        exclude_doc_ids: Danh sách doc_id cần loại trừ khỏi kết quả (optional).
                         Dùng khi biết chắc một văn bản không liên quan để tránh false positive.

    Returns:
        dict với results list, mỗi item có snippet + citation.
    """
    top_k = min(max(1, top_k), 10)

    # P0 Gate — 3 lớp chặn 310_2025_NDCP false positive:
    effective_exclude = list(exclude_doc_ids) if exclude_doc_ids else []

    if not doc_filter:
        # Lớp 1: General search → luôn exclude 310 khỏi pool + expansion
        for _excl_id in _GENERAL_SEARCH_EXCLUDE:
            if _excl_id not in effective_exclude:
                effective_exclude.append(_excl_id)
                logger.debug(f"[P0] General-search exclude: {_excl_id}")
    elif doc_filter in ("310_2025_NDCP", "310_2025_ndcp"):
        # Lớp 2: Explicit doc_filter='310_2025_NDCP' → chỉ cho phép khi có penalty keywords
        if not _has_penalty_keywords(query):
            logger.info(
                f"[P0] Blocked doc_filter='310_2025_NDCP' — no penalty keywords in query: {query[:80]!r}"
            )
            return {"query": query, "total_found": 0, "results": [], "_p0_blocked": True}
    else:
        # Lớp 3: doc_filter là doc khác (125, 68, 126...) → vẫn exclude 310 khỏi expansion.
        # _expand_amendments tự động kéo 310 vào khi 125 được retrieved (amended_by).
        # Ngăn điều này trừ khi agent explicitly muốn 310 (covered by Lớp 2).
        for _excl_id in _GENERAL_SEARCH_EXCLUDE:
            if _excl_id not in effective_exclude:
                effective_exclude.append(_excl_id)
                logger.debug(f"[P0] Doc-filter-search exclude expansion: {_excl_id}")

    try:
        searcher = _get_searcher()
        normalized_filter = _normalize_doc_filter(doc_filter, searcher)
        hits = searcher.search(
            query=query,
            n_results=top_k,
            filter_doc_id=normalized_filter,
            exclude_doc_ids=effective_exclude if effective_exclude else None,
        )
    except Exception as e:
        logger.error(f"search_legal_docs failed: {e}")
        return {"query": query, "results": [], "error": str(e)}

    results = []
    for hit in hits:
        meta = hit.get("metadata", {})
        results.append({
            "chunk_id":   hit.get("chunk_id", ""),
            "doc_id":     meta.get("doc_id", ""),
            "node_type":  meta.get("node_type", ""),
            "breadcrumb": meta.get("breadcrumb", ""),
            "snippet":    hit.get("text", "")[:2000],
            "score":      hit.get("rrf_score", 0.0),
            "citation": {
                "doc_id":      meta.get("doc_id", ""),
                "doc_number":  meta.get("doc_number", ""),
                "breadcrumb":  meta.get("breadcrumb", ""),
            },
        })

    # B4/Q115: Tiền chậm nộp co-retrieval injection
    # Khi query hỏi về mức tiền chậm nộp → inject 108_2025_QH15 (Điều 16 K2: 0,03%/ngày)
    # Fire cả khi agent dùng doc_filter=125/310 (penalty docs) vì agent có thể route nhầm sang xử phạt
    _PENALTY_DOC_IDS = frozenset(["125_2020_NDCP", "310_2025_NDCP"])
    if (not normalized_filter or normalized_filter in _PENALTY_DOC_IDS) and _is_cham_nop_query(query):
        doc_ids_in_results = {r["doc_id"] for r in results}
        if "108_2025_QH15" not in doc_ids_in_results:
            try:
                logger.info(f"[B4] Chậm nộp query → co-retrieve 108_2025_QH15")
                # Pinpointed query: Điều 16 K2 Đ.a — mức 0,03%/ngày tiền chậm nộp thuế
                hits_108 = searcher.search(
                    query="mức tính tiền chậm nộp thuế 0,03% ngày Điều 16 Luật Quản lý thuế",
                    n_results=2,
                    filter_doc_id="108_2025_QH15",
                    exclude_doc_ids=None,
                )
                injected = []
                for hit in hits_108:
                    meta = hit.get("metadata", {})
                    injected.append({
                        "chunk_id":   hit.get("chunk_id", ""),
                        "doc_id":     meta.get("doc_id", ""),
                        "node_type":  meta.get("node_type", ""),
                        "breadcrumb": meta.get("breadcrumb", ""),
                        "snippet":    hit.get("text", "")[:2000],
                        "score":      hit.get("rrf_score", 0.0),
                        "citation": {
                            "doc_id":      meta.get("doc_id", ""),
                            "doc_number":  meta.get("doc_number", ""),
                            "breadcrumb":  meta.get("breadcrumb", ""),
                        },
                    })
                # Prepend 108 chunks so agent sees 0,03% rate before penalty content
                results = injected + results
                logger.info(f"[B4] Prepended {len(injected)} chunks from 108_2025_QH15")
            except Exception as e:
                logger.warning(f"[B4] Co-retrieval 108 failed: {e}")

    # B2: Accounting co-retrieval injection
    # Khi agent tìm trong 68_2026_NDCP mà query liên quan sổ kế toán HKD
    # → tự động inject thêm kết quả từ 152_2025_TTBTC (nếu chưa có)
    if normalized_filter == "68_2026_NDCP" and _is_accounting_query(query):
        doc_ids_in_results = {r["doc_id"] for r in results}
        if "152_2025_TTBTC" not in doc_ids_in_results:
            try:
                logger.info(f"[B2] Accounting query + 68 filter → co-retrieve 152_2025_TTBTC")
                hits_152 = searcher.search(
                    query=query,
                    n_results=3,
                    filter_doc_id="152_2025_TTBTC",
                    exclude_doc_ids=None,
                )
                for hit in hits_152:
                    meta = hit.get("metadata", {})
                    results.append({
                        "chunk_id":   hit.get("chunk_id", ""),
                        "doc_id":     meta.get("doc_id", ""),
                        "node_type":  meta.get("node_type", ""),
                        "breadcrumb": meta.get("breadcrumb", ""),
                        "snippet":    hit.get("text", "")[:2000],
                        "score":      hit.get("rrf_score", 0.0),
                        "citation": {
                            "doc_id":      meta.get("doc_id", ""),
                            "doc_number":  meta.get("doc_number", ""),
                            "breadcrumb":  meta.get("breadcrumb", ""),
                        },
                    })
                logger.info(f"[B2] Injected {len(hits_152)} chunks from 152_2025_TTBTC")
            except Exception as e:
                logger.warning(f"[B2] Co-retrieval 152 failed: {e}")

    # P3a: Penalty co-retrieval — doc_filter='125_2020_NDCP' + penalty keywords → inject 310+68
    # 310 = NĐ mới xử phạt vi phạm thuế (2025), supersedes 125 for HKD-specific violations
    # Lớp 3 P0 gate exclude 310 khỏi expansion khi agent dùng 125 filter → 310 không bao giờ xuất hiện
    # Fix: explicitly inject 310 (và 68 nếu cần) khi agent tìm trong 125 với penalty query
    if normalized_filter == "125_2020_NDCP" and _has_penalty_keywords(query):
        doc_ids_in_results = {r["doc_id"] for r in results}
        try:
            injected_penalty = []
            for target_doc in ["310_2025_NDCP", "68_2026_NDCP"]:
                if target_doc not in doc_ids_in_results:
                    logger.info(f"[P3a] Penalty+125 filter → co-retrieve {target_doc}")
                    hits = searcher.search(
                        query=query,
                        n_results=2,
                        filter_doc_id=target_doc,
                        exclude_doc_ids=None,
                    )
                    for hit in hits:
                        meta = hit.get("metadata", {})
                        injected_penalty.append({
                            "chunk_id":   hit.get("chunk_id", ""),
                            "doc_id":     meta.get("doc_id", ""),
                            "node_type":  meta.get("node_type", ""),
                            "breadcrumb": meta.get("breadcrumb", ""),
                            "snippet":    hit.get("text", "")[:2000],
                            "score":      hit.get("rrf_score", 0.0),
                            "citation": {
                                "doc_id":      meta.get("doc_id", ""),
                                "doc_number":  meta.get("doc_number", ""),
                                "breadcrumb":  meta.get("breadcrumb", ""),
                            },
                        })
            if injected_penalty:
                results = injected_penalty + results
                logger.info(f"[P3a] Prepended {len(injected_penalty)} chunks (310+68) to 125 results")
        except Exception as e:
            logger.warning(f"[P3a] Penalty co-retrieval failed: {e}")

    # C1a: TMĐT co-retrieval — doc_filter='117_2025_NDCP' → inject 68_2026_NDCP
    # 117 = NĐ TMĐT (quy định cụ thể), 68 = NĐ HKD tổng thể (kê khai, nghĩa vụ)
    # Agent thường route TMĐT → 117 nhưng miss 68 → T2 PARTIAL 1/2
    if normalized_filter == "117_2025_NDCP":
        doc_ids_in_results = {r["doc_id"] for r in results}
        if "68_2026_NDCP" not in doc_ids_in_results:
            try:
                logger.info(f"[C1a] 117 filter → co-retrieve 68_2026_NDCP")
                hits_68 = searcher.search(
                    query=query,
                    n_results=3,
                    filter_doc_id="68_2026_NDCP",
                    exclude_doc_ids=None,
                )
                for hit in hits_68:
                    meta = hit.get("metadata", {})
                    results.append({
                        "chunk_id":   hit.get("chunk_id", ""),
                        "doc_id":     meta.get("doc_id", ""),
                        "node_type":  meta.get("node_type", ""),
                        "breadcrumb": meta.get("breadcrumb", ""),
                        "snippet":    hit.get("text", "")[:2000],
                        "score":      hit.get("rrf_score", 0.0),
                        "citation": {
                            "doc_id":      meta.get("doc_id", ""),
                            "doc_number":  meta.get("doc_number", ""),
                            "breadcrumb":  meta.get("breadcrumb", ""),
                        },
                    })
                logger.info(f"[C1a] Injected {len(hits_68)} chunks from 68_2026_NDCP")
            except Exception as e:
                logger.warning(f"[C1a] Co-retrieval 68 failed: {e}")

    # P3b: Ủy quyền co-retrieval — doc_filter='126_2020_NDCP' + ủy quyền keywords → inject 373_2025_NDCP
    # 373 = NĐ mới (2025) sửa đổi quy định ủy quyền quyết toán TNCN trong 126
    # Agent luôn route ủy quyền → 126, nhưng 373 chứa điều khoản cập nhật → T2 partial 1/2
    # KEYWORD GUARD: chỉ inject khi query có từ khóa ủy quyền, tránh precision penalty cho query 126 khác
    _UY_QUYEN_KEYWORDS = frozenset([
        "ủy quyền", "uỷ quyền", "quyết toán thay", "ủy quyền quyết toán",
        "được ủy quyền", "ủy quyền cho", "uỷ quyền cho",
    ])
    _is_uy_quyen_query = any(kw in query.lower() for kw in _UY_QUYEN_KEYWORDS)
    if normalized_filter == "126_2020_NDCP" and _is_uy_quyen_query:
        doc_ids_in_results = {r["doc_id"] for r in results}
        if "373_2025_NDCP" not in doc_ids_in_results:
            try:
                logger.info(f"[P3b] 126 filter → co-retrieve 373_2025_NDCP")
                hits_373 = searcher.search(
                    query=query,
                    n_results=3,
                    filter_doc_id="373_2025_NDCP",
                    exclude_doc_ids=None,
                )
                for hit in hits_373:
                    meta = hit.get("metadata", {})
                    results.append({
                        "chunk_id":   hit.get("chunk_id", ""),
                        "doc_id":     meta.get("doc_id", ""),
                        "node_type":  meta.get("node_type", ""),
                        "breadcrumb": meta.get("breadcrumb", ""),
                        "snippet":    hit.get("text", "")[:2000],
                        "score":      hit.get("rrf_score", 0.0),
                        "citation": {
                            "doc_id":      meta.get("doc_id", ""),
                            "doc_number":  meta.get("doc_number", ""),
                            "breadcrumb":  meta.get("breadcrumb", ""),
                        },
                    })
                logger.info(f"[P3b] Injected {len(hits_373)} chunks from 373_2025_NDCP")
            except Exception as e:
                logger.warning(f"[P3b] Co-retrieval 373 failed: {e}")

    # G1: Giảm trừ gia cảnh co-retrieval — doc_filter='111_2013_TTBTC' → inject 86_2024_TTBTC
    # 86 = TT86/2024 hướng dẫn đăng ký người phụ thuộc (mới nhất)
    # Agent tìm giảm trừ → 111, nhưng 86 là văn bản hướng dẫn cập nhật → hay bị bỏ sót
    if normalized_filter == "111_2013_TTBTC":
        doc_ids_in_results = {r["doc_id"] for r in results}
        if "86_2024_TTBTC" not in doc_ids_in_results:
            try:
                logger.info(f"[G1] 111 filter → co-retrieve 86_2024_TTBTC")
                hits_86 = searcher.search(
                    query=query,
                    n_results=3,
                    filter_doc_id="86_2024_TTBTC",
                    exclude_doc_ids=None,
                )
                for hit in hits_86:
                    meta = hit.get("metadata", {})
                    results.append({
                        "chunk_id":   hit.get("chunk_id", ""),
                        "doc_id":     meta.get("doc_id", ""),
                        "node_type":  meta.get("node_type", ""),
                        "breadcrumb": meta.get("breadcrumb", ""),
                        "snippet":    hit.get("text", "")[:2000],
                        "score":      hit.get("rrf_score", 0.0),
                        "citation": {
                            "doc_id":      meta.get("doc_id", ""),
                            "doc_number":  meta.get("doc_number", ""),
                            "breadcrumb":  meta.get("breadcrumb", ""),
                        },
                    })
                logger.info(f"[G1] Injected {len(hits_86)} chunks from 86_2024_TTBTC")
            except Exception as e:
                logger.warning(f"[G1] Co-retrieval 86 failed: {e}")

    # C1b: TMĐT co-retrieval — doc_filter='18_2026_TTBTC' + TMĐT keywords → inject 117+68
    # Agent có thể route TMĐT → 18 (tờ khai mẫu) thay vì 117 (NĐ TMĐT)
    _TMDT_KEYWORDS = frozenset([
        "thương mại điện tử", "tmđt", "sàn tmđt", "sàn giao dịch điện tử",
        "nền tảng số", "bán hàng online", "bán hàng trực tuyến",
        "117/2025", "nghị định 117",
        "khấu trừ tại nguồn", "thuế tmđt",
        "cnkd-tmđt", "02/cnkd",
    ])
    if normalized_filter == "18_2026_TTBTC":
        query_lower = query.lower()
        if any(kw in query_lower for kw in _TMDT_KEYWORDS):
            doc_ids_in_results = {r["doc_id"] for r in results}
            try:
                injected_tmdt = []
                for target_doc in ["117_2025_NDCP", "68_2026_NDCP"]:
                    if target_doc not in doc_ids_in_results:
                        logger.info(f"[C1b] TMĐT+18 filter → co-retrieve {target_doc}")
                        hits = searcher.search(
                            query=query,
                            n_results=3,
                            filter_doc_id=target_doc,
                            exclude_doc_ids=None,
                        )
                        for hit in hits:
                            meta = hit.get("metadata", {})
                            injected_tmdt.append({
                                "chunk_id":   hit.get("chunk_id", ""),
                                "doc_id":     meta.get("doc_id", ""),
                                "node_type":  meta.get("node_type", ""),
                                "breadcrumb": meta.get("breadcrumb", ""),
                                "snippet":    hit.get("text", "")[:2000],
                                "score":      hit.get("rrf_score", 0.0),
                                "citation": {
                                    "doc_id":      meta.get("doc_id", ""),
                                    "doc_number":  meta.get("doc_number", ""),
                                    "breadcrumb":  meta.get("breadcrumb", ""),
                                },
                            })
                if injected_tmdt:
                    results = injected_tmdt + results
                    logger.info(f"[C1b] Prepended {len(injected_tmdt)} TMĐT chunks (117+68)")
            except Exception as e:
                logger.warning(f"[C1b] TMĐT co-retrieval failed: {e}")

    return {
        "query":        query,
        "total_found":  len(results),
        "results":      results,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 4 — get_article
# ═══════════════════════════════════════════════════════════════════════════════

def get_article(article_id: str) -> dict:
    """
    Lấy toàn văn một Điều luật từ Neo4j, bao gồm tất cả Khoản và Điểm.

    Args:
        article_id: ID của Article node. Định dạng: 'doc_{doc_id}_[chuong_X_]dieu_N'.
                    Ví dụ: 'doc_68_2026_NDCP_chuong_II_dieu_4'
                    hoặc:  'doc_110_2025_UBTVQH15_dieu_1'

    Returns:
        dict với article text, clauses, points, và citation.
    """
    try:
        grapher = _get_grapher()
        data = grapher.get_article_full(article_id)
    except Exception as e:
        logger.error(f"get_article failed for {article_id!r}: {e}")
        return {"article_id": article_id, "found": False, "error": str(e)}

    if not data:
        return {"article_id": article_id, "found": False}

    article = data.get("article", {})
    clauses = data.get("clauses", [])
    points  = data.get("points", [])

    # Build full text
    text_parts = [article.get("title", ""), article.get("content", "")]
    for clause in sorted(clauses, key=lambda c: c.get("node_index", 0)):
        text_parts.append(clause.get("content", ""))
        for point in sorted(
            [p for p in points if p.get("parent_id") == clause.get("id")],
            key=lambda p: p.get("node_index", 0),
        ):
            text_parts.append("  " + point.get("content", ""))

    full_text = "\n".join(t for t in text_parts if t)

    return {
        "article_id":  article_id,
        "found":       True,
        "doc_id":      article.get("doc_id", ""),
        "title":       article.get("title", ""),
        "content":     article.get("content", ""),
        "full_text":   full_text,
        "clause_count": len(clauses),
        "point_count":  len(points),
        "clauses": [
            {
                "id":      c.get("id", ""),
                "index":   c.get("node_index"),
                "content": c.get("content", ""),
            }
            for c in sorted(clauses, key=lambda c: c.get("node_index", 0))
        ],
        "citation": {
            "doc_id":     article.get("doc_id", ""),
            "article_id": article_id,
            "title":      article.get("title", ""),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 6 — get_guidance
# ═══════════════════════════════════════════════════════════════════════════════

def get_guidance(
    article_id: str,
    min_confidence: float = 0.82,
) -> dict:
    """
    Lấy các GuidanceChunks (từ Sổ tay HKD, Công văn) giải thích cho một Điều luật.

    Args:
        article_id:      ID của Article cần tra cứu hướng dẫn.
        min_confidence:  Ngưỡng confidence tối thiểu (mặc định 0.82).

    Returns:
        dict với list guidance chunks có nguồn từ Sổ tay / Công văn.
    """
    try:
        grapher = _get_grapher()
        chunks = grapher.get_guidance(article_id, min_confidence=min_confidence)
    except Exception as e:
        logger.error(f"get_guidance failed for {article_id!r}: {e}")
        return {"article_id": article_id, "chunks": [], "error": str(e)}

    return {
        "article_id":     article_id,
        "guidance_count": len(chunks),
        "chunks": [
            {
                "chunk_id":   c.get("id", ""),
                "doc_id":     c.get("doc_id", ""),
                "content":    c.get("content", "")[:600],
                "confidence": c.get("confidence", 0.0),
                "method":     c.get("method", ""),
                "citation": {
                    "doc_id":  c.get("doc_id", ""),
                    "node_id": c.get("id", ""),
                },
            }
            for c in chunks
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 7 — get_impl_chain
# ═══════════════════════════════════════════════════════════════════════════════

_DOC_TYPE_RANK = {
    "Luật": 1,
    "Nghị quyết": 2,
    "Nghị định": 3,
    "Thông tư": 4,
    "Công văn": 5,
    "Sổ tay hướng dẫn": 6,
}


def get_impl_chain(doc_id: str) -> dict:
    """
    Trả về chuỗi văn bản liên quan đến doc_id qua quan hệ
    IMPLEMENTS / AMENDS / SUPERSEDES.

    Giúp LLM hiểu hierarchy pháp lý: Luật → Nghị định → Thông tư → Công văn.

    Args:
        doc_id: ID của văn bản cần tra cứu. Ví dụ: '68_2026_NDCP'.

    Returns:
        dict với danh sách văn bản liên quan và loại quan hệ.
    """
    try:
        grapher = _get_grapher()
        chain = grapher.get_impl_chain(doc_id)
    except Exception as e:
        logger.error(f"get_impl_chain failed for {doc_id!r}: {e}")
        return {"doc_id": doc_id, "chain": [], "error": str(e)}

    items = []
    for node in chain:
        items.append({
            "doc_id":       node.get("doc_id", node.get("id", "")),
            "doc_number":   node.get("doc_number", ""),
            "doc_type":     node.get("doc_type", ""),
            "title":        (node.get("title") or "")[:100],
            "status":       node.get("status", ""),
            "valid_from":   node.get("valid_from", ""),
            "rel_type":     node.get("rel_type", ""),
        })

    # Sort theo hierarchy rank (Luật → Nghị định → Thông tư)
    items.sort(key=lambda x: _DOC_TYPE_RANK.get(x["doc_type"], 99))

    return {
        "doc_id":      doc_id,
        "chain_count": len(items),
        "chain":       items,
        "summary": _format_chain_summary(doc_id, items),
    }


def _format_chain_summary(doc_id: str, items: list[dict]) -> str:
    if not items:
        return f"Không tìm thấy văn bản liên quan đến {doc_id}."
    lines = [f"Văn bản liên quan đến {doc_id}:"]
    for item in items:
        rel = item["rel_type"]
        rel_vi = {"IMPLEMENTS": "hướng dẫn thi hành", "AMENDS": "sửa đổi/bổ sung",
                  "SUPERSEDES": "thay thế"}.get(rel, rel)
        lines.append(
            f"  [{item['doc_type']}] {item['doc_number'] or item['doc_id']} "
            f"— {rel_vi} — {item['title'][:60]}"
        )
    return "\n".join(lines)
