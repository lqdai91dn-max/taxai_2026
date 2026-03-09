"""
src/retrieval/vector_store.py
Lưu và truy vấn embeddings bằng ChromaDB cho TaxAI 2026
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings

from src.retrieval.embedder import Chunk, DocumentEmbedder

logger = logging.getLogger(__name__)

CHROMA_DIR = "data/chroma"
COLLECTION_NAME = "taxai_legal_docs"


class VectorStore:
    """
    Quản lý ChromaDB collection cho TaxAI
    - Upsert chunks + embeddings
    - Query by vector similarity
    """

    def __init__(self, persist_dir: str = CHROMA_DIR):
        self.persist_dir = persist_dir
        Path(persist_dir).mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )

        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}  # cosine similarity
        )

        logger.info(f"✅ VectorStore initialized — collection: {COLLECTION_NAME}")
        logger.info(f"   Existing docs: {self.collection.count()}")

    # ── Upsert ───────────────────────────────────────────────────────────

    def upsert(
        self,
        chunks: List[Chunk],
        embeddings: List[List[float]]
    ) -> int:
        """Thêm hoặc cập nhật chunks vào ChromaDB"""
        if not chunks:
            return 0

        ids        = [c.chunk_id for c in chunks]
        documents  = [c.text for c in chunks]
        metadatas  = [c.metadata for c in chunks]

        # ChromaDB batch upsert
        batch_size = 100
        total = 0
        for i in range(0, len(ids), batch_size):
            self.collection.upsert(
                ids        = ids[i:i+batch_size],
                embeddings = embeddings[i:i+batch_size],
                documents  = documents[i:i+batch_size],
                metadatas  = metadatas[i:i+batch_size],
            )
            total += len(ids[i:i+batch_size])

        logger.info(f"✅ Upserted {total} chunks — total in DB: {self.collection.count()}")
        return total

    # ── Query ─────────────────────────────────────────────────────────────

    def query(
        self,
        query_embedding: List[float],
        n_results: int = 10,
        filter_doc_id: Optional[str] = None,
        filter_node_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Vector similarity search"""

        where = {}
        if filter_doc_id:
            where["doc_id"] = filter_doc_id
        if filter_node_type:
            where["node_type"] = filter_node_type

        results = self.collection.query(
            query_embeddings = [query_embedding],
            n_results        = n_results,
            where            = where if where else None,
            include          = ["documents", "metadatas", "distances"],
        )

        hits = []
        for i in range(len(results["ids"][0])):
            hits.append({
                "chunk_id":  results["ids"][0][i],
                "text":      results["documents"][0][i],
                "metadata":  results["metadatas"][0][i],
                "score":     1 - results["distances"][0][i],  # cosine similarity
            })

        return hits

    def count(self) -> int:
        return self.collection.count()

    def delete_doc(self, doc_id: str):
        """Xóa tất cả chunks của một document"""
        self.collection.delete(where={"doc_id": doc_id})
        logger.info(f"🗑️ Deleted all chunks for doc: {doc_id}")


# ── Full pipeline: JSON → embed → store ──────────────────────────────────

def index_all_documents(
    parsed_dir: str = "data/parsed",
    chroma_dir: str = CHROMA_DIR,
    model_name: str = "keepitreal/vietnamese-sbert",
):
    """Index tất cả file JSON trong parsed_dir vào ChromaDB"""

    parsed_path = Path(parsed_dir)
    json_files  = list(parsed_path.glob("*.json"))

    if not json_files:
        logger.error(f"❌ Không có file JSON trong {parsed_dir}")
        return

    embedder = DocumentEmbedder(model_name)
    store    = VectorStore(chroma_dir)

    for json_path in json_files:
        logger.info(f"\n📄 Indexing: {json_path.name}")
        chunks, embeddings = embedder.process_file(json_path)
        store.upsert(chunks, embeddings)

    logger.info(f"\n🎉 Done! Total chunks in DB: {store.count()}")
    return store


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    store = index_all_documents()

    if store:
        print(f"\n✅ Vector DB ready — {store.count()} chunks indexed")
        print(f"   Location: {CHROMA_DIR}")