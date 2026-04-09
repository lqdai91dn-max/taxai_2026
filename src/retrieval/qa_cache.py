"""
src/retrieval/qa_cache.py

Semantic Q&A Cache — lưu trữ câu hỏi + câu trả lời + key_facts.
Khi câu hỏi mới đến, tìm câu hỏi tương tự trong cache (similarity > threshold).
Nếu hit → trả về cached answer ngay (bypass full pipeline).
Nếu miss → chạy full pipeline → lưu kết quả vào cache.

Storage: ChromaDB collection riêng "taxai_qa_cache"
Embedding: cùng model vietnamese-sbert để so sánh được với legal doc vectors

Cache key design (P1):
    key = sha256(normalized_question + "|" + temporal_context)

    Trong đó temporal_context được extract từ query text:
        "năm 2025"         → "year:2025"
        "trước 01/07/2026" → "before:2026-07-01"
        "sau khi Luật 109" → "after:109"
        "hiện nay"/"nay"   → "year:<current_year>"
        (không có marker)  → "" (no temporal context)

    Lý do: cùng câu hỏi nhưng khác năm → khác context → khác answer.
    Ví dụ: "thuế suất TNCN năm 2025" ≠ "thuế suất TNCN năm 2026"
    → 2 cache entries riêng biệt dù embedding similarity cao.

    CACHE_VERSION: chỉ bump khi đổi embedding model hoặc thay đổi pipeline lớn.
    KHÔNG bump khi luật mới có hiệu lực (temporal_context tự xử lý).

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
import re
import time
from dataclasses import dataclass
from datetime import date
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
CACHE_TTL_SECONDS = 86_400   # 24h — user-facing cache entries expire sau 1 ngày
                             # Benchmark-seeded entries (source_round != "user") không expire


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

    def lookup_exact(
        self,
        question: str,
        top_doc_ids: list[str],
    ) -> CacheHit | None:
        """
        P1 — Exact hash lookup: hash(question + temporal + sorted(doc_ids)).

        Dùng sau preliminary retrieval để biết top_doc_ids.
        Không dùng embedding search — tra thẳng ChromaDB bằng ID → O(1).

        Return CacheHit nếu tồn tại entry với cùng hash, None nếu miss.
        """
        qid = _question_id(question, top_doc_ids)
        try:
            result = self._col.get(ids=[qid], include=["documents", "metadatas"])
        except Exception as e:
            logger.debug(f"[QACache] lookup_exact error: {e}")
            return None

        if not result["ids"]:
            logger.debug(f"[QACache] EXACT MISS (id={qid[:8]})")
            return None

        meta            = result["metadatas"][0]
        cached_question = result["documents"][0]

        entry_version = meta.get("cache_version", "")
        if entry_version != CACHE_VERSION:
            logger.debug(f"[QACache] EXACT VERSION MISMATCH — entry={entry_version}")
            return None

        # P3 — Soft TTL: chỉ expire user-generated entries (source_round == "user")
        # Benchmark-seeded entries không expire (luật không thay đổi theo ngày)
        source_round = str(meta.get("source_round", ""))
        created_at   = meta.get("created_at")
        if source_round == "user" and created_at is not None:
            age_seconds = time.time() - float(created_at)
            if age_seconds > CACHE_TTL_SECONDS:
                logger.info(
                    f"[QACache] EXACT EXPIRED (age={age_seconds/3600:.1f}h > TTL=24h) "
                    f"— id={qid[:8]}"
                )
                return None

        answer        = meta.get("answer", "")
        key_facts_raw = meta.get("key_facts", "[]")
        try:
            key_facts = json.loads(key_facts_raw)
        except Exception:
            key_facts = []

        logger.info(f"[QACache] EXACT HIT (doc_ids={top_doc_ids}) — '{cached_question[:60]}'")
        return CacheHit(
            question_cached=cached_question,
            answer=answer,
            key_facts=key_facts,
            similarity=1.0,   # exact match
            topic=meta.get("topic", ""),
            source_round=source_round,
        )

    def store(
        self,
        question: str,
        answer: str,
        key_facts: list[str] | None = None,
        topic: str = "",
        source_round: str = "user",
        top_doc_ids: list[str] | None = None,
    ) -> str:
        """
        Lưu Q&A vào cache (idempotent — upsert theo question_id).

        Nếu top_doc_ids được cung cấp, hash key sẽ bao gồm doc_ids
        → entries có cùng câu hỏi nhưng khác corpus sẽ được lưu riêng.

        Trả về question_id.
        """
        question_id = _question_id(question, top_doc_ids)
        emb         = self._embed(question)

        metadata: dict[str, Any] = {
            "answer":        answer[:2000],          # ChromaDB metadata có giới hạn
            "key_facts":     json.dumps(key_facts or [], ensure_ascii=False),
            "topic":         topic,
            "source_round":  source_round,
            "cache_version": CACHE_VERSION,
            "top_doc_ids":   json.dumps(sorted(top_doc_ids) if top_doc_ids else []),
            "created_at":    time.time(),            # P3 — soft TTL 24h
        }

        self._col.upsert(
            ids=[question_id],
            embeddings=[emb],
            documents=[question],
            metadatas=[metadata],
        )
        logger.debug(
            f"[QACache] Stored '{question[:60]}' (id={question_id[:8]}, "
            f"docs={top_doc_ids or []})"
        )
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

def _parse_temporal_context(question: str) -> str:
    """
    P1 — Trích xuất temporal context từ câu hỏi để đưa vào cache key.

    Mục đích: phân biệt cache entry cho cùng câu hỏi nhưng khác kỳ thời gian.
    Ví dụ: "thuế suất năm 2025" vs "thuế suất năm 2026" → 2 entries riêng biệt.

    Returns:
        Chuỗi ngắn đại diện temporal context (empty string nếu không phát hiện).

    Ưu tiên match (theo thứ tự):
        1. Ngày cụ thể: "01/07/2026", "trước 2026-07-01"
        2. Năm cụ thể: "năm 2025", "2025", "trước 2026"
        3. Luật theo số: "Luật 109", "sau khi 109"
        4. "hiện nay" / "hiện tại" / "nay" → năm hiện tại
        5. Không có marker → empty
    """
    q = question.lower()

    # 1. Ngày cụ thể "dd/mm/yyyy" hoặc "before:date"
    _date_pat = re.compile(r'(\d{1,2}/\d{1,2}/(\d{4}))')
    m = _date_pat.search(q)
    if m:
        return f"date:{m.group(2)}"   # dùng năm đủ làm key

    # 2. Năm 4 chữ số (2020-2030)
    _year_pat = re.compile(r'\b(20(?:2[0-9]|30))\b')
    years = _year_pat.findall(q)
    if years:
        # Lấy năm nhỏ nhất (thường là năm đang hỏi)
        return f"year:{min(years)}"

    # 3. Tham chiếu luật theo số (Luật 109, NĐ68...)
    _law_pat = re.compile(r'(?:luật|nghị định|thông tư|nd|tt)\s*(\d{2,3})')
    m = _law_pat.search(q)
    if m:
        return f"law:{m.group(1)}"

    # 4. "hiện nay" / "hiện tại" / "nay" / "bây giờ" → năm hiện tại
    _present_words = ("hiện nay", "hiện tại", "bây giờ", "năm nay", "ngay bây giờ")
    if any(w in q for w in _present_words):
        return f"year:{date.today().year}"

    return ""   # không có temporal marker


def _question_id(question: str, top_doc_ids: list[str] | None = None) -> str:
    """
    SHA-256 của (câu hỏi normalized + temporal context + sorted doc_ids) làm unique ID.

    P1 update: Thêm top_doc_ids vào hash để phân biệt cache entry theo corpus state.
    - Cùng câu hỏi + cùng doc_ids (luật chưa đổi)  → cùng hash → cache hit
    - Cùng câu hỏi + khác doc_ids (luật mới hiệu lực) → hash khác → cache miss → re-generate

    Ý nghĩa thực tiễn:
        Khi Luật 109/2025/QH15 có hiệu lực (01/07/2026), câu hỏi về TNCN sẽ
        retrieve 109_2025_QH15 thay vì 111_2013_TTBTC → hash khác → answer mới.
        Không cần CACHE_VERSION bump (anti-pattern vì invalidate toàn bộ cache).

    Args:
        question:    Câu hỏi gốc.
        top_doc_ids: List doc_ids từ preliminary retrieval (top_k=3).
                     None → chỉ dùng question + temporal (backward compat).
    """
    normalized  = question.strip().lower()
    temporal    = _parse_temporal_context(question)
    docs_key    = "|".join(sorted(top_doc_ids)) if top_doc_ids else ""
    cache_input = normalized + "|" + temporal + "|" + docs_key
    return hashlib.sha256(cache_input.encode("utf-8")).hexdigest()[:32]
