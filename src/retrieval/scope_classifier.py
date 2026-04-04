"""
src/retrieval/scope_classifier.py
C1 — Query Legal Scope Classifier

Maps user query → legal scope(s) → used to boost/penalize ChromaDB results.

Design:
  - Rule-based keyword matching (V1) — zero latency, zero API cost
  - Multi-scope output (never force single scope)
  - Confidence gating: if confidence < 0.4 → ALL (no boost)
  - Intent coupling: leverages existing QueryIntent from query_classifier.py
  - Colloquial + legal keywords (user-friendly)
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import List, Dict

logger = logging.getLogger(__name__)

# ── Scope → doc_id mapping ────────────────────────────────────────────────

SCOPE_DOCS: Dict[str, List[str]] = {
    "PIT": [
        "109_2025_QH15",      # Luật Thuế TNCN 2025
        "110_2025_UBTVQH15",  # Nghị quyết giảm trừ gia cảnh
        "149_2025_QH15",      # Luật sửa đổi bổ sung Luật TNCN
        "108_2025_QH15",      # Luật 108
        "126_2020_NDCP",      # Nghị định quản lý thuế — thủ tục QT, ngoại lệ, khai nộp TNCN
        "111_2013_TTBTC",     # Thông tư hướng dẫn Luật TNCN — giảm trừ NPT, thời điểm tính thuế
        "92_2015_TTBTC",      # Thông tư sửa đổi TT111 — NPT hồi tố, giảm trừ gia cảnh mới
        "1296_CTNVT",         # Công văn hướng dẫn quyết toán TNCN
    ],
    "HKD": [
        "117_2025_NDCP",      # Nghị định HKD
        "68_2026_NDCP",       # Nghị định HKD 2026
        "152_2025_TTBTC",     # Thông tư hướng dẫn HKD
        "18_2026_TTBTC",      # Thông tư 18
        "So_Tay_HKD",         # Sổ tay HKD
    ],
    "TMDT": [
        "68_2026_NDCP",       # Nghị định HKD 2026 (có phần TMĐT)
        "117_2025_NDCP",      # Nghị định HKD (có phần TMĐT)
    ],
    "PENALTY": [
        "125_2020_NDCP",      # Xử phạt vi phạm hành chính thuế
        "373_2025_NDCP",      # Nghị định 373
        "310_2025_NDCP",      # Nghị định 310
    ],
    # ALL = fallback — no boost/penalty applied
}

# Docs không thuộc scope cụ thể nào → không boost, không penalty
_ALL_SCOPE_DOCS = {
    "198_2025_QH15",
    "20_2026_NDCP",
    "LQT_38_2019",
}

# ── Keyword signals ───────────────────────────────────────────────────────

_SCOPE_KEYWORDS: Dict[str, List[str]] = {
    "PIT": [
        # Legal terms
        "tncn", "thu nhập cá nhân", "giảm trừ gia cảnh", "người phụ thuộc",
        "cá nhân cư trú", "cá nhân không cư trú", "quyết toán thuế",
        "khấu trừ thuế tại nguồn", "biểu thuế lũy tiến", "thuế suất lũy tiến",
        "thu nhập từ tiền lương", "thu nhập từ tiền công", "thu nhập chịu thuế",
        "miễn thuế tncn", "hoàn thuế tncn", "ủy quyền quyết toán",
        # Colloquial
        "lương", "lương net", "lương gross", "thưởng", "thử việc",
        "hợp đồng thời vụ", "thu nhập từ lương", "đóng thuế thu nhập",
    ],
    "HKD": [
        # Legal terms
        "hộ kinh doanh", "thuế khoán", "nộp thuế theo khoán",
        "đăng ký kinh doanh hộ", "doanh thu từ kinh doanh",
        "phương pháp khoán", "hộ gia đình kinh doanh",
        # Profit-based tax method (Q4: "nộp thuế theo lợi nhuận / doanh thu trừ chi phí")
        "doanh thu trừ chi phí", "nộp thuế theo lợi nhuận",
        "tính thuế theo lợi nhuận", "phương pháp lợi nhuận",
        "kê khai theo doanh thu thực tế", "chế độ kế toán hộ",
        # Rental income under HKD (Q7: "cho thuê nhà 30 triệu/tháng theo luật mới")
        "cho thuê nhà", "cho thuê tài sản", "cho thuê mặt bằng",
        "cho thuê phòng", "cho thuê nguyên căn", "thu nhập từ cho thuê",
        "cho thuê bất động sản",
        # Colloquial — existing
        "mở tiệm", "mở quán", "tạp hóa", "chạy grab", "xe ôm công nghệ",
        "làm nail", "buôn bán nhỏ", "kinh doanh nhỏ lẻ", "bán lẻ",
        "tiệm", "quán", "cửa hàng nhỏ", "bán hàng", "hkd",
        # Additional business types common in HKD questions
        "tiệm vàng", "tiệm cà phê", "tiệm tóc", "sạp hàng",
        "cửa hàng", "ki ốt", "kinh doanh cá nhân",
    ],
    "TMDT": [
        # Platform names
        "shopee", "lazada", "tiktok", "tiki", "sendo", "facebook shop",
        "zalo shop", "grab food", "baemin", "gojek",
        # Terms
        "sàn thương mại điện tử", "bán online", "bán qua sàn",
        "thương mại điện tử", "tmđt", "kinh doanh online",
        "livestream bán hàng", "dropship", "affiliate",
        "sàn giao dịch trực tuyến",
    ],
    "PENALTY": [
        # Legal terms
        "xử phạt vi phạm", "vi phạm hành chính", "truy thu thuế",
        "cưỡng chế thuế", "mức phạt", "chế tài thuế",
        "hành vi trốn thuế", "khai sai thuế", "chậm nộp",
        # Colloquial
        "bị phạt", "phạt tiền", "nộp chậm", "quá hạn nộp",
        "trốn thuế", "phạt bao nhiêu", "mức phạt là",
    ],
}

# Pre-compiled patterns for speed
_COMPILED: Dict[str, re.Pattern] = {
    scope: re.compile(
        "|".join(re.escape(kw) for kw in keywords),
        re.IGNORECASE | re.UNICODE,
    )
    for scope, keywords in _SCOPE_KEYWORDS.items()
}

# ── Output dataclass ──────────────────────────────────────────────────────

@dataclass
class ScopeClassification:
    scopes:     List[str]          # e.g. ["HKD", "TMDT"] or ["ALL"]
    confidence: float              # 0.0–1.0
    hits:       Dict[str, int] = field(default_factory=dict)   # scope → hit count

    @property
    def is_all(self) -> bool:
        return self.scopes == ["ALL"]


# ── Classifier ────────────────────────────────────────────────────────────

def classify_scope(
    query: str,
    intent: str = "",          # QueryIntent value string, e.g. "CALCULATION"
    confidence_threshold: float = 0.4,
) -> ScopeClassification:
    """Classify query into legal scope(s).

    Args:
        query:                 raw user query
        intent:                QueryIntent.value from query_classifier.classify()
        confidence_threshold:  below this → return ALL (no boost)

    Returns:
        ScopeClassification(scopes, confidence, hits)
    """
    q = query.lower()

    # Count keyword hits per scope
    hits: Dict[str, int] = {}
    for scope, pattern in _COMPILED.items():
        matches = pattern.findall(q)
        if matches:
            hits[scope] = len(matches)

    total_hits = sum(hits.values())

    # Edge case: zero hits → ALL fallback
    if total_hits == 0:
        # Light intent coupling: CALCULATION usually means PIT or HKD
        if intent == "CALCULATION":
            return ScopeClassification(
                scopes=["PIT", "HKD"], confidence=0.45, hits={}
            )
        return ScopeClassification(scopes=["ALL"], confidence=0.0, hits={})

    max_hits   = max(hits.values())
    confidence = max_hits / total_hits  # confidence of the dominant scope

    if confidence < confidence_threshold:
        # Ambiguous signal → ALL (no boost/penalty)
        return ScopeClassification(scopes=["ALL"], confidence=confidence, hits=hits)

    # Return all scopes with at least 1 hit
    detected = [s for s, c in hits.items() if c > 0]

    # Intent coupling (lightweight)
    if intent == "CALCULATION" and not any(s in detected for s in ("PIT", "HKD")):
        detected.append("PIT")

    return ScopeClassification(scopes=detected, confidence=confidence, hits=hits)


# ── Boost/penalty application ─────────────────────────────────────────────

def apply_scope_boost(
    results: list,
    sc: ScopeClassification,
    boost: float = 1.3,
) -> list:
    """Apply soft scope boost (no penalty) based on scope classification.

    Rules:
      - ALL scope → no changes
      - doc in scope → score × boost
      - doc not in scope → score unchanged (safety net: high semantic similarity can still win)
      - TMDT + HKD co-occurrence → boost × 1.5 (stronger signal)

    No penalty applied: classifier errors should not remove correct documents from context.
    """
    if sc.is_all:
        return results

    # Build set of boosted doc_ids
    boosted_docs: set[str] = set()
    for scope in sc.scopes:
        boosted_docs.update(SCOPE_DOCS.get(scope, []))

    # TMDT + HKD co-occurrence: stronger boost
    tmdt_hkd_overlap = "TMDT" in sc.scopes and "HKD" in sc.scopes
    effective_boost = 1.5 if tmdt_hkd_overlap else boost

    n_boosted = 0

    for r in results:
        doc_id = r.get("metadata", {}).get("doc_id", "")
        if doc_id in boosted_docs:
            r["rrf_score"] = round(r.get("rrf_score", 0) * effective_boost, 6)
            n_boosted += 1

    results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)

    logger.debug(
        f"[ScopeBoost] scopes={sc.scopes} conf={sc.confidence:.2f} "
        f"boost×{effective_boost} on {n_boosted} docs (no penalty)"
    )

    return results
