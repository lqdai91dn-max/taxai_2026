"""
src/agent/retrieval_stage.py — Stage 2: Retrieval + Calculation

Thực thi kế hoạch retrieval từ RouterOutput:
  - Chạy N hybrid search queries song song (sequential, ChromaDB không cần async)
  - Deduplicate + merge theo chunk_id (giữ score cao nhất)
  - FM01: Scope mismatch check → expand nếu cần
  - FM02b: Zero results → corpus gap flag
  - FM08: Conflict detection → flag + resolve winner

Stage 2b (Calculation):
  - Extract tham số từ query bằng regex
  - Gọi calculator tool nếu RouterOutput.calc_tool != None
  - FM05: Missing STRICT params → CalcOutput.missing_params

Input:  PipelineState (sau khi router_output đã được populate)
Output: (RetrievalOutput, Optional[CalcOutput])
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.agent.schemas import (
    CalcOutput,
    CalcParamStatus,
    ConflictPair,
    DegradeLevel,
    PipelineState,
    RetrievedChunk,
    RetrievalOutput,
    legal_level_from_doc_type,
)
from src.retrieval.hybrid_search import HybridSearch
from src.retrieval.scope_classifier import SCOPE_DOCS
from src.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

# Corpus date — cập nhật khi thêm văn bản mới
CORPUS_DATE = "03/2026"

# FM01: tỷ lệ overlap tối thiểu giữa retrieved doc_ids và expected doc_ids
# Nếu thấp hơn → scope mismatch → expand
_SCOPE_OVERLAP_THRESHOLD = 0.3

# FM08: cặp văn bản có thể mâu thuẫn (từ hybrid_search._PARTIAL_AMEND)
_CONFLICT_PAIRS: Dict[str, str] = {
    "125_2020_NDCP": "310_2025_NDCP",
    "109_2025_QH15": "149_2025_QH15",
}


# ── Parameter extraction helpers (Stage 2b) ───────────────────────────────────

_VND_PATTERN = re.compile(
    r"(\d[\d\s,\.]*)\s*"
    r"(?:tỷ(?:\s*đồng)?|triệu(?:\s*đồng)?|nghìn(?:\s*đồng)?|"
    r"tr\b|k\b|\bvnd\b|\bđồng\b)",
    re.IGNORECASE | re.UNICODE,
)

_CATEGORY_MAP = {
    "hàng hóa":      "goods",
    "hàng hoá":      "goods",
    "phân phối":     "goods",
    "bán lẻ":        "goods",
    "bán buôn":      "goods",
    "dịch vụ":       "services",
    "sửa chữa":      "services",
    "tư vấn":        "services",
    "nail":          "services",
    "làm đẹp":       "services",
    "ăn uống":       "services",
    "nhà hàng":      "services",
    "quán":          "services",
    "sản xuất":      "manufacturing",
    "vận tải":       "manufacturing",
    "xây dựng":      "manufacturing",
    "cho thuê":      "real_estate",
    "thuê nhà":      "real_estate",
    "bất động sản":  "real_estate",
    "bđs":           "real_estate",
}

_DEPENDENTS_PATTERN = re.compile(
    r"(\d+)\s*(?:người\s*phụ\s*thuộc|npt|con|người\s*phụ)",
    re.IGNORECASE | re.UNICODE,
)

_MONTHS_PATTERN = re.compile(
    r"(\d+)\s*tháng",
    re.IGNORECASE | re.UNICODE,
)

_INCOME_PATTERN = re.compile(
    r"(?:lương|thu\s+nhập|tiền\s+công)\s+(?:là\s+|khoảng\s+|tầm\s+)?"
    r"(\d[\d\s,\.]*)\s*(?:tỷ|triệu|nghìn|tr\b|k\b|đồng|vnd)",
    re.IGNORECASE | re.UNICODE,
)


def _parse_vnd(text: str) -> Optional[float]:
    """Parse số tiền VND từ text. Returns None nếu không tìm thấy."""
    for m in _VND_PATTERN.finditer(text):
        raw = m.group(1).replace(" ", "").replace(",", "")
        try:
            num = float(raw)
        except ValueError:
            continue
        unit = m.group(0)[len(m.group(1)):].strip().lower()
        if "tỷ" in unit:
            return num * 1_000_000_000
        if "triệu" in unit or unit.startswith("tr"):
            return num * 1_000_000
        if "nghìn" in unit or unit == "k":
            return num * 1_000
        return num
    return None


def _parse_category(query: str) -> Optional[str]:
    q = query.lower()
    for phrase, cat in _CATEGORY_MAP.items():
        if phrase in q:
            return cat
    return None


def _parse_dependents(query: str) -> Optional[int]:
    m = _DEPENDENTS_PATTERN.search(query)
    if m:
        return int(m.group(1))
    return None


def _parse_months(query: str) -> Optional[int]:
    m = _MONTHS_PATTERN.search(query)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 12 else None
    return None


def _parse_income(query: str) -> Optional[float]:
    """Thu nhập tháng/năm — check INCOME_PATTERN trước, fallback _parse_vnd."""
    m = _INCOME_PATTERN.search(query)
    if m:
        return _parse_vnd(m.group(0))
    return _parse_vnd(query)


# ── Stage 2b: Calculator ──────────────────────────────────────────────────────

def _run_calculator(query: str, calc_tool: str) -> CalcOutput:
    """
    Extract params từ query và gọi calculator tool.
    FM05: STRICT params thiếu → set missing_params, không block.
    """
    tool_fn = TOOL_REGISTRY.get(calc_tool)
    if tool_fn is None:
        return CalcOutput(
            tool_name=calc_tool,
            error=f"Tool '{calc_tool}' không tìm thấy trong TOOL_REGISTRY",
        )

    missing:  List[str] = []
    assumed:  Dict[str, Any] = {}
    statuses: Dict[str, CalcParamStatus] = {}

    try:
        # ── calculate_tax_hkd ────────────────────────────────────────────
        if calc_tool == "calculate_tax_hkd":
            revenue  = _parse_vnd(query)
            category = _parse_category(query)

            if revenue is None:
                missing.append("annual_revenue")
                statuses["annual_revenue"] = CalcParamStatus.MISSING
            else:
                statuses["annual_revenue"] = CalcParamStatus.OK

            if category is None:
                missing.append("business_category")
                statuses["business_category"] = CalcParamStatus.MISSING
            else:
                statuses["business_category"] = CalcParamStatus.OK

            if missing:
                return CalcOutput(
                    tool_name=calc_tool,
                    missing_params=missing,
                    param_status=statuses,
                    error="Thiếu tham số bắt buộc",
                )

            result = tool_fn(annual_revenue=revenue, business_category=category)

        # ── calculate_tax_hkd_profit ─────────────────────────────────────
        elif calc_tool == "calculate_tax_hkd_profit":
            revenue  = _parse_vnd(query)
            category = _parse_category(query)
            # Expenses: tìm "chi phí X triệu"
            expense_m = re.search(
                r"chi\s*phí\s+(\d[\d\s,\.]*)\s*(?:tỷ|triệu|nghìn|tr\b|k\b|đồng)",
                query, re.IGNORECASE
            )
            expenses = _parse_vnd(expense_m.group(0)) if expense_m else None

            for name, val in [("annual_revenue", revenue),
                               ("business_category", category),
                               ("annual_expenses", expenses)]:
                if val is None:
                    missing.append(name)
                    statuses[name] = CalcParamStatus.MISSING
                else:
                    statuses[name] = CalcParamStatus.OK

            if missing:
                return CalcOutput(
                    tool_name=calc_tool,
                    missing_params=missing,
                    param_status=statuses,
                    error="Thiếu tham số bắt buộc",
                )

            result = tool_fn(
                annual_revenue=revenue,
                annual_expenses=expenses,
                business_category=category,
            )

        # ── calculate_deduction ──────────────────────────────────────────
        elif calc_tool == "calculate_deduction":
            dependents = _parse_dependents(query)
            months     = _parse_months(query)

            if dependents is None:
                dependents = 0
                assumed["dependents"] = 0
                statuses["dependents"] = CalcParamStatus.ASSUMED
            else:
                statuses["dependents"] = CalcParamStatus.OK

            if months is None:
                months = 12
                assumed["months"] = 12
                statuses["months"] = CalcParamStatus.ASSUMED
            else:
                statuses["months"] = CalcParamStatus.OK

            result = tool_fn(dependents=dependents, months=months)

        # ── calculate_tncn_progressive ───────────────────────────────────
        elif calc_tool == "calculate_tncn_progressive":
            income = _parse_income(query)
            if income is None:
                return CalcOutput(
                    tool_name=calc_tool,
                    missing_params=["annual_taxable_income"],
                    param_status={"annual_taxable_income": CalcParamStatus.MISSING},
                    error="Thiếu thu nhập tính thuế",
                )
            statuses["annual_taxable_income"] = CalcParamStatus.OK
            result = tool_fn(annual_taxable_income=income)

        # ── evaluate_tax_obligation ──────────────────────────────────────
        elif calc_tool == "evaluate_tax_obligation":
            revenue  = _parse_vnd(query)
            if revenue is None:
                return CalcOutput(
                    tool_name=calc_tool,
                    missing_params=["annual_revenue"],
                    param_status={"annual_revenue": CalcParamStatus.MISSING},
                    error="Thiếu doanh thu",
                )
            q = query.lower()
            has_online = any(
                kw in q for kw in ["shopee", "lazada", "tiktok", "online", "sàn", "tmđt"]
            )
            has_payment = any(
                kw in q for kw in ["shopee", "lazada", "tiktok shop", "tiki", "sendo"]
            )
            result = tool_fn(
                annual_revenue=revenue,
                has_online_sales=has_online,
                platform_has_payment=has_payment,
            )

        else:
            return CalcOutput(
                tool_name=calc_tool,
                error=f"Chưa implement extraction cho tool: {calc_tool}",
            )

        # Format kết quả thành string cho Generator prompt
        formatted = _format_calc_result(calc_tool, result)
        return CalcOutput(
            tool_name=calc_tool,
            result=result if isinstance(result, dict) else vars(result),
            formatted=formatted,
            assumed_params=assumed,
            param_status=statuses,
        )

    except Exception as e:
        logger.error(f"[Stage2b] Calculator error: {e}", exc_info=True)
        return CalcOutput(tool_name=calc_tool, error=str(e))


def _format_calc_result(tool_name: str, result: Any) -> str:
    """Format kết quả calculator thành human-readable string cho Generator."""
    try:
        if hasattr(result, "__dict__"):
            d = vars(result)
        elif isinstance(result, dict):
            d = result
        else:
            return str(result)

        lines = [f"[Kết quả tính toán — {tool_name}]"]
        for k, v in d.items():
            if k.startswith("_") or k == "citation":
                continue
            if isinstance(v, float):
                # Format số tiền VND
                if v >= 1_000_000:
                    lines.append(f"  {k}: {v:,.0f} đồng")
                else:
                    lines.append(f"  {k}: {v}")
            elif v is not None:
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)
    except Exception:
        return str(result)


# ── Chunk conversion ──────────────────────────────────────────────────────────

def _to_retrieved_chunk(raw: Dict[str, Any]) -> RetrievedChunk:
    """Convert raw hybrid search dict → RetrievedChunk với FM08 fields populated."""
    meta = raw.get("metadata", {})
    doc_type = meta.get("document_type", "")
    return RetrievedChunk(
        chunk_id       = raw["chunk_id"],
        doc_id         = meta.get("doc_id", raw.get("chunk_id", "").split("_chunk")[0]),
        text           = raw.get("text", ""),
        score          = raw.get("rrf_score", raw.get("score", 0.0)),
        metadata       = meta,
        legal_level    = legal_level_from_doc_type(doc_type),
        effective_date = meta.get("effective_date", ""),
    )


# ── FM01: Scope mismatch check ────────────────────────────────────────────────

def _check_scope_mismatch(
    chunks: List[RetrievedChunk],
    expected_scopes: List[str],
) -> bool:
    """
    FM01: Kiểm tra retrieved doc_ids có khớp với expected scopes không.
    True = mismatch (cần expand).
    """
    if not expected_scopes or not chunks:
        return False

    expected_docs: set = set()
    for scope in expected_scopes:
        expected_docs.update(SCOPE_DOCS.get(scope, []))

    if not expected_docs:
        return False

    retrieved_doc_ids = {c.doc_id for c in chunks}
    overlap = retrieved_doc_ids & expected_docs
    ratio   = len(overlap) / len(retrieved_doc_ids) if retrieved_doc_ids else 0

    if ratio < _SCOPE_OVERLAP_THRESHOLD:
        logger.info(
            f"[FM01] Scope mismatch: expected={expected_docs} "
            f"got={retrieved_doc_ids} overlap={ratio:.2f}"
        )
        return True
    return False


# ── FM08: Conflict detection ──────────────────────────────────────────────────

def _detect_conflicts(chunks: List[RetrievedChunk]) -> List[ConflictPair]:
    """
    FM08: Phát hiện cặp văn bản mâu thuẫn trong retrieved chunks.
    Dùng _CONFLICT_PAIRS để xác định cặp cũ→mới đã biết.
    """
    present = {c.doc_id for c in chunks}
    conflicts: List[ConflictPair] = []

    for old_doc, new_doc in _CONFLICT_PAIRS.items():
        if old_doc not in present or new_doc not in present:
            continue

        # Tìm representative chunk của mỗi doc
        old_chunk = next((c for c in chunks if c.doc_id == old_doc), None)
        new_chunk = next((c for c in chunks if c.doc_id == new_doc), None)
        if not old_chunk or not new_chunk:
            continue

        # Resolution: ưu tiên effective_date mới hơn
        winner_id, reason = _resolve_conflict(old_chunk, new_chunk)
        conflicts.append(ConflictPair(
            chunk_id_a = old_chunk.chunk_id,
            chunk_id_b = new_chunk.chunk_id,
            winner_id  = winner_id,
            reason     = reason,
        ))
        logger.info(
            f"[FM08] Conflict: {old_doc} vs {new_doc} → winner={winner_id} ({reason})"
        )

    return conflicts


def _resolve_conflict(a: RetrievedChunk, b: RetrievedChunk) -> Tuple[str, str]:
    """
    Xác định chunk nào thắng khi mâu thuẫn.
    Bước 1: effective_date mới hơn thắng.
    Bước 2: legal_level thấp hơn (Luật=1 > NĐ=3) thắng.
    Bước 3: unresolved nếu không phân biệt được.

    NGOẠI LỆ: văn bản cấp thấp hơn explicitly amends điều khoản cụ thể
    của văn bản cấp cao hơn → phần sửa đổi thắng.
    (Xử lý bởi B4 amendment expansion trong HybridSearch — không cần xử lý lại ở đây)
    """
    # Bước 1: effective_date
    if a.effective_date and b.effective_date:
        if b.effective_date > a.effective_date:
            return b.chunk_id, "newer_date"
        if a.effective_date > b.effective_date:
            return a.chunk_id, "newer_date"

    # Bước 2: legal_level (thấp hơn = uy quyền cao hơn)
    if a.legal_level != b.legal_level:
        if a.legal_level < b.legal_level:
            return a.chunk_id, "higher_level"
        return b.chunk_id, "higher_level"

    return b.chunk_id, "unresolved"  # default: newer doc (b = amending doc)


# ── Merge + deduplicate ───────────────────────────────────────────────────────

def _merge_chunks(
    all_raw: List[Dict[str, Any]],
) -> List[RetrievedChunk]:
    """
    Merge kết quả từ nhiều queries (flat list), deduplicate theo chunk_id.
    Giữ score cao nhất nếu cùng chunk_id xuất hiện nhiều lần.
    Dùng cho FM01 scope expand (flat input, global re-sort OK).
    """
    best: Dict[str, Dict[str, Any]] = {}
    for raw in all_raw:
        cid = raw["chunk_id"]
        score = raw.get("rrf_score", raw.get("score", 0.0))
        if cid not in best or score > best[cid].get("rrf_score", 0):
            best[cid] = raw

    sorted_raws = sorted(best.values(), key=lambda x: x.get("rrf_score", 0), reverse=True)
    return [_to_retrieved_chunk(r) for r in sorted_raws]


def _merge_slotted(
    query_raw_lists: List[List[Dict[str, Any]]],
) -> List[RetrievedChunk]:
    """
    Slot-based merge: giữ thứ tự per-query, không global re-sort.

    Thứ tự: query[0] (global safety net) → query[1] (primary targeted)
            → query[2] (secondary targeted).
    Đảm bảo targeted doc có cơ hội xuất hiện dù RRF score thấp hơn
    so với doc dominant trong global search.

    Dedup: chunk_id xuất hiện lần đầu được giữ; nếu xuất hiện lại ở
    query sau với score cao hơn, update score nhưng không đổi vị trí.
    """
    seen: Dict[str, int] = {}   # chunk_id → index in result list
    result: List[RetrievedChunk] = []

    for raw_list in query_raw_lists:
        for raw in raw_list:
            cid = raw["chunk_id"]
            new_score = raw.get("rrf_score", raw.get("score", 0.0))
            if cid in seen:
                # Update score nếu tốt hơn, nhưng giữ nguyên vị trí
                existing = result[seen[cid]]
                if new_score > existing.score:
                    existing.score = new_score
            else:
                chunk = _to_retrieved_chunk(raw)
                seen[cid] = len(result)
                result.append(chunk)

    return result


# ── Main Stage 2 class ────────────────────────────────────────────────────────

class RetrievalStage:
    """
    Stage 2 + 2b: Retrieval + Calculation.

    Usage:
        stage = RetrievalStage(searcher)
        retrieval_out, calc_out = stage.run(state)
    """

    def __init__(self, searcher: HybridSearch):
        self.searcher = searcher

    def run(self, state: PipelineState) -> Tuple[RetrievalOutput, Optional[CalcOutput]]:
        """
        Thực thi toàn bộ Stage 2.
        Cập nhật state.fallback_log cho FM01, FM02b, FM08.
        """
        router_out = state.router_output
        assert router_out is not None, "router_output must be set before Stage 2"

        t0 = time.monotonic()

        # ── Stage 2a: Retrieval ──────────────────────────────────────────

        retrieval_out = self._run_retrieval(state, router_out)

        # ── Stage 2b: Calculation (nếu có calc_tool) ────────────────────

        calc_out: Optional[CalcOutput] = None
        if router_out.calc_tool and not retrieval_out.zero_results:
            calc_out = _run_calculator(state.query, router_out.calc_tool)
            if calc_out.missing_params:
                logger.info(
                    f"[FM05] Missing STRICT params: {calc_out.missing_params}"
                )
                state.log_fallback(
                    fm_id="FM05",
                    action="missing_strict_params",
                    success=False,
                    params=calc_out.missing_params,
                )

        state.record_stage_latency("RETRIEVING", time.monotonic() - t0)
        return retrieval_out, calc_out

    def _run_retrieval(
        self,
        state: PipelineState,
        router_out,
    ) -> RetrievalOutput:
        """Chạy retrieval queries + FM01 + FM02b + FM08."""

        queries = router_out.retrieval_queries
        expected_scopes = router_out.scopes

        # Chạy tất cả queries — slotted để giữ per-query exposure
        query_raw_lists = self._execute_queries_slotted(queries)
        chunks = _merge_slotted(query_raw_lists)
        all_raw = [r for lst in query_raw_lists for r in lst]  # flat for FM01 expand

        # FM01: Scope mismatch check
        scope_mismatch = False
        scope_expanded  = False
        if chunks and expected_scopes:
            scope_mismatch = _check_scope_mismatch(chunks, expected_scopes)
            if scope_mismatch:
                state.log_fallback(
                    fm_id="FM01",
                    action="scope_mismatch_detected",
                    success=False,
                    expected=expected_scopes,
                    retrieved=[c.doc_id for c in chunks[:5]],
                )
                # Expand: retry toàn bộ queries KHÔNG có doc_filter
                expanded_raw = self._execute_queries(queries, force_no_filter=True)
                if expanded_raw:
                    extra = _merge_chunks(expanded_raw)
                    # Merge extra vào chunks hiện có
                    all_raw += expanded_raw
                    chunks   = _merge_chunks(all_raw)
                    scope_expanded = True
                    state.log_fallback(
                        fm_id="FM01",
                        action="scope_expand",
                        success=True,
                        extra_chunks=len(extra),
                    )

        # FM02b: Zero results check
        if not chunks:
            logger.warning("[FM02b] Zero results after all queries")
            state.log_fallback(
                fm_id="FM02b",
                action="zero_results",
                success=False,
                scopes=expected_scopes,
            )
            return RetrievalOutput(
                chunks        = [],
                query_count   = len(queries),
                zero_results  = True,
                scope_mismatch= scope_mismatch,
                scope_expanded= scope_expanded,
            )

        # Record retrieved doc_ids for eval diagnostic (false_refusal detection)
        state.retrieved_doc_ids = list({c.doc_id for c in chunks})

        # FM08: Conflict detection
        conflicts   = _detect_conflicts(chunks)
        has_conflict = bool(conflicts)
        if has_conflict:
            state.log_fallback(
                fm_id="FM08",
                action="conflict_detected",
                success=True,   # detected = success; resolution happens in Stage 3 prompt
                pairs=[(c.chunk_id_a, c.chunk_id_b, c.reason) for c in conflicts],
            )

        logger.info(
            f"[Stage2] {len(chunks)} chunks | "
            f"mismatch={scope_mismatch} expanded={scope_expanded} "
            f"conflicts={len(conflicts)}"
        )

        return RetrievalOutput(
            chunks        = chunks,
            query_count   = len(queries),
            zero_results  = False,
            scope_mismatch= scope_mismatch,
            scope_expanded= scope_expanded,
            conflicts     = conflicts,
            has_conflict  = has_conflict,
        )

    def _execute_queries(
        self,
        queries,
        force_no_filter: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Thực thi queries, trả về flat list.
        Dùng cho FM01 expand (không cần slot preservation).
        """
        return [r for lst in self._execute_queries_slotted(queries, force_no_filter) for r in lst]

    def _execute_queries_slotted(
        self,
        queries,
        force_no_filter: bool = False,
    ) -> List[List[Dict[str, Any]]]:
        """
        Thực thi danh sách RetrievalQuery, trả về per-query result lists.
        Giữ nguyên thứ tự để _merge_slotted có thể preserve exposure.
        force_no_filter=True → bỏ qua doc_filter của tất cả queries (FM01 expand).
        """
        result_lists: List[List[Dict[str, Any]]] = []

        for rq in queries:
            doc_filter = None if force_no_filter else rq.doc_filter
            try:
                results = self.searcher.search(
                    query         = rq.query,
                    n_results     = rq.top_k,
                    filter_doc_id = doc_filter,
                )
                result_lists.append(results)
                logger.debug(
                    f"[Stage2] Query '{rq.query[:50]}' "
                    f"filter={doc_filter} → {len(results)} results"
                )
            except Exception as e:
                logger.error(f"[Stage2] Search error: {e}", exc_info=True)
                result_lists.append([])

        return result_lists
