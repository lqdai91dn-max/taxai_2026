"""
tests/test_exception_router.py — Unit tests cho ExceptionRouter.

Test logic routing + calibration WITHOUT loading the embedding model
(để test chạy nhanh không cần GPU/sentence-transformers).

Chạy: pytest tests/test_exception_router.py -v
"""

import pytest
from unittest.mock import MagicMock, patch
import numpy as np


def _make_router_with_mock_model():
    """Tạo ExceptionRouter với model thật thay bằng mock returns normalized embeddings."""
    from src.retrieval.exception_router import ExceptionRouter

    with patch("src.retrieval.exception_router._MODEL_CACHE", {}):
        router = ExceptionRouter.__new__(ExceptionRouter)
        router._entries = []

        # Mock model: encode returns unit vectors (normalized)
        mock_model = MagicMock()

        def mock_encode(texts, **kwargs):
            # Returns fixed vectors — dimension 4 for simplicity
            vecs = []
            for t in texts:
                if "ăn trưa" in t or "tiền ăn" in t or "phụ cấp bữa ăn" in t or "ăn giữa ca" in t:
                    vecs.append(np.array([1.0, 0.0, 0.0, 0.0]))  # pos match for 111
                elif "tạm ngừng" in t or "HKD bị ốm" in t or "đóng cửa tạm" in t or "chủ hộ ốm" in t:
                    vecs.append(np.array([0.0, 1.0, 0.0, 0.0]))  # pos match for 92
                elif "quyết toán TNCN" in t or "mức giảm trừ" in t or "đăng ký HKD" in t:
                    vecs.append(np.array([0.0, 0.0, 1.0, 0.0]))  # neg (unrelated)
                else:
                    vecs.append(np.array([0.5, 0.5, 0.0, 0.0]))  # ambiguous
            return np.array(vecs)

        mock_model.encode = mock_encode
        router._model = mock_model
        return router


class TestExceptionRouterDecision:

    def test_route_returns_none_for_unknown_doc(self):
        from src.retrieval.exception_router import ExceptionRouter, _ExceptionEntry
        router = ExceptionRouter.__new__(ExceptionRouter)
        router._entries = []
        router._model = MagicMock()
        result = router.route("bất kỳ câu hỏi nào", "FAKE_DOC")
        assert result is None

    def test_penalty_map_values(self):
        from src.retrieval.exception_router import _PENALTY
        assert _PENALTY["allow"] == 1.00
        assert _PENALTY["soft"] == 0.35
        assert _PENALTY["block"] == 0.05

    def test_apply_penalty_passthrough_non_exception_doc(self):
        from src.retrieval.exception_router import ExceptionRouter
        router = ExceptionRouter.__new__(ExceptionRouter)
        router._entries = []
        router._model = MagicMock()

        hits = [
            {"metadata": {"doc_id": "68_2026_NDCP"}, "final_score": 0.8},
            {"metadata": {"doc_id": "109_2025_QH15"}, "final_score": 0.75},
        ]
        result = router.apply_penalty(hits, "bất kỳ câu hỏi nào")
        # No exception entries → scores unchanged
        assert result[0]["final_score"] == 0.8
        assert result[1]["final_score"] == 0.75

    def test_apply_penalty_reduces_score_for_block_tier(self):
        from src.retrieval.exception_router import ExceptionRouter, _ExceptionEntry, _PENALTY
        import numpy as np

        router = ExceptionRouter.__new__(ExceptionRouter)

        # Mock entry for 111_2013_TTBTC
        desc_emb = np.array([1.0, 0.0, 0.0, 0.0])
        entry = _ExceptionEntry(
            doc_id="111_2013_TTBTC",
            description_emb=desc_emb,
            threshold_upper=0.72,
            threshold_lower=0.50,
        )
        router._entries = [entry]

        def mock_encode(texts, **kwargs):
            # Returns very low similarity vector (block tier)
            return np.array([[0.0, 0.0, 1.0, 0.0]] * len(texts))

        mock_model = MagicMock()
        mock_model.encode = mock_encode
        router._model = mock_model

        hits = [
            {"metadata": {"doc_id": "111_2013_TTBTC"}, "final_score": 0.8, "rrf_score": 0.8},
        ]
        result = router.apply_penalty(hits, "đăng ký HKD")
        assert result[0]["exception_tier"] == "block"
        assert abs(result[0]["final_score"] - 0.8 * _PENALTY["block"]) < 0.001

    def test_apply_penalty_allow_tier_no_change(self):
        from src.retrieval.exception_router import ExceptionRouter, _ExceptionEntry, _PENALTY
        import numpy as np

        router = ExceptionRouter.__new__(ExceptionRouter)

        desc_emb = np.array([1.0, 0.0, 0.0, 0.0])
        entry = _ExceptionEntry(
            doc_id="111_2013_TTBTC",
            description_emb=desc_emb,
            threshold_upper=0.72,
            threshold_lower=0.50,
        )
        router._entries = [entry]

        def mock_encode(texts, **kwargs):
            # Returns high similarity → allow tier
            return np.array([[1.0, 0.0, 0.0, 0.0]] * len(texts))

        mock_model = MagicMock()
        mock_model.encode = mock_encode
        router._model = mock_model

        hits = [
            {"metadata": {"doc_id": "111_2013_TTBTC"}, "final_score": 0.8, "rrf_score": 0.8},
        ]
        result = router.apply_penalty(hits, "phụ cấp bữa ăn miễn thuế tối đa")
        assert result[0]["exception_tier"] == "allow"
        assert abs(result[0]["final_score"] - 0.8 * _PENALTY["allow"]) < 0.001


class TestCalibration:

    def test_calibrate_overlap_uses_defaults(self):
        """Khi pos_scores < neg_scores (overlap) → fallback defaults."""
        from src.retrieval.exception_router import ExceptionRouter, _DEFAULT_UPPER, _DEFAULT_LOWER
        import numpy as np

        router = ExceptionRouter.__new__(ExceptionRouter)

        def mock_encode(texts, **kwargs):
            return np.array([[0.5, 0.5, 0.0, 0.0]] * len(texts))

        mock_model = MagicMock()
        mock_model.encode = mock_encode
        router._model = mock_model

        ex = {
            "semantic_intent_description": "câu hỏi về phụ cấp",
            "test_should_match": ["phụ cấp bữa ăn"],
            "test_should_not_match": ["đăng ký HKD"],
        }
        upper, lower = router._calibrate(ex)
        # pos and neg would have same score (0.5·0.5+0.5·0.5 = 0.5)
        # upper = 0.5*0.95 = 0.475, lower = 0.5*1.10 = 0.55 → overlap → defaults
        assert upper == _DEFAULT_UPPER
        assert lower == _DEFAULT_LOWER

    def test_calibrate_empty_queries_uses_defaults(self):
        from src.retrieval.exception_router import ExceptionRouter, _DEFAULT_UPPER, _DEFAULT_LOWER

        router = ExceptionRouter.__new__(ExceptionRouter)
        router._model = MagicMock()

        ex = {"semantic_intent_description": "test", "test_should_match": [], "test_should_not_match": []}
        upper, lower = router._calibrate(ex)
        assert upper == _DEFAULT_UPPER
        assert lower == _DEFAULT_LOWER
