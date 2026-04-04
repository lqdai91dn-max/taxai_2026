"""
src/agent/router.py — Stage 1: Query Router

Phân loại câu hỏi và lập kế hoạch retrieval trước khi gọi bất kỳ LLM nào.

Hybrid approach:
  Path A — Rule-based (80% queries, zero latency, zero cost)
  Path B — LLM structured output fallback (complex/ambiguous, ~300ms)

FM addressed:
  FM02a  OOD detection          → QueryType.OOD, short-circuit pipeline
  FM05   STRICT param detection → clarify_needed nếu thiếu số bắt buộc
  FM06   Phrase scope safety net → force thêm scope bị Router miss
  FM07   LLM fallback           → rule-based Router khi Gemini fail
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import List, Optional, Tuple

from src.agent.schemas import (
    QueryType,
    RetrievalQuery,
    RouterOutput,
)
from src.retrieval.scope_classifier import SCOPE_DOCS, classify_scope

logger = logging.getLogger(__name__)


# ── OOD detection ─────────────────────────────────────────────────────────────
# Câu hỏi có ÍT NHẤT MỘT trong các từ này → không phải OOD

_TAX_ANCHOR_PATTERN = re.compile(
    r"thuế|tncn|hkd|hộ kinh doanh|kê khai|quyết toán|hoàn thuế|miễn thuế"
    r"|hóa đơn|nghị định|thông tư|luật|điều\s+\d|khoản\s+\d"
    r"|doanh thu|thu nhập|giảm trừ|người phụ thuộc|lương|tiền công"
    r"|phạt|vi phạm|truy thu|cưỡng chế|sàn tmđt|shopee|lazada|tiktok"
    r"|gtgt|vat|bhxh|bhyt|bhtn",
    re.IGNORECASE | re.UNICODE,
)


def _is_ood(query: str) -> bool:
    """True nếu câu hỏi không liên quan đến thuế Việt Nam."""
    return not bool(_TAX_ANCHOR_PATTERN.search(query))


# ── QueryType detection ───────────────────────────────────────────────────────

_CALCULATION_RE = re.compile(
    r"tính\s+thuế|phải\s+nộp\s+bao\s+nhiêu|bao\s+nhiêu\s+tiền\s+thuế"
    r"|thuế\s+phải\s+đóng|số\s+tiền\s+thuế|tổng\s+thuế|tính\s+ra\s+bao\s+nhiêu"
    r"|thuế\s+bao\s+nhiêu|đóng\s+thuế\s+bao\s+nhiêu|nộp\s+bao\s+nhiêu",
    re.IGNORECASE | re.UNICODE,
)

_LEGAL_LOOKUP_RE = re.compile(
    r"[Ðđ]iều\s+\d+|khoản\s+\d+\s+[Ðđ]iều|quy\s+định\s+tại|căn\s+cứ\s+"
    r"|nội\s+dung\s+[Ðđ]iều|toàn\s+văn|xem\s+[Ðđ]iều|theo\s+[Ðđ]iều",
    re.IGNORECASE | re.UNICODE,
)

_PROCEDURE_RE = re.compile(
    r"thủ\s+tục|hồ\s+sơ|mẫu\s+\d|nộp\s+ở\s+đâu|nộp\s+như\s+thế\s+nào"
    r"|hạn\s+chót|deadline|kê\s+khai|quyết\s+toán|đăng\s+ký|thời\s+hạn\s+nộp"
    r"|nộp\s+tờ\s+khai|tờ\s+khai|mẫu\s+biểu",
    re.IGNORECASE | re.UNICODE,
)

_ELIGIBILITY_RE = re.compile(
    r"có\s+phải\s+|có\s+được\s+|có\s+bị\s+|có\s+cần\s+|phải\s+không"
    r"|có\s+chịu\s+thuế|được\s+miễn\s+thuế\s+không|có\s+được\s+trừ"
    r"|có\s+thuộc\s+|có\s+áp\s+dụng|được\s+khấu\s+trừ\s+không"
    r"|bắt\s+buộc\s+không|cần\s+thiết\s+không",
    re.IGNORECASE | re.UNICODE,
)


_METHOD_QUESTION_RE = re.compile(
    r"kiểu\s+gì|như\s+thế\s+nào|theo\s+cách\s+nào|phương\s+pháp\s+gì"
    r"|tính\s+như\s+thế\s+nào|xác\s+định\s+như\s+thế\s+nào",
    re.IGNORECASE | re.UNICODE,
)


def _detect_query_type(query: str) -> Tuple[QueryType, float]:
    """
    Phát hiện QueryType từ query bằng regex.
    Trả về (type, confidence): confidence = 1.0 nếu rule rõ ràng, 0.6 nếu yếu.
    """
    q = query.lower()

    # ELIGIBILITY có ưu tiên cao (bắt trước CALCULATION)
    # Tránh: "có phải tính thuế bao nhiêu" → ELIGIBILITY chứ không phải CALCULATION
    if _ELIGIBILITY_RE.search(q):
        return QueryType.ELIGIBILITY, 1.0

    if _CALCULATION_RE.search(q) and re.search(r"\d", q):
        # Có từ khóa tính toán VÀ có số → CALCULATION rõ ràng
        # Nhưng nếu hỏi về phương pháp ("tính như thế nào") → GENERAL
        if _METHOD_QUESTION_RE.search(q):
            return QueryType.GENERAL, 0.8
        return QueryType.CALCULATION, 1.0

    if _CALCULATION_RE.search(q):
        # Có từ khóa tính toán nhưng KHÔNG có số:
        # - Hỏi về phương pháp ("tính thuế kiểu gì") → GENERAL
        # - Còn lại → GENERAL với confidence thấp (không trigger clarify)
        return QueryType.GENERAL, 0.6

    if _LEGAL_LOOKUP_RE.search(q):
        return QueryType.LEGAL_LOOKUP, 1.0

    if _PROCEDURE_RE.search(q):
        return QueryType.PROCEDURE, 1.0

    return QueryType.GENERAL, 0.8


# ── FM06: Phrase-based scope safety net ──────────────────────────────────────
# Phrase matching — không dùng single keyword để tránh false positive
# Ví dụ: "phạt" trong "công ty phạt em" ≠ PENALTY scope

_PENALTY_FORCE_PHRASES = [
    "bị phạt thuế", "phạt vi phạm hành chính thuế",
    "truy thu thuế", "cưỡng chế thuế", "chậm nộp thuế",
    "vi phạm khai thuế", "xử phạt khai sai", "tiền chậm nộp thuế",
    "phạt vi phạm hành chính", "xử phạt vi phạm hành chính",
    "mức phạt thuế", "bị truy thu", "bị xử phạt thuế",
    "khai sai thuế", "trốn thuế",
]

_PIT_FORCE_PHRASES = [
    "thuế thu nhập cá nhân", "thuế tncn",
    "khấu trừ tncn", "người phụ thuộc",
    "quyết toán thuế tncn", "giảm trừ gia cảnh",
    "biểu thuế lũy tiến", "thu nhập từ tiền lương",
    "thu nhập từ tiền công",
]

_HKD_FORCE_PHRASES = [
    "hộ kinh doanh", "thuế khoán", "nộp thuế theo khoán",
    "phương pháp khoán", "phương pháp doanh thu",
]

_PENALTY_NEG_CONTEXT = re.compile(
    r"phạt\b",
    re.IGNORECASE
)
_TAX_CONTEXT = re.compile(
    r"thuế|cơ quan thuế|biên bản|vi phạm hành chính",
    re.IGNORECASE
)


def _apply_scope_safety_net(query: str, scopes: List[str]) -> List[str]:
    """
    FM06: Bổ sung scope bị Router miss dựa trên phrase matching.
    Không dùng single keyword — phải là phrase có ngữ cảnh.
    """
    q = query.lower()
    scopes = list(scopes)

    # PENALTY: chỉ force nếu có phrase rõ ràng
    # Negative check: nếu "phạt" không đi kèm ngữ cảnh thuế → bỏ qua
    if "PENALTY" not in scopes:
        for phrase in _PENALTY_FORCE_PHRASES:
            if phrase in q:
                scopes.append("PENALTY")
                logger.debug(f"[FM06] Forced PENALTY scope via phrase: '{phrase}'")
                break
        else:
            # Single "phạt" chỉ trigger nếu đi cùng ngữ cảnh thuế
            if "phạt" in q and _TAX_CONTEXT.search(q):
                scopes.append("PENALTY")
                logger.debug("[FM06] Forced PENALTY scope via 'phạt' + tax context")

    if "PIT" not in scopes:
        for phrase in _PIT_FORCE_PHRASES:
            if phrase in q:
                scopes.append("PIT")
                logger.debug(f"[FM06] Forced PIT scope via phrase: '{phrase}'")
                break

    if "HKD" not in scopes:
        for phrase in _HKD_FORCE_PHRASES:
            if phrase in q:
                scopes.append("HKD")
                logger.debug(f"[FM06] Forced HKD scope via phrase: '{phrase}'")
                break

    return scopes


# ── STRICT param detection (FM05) ─────────────────────────────────────────────
# STRICT = không được assume, phải clarify nếu thiếu
# FLEXIBLE = có thể default + ghi chú (không block answer)

_HAS_NUMBER_RE   = re.compile(r"\d[\d\s,\.]*(?:triệu|tỷ|nghìn|tr|k\b|đồng|vnd)?", re.IGNORECASE)
_HAS_CATEGORY_RE = re.compile(
    r"hàng\s+hóa|dịch\s+vụ|sản\s+xuất|vận\s+tải|bất\s+động\s+sản|cho\s+thuê|kinh\s+doanh\s+khác"
    r"|goods|services|manufacturing|real_estate",
    re.IGNORECASE
)
_HAS_INCOME_RE = re.compile(
    r"lương|thu\s+nhập|tiền\s+công|gross|net|\d[\d\s,\.]*(?:triệu|tr|k\b)",
    re.IGNORECASE
)


def _check_strict_params(query: str, scopes: List[str], calc_tool: Optional[str]) -> Tuple[bool, str]:
    """
    Kiểm tra STRICT params cho CALCULATION query.
    Returns (clarify_needed, clarify_question).
    """
    if not calc_tool:
        return False, ""

    q = query.lower()

    if calc_tool in ("calculate_tax_hkd", "calculate_tax_hkd_profit"):
        missing = []
        if not _HAS_NUMBER_RE.search(q):
            missing.append("doanh thu năm")
        if not _HAS_CATEGORY_RE.search(q):
            missing.append("ngành kinh doanh (hàng hóa / dịch vụ / sản xuất / cho thuê BĐS)")
        if missing:
            return True, f"Để tính thuế, bạn cho biết thêm: {' và '.join(missing)}?"

    elif calc_tool in ("calculate_tncn_progressive", "calculate_deduction"):
        if not _HAS_INCOME_RE.search(q):
            return True, "Để tính thuế TNCN, bạn cho biết thu nhập tháng (hoặc năm) là bao nhiêu?"

    return False, ""


# ── Calc tool selection ───────────────────────────────────────────────────────

def _select_calc_tool(query: str, scopes: List[str]) -> Optional[str]:
    """Chọn calculator tool phù hợp dựa trên query và scopes."""
    q = query.lower()

    if "HKD" in scopes or "TMDT" in scopes:
        if any(kw in q for kw in [
            # Formal
            "lợi nhuận", "chi phí", "profit", "doanh thu trừ",
            # Colloquial — tiếng lóng
            "phần lãi", "lãi ròng", "lãi gộp", "lãi thực", "tiền lãi",
            "trừ vốn", "trừ chi phí", "sau khi trừ", "còn lại",
            "thực lãi", "tính trên lãi", "lời", "lãi",
        ]):
            return "calculate_tax_hkd_profit"
        if any(kw in q for kw in ["nghĩa vụ", "có phải kê khai", "có cần hóa đơn", "kỳ kê khai"]):
            return "evaluate_tax_obligation"
        return "calculate_tax_hkd"

    if "PIT" in scopes:
        if any(kw in q for kw in ["giảm trừ", "người phụ thuộc", "khấu trừ"]):
            return "calculate_deduction"
        return "calculate_tncn_progressive"

    return None


# ── Retrieval query generation ────────────────────────────────────────────────

# Primary doc per scope — doc được targeted đầu tiên khi có scope rõ ràng
_PRIMARY_DOC: dict = {
    "HKD":     "68_2026_NDCP",
    "PIT":     "109_2025_QH15",
    "TMDT":    "68_2026_NDCP",
    "PENALTY": "373_2025_NDCP",
}


def _build_retrieval_queries(
    query: str,
    query_type: QueryType,
    scopes: List[str],
) -> List[RetrievalQuery]:
    """
    Tạo danh sách RetrievalQuery từ query gốc.

    Rules:
    - Tối đa 3 queries để tránh latency spike
    - Query 1: global safety net (doc_filter=None, top_k=3)
    - Query 2: targeted primary scope doc (top_k=5) — đảm bảo exposure
    - Query 3: targeted secondary scope doc HOẶC exception/procedure query
    - Multi-scope: cả 2 scope đều có targeted query → tránh global dominance
    """
    queries: List[RetrievalQuery] = []
    q = query.strip()

    # Lọc ra các scope có primary doc, loại ALL
    # Khi hit tie: HKD/TMDT ưu tiên trước PIT (68_2026_NDCP là doc chính của đa số câu hỏi)
    _SCOPE_PRIORITY = {"HKD": 0, "TMDT": 1, "PIT": 2, "PENALTY": 3}
    targetable = sorted(
        [s for s in scopes if s in _PRIMARY_DOC],
        key=lambda s: _SCOPE_PRIORITY.get(s, 9),
    )

    # Query ordering strategy:
    # - Pure PIT (không có HKD): đưa 109_2025_QH15 lên Q1 (targeted) vì corpus có
    #   doc cũ (111_2013_TTBTC, 92_2015_TTBTC) dễ dominate global search → sai doc
    # - HKD primary hoặc multi-scope khác: Q1=global safety net (default)
    _pit_only = targetable == ["PIT"]
    if _pit_only:
        # Q1: targeted 109 (PIT primary)
        queries.append(RetrievalQuery(query=q, doc_filter="109_2025_QH15", top_k=5))
        # Q2: global safety net
        queries.append(RetrievalQuery(query=q, doc_filter=None, top_k=3))
    else:
        # Query 1: global safety net (top_k nhỏ hơn để nhường slot cho targeted)
        queries.append(RetrievalQuery(query=q, doc_filter=None, top_k=3))

        # Query 2: primary scope targeted
        if targetable:
            primary_doc = _PRIMARY_DOC[targetable[0]]
            queries.append(RetrievalQuery(query=q, doc_filter=primary_doc, top_k=5))

    # Query 3: secondary scope targeted, hoặc ELIGIBILITY exception, hoặc PROCEDURE
    q_lower = q.lower()
    _TMDT_SOCIAL_KWS = ["tiktok", "youtube", "facebook", "instagram", "nền tảng số"]

    if len(targetable) >= 2:
        primary_scope  = targetable[0]
        secondary_scope = targetable[1]
        secondary_doc  = _PRIMARY_DOC[secondary_scope]

        # Fix: HKD+PIT với context bán hàng sàn/mạng xã hội → dùng 117_2025_NDCP
        # (NĐ 117 chứa quy định khấu trừ thuế sàn TMĐT, không nằm trong 68 hay 109)
        if (primary_scope in ("HKD", "TMDT") and secondary_scope == "PIT"
                and any(kw in q_lower for kw in _TMDT_SOCIAL_KWS)):
            queries.append(RetrievalQuery(query=q, doc_filter="117_2025_NDCP", top_k=3))

        # Fix: HKD+PIT với câu hỏi về thuế suất TNCN theo ngành nghề HKD
        # → 109_2025_QH15 (Luật TNCN lũy tiến cá nhân) KHÔNG phải nguồn đúng
        # → Nguồn đúng là 68_2026_NDCP Điều 4 Khoản 3 (flat rate theo ngành)
        # → KHÔNG thêm secondary 109 cho loại query này
        elif (primary_scope == "HKD" and secondary_scope == "PIT"
                and any(kw in q_lower for kw in [
                    "thuế suất tncn", "tỷ lệ tncn", "% tncn",
                    "tncn bao nhiêu", "tỷ lệ % tncn", "bao nhiêu phần trăm",
                    "tính thuế tncn", "thuế tncn tiệm", "thuế tncn hộ",
                ])):
            # Skip: không thêm 109 — 68 (đã ở Q2) là đủ cho HKD TNCN rate
            pass

        elif secondary_doc != _PRIMARY_DOC[primary_scope]:
            queries.append(RetrievalQuery(query=q, doc_filter=secondary_doc, top_k=3))

    elif "TMDT" in scopes:
        # TMDT đơn độc: luôn thêm 117_2025_NDCP (doc khấu trừ/tỷ lệ % sàn TMĐT)
        # 68_2026_NDCP đã được Q2 bao phủ; 117 có Điều 5 tỷ lệ % mà 68 không có
        queries.append(RetrievalQuery(query=q, doc_filter="117_2025_NDCP", top_k=3))

    elif query_type == QueryType.ELIGIBILITY and "PIT" in scopes:
        # PIT ELIGIBILITY về quyết toán/kê khai → ưu tiên 126_2020_NDCP
        # (chứa trách nhiệm kê khai của tổ chức trả thu nhập "không phân biệt")
        if any(kw in q_lower for kw in ["quyết toán", "kê khai", "nộp thêm", "khấu trừ thuế"]):
            queries.append(RetrievalQuery(query=q, doc_filter="126_2020_NDCP", top_k=3))
        else:
            exception_query = _build_exception_query(q, scopes)
            queries.append(RetrievalQuery(query=exception_query, doc_filter=None, top_k=3))

    elif query_type == QueryType.ELIGIBILITY:
        exception_query = _build_exception_query(q, scopes)
        queries.append(RetrievalQuery(
            query=exception_query, doc_filter=None, top_k=3,
        ))
    elif query_type == QueryType.PROCEDURE and "PIT" in scopes:
        queries.append(RetrievalQuery(
            query=q, doc_filter="126_2020_NDCP", top_k=3,
        ))

    elif "HKD" in scopes and any(kw in q_lower for kw in [
        "sổ sách", "sổ kế toán", "loại sổ", "mẫu sổ", "sổ doanh thu",
    ]):
        # Q10-type: HKD sổ sách kế toán → TT152/2025 là nguồn chính về chế độ kế toán HKD
        queries.append(RetrievalQuery(query=q, doc_filter="152_2025_TTBTC", top_k=3))

    elif "PIT" in scopes and "giảm trừ gia cảnh" in q_lower and any(
        kw in q_lower for kw in ["bao nhiêu", "mức", "2026"]
    ):
        # Q11-type: giảm trừ gia cảnh 2026 → NQ 110/2025/UBTVQH15 điều chỉnh mức
        queries.append(RetrievalQuery(query=q, doc_filter="110_2025_UBTVQH15", top_k=3))

    return queries[:3]  # Hard cap: tối đa 3 queries


def _build_exception_query(query: str, scopes: List[str]) -> str:
    """
    Tạo query tìm ngoại lệ / miễn trừ cho ELIGIBILITY questions.
    FM mandatory pairs: tìm cả "có" và "không/miễn/ngoại lệ".
    """
    # Extract core subject từ query
    q = query.lower()

    # Xác định topic chính
    if any(kw in q for kw in ["chịu thuế", "phải đóng thuế", "phải nộp thuế"]):
        subject = re.sub(
            r"có\s+phải|có\s+chịu|phải\s+không|không\?*$", "",
            q, flags=re.IGNORECASE
        ).strip()
        return f"{subject} miễn thuế không phải nộp ngoại lệ trừ trường hợp"

    if any(kw in q for kw in ["phải kê khai", "cần kê khai", "có khai không"]):
        return f"{query} không phải kê khai miễn kê khai ngoại lệ điều kiện"

    if any(kw in q for kw in ["bị phạt", "có phạt không", "phạt bao nhiêu"]):
        return f"{query} không bị xử phạt tự nguyện khai bổ sung miễn phạt"

    if any(kw in q for kw in ["được trừ", "được khấu trừ", "chi phí được trừ"]):
        return f"{query} không được trừ điều kiện hạn chế loại trừ"

    # Generic exception query
    return f"{query} ngoại lệ miễn trừ không phải không được trừ trường hợp"


# ── Rule-based Router (Path A) ────────────────────────────────────────────────

def route_rule_based(query: str) -> RouterOutput:
    """
    Path A: Rule-based routing — zero latency, zero cost.
    Dùng làm primary router và FM07 fallback khi LLM fail.
    """
    # FM02a: OOD check
    if _is_ood(query):
        return RouterOutput(
            query_type=QueryType.OOD,
            scopes=[],
            retrieval_queries=[],
            reasoning="OOD: không tìm thấy từ khóa thuế Việt Nam",
        )

    # QueryType detection
    query_type, type_confidence = _detect_query_type(query)

    # Scope detection — reuse scope_classifier
    sc = classify_scope(query)
    scopes: List[str] = [] if sc.is_all else sc.scopes

    # FM06: Phrase safety net — bổ sung scope bị miss
    scopes = _apply_scope_safety_net(query, scopes)

    # Dedup và limit scopes
    seen: dict = {}
    scopes = [seen.setdefault(s, s) for s in scopes if s not in seen][:2]

    # Calculator tool selection
    calc_tool: Optional[str] = None
    if query_type == QueryType.CALCULATION:
        calc_tool = _select_calc_tool(query, scopes)

    # STRICT param check (FM05)
    clarify_needed, clarify_question = _check_strict_params(query, scopes, calc_tool)

    # Mandatory pairs cho ELIGIBILITY
    mandatory_pairs = query_type == QueryType.ELIGIBILITY

    # Retrieval query generation
    retrieval_queries = _build_retrieval_queries(query, query_type, scopes)

    return RouterOutput(
        query_type         = query_type,
        scopes             = scopes,
        retrieval_queries  = retrieval_queries,
        mandatory_pairs    = mandatory_pairs,
        clarify_needed     = clarify_needed,
        clarify_question   = clarify_question if clarify_needed else None,
        calc_tool          = calc_tool,
        reasoning          = (
            f"rule_based | type={query_type.value}({type_confidence:.1f}) "
            f"| scopes={scopes} | calc={calc_tool}"
        ),
    )


# ── LLM-based Router (Path B) ─────────────────────────────────────────────────

_ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "query_type": {
            "type": "string",
            "enum": ["CALCULATION", "LEGAL_LOOKUP", "PROCEDURE",
                     "ELIGIBILITY", "GENERAL", "AMBIGUOUS", "OOD"],
        },
        "scopes": {
            "type": "array",
            "items": {"type": "string", "enum": ["PIT", "HKD", "TMDT", "PENALTY", "ALL"]},
            "maxItems": 2,
        },
        "calc_tool": {
            "type": ["string", "null"],
            "enum": [
                "calculate_tax_hkd", "calculate_tax_hkd_profit",
                "calculate_tncn_progressive", "calculate_deduction",
                "evaluate_tax_obligation", None,
            ],
        },
        "clarify_needed": {"type": "boolean"},
        "clarify_question": {"type": ["string", "null"]},
        "ambiguous_interpretations": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2,
        },
        "reasoning": {"type": "string"},
    },
    "required": ["query_type", "scopes", "reasoning"],
}

_ROUTER_PROMPT = """\
Bạn là Router của TaxAI — hệ thống tư vấn thuế Việt Nam.
Phân loại câu hỏi sau và trả về JSON theo schema được cung cấp.

Câu hỏi: {query}

Hướng dẫn phân loại:
- OOD: không liên quan đến thuế Việt Nam
- CALCULATION: cần tính số tiền thuế cụ thể (có hoặc thiếu số liệu)
- ELIGIBILITY: hỏi "có phải/được/bị/cần... không?" → cần tìm cả quy định lẫn ngoại lệ
- PROCEDURE: hỏi thủ tục, hồ sơ, mẫu biểu, thời hạn, quyết toán
- LEGAL_LOOKUP: hỏi nội dung Điều/Khoản cụ thể
- AMBIGUOUS: có thể hiểu theo 2+ cách dẫn đến nghĩa vụ thuế khác nhau
- GENERAL: hỏi quy định chung, giải thích cơ chế

Scopes: PIT=Thuế TNCN, HKD=Hộ kinh doanh, TMDT=Thương mại điện tử, PENALTY=Xử phạt vi phạm thuế
Tối đa 2 scopes. Để rỗng [] nếu không rõ.

Trả về JSON:"""


def route_llm(query: str, llm_client) -> Optional[RouterOutput]:
    """
    Path B: LLM structured output routing.
    Chỉ dùng cho queries phức tạp/ambiguous mà rule-based không handle tốt.
    Returns None nếu LLM call fail (→ caller dùng rule-based fallback).
    """
    try:
        from google.genai import types as genai_types

        response = llm_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=_ROUTER_PROMPT.format(query=query),
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ROUTER_SCHEMA,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                temperature=0.0,
            ),
        )

        raw = json.loads(response.text)
        query_type  = QueryType(raw.get("query_type", "GENERAL"))
        scopes      = raw.get("scopes", [])
        calc_tool   = raw.get("calc_tool")
        reasoning   = raw.get("reasoning", "llm_router")
        ambiguous   = raw.get("ambiguous_interpretations", [])
        clarify_q   = raw.get("clarify_question")
        clarify_n   = bool(raw.get("clarify_needed", False))

        # FM06 safety net vẫn áp dụng sau LLM
        scopes = _apply_scope_safety_net(query, scopes)
        seen: dict = {}
        scopes = [seen.setdefault(s, s) for s in scopes if s not in seen][:2]

        mandatory_pairs = query_type == QueryType.ELIGIBILITY
        retrieval_queries = _build_retrieval_queries(query, query_type, scopes)

        return RouterOutput(
            query_type                = query_type,
            scopes                    = scopes,
            retrieval_queries         = retrieval_queries,
            mandatory_pairs           = mandatory_pairs,
            clarify_needed            = clarify_n,
            clarify_question          = clarify_q if clarify_n else None,
            calc_tool                 = calc_tool,
            ambiguous_interpretations = ambiguous,
            reasoning                 = f"llm | {reasoning}",
        )

    except Exception as e:
        logger.warning(f"[Router] LLM fallback failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

class TaxRouter:
    """
    Stage 1 Router — entrypoint cho pipeline.

    Sử dụng:
        router = TaxRouter(llm_client=gemini_client)
        result = router.route(query)
        # result.query_type, result.scopes, result.retrieval_queries, ...
    """

    # Threshold confidence của rule-based để KHÔNG cần LLM
    _RULE_CONFIDENCE_THRESHOLD = 0.85

    def __init__(self, llm_client=None):
        """
        llm_client: google.genai Client instance (optional).
        Nếu None → luôn dùng rule-based (FM07 mode).
        """
        self.llm_client = llm_client

    def route(self, query: str) -> RouterOutput:
        """
        Route một query.
        Path A (rule-based) → Path B (LLM) nếu cần.
        FM07: nếu LLM fail → fallback về rule-based.
        """
        t0 = time.monotonic()

        # FM02a: OOD fast path (không cần LLM)
        if _is_ood(query):
            result = RouterOutput(
                query_type=QueryType.OOD,
                scopes=[],
                retrieval_queries=[],
                reasoning="OOD: no tax keywords detected",
            )
            logger.info(f"[Router] OOD | {query[:60]!r} | {(time.monotonic()-t0)*1000:.0f}ms")
            return result

        # Path A: Rule-based
        rule_result = route_rule_based(query)

        # Quyết định có cần LLM không
        needs_llm = (
            self.llm_client is not None
            and self._needs_llm_routing(query, rule_result)
        )

        if needs_llm:
            llm_result = route_llm(query, self.llm_client)
            if llm_result is not None:
                result = llm_result
                logger.info(
                    f"[Router] LLM | type={result.query_type.value} "
                    f"scopes={result.scopes} | {(time.monotonic()-t0)*1000:.0f}ms"
                )
                return result
            # FM07: LLM fail → fallback rule-based
            logger.warning("[Router] FM07: LLM failed, using rule-based fallback")

        logger.info(
            f"[Router] Rule | type={rule_result.query_type.value} "
            f"scopes={rule_result.scopes} | {(time.monotonic()-t0)*1000:.0f}ms"
        )
        return rule_result

    def _needs_llm_routing(self, query: str, rule_result: RouterOutput) -> bool:
        """
        Quyết định có cần LLM không.
        True khi rule-based không đủ tự tin:
          - QueryType = AMBIGUOUS → LLM cần liệt kê các interpretation
          - QueryType = GENERAL với multi-scope (2+) → câu phức tạp, nhiều chủ thể
          - QueryType = GENERAL với query bất kỳ độ dài → heuristic cũ (giảm threshold)
          - Không detect được scope nào
        """
        # AMBIGUOUS luôn cần LLM để liệt kê interpretations
        if rule_result.query_type == QueryType.AMBIGUOUS:
            return True
        # Multi-scope GENERAL → nhiều chủ thể, cần LLM phân tích rõ hơn
        if rule_result.query_type == QueryType.GENERAL and len(rule_result.scopes) >= 2:
            return True
        # GENERAL với query dài → phức tạp (giảm threshold từ 80 → 60)
        if rule_result.query_type == QueryType.GENERAL and len(query) > 60:
            return True
        # Không detect được scope → router không chắc
        if not rule_result.scopes and rule_result.query_type != QueryType.OOD:
            return True
        return False
