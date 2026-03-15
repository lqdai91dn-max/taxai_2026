"""
query_classifier.py — Phân loại intent của câu hỏi pháp luật thuế.

Intent types:
  DIRECT_LOOKUP   — hỏi thẳng một Điều/Khoản cụ thể → graph direct fetch
  VALIDITY_CHECK  — câu hỏi về hiệu lực, còn áp dụng không
  CROSS_DOC       — hỏi quan hệ giữa văn bản (NĐ hướng dẫn Luật nào...)
  CALCULATION     — câu hỏi yêu cầu tính toán (thuế bao nhiêu, với lương X...)
  GENERAL_QA      — câu hỏi thông thường → hybrid search + graph enrich

Classifier là rule-based (không cần model), latency ~0ms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class QueryIntent(str, Enum):
    DIRECT_LOOKUP  = "DIRECT_LOOKUP"
    VALIDITY_CHECK = "VALIDITY_CHECK"
    CROSS_DOC      = "CROSS_DOC"
    CALCULATION    = "CALCULATION"
    GENERAL_QA     = "GENERAL_QA"


@dataclass
class ClassifiedQuery:
    raw:             str
    intent:          QueryIntent
    # Extracted slots
    article_refs:    list[str]  = field(default_factory=list)   # ["Điều 9", "Khoản 2"]
    doc_refs:        list[str]  = field(default_factory=list)   # ["109/2025/QH15"]
    needs_validity:  bool       = False   # thêm validity filter
    needs_guidance:  bool       = True    # thêm GuidanceChunk lookup
    needs_cross_doc: bool       = False   # thêm impl/amends chain


# ── Patterns ──────────────────────────────────────────────────────────────

_ARTICLE_PAT = re.compile(
    r"(?:Điều|điều|Khoản|khoản|Điểm|điểm)\s+\d+[a-zđ]?",
    re.IGNORECASE | re.UNICODE,
)

_DOC_NUMBER_PAT = re.compile(
    r"\d{1,4}[/\-]\d{4}[/\-][A-ZĐ\-]{2,20}",
    re.IGNORECASE,
)

_VALIDITY_KEYWORDS = re.compile(
    r"còn hiệu lực|đã hết hiệu lực|hiện (nay|tại|đang)|"
    r"có còn áp dụng|còn áp dụng|thời điểm|khi nào có hiệu lực",
    re.IGNORECASE | re.UNICODE,
)

_CROSS_DOC_KEYWORDS = re.compile(
    r"văn bản nào|nghị định nào|thông tư nào|hướng dẫn (luật|nghị định)|"
    r"quy định tại|theo (luật|nghị định|thông tư)|"
    r"sửa đổi|thay thế|bãi bỏ|ban hành kèm",
    re.IGNORECASE | re.UNICODE,
)

_CALCULATION_KEYWORDS = re.compile(
    r"tính (thuế|thu nhập|khấu trừ|giảm trừ)|"
    r"bao nhiêu tiền|phải nộp|thuế phải trả|"
    r"lương\s+\d|thu nhập\s+\d|\d+\s*(triệu|nghìn|đồng)",
    re.IGNORECASE | re.UNICODE,
)

_DIRECT_ARTICLE_ONLY = re.compile(
    r"^(?:cho tôi biết\s+)?(?:Điều|điều)\s+\d+[a-zđ]?\s*"
    r"(?:(?:Luật|luật|NĐ|TT|QH)\s+[\d/\-A-Z]+)?[?.]?\s*$",
    re.IGNORECASE | re.UNICODE,
)


def classify(query: str) -> ClassifiedQuery:
    """Phân loại query → ClassifiedQuery với intent + extracted slots."""

    article_refs = _ARTICLE_PAT.findall(query)
    doc_refs     = _DOC_NUMBER_PAT.findall(query)

    needs_validity  = bool(_VALIDITY_KEYWORDS.search(query))
    needs_cross_doc = bool(_CROSS_DOC_KEYWORDS.search(query))

    # Priority order: DIRECT > VALIDITY > CROSS_DOC > CALCULATION > GENERAL

    # DIRECT_LOOKUP — "Điều 9" alone, or very short query referencing a specific article
    if _DIRECT_ARTICLE_ONLY.match(query.strip()) or (
        article_refs and len(query.split()) <= 8 and not needs_cross_doc
    ):
        intent = QueryIntent.DIRECT_LOOKUP

    elif needs_validity:
        intent = QueryIntent.VALIDITY_CHECK

    elif needs_cross_doc:
        intent = QueryIntent.CROSS_DOC

    elif _CALCULATION_KEYWORDS.search(query):
        intent = QueryIntent.CALCULATION

    else:
        intent = QueryIntent.GENERAL_QA

    return ClassifiedQuery(
        raw             = query,
        intent          = intent,
        article_refs    = article_refs,
        doc_refs        = doc_refs,
        needs_validity  = needs_validity or intent == QueryIntent.VALIDITY_CHECK,
        needs_guidance  = intent not in (QueryIntent.DIRECT_LOOKUP, QueryIntent.CROSS_DOC),
        needs_cross_doc = needs_cross_doc or intent == QueryIntent.CROSS_DOC,
    )
