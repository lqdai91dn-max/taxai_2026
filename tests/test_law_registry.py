"""
tests/test_law_registry.py — Smoke tests cho law_registry.py (single source of truth).

Chạy: pytest tests/test_law_registry.py -v
"""

import pytest
from src.utils.law_registry import (
    get_all_documents,
    get_document,
    get_active_documents,
    get_superseded_documents,
    get_exception_docs,
    get_status_changed_date,
    get_not_in_database,
    get_doc_number,
    invalidate_cache,
)


class TestLawRegistry:

    def test_get_all_documents_nonempty(self):
        docs = get_all_documents()
        assert len(docs) >= 5

    def test_get_document_exists(self):
        doc = get_document("109_2025_QH15")
        assert doc is not None
        assert doc["status"] == "active"

    def test_get_document_missing(self):
        assert get_document("FAKE_DOC_999") is None

    def test_active_documents_all_have_status_active(self):
        for doc_id, doc in get_active_documents().items():
            assert doc["status"] == "active", f"{doc_id} is not active"

    def test_superseded_documents_all_have_superseded_by(self):
        for doc_id, doc in get_superseded_documents().items():
            assert "superseded_by" in doc, f"{doc_id} missing superseded_by"
            assert doc["superseded_by"], f"{doc_id} has empty superseded_by"

    def test_exception_docs_returns_allowed_only(self):
        exceptions = get_exception_docs()
        for ex in exceptions:
            assert ex["exception_use"]["allowed"] is True

    def test_exception_docs_have_semantic_description(self):
        exceptions = get_exception_docs()
        assert len(exceptions) >= 2, "Expected at least 111 and 92 as exception docs"
        for ex in exceptions:
            desc = ex["exception_use"].get("semantic_intent_description", "")
            assert len(desc) > 20, f"{ex['doc_id']}: semantic_intent_description too short"

    def test_exception_docs_have_test_queries(self):
        for ex in get_exception_docs():
            pos = ex["exception_use"].get("test_should_match", [])
            neg = ex["exception_use"].get("test_should_not_match", [])
            assert len(pos) >= 3, f"{ex['doc_id']}: need >=3 test_should_match"
            assert len(neg) >= 3, f"{ex['doc_id']}: need >=3 test_should_not_match"

    def test_status_changed_date_exists(self):
        d = get_status_changed_date("109_2025_QH15")
        assert d is not None
        assert d == "2025-11-29"

    def test_status_changed_date_missing_doc(self):
        assert get_status_changed_date("FAKE_DOC_999") is None

    def test_get_doc_number(self):
        assert get_doc_number("68_2026_NDCP") == "68/2026/NĐ-CP"
        assert get_doc_number("109_2025_QH15") == "109/2025/QH15"
        assert get_doc_number("FAKE_999") is None

    def test_not_in_database(self):
        nid = get_not_in_database()
        assert "TT40_2021_TTBTC" in nid

    def test_invalidate_cache_does_not_crash(self):
        invalidate_cache()
        docs = get_all_documents()
        assert len(docs) > 0

    def test_all_docs_have_status_changed_date(self):
        """Schema v2: mọi doc phải có status_changed_date."""
        for doc_id, doc in get_all_documents().items():
            assert "status_changed_date" in doc, f"{doc_id} missing status_changed_date"
            assert doc["status_changed_date"], f"{doc_id} has empty status_changed_date"

    def test_all_docs_have_doc_number(self):
        """Schema v2: mọi doc phải có doc_number."""
        for doc_id, doc in get_all_documents().items():
            assert "doc_number" in doc, f"{doc_id} missing doc_number"
