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

def search_legal_docs(
    query: str,
    top_k: int = 5,
    doc_filter: str | None = None,
) -> dict:
    """
    Tìm kiếm điều khoản pháp luật liên quan bằng hybrid search (BM25 + vector).

    Args:
        query:       Câu hỏi hoặc từ khóa cần tìm.
        top_k:       Số kết quả trả về (mặc định 5, tối đa 10).
        doc_filter:  doc_id cụ thể để giới hạn phạm vi tìm kiếm (optional).

    Returns:
        dict với results list, mỗi item có snippet + citation.
    """
    top_k = min(max(1, top_k), 10)

    try:
        searcher = _get_searcher()
        hits = searcher.search(
            query=query,
            n_results=top_k,
            filter_doc_id=doc_filter,
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
            "snippet":    hit.get("text", "")[:400],
            "score":      hit.get("rrf_score", 0.0),
            "citation": {
                "doc_id":      meta.get("doc_id", ""),
                "doc_number":  meta.get("doc_number", ""),
                "breadcrumb":  meta.get("breadcrumb", ""),
            },
        })

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
