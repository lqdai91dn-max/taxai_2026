"""
src/retrieval/vector_store.py
Vector store cho TaxAI — Qdrant backend.

Cloud (production): set QDRANT_URL + QDRANT_API_KEY trong .env
Local  (dev):       QDRANT_URL không set → dùng data/qdrant/ (persistent local)
"""

from __future__ import annotations
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchValue,
    FilterSelector, ScrollRequest,
)

from src.retrieval.embedder import Chunk, DocumentEmbedder

logger = logging.getLogger(__name__)

COLLECTION_NAME = "taxai_legal_docs"
VECTOR_DIM      = 768          # keepitreal/vietnamese-sbert output dim
QDRANT_LOCAL    = "data/qdrant"
_NAMESPACE      = uuid.UUID("a3f2d8e1-7c4b-4a9f-8e6d-2b1c5f3e9a7d")


def _to_uuid(chunk_id: str) -> str:
    """Deterministically map string chunk_id → UUID (Qdrant point ID)."""
    return str(uuid.uuid5(_NAMESPACE, chunk_id))


def _make_client() -> QdrantClient:
    url     = os.getenv("QDRANT_URL", "").strip()
    api_key = os.getenv("QDRANT_API_KEY", "").strip()
    if url:
        logger.info(f"Qdrant Cloud: {url}")
        return QdrantClient(url=url, api_key=api_key or None)
    path = QDRANT_LOCAL
    Path(path).mkdir(parents=True, exist_ok=True)
    logger.info(f"Qdrant local: {path}")
    return QdrantClient(path=path)


class VectorStore:
    """
    Quản lý Qdrant collection cho TaxAI.
    Interface giữ nguyên so với ChromaDB version.
    """

    def __init__(self):
        self.client = _make_client()
        self._ensure_collection()
        logger.info(f"✅ VectorStore ready — {self.count()} chunks")

    def _ensure_collection(self):
        existing = [c.name for c in self.client.get_collections().collections]
        if COLLECTION_NAME not in existing:
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )
            logger.info(f"Created collection '{COLLECTION_NAME}'")

    # ── Upsert ────────────────────────────────────────────────────────────────

    def upsert(self, chunks: List[Chunk], embeddings: List[List[float]]) -> int:
        if not chunks:
            return 0

        batch_size = 50
        total = 0
        for i in range(0, len(chunks), batch_size):
            points = [
                PointStruct(
                    id      = _to_uuid(c.chunk_id),
                    vector  = embeddings[i + j],
                    payload = {"chunk_id": c.chunk_id, "text": c.text, **c.metadata},
                )
                for j, c in enumerate(chunks[i:i + batch_size])
            ]
            self.client.upsert(collection_name=COLLECTION_NAME, points=points, timeout=60)
            total += len(points)

        logger.info(f"Upserted {total} chunks — total: {self.count()}")
        return total

    # ── Query ─────────────────────────────────────────────────────────────────

    def query(
        self,
        query_embedding: List[float],
        n_results: int = 10,
        filter_doc_id: Optional[str] = None,
        filter_node_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        qfilter = None
        conditions = []
        if filter_doc_id:
            conditions.append(FieldCondition(key="doc_id", match=MatchValue(value=filter_doc_id)))
        if filter_node_type:
            conditions.append(FieldCondition(key="node_type", match=MatchValue(value=filter_node_type)))
        if conditions:
            from qdrant_client.models import Filter as QFilter, Must
            qfilter = QFilter(must=conditions)

        hits = self.client.search(
            collection_name = COLLECTION_NAME,
            query_vector    = query_embedding,
            limit           = n_results,
            query_filter    = qfilter,
            with_payload    = True,
        )

        return [
            {
                "chunk_id": h.payload.get("chunk_id", str(h.id)),
                "text":     h.payload.get("text", ""),
                "metadata": {k: v for k, v in h.payload.items() if k not in ("chunk_id", "text")},
                "score":    h.score,
            }
            for h in hits
        ]

    # ── Fetch helpers ─────────────────────────────────────────────────────────

    def get_by_ids(self, chunk_ids: List[str]) -> List[Dict[str, Any]]:
        if not chunk_ids:
            return []
        try:
            pts = self.client.retrieve(
                collection_name = COLLECTION_NAME,
                ids             = [_to_uuid(cid) for cid in chunk_ids],
                with_payload    = True,
            )
        except Exception:
            return []
        return [
            {
                "chunk_id": p.payload.get("chunk_id", str(p.id)),
                "text":     p.payload.get("text", ""),
                "metadata": {k: v for k, v in p.payload.items() if k not in ("chunk_id", "text")},
            }
            for p in pts
        ]

    def get_by_doc_id(self, doc_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        pts, _ = self.client.scroll(
            collection_name = COLLECTION_NAME,
            scroll_filter   = Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
            limit           = limit,
            with_payload    = True,
        )
        return [
            {
                "chunk_id": p.payload.get("chunk_id", str(p.id)),
                "text":     p.payload.get("text", ""),
                "metadata": {k: v for k, v in p.payload.items() if k not in ("chunk_id", "text")},
            }
            for p in pts
        ]

    def get_all(self) -> Dict[str, List]:
        """
        Trả về toàn bộ chunks — dùng để build BM25 index.
        Format: {"ids": [...], "documents": [...], "metadatas": [...]}
        """
        ids, documents, metadatas = [], [], []
        offset = None

        while True:
            pts, next_offset = self.client.scroll(
                collection_name = COLLECTION_NAME,
                limit           = 500,
                offset          = offset,
                with_payload    = True,
                with_vectors    = False,
            )
            for p in pts:
                ids.append(p.payload.get("chunk_id", str(p.id)))
                documents.append(p.payload.get("text", ""))
                metadatas.append({k: v for k, v in p.payload.items()
                                   if k not in ("chunk_id", "text")})
            if next_offset is None:
                break
            offset = next_offset

        return {"ids": ids, "documents": documents, "metadatas": metadatas}

    def count(self) -> int:
        try:
            return self.client.count(collection_name=COLLECTION_NAME).count
        except Exception:
            return 0

    def delete_doc(self, doc_id: str):
        self.client.delete(
            collection_name = COLLECTION_NAME,
            points_selector = FilterSelector(
                filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))])
            ),
        )
        logger.info(f"Deleted chunks for doc: {doc_id}")


# ── Full pipeline: JSON → embed → store ──────────────────────────────────────

def index_all_documents(
    parsed_dir: str = "data/parsed",
    model_name: str = "keepitreal/vietnamese-sbert",
):
    """Index tất cả file JSON trong parsed_dir vào Qdrant."""
    parsed_path = Path(parsed_dir)
    json_files  = list(parsed_path.glob("*.json"))

    if not json_files:
        logger.error(f"Không có file JSON trong {parsed_dir}")
        return

    embedder = DocumentEmbedder(model_name)
    store    = VectorStore()

    for json_path in json_files:
        logger.info(f"Indexing: {json_path.name}")
        chunks, embeddings = embedder.process_file(json_path)
        store.upsert(chunks, embeddings)

    logger.info(f"Done! Total: {store.count()} chunks")
    return store


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    store = index_all_documents()
    if store:
        print(f"✅ Qdrant ready — {store.count()} chunks")
