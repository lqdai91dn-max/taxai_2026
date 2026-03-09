"""
src/retrieval/hybrid_search.py
Hybrid search = BM25 (keyword) + Vector (semantic) cho TaxAI 2026
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from rank_bm25 import BM25Okapi

from src.retrieval.embedder import Chunk, DocumentEmbedder
from src.retrieval.vector_store import VectorStore, CHROMA_DIR

logger = logging.getLogger(__name__)


def tokenize_vi(text: str) -> List[str]:
    """Tokenize tiếng Việt đơn giản — tách theo khoảng trắng và dấu câu"""
    import re
    text = text.lower()
    tokens = re.findall(r'[\w]+', text)
    return tokens


class HybridSearch:
    """
    Kết hợp BM25 + Vector search với RRF (Reciprocal Rank Fusion)
    
    BM25  → tốt cho keyword chính xác (số điều, thuế suất, %)
    Vector → tốt cho semantic (câu hỏi tự nhiên)
    RRF   → kết hợp rank từ cả 2, không cần normalize score
    """

    def __init__(
        self,
        chroma_dir: str = CHROMA_DIR,
        model_name: str = "keepitreal/vietnamese-sbert",
    ):
        # Load embedder và vector store
        self.embedder = DocumentEmbedder(model_name)
        self.store    = VectorStore(chroma_dir)

        # Build BM25 index từ ChromaDB
        self._build_bm25_index()

        logger.info("✅ HybridSearch initialized")

    # ── BM25 Index ───────────────────────────────────────────────────────

    def _build_bm25_index(self):
        """Load tất cả chunks từ ChromaDB → build BM25 index"""
        logger.info("🔨 Building BM25 index...")

        # Lấy tất cả documents từ ChromaDB
        total = self.store.count()
        if total == 0:
            logger.warning("⚠️ ChromaDB trống — chưa index documents!")
            self.bm25       = None
            self.bm25_chunks = []
            return

        results = self.store.collection.get(
            include=["documents", "metadatas"]
        )

        self.bm25_ids      = results["ids"]
        self.bm25_texts    = results["documents"]
        self.bm25_metas    = results["metadatas"]

        # Tokenize và build BM25
        tokenized = [tokenize_vi(text) for text in self.bm25_texts]
        self.bm25 = BM25Okapi(tokenized)

        logger.info(f"✅ BM25 index built — {len(self.bm25_texts)} documents")

    # ── Search ───────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        n_results: int = 5,
        bm25_weight: float = 0.3,
        vector_weight: float = 0.7,
        filter_doc_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search với RRF fusion
        
        Args:
            query: câu hỏi của user
            n_results: số kết quả trả về
            bm25_weight: trọng số BM25 (keyword)
            vector_weight: trọng số vector (semantic)
            filter_doc_id: lọc theo document cụ thể
        """

        # 1. Vector search
        query_embedding = self.embedder.model.encode(
            query,
            normalize_embeddings=True
        ).tolist()

        vector_hits = self.store.query(
            query_embedding = query_embedding,
            n_results       = min(n_results * 3, 30),
            filter_doc_id   = filter_doc_id,
        )

        # 2. BM25 search
        bm25_hits = self._bm25_search(query, n_results * 3, filter_doc_id)

        # 3. RRF Fusion
        results = self._rrf_fusion(
            vector_hits, bm25_hits,
            bm25_weight, vector_weight,
            n_results
        )

        return results

    def _bm25_search(
        self,
        query: str,
        n_results: int,
        filter_doc_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """BM25 keyword search"""
        if not self.bm25:
            return []

        tokens = tokenize_vi(query)
        scores = self.bm25.get_scores(tokens)

        # Tạo list (index, score) và sort
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        hits = []
        for idx, score in ranked[:n_results * 2]:
            meta = self.bm25_metas[idx]

            # Filter theo doc_id nếu có
            if filter_doc_id and meta.get("doc_id") != filter_doc_id:
                continue

            if score > 0:
                hits.append({
                    "chunk_id": self.bm25_ids[idx],
                    "text":     self.bm25_texts[idx],
                    "metadata": meta,
                    "score":    float(score),
                })

            if len(hits) >= n_results:
                break

        return hits

    def _rrf_fusion(
        self,
        vector_hits: List[Dict],
        bm25_hits: List[Dict],
        bm25_weight: float,
        vector_weight: float,
        n_results: int,
        k: int = 60,  # RRF constant
    ) -> List[Dict[str, Any]]:
        """Reciprocal Rank Fusion"""

        rrf_scores: Dict[str, float] = {}
        chunk_data: Dict[str, Dict]  = {}

        # Vector ranks
        for rank, hit in enumerate(vector_hits):
            cid = hit["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0) + vector_weight / (k + rank + 1)
            chunk_data[cid] = hit

        # BM25 ranks
        for rank, hit in enumerate(bm25_hits):
            cid = hit["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0) + bm25_weight / (k + rank + 1)
            if cid not in chunk_data:
                chunk_data[cid] = hit

        # Sort by RRF score
        sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)

        results = []
        for cid in sorted_ids[:n_results]:
            hit = chunk_data[cid].copy()
            hit["rrf_score"] = round(rrf_scores[cid], 6)
            results.append(hit)

        return results


# ── Test search ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    searcher = HybridSearch()

    test_queries = [
        "thuế suất thu nhập cá nhân từ tiền lương",
        "mức giảm trừ gia cảnh cho người phụ thuộc",
        "tổ chức quản lý nền tảng thương mại điện tử khấu trừ thuế",
        "Điều 9 biểu thuế lũy tiến",
        "thu nhập được miễn thuế",
    ]

    for query in test_queries:
        print(f"\n{'='*60}")
        print(f"🔍 Query: {query}")
        print('='*60)

        results = searcher.search(query, n_results=3)

        for i, r in enumerate(results, 1):
            print(f"\n[{i}] Score: {r['rrf_score']:.4f}")
            print(f"    Breadcrumb: {r['metadata'].get('breadcrumb','')}")
            print(f"    Text: {r['text'][:120]}...")