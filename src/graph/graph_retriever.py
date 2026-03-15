"""
graph_retriever.py — Neo4j-backed context expansion cho retrieval pipeline.

Các chức năng chính:
  expand_context   — lấy parent Article + siblings của một node
  get_references   — follow REFERENCES edges
  get_guidance     — EXPLAINED_BY lookup (GuidanceChunk)
  validity_filter  — lọc doc_id còn hiệu lực tại query_date
  get_impl_chain   — IMPLEMENTS/AMENDS traversal từ một Document
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from src.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


class GraphRetriever:
    def __init__(self, client: Neo4jClient | None = None):
        self._own = client is None
        self.client = client or Neo4jClient()

    def close(self):
        if self._own:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── 1. Context expansion ──────────────────────────────────────────────

    def expand_context(self, node_id: str) -> dict[str, Any]:
        """
        Cho một node_id (Clause hoặc Point), trả về:
          - node: chính node đó
          - parent: Article hoặc Clause cha
          - siblings: các node cùng cấp (cùng cha)
          - article_context: Article gốc (nếu node là Point/SubPoint)
        """
        result: dict[str, Any] = {"node": None, "parent": None,
                                  "siblings": [], "article_context": None}

        # Lấy node + parent trực tiếp
        rows = self.client.run("""
MATCH (child {id: $nid})
OPTIONAL MATCH (parent)-[]->(child)
WHERE NOT parent:Document AND NOT parent:GuidanceDocument
RETURN child, parent
LIMIT 1
""", {"nid": node_id})

        if not rows:
            return result

        result["node"]   = dict(rows[0]["child"])
        result["parent"] = dict(rows[0]["parent"]) if rows[0]["parent"] else None

        # Siblings — nodes cùng cha
        if result["parent"]:
            parent_id = result["parent"].get("id")
            if parent_id:
                sibs = self.client.run("""
MATCH (p {id: $pid})-[]->(sib)
WHERE sib.id <> $nid
  AND NOT sib:Document AND NOT sib:GuidanceDocument
RETURN sib
ORDER BY sib.node_index
LIMIT 10
""", {"pid": parent_id, "nid": node_id})
                result["siblings"] = [dict(r["sib"]) for r in sibs]

        # Article gốc — leo lên tối đa 3 bậc để tìm Article
        article_rows = self.client.run("""
MATCH path = (art:Article)-[*1..3]->(n {id: $nid})
RETURN art LIMIT 1
""", {"nid": node_id})
        if article_rows:
            result["article_context"] = dict(article_rows[0]["art"])

        return result

    # ── 2. References ─────────────────────────────────────────────────────

    def get_references(self, node_id: str) -> list[dict[str, Any]]:
        """Trả về các node mà node_id REFERENCES đến (nội bộ)."""
        rows = self.client.run("""
MATCH (src {id: $nid})-[r:REFERENCES]->(tgt)
RETURN tgt, r.text_match AS text_match
""", {"nid": node_id})
        return [
            {**dict(r["tgt"]), "text_match": r["text_match"]}
            for r in rows
        ]

    # ── 3. GuidanceChunk (EXPLAINED_BY) ───────────────────────────────────

    def get_guidance(
        self,
        article_id: str,
        min_confidence: float = 0.82,
    ) -> list[dict[str, Any]]:
        """
        Trả về GuidanceChunks giải thích cho article_id
        với confidence >= min_confidence.
        """
        rows = self.client.run("""
MATCH (art {id: $aid})-[r:EXPLAINED_BY]->(chunk:GuidanceChunk)
WHERE r.confidence >= $min_conf
RETURN chunk, r.confidence AS confidence, r.method AS method
ORDER BY r.confidence DESC
""", {"aid": article_id, "min_conf": min_confidence})
        return [
            {**dict(r["chunk"]),
             "confidence": r["confidence"],
             "method":     r["method"]}
            for r in rows
        ]

    # ── 4. Validity filter ────────────────────────────────────────────────

    def validity_filter(
        self,
        doc_ids: list[str],
        query_date: date | None = None,
    ) -> list[str]:
        """
        Trả về subset của doc_ids còn hiệu lực tại query_date.
        doc_ids không có trong DB được giữ lại (no-op).
        """
        if not doc_ids:
            return []

        qd = (query_date or date.today()).isoformat()

        rows = self.client.run("""
UNWIND $ids AS did
MATCH (d)
WHERE (d:Document OR d:GuidanceDocument) AND d.doc_id = did
  AND (d.valid_from IS NULL OR date(d.valid_from) <= date($qd))
  AND (d.valid_to   IS NULL OR date(d.valid_to)   >  date($qd))
RETURN d.doc_id AS doc_id
""", {"ids": doc_ids, "qd": qd})

        valid_set = {r["doc_id"] for r in rows}

        # doc_ids không có trong DB → giữ nguyên
        db_known = {r["doc_id"] for r in self.client.run("""
UNWIND $ids AS did
MATCH (d)
WHERE (d:Document OR d:GuidanceDocument) AND d.doc_id = did
RETURN d.doc_id AS doc_id
""", {"ids": doc_ids})}
        return [d for d in doc_ids if d not in db_known or d in valid_set]

    # ── 5. Implementation chain ───────────────────────────────────────────

    def get_impl_chain(self, doc_id: str) -> list[dict[str, Any]]:
        """
        Trả về các Document liên quan đến doc_id qua
        IMPLEMENTS / AMENDS / SUPERSEDES (tối đa 2 bước).
        """
        # Outgoing: doc_id → target (e.g. NĐ 68 IMPLEMENTS Luật 109)
        rows = self.client.run("""
MATCH (d:Document {doc_id: $did})-[r:IMPLEMENTS|AMENDS|SUPERSEDES]->(target)
RETURN DISTINCT target, type(r) AS rel_type
""", {"did": doc_id})

        # Incoming: ai IMPLEMENTS/AMENDS doc này (e.g. Luật 109 ← NĐ 68)
        if not rows:
            rows = self.client.run("""
MATCH (impl:Document)-[r:IMPLEMENTS|AMENDS]->(d:Document {doc_id: $did})
RETURN DISTINCT impl AS target, type(r) AS rel_type
""", {"did": doc_id})

        return [
            {**dict(r["target"]), "rel_type": r["rel_type"]}
            for r in rows
        ]

    # ── 6. Direct article lookup ──────────────────────────────────────────

    def get_article_full(self, article_id: str) -> dict[str, Any]:
        """
        Trả về Article + tất cả Clause + Point con.
        Dùng khi query type = DIRECT_LOOKUP.
        """
        rows = self.client.run("""
MATCH (a:Article {id: $aid})
OPTIONAL MATCH (a)-[:HAS_CLAUSE]->(k:Clause)
OPTIONAL MATCH (k)-[:HAS_POINT]->(d:Point)
RETURN a, collect(DISTINCT k) AS clauses, collect(DISTINCT d) AS points
""", {"aid": article_id})

        if not rows:
            return {}

        r = rows[0]
        return {
            "article": dict(r["a"]),
            "clauses": [dict(k) for k in r["clauses"]],
            "points":  [dict(d) for d in r["points"]],
        }

    # ── 7. Enrich hybrid-search hits ─────────────────────────────────────

    def enrich_hits(
        self,
        hits: list[dict[str, Any]],
        query_date: date | None = None,
        guidance_min_confidence: float = 0.82,
    ) -> list[dict[str, Any]]:
        """
        Nhận list hits từ HybridSearch, trả về hits đã enrich với:
          - validity_ok: bool
          - parent_title: tiêu đề Article cha
          - guidance_chunks: list GuidanceChunk nếu có EXPLAINED_BY
          - referenced_nodes: list node được REFERENCES đến

        Hits từ doc đã hết hiệu lực (valid_to < query_date) bị đánh dấu
        validity_ok=False nhưng KHÔNG bị loại (để generator có thể cảnh báo).
        """
        if not hits:
            return hits

        # Validity check theo doc_id
        doc_ids = list({h["metadata"].get("doc_id", "") for h in hits})
        valid_doc_ids = set(self.validity_filter(doc_ids, query_date))

        enriched = []
        for hit in hits:
            node_id = hit["metadata"].get("node_id") or hit.get("chunk_id", "").replace("_chunk", "")
            doc_id  = hit["metadata"].get("doc_id", "")

            extra: dict[str, Any] = {
                "validity_ok":      doc_id in valid_doc_ids,
                "parent_title":     None,
                "guidance_chunks":  [],
                "referenced_nodes": [],
            }

            # Parent Article title
            ctx = self.expand_context(node_id)
            if ctx.get("article_context"):
                extra["parent_title"] = ctx["article_context"].get("title") or \
                                        ctx["article_context"].get("content", "")[:80]

            # References
            refs = self.get_references(node_id)
            if refs:
                extra["referenced_nodes"] = [
                    {"id": r["id"], "content": (r.get("content") or "")[:200]}
                    for r in refs
                ]

            # Guidance — chỉ tra nếu node là Article hoặc parent là Article
            art_id = (ctx.get("article_context") or {}).get("id")
            if art_id:
                extra["guidance_chunks"] = self.get_guidance(
                    art_id, min_confidence=guidance_min_confidence
                )

            enriched.append({**hit, **extra})

        return enriched
