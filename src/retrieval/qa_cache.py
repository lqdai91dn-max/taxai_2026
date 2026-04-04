"""
src/retrieval/qa_cache.py

Semantic Q&A Cache — lưu trữ câu hỏi + câu trả lời + key_facts.
Khi câu hỏi mới đến, tìm câu hỏi tương tự trong cache (similarity > threshold).
Nếu hit → trả về cached answer ngay (bypass full pipeline).
Nếu miss → chạy full pipeline → lưu kết quả vào cache.

Storage: ChromaDB collection riêng "taxai_qa_cache"
Embedding: cùng model vietnamese-sbert để so sánh được với legal doc vectors

Usage:
    cache = QACache()
    hit   = cache.lookup("câu hỏi mới")
    if hit:
        return hit.answer, hit.key_facts  # cache hit: instant
    else:
        answer, kf = run_pipeline(question)
        cache.store(question, answer, kf)
        return answer, kf

Seed từ benchmark:
    cache.seed_from_benchmark("data/eval/results/benchmark_round12.json")
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from src.retrieval.embedder import DocumentEmbedder

logger = logging.getLogger(__name__)

CHROMA_DIR        = "data/chroma"
QA_COLLECTION     = "taxai_qa_cache"
DEFAULT_THRESHOLD = 0.88   # cosine similarity >= 0.88 → cache hit
# Note: vietnamese-sbert cho paraphrase ~0.72-0.73, exact = 1.0
# Dùng 0.88 để chỉ hit khi câu hỏi gần như giống nhau (±từ ngữ nhỏ)
DEFAULT_MODEL     = "keepitreal/vietnamese-sbert"

# Cache version — bump khi thay đổi pipeline, model, hoặc luật có hiệu lực mới.
# Cache entries có version khác sẽ bị skip (miss) dù similarity cao.
# History:
#   v1 — R49+: TaxAIAgent + 8 active tools (4 Neo4j tools removed)
CACHE_VERSION     = "v1"


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class CacheHit:
    question_cached: str    # câu hỏi gốc trong cache (gần nhất)
    answer: str             # cached answer
    key_facts: list[str]    # cached key facts
    similarity: float       # cosine similarity với query
    topic: str = ""
    source_round: str = ""  # benchmark round hoặc "user"


# ── QACache ───────────────────────────────────────────────────────────────────

class QACache:
    """
    Semantic Q&A Cache dùng ChromaDB + vietnamese-sbert.

    Cơ chế:
      - Embed câu hỏi → search collection → trả về CacheHit nếu similarity đủ cao
      - Store: upsert (idempotent theo question_id = sha256 của câu hỏi)
      - Seed: import hàng loạt từ benchmark JSON
    """

    def __init__(
        self,
        chroma_dir: str = CHROMA_DIR,
        model_name: str = DEFAULT_MODEL,
        threshold: float = DEFAULT_THRESHOLD,
    ):
        self.threshold = threshold
        self.embedder  = DocumentEmbedder(model_name)

        # ChromaDB — dùng cùng persistent client nhưng collection riêng
        Path(chroma_dir).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=chroma_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._col = self._client.get_or_create_collection(
            name=QA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"QACache initialized — {self._col.count()} entries in '{QA_COLLECTION}'"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def lookup(self, question: str) -> CacheHit | None:
        """
        Tìm cached answer cho câu hỏi.
        Trả về CacheHit nếu similarity >= threshold, None nếu miss.
        """
        if self._col.count() == 0:
            return None

        emb = self._embed(question)
        results = self._col.query(
            query_embeddings=[emb],
            n_results=1,
            include=["documents", "metadatas", "distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return None

        distance   = results["distances"][0][0]   # ChromaDB cosine: distance = 1 - similarity
        similarity = 1.0 - distance
        if similarity < self.threshold:
            logger.debug(f"[QACache] MISS (sim={similarity:.3f} < {self.threshold})")
            return None

        meta            = results["metadatas"][0][0]
        cached_question = results["documents"][0][0]

        # Version check — skip nếu entry từ pipeline version cũ
        entry_version = meta.get("cache_version", "")
        if entry_version != CACHE_VERSION:
            logger.debug(
                f"[QACache] VERSION MISMATCH — entry={entry_version} current={CACHE_VERSION}, skip"
            )
            return None

        answer        = meta.get("answer", "")
        key_facts_raw = meta.get("key_facts", "[]")

        try:
            key_facts = json.loads(key_facts_raw)
        except Exception:
            key_facts = []

        logger.info(
            f"[QACache] HIT (sim={similarity:.3f}) — '{cached_question[:60]}'"
        )
        return CacheHit(
            question_cached=cached_question,
            answer=answer,
            key_facts=key_facts,
            similarity=similarity,
            topic=meta.get("topic", ""),
            source_round=meta.get("source_round", ""),
        )

    def store(
        self,
        question: str,
        answer: str,
        key_facts: list[str] | None = None,
        topic: str = "",
        source_round: str = "user",
    ) -> str:
        """
        Lưu Q&A vào cache (idempotent — upsert theo question_id).
        Trả về question_id.
        """
        question_id = _question_id(question)
        emb         = self._embed(question)

        metadata: dict[str, Any] = {
            "answer":        answer[:2000],          # ChromaDB metadata có giới hạn
            "key_facts":     json.dumps(key_facts or [], ensure_ascii=False),
            "topic":         topic,
            "source_round":  source_round,
            "cache_version": CACHE_VERSION,
        }

        self._col.upsert(
            ids=[question_id],
            embeddings=[emb],
            documents=[question],
            metadatas=[metadata],
        )
        logger.debug(f"[QACache] Stored '{question[:60]}' (id={question_id[:8]})")
        return question_id

    def count(self) -> int:
        return self._col.count()

    def flush(self) -> int:
        """
        Xóa toàn bộ entries trong collection.
        Dùng khi cần reset cache sau khi nâng CACHE_VERSION.
        Trả về số entries đã xóa.
        """
        n = self._col.count()
        if n == 0:
            return 0
        self._client.delete_collection(QA_COLLECTION)
        self._col = self._client.get_or_create_collection(
            name=QA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"[QACache] Flushed {n} entries (version bump to {CACHE_VERSION})")
        return n

    def seed_from_benchmark(
        self,
        benchmark_path: str | Path,
        source_round: str = "",
        overwrite: bool = False,
    ) -> int:
        """
        Import Q&A pairs từ benchmark result JSON vào cache.

        benchmark_path: path đến data/eval/results/benchmark_round*.json
        overwrite:      nếu False, bỏ qua câu đã tồn tại trong cache

        Trả về số câu đã import.
        """
        path = Path(benchmark_path)
        if not path.exists():
            logger.error(f"[QACache] Benchmark file not found: {path}")
            return 0

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        results: list[dict] = data.get("results", [])
        if not results:
            logger.warning(f"[QACache] No results in {path.name}")
            return 0

        # Detect source_round từ tên file nếu không được cung cấp
        if not source_round:
            source_round = path.stem  # e.g. "benchmark_round12"

        imported = 0
        skipped  = 0

        for item in results:
            question  = item.get("question", "").strip()
            answer    = item.get("answer", "").strip()
            if not question or not answer:
                continue

            # Bỏ qua câu có lỗi (503, timeout...)
            if item.get("error"):
                continue

            qid = _question_id(question)

            # Kiểm tra đã tồn tại chưa
            if not overwrite:
                existing = self._col.get(ids=[qid], include=[])
                if existing["ids"]:
                    skipped += 1
                    continue

            # Extract key_facts từ tier4 details
            key_facts: list[str] = []
            tier4 = item.get("tier4", {})
            if isinstance(tier4, dict):
                matched = tier4.get("details", {}).get("matched", [])
                if isinstance(matched, list):
                    key_facts = [str(f) for f in matched]

            self.store(
                question=question,
                answer=answer,
                key_facts=key_facts,
                topic=item.get("topic", ""),
                source_round=source_round,
            )
            imported += 1

        logger.info(
            f"[QACache] Seeded {imported} entries from {path.name} "
            f"(skipped {skipped} existing)"
        )
        return imported

    # ── Internal ──────────────────────────────────────────────────────────────

    def _embed(self, text: str) -> list[float]:
        return self.embedder.model.encode(
            text, normalize_embeddings=True
        ).tolist()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _question_id(question: str) -> str:
    """SHA-256 của câu hỏi (normalized) làm unique ID."""
    normalized = question.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
