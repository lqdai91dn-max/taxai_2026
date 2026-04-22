"""
src/retrieval/exception_router.py — Semantic Exception Router

Phát hiện câu hỏi thuộc exception_use của văn bản đã superseded
(ví dụ: TT111 cho phụ cấp ăn trưa, TT92 cho HKD tạm ngừng bệnh).

Thiết kế:
  - Penalty 3-tier: allow(1.0) / soft(0.35) / block(0.05) — không hard filter 0.0
  - Threshold calibrated từ test_should_match / test_should_not_match trong law_validity.json
  - Singleton, khởi tạo 1 lần lúc startup — reuse _MODEL_CACHE từ embedder.py
  - apply_penalty() là additive pass sau NodeMetadata reranking
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.utils.law_registry import get_exception_docs
from src.retrieval.embedder import _MODEL_CACHE, EMBEDDING_MODEL, FALLBACK_MODEL

logger = logging.getLogger(__name__)

_PENALTY: Dict[str, float] = {
    "allow": 1.00,
    "soft":  0.35,
    "block": 0.05,
}

_DEFAULT_UPPER = 0.72   # used when calibration fails (not enough test queries)
_DEFAULT_LOWER = 0.50


@dataclass
class ExceptionDecision:
    doc_id:    str
    tier:      str           # "allow" | "soft" | "block"
    score:     float
    threshold_upper: float
    threshold_lower: float


@dataclass
class _ExceptionEntry:
    doc_id:         str
    description_emb: Any          # np.ndarray
    threshold_upper: float
    threshold_lower: float


class ExceptionRouter:
    """
    Singleton router.
    Khởi tạo 1 lần lúc startup — gọi get_router() thay vì ExceptionRouter() trực tiếp.
    """

    def __init__(self) -> None:
        self._model = self._load_model()
        self._entries: List[_ExceptionEntry] = []
        self._build_index()

    # ── Model loading (reuse _MODEL_CACHE from embedder) ──────────────────────

    def _load_model(self):
        if EMBEDDING_MODEL in _MODEL_CACHE:
            logger.debug("[ExceptionRouter] Reusing cached embedding model")
            return _MODEL_CACHE[EMBEDDING_MODEL]
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(EMBEDDING_MODEL, local_files_only=True)
            _MODEL_CACHE[EMBEDDING_MODEL] = model
            return model
        except Exception:
            try:
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer(EMBEDDING_MODEL)
                _MODEL_CACHE[EMBEDDING_MODEL] = model
                return model
            except Exception as e:
                logger.warning(f"[ExceptionRouter] Failed to load {EMBEDDING_MODEL}: {e}, trying fallback")
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer(FALLBACK_MODEL)
                _MODEL_CACHE[FALLBACK_MODEL] = model
                return model

    # ── Index building ────────────────────────────────────────────────────────

    def _build_index(self) -> None:
        exception_docs = get_exception_docs()
        if not exception_docs:
            logger.info("[ExceptionRouter] No exception docs found")
            return

        for doc in exception_docs:
            doc_id = doc["doc_id"]
            ex = doc.get("exception_use", {})
            if not ex.get("allowed"):
                continue

            description = ex.get("semantic_intent_description", "")
            if not description:
                logger.warning(f"[ExceptionRouter] {doc_id}: no semantic_intent_description, skipping")
                continue

            desc_emb = self._encode([description])[0]
            upper, lower = self._calibrate(ex)

            self._entries.append(_ExceptionEntry(
                doc_id=doc_id,
                description_emb=desc_emb,
                threshold_upper=upper,
                threshold_lower=lower,
            ))
            logger.info(
                f"[ExceptionRouter] Indexed {doc_id}: upper={upper:.3f} lower={lower:.3f}"
            )

    def _calibrate(self, ex: dict) -> Tuple[float, float]:
        """
        Tính threshold từ test queries trong exception_use.
        upper = min(positive scores) * 0.95  → queries TRÊN upper được allow
        lower = max(negative scores) * 1.10  → queries DƯỚI lower được block
        """
        pos_queries = ex.get("test_should_match", [])
        neg_queries = ex.get("test_should_not_match", [])
        desc = ex.get("semantic_intent_description", "")
        if not desc or (not pos_queries and not neg_queries):
            return _DEFAULT_UPPER, _DEFAULT_LOWER

        desc_emb = self._encode([desc])[0]

        pos_scores: List[float] = []
        neg_scores: List[float] = []

        if pos_queries:
            pos_embs = self._encode(pos_queries)
            pos_scores = [float(self._cosine(desc_emb, e)) for e in pos_embs]

        if neg_queries:
            neg_embs = self._encode(neg_queries)
            neg_scores = [float(self._cosine(desc_emb, e)) for e in neg_embs]

        upper = min(pos_scores) * 0.95 if pos_scores else _DEFAULT_UPPER
        lower = max(neg_scores) * 1.10 if neg_scores else _DEFAULT_LOWER

        if upper <= lower:
            logger.warning(
                f"[ExceptionRouter] Calibration overlap (upper={upper:.3f} <= lower={lower:.3f}), using defaults"
            )
            return _DEFAULT_UPPER, _DEFAULT_LOWER

        return round(upper, 4), round(lower, 4)

    # ── Inference ─────────────────────────────────────────────────────────────

    def route(self, query: str, doc_id: str) -> Optional[ExceptionDecision]:
        """
        Tính penalty tier cho 1 (query, doc_id) pair.
        Returns None nếu doc_id không có trong exception index.
        """
        entry = next((e for e in self._entries if e.doc_id == doc_id), None)
        if entry is None:
            return None

        q_emb = self._encode([query])[0]
        score = float(self._cosine(entry.description_emb, q_emb))

        if score >= entry.threshold_upper:
            tier = "allow"
        elif score <= entry.threshold_lower:
            tier = "block"
        else:
            tier = "soft"

        return ExceptionDecision(
            doc_id=doc_id,
            tier=tier,
            score=round(score, 4),
            threshold_upper=entry.threshold_upper,
            threshold_lower=entry.threshold_lower,
        )

    def apply_penalty(
        self,
        results: List[Dict[str, Any]],
        query: str,
    ) -> List[Dict[str, Any]]:
        """
        Additive pass sau NodeMetadata reranking.
        Chỉ ảnh hưởng hits từ superseded docs có exception_use.
        """
        if not self._entries or not results:
            return results

        exception_doc_ids = {e.doc_id for e in self._entries}

        for hit in results:
            doc_id = hit.get("metadata", {}).get("doc_id", "")
            if doc_id not in exception_doc_ids:
                continue

            decision = self.route(query, doc_id)
            if decision is None:
                continue

            multiplier = _PENALTY[decision.tier]
            base_score = hit.get("final_score", hit.get("rrf_score", 0.0))
            hit["final_score"] = round(base_score * multiplier, 6)
            hit["exception_tier"]  = decision.tier
            hit["exception_score"] = decision.score

            logger.debug(
                "[ExceptionRouter] %s: tier=%s score=%.3f multiplier=%.2f → final=%.4f",
                doc_id, decision.tier, decision.score, multiplier, hit["final_score"],
            )

        results.sort(key=lambda h: h.get("final_score", h.get("rrf_score", 0)), reverse=True)
        return results

    # ── Low-level ─────────────────────────────────────────────────────────────

    def _encode(self, texts: List[str]):
        import numpy as np
        embs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return embs

    @staticmethod
    def _cosine(a, b) -> float:
        import numpy as np
        # embeddings already normalized → dot product = cosine similarity
        return float(np.dot(a, b))


# ── Singleton ─────────────────────────────────────────────────────────────────

_router_instance: Optional[ExceptionRouter] = None
_router_lock = threading.Lock()


def get_router() -> ExceptionRouter:
    global _router_instance
    if _router_instance is None:
        with _router_lock:
            if _router_instance is None:
                _router_instance = ExceptionRouter()
    return _router_instance
