"""
Single access point for law_validity.json.
All layers (prompt_builder, exception_router, qa_cache, reranker) read through here.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_LAW_VALIDITY_PATH = Path(__file__).parent.parent.parent / "data" / "law_validity.json"


@lru_cache(maxsize=1)
def _load_raw() -> dict:
    return json.loads(_LAW_VALIDITY_PATH.read_text(encoding="utf-8"))


def invalidate_cache() -> None:
    _load_raw.cache_clear()


def get_all_documents() -> dict[str, dict]:
    return _load_raw()["documents"]


def get_document(doc_id: str) -> Optional[dict]:
    return _load_raw()["documents"].get(doc_id)


def get_active_documents() -> dict[str, dict]:
    return {k: v for k, v in get_all_documents().items() if v.get("status") == "active"}


def get_superseded_documents() -> dict[str, dict]:
    return {k: v for k, v in get_all_documents().items() if v.get("status") == "superseded"}


def get_exception_docs() -> list[dict]:
    """Return flattened list of superseded docs that have allowed exception_use."""
    result = []
    for doc_id, doc in get_superseded_documents().items():
        ex = doc.get("exception_use")
        if ex and ex.get("allowed"):
            result.append({"doc_id": doc_id, **doc})
    return result


def get_status_changed_date(doc_id: str) -> Optional[str]:
    doc = get_document(doc_id)
    if doc is None:
        return None
    return doc.get("status_changed_date")


def get_not_in_database() -> dict[str, dict]:
    return _load_raw().get("not_in_database", {})


def get_doc_number(doc_id: str) -> Optional[str]:
    doc = get_document(doc_id)
    if doc is None:
        return None
    return doc.get("doc_number")
