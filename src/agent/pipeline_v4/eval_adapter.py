"""
src/agent/pipeline_v4/eval_adapter.py — Adapter cho eval_runner.py

Wrap PipelineV4 để export interface giống TaxAIAgent:
    agent.answer(question, show_sources=True) → dict

Output dict (compatible với eval_runner.py):
    answer:            str
    sources:           list[dict]         # từ citations
    tool_calls:        list[dict]         # synthetic từ template_type
    model:             str
    retrieved_doc_ids: list[str]
    degrade_level:     int (1=OK, 2=fallback, 3=full degrade)
    fm_breakdown:      dict               # A0 diagnostic fields

Diagnostic fields trong fm_breakdown (A0):
    template_type:         str             "HKD_percentage" | "PIT_full" | ...
    clarification_needed:  bool            True nếu pipeline hỏi lại user
    params:                dict            params_validated đã extract
    compute_violations:    list[str]       G4-Warn violations
    intent_summary:        dict | None     QueryIntent summary
    top_chunks:            list[dict]      top-3 retrieved chunks
    stage_fail:            str | None      "retrieval" | "reasoner" | "validation" | "synthesizer"
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Mapping template_type → calculator tool name (cho T3 scoring)
_TEMPLATE_TO_TOOL = {
    "HKD_percentage":  "calculate_tax_hkd",
    "HKD_profit":      "calculate_tax_hkd_profit",
    "PIT_full":        "calculate_tncn_progressive",
    "PIT_progressive": "calculate_tncn_progressive",
    "deduction_calc":  "calculate_deduction",
}


def _infer_stage_fail(output: dict) -> Optional[str]:
    """
    Suy luận stage nào fail từ output v4.
    Dùng cho A0 diagnostic logging.

    Returns:
        "retrieval"   — không có chunks nào relevant
        "reasoner"    — clarification trigger hoặc template=None
        "validation"  — degrade từ validation
        "synthesizer" — degrade sau khi có calc result
        None          — không fail
    """
    degrade = output.get("degrade_level", 1)
    if degrade < 4:
        return None   # pipeline hoàn thành bình thường (kể cả clarification)

    # degrade_level=4: xác định giai đoạn fail qua clues
    template = output.get("template")
    top_chunks = output.get("top_chunks", [])
    tax_amount = output.get("tax_amount")

    if not top_chunks:
        return "retrieval"   # không retrieve được gì relevant
    if not template:
        return "reasoner"    # retrieval OK nhưng LLM không chọn được template
    if tax_amount is None:
        return "validation"  # có template nhưng calculator fail
    return "synthesizer"     # có kết quả nhưng synthesizer fail


def _map_degrade_level(v4_level: int) -> int:
    """
    Map v4 degrade level (1–4) → eval_runner degrade level (1–3).

    v4: 1=normal, 2=fallback template, 4=full degrade
    eval: 1=1.0x, 2=0.85x, 3=0.0x
    """
    if v4_level <= 2:
        return 1   # clarification hoặc fallback template → vẫn đúng → full score
    return 3       # full degrade → 0x


def _build_tool_calls(output: dict) -> list:
    """
    Tổng hợp synthetic tool_calls từ template_type để T3 scoring hoạt động.

    Calc path: map template → calculator tool name
    RAG/Explain path: thêm search_legal_docs (hệ thống đang thực sự làm hybrid search)
    """
    template = output.get("template")
    tool_name = _TEMPLATE_TO_TOOL.get(template) if template else None

    if tool_name:
        # Calc path: dùng calculator tool tương ứng
        return [{"tool": tool_name}]
    else:
        # RAG/Explain path: system làm retrieval → search_legal_docs là honest
        answer = output.get("answer", "")
        if len(answer) > 50:
            return [{"tool": "search_legal_docs"}]
        return []


def _build_sources(output: dict) -> list:
    """
    Chuyển citations list sang format sources cho eval_runner.
    """
    citations = output.get("citations", []) or []
    sources = []
    for c in citations:
        doc_id = c.get("doc_number") or c.get("doc_id") or ""
        title  = c.get("title", "")[:80]
        note   = c.get("note", "")
        sources.append({
            "doc_id":  doc_id,
            "title":   title,
            "note":    note,
            "snippet": "",
        })
    return sources


class V4Adapter:
    """
    Adapter wrap PipelineV4 để dùng với eval_runner.py.

    Usage:
        adapter = V4Adapter()
        result = adapter.answer("Tôi bán hàng 1.2 tỷ, thuế bao nhiêu?")
    """

    def __init__(
        self,
        api_key:   Optional[str] = None,
        model:     str = "gemini-2.5-flash",
        log_audit: bool = True,
    ):
        import os
        from src.retrieval.hybrid_search import HybridSearch
        from src.agent.pipeline_v4.orchestrator import PipelineV4

        self.model = model
        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY")

        self._searcher = HybridSearch()
        self._pipeline = PipelineV4(
            searcher   = self._searcher,
            api_key    = self._api_key,
            model      = self.model,
            log_audit  = log_audit,
        )
        logger.info("V4Adapter ready — model=%s", self.model)

    def answer(
        self,
        question:     str,
        show_sources: bool = True,
    ) -> Dict[str, Any]:
        """
        Run PipelineV4 và trả về dict compatible với eval_runner.

        A0 Diagnostic fields trong fm_breakdown:
            template_type, clarification_needed, params (empty dict nếu không có),
            compute_violations, intent_summary, top_chunks, stage_fail
        """
        session_id = str(uuid.uuid4())[:8]

        try:
            output = self._pipeline.run(
                question   = question,
                session_id = session_id,
            )
        except Exception as e:
            logger.error("V4Adapter pipeline error: %s", e)
            return {
                "answer":            "",
                "sources":           [],
                "tool_calls":        [],
                "model":             self.model,
                "retrieved_doc_ids": [],
                "citations_doc_ids": [],
                "degrade_level":     3,
                "fm_breakdown":      {"stage_fail": "pipeline_crash", "error": str(e)},
                "error":             str(e),
            }

        # ── A0 Diagnostic ──────────────────────────────────────────────────────
        stage_fail = _infer_stage_fail(output)
        template = output.get("template")
        is_rag = template not in (
            "PIT_full", "PIT_progressive", "PIT_flat_20",
            "HKD_percentage", "HKD_profit", "deduction_calc",
        ) if template else True

        fm_breakdown = {
            # Stage attribution
            "stage_fail":           stage_fail,
            # Path routing (calc vs rag)
            "path":                 "rag" if is_rag else "calc",
            # Reasoning diagnostics
            "template_type":        template,
            "clarification_needed": output.get("clarification_needed", False),
            "params":               {},   # không expose full params (privacy)
            # Guard diagnostics
            "compute_violations":   output.get("compute_violations", []),
            # Retrieval diagnostics
            "top_chunks":           output.get("top_chunks", []),
            "intent_summary":       output.get("intent_summary"),
            # Quality
            "tax_amount":           output.get("tax_amount"),
            "degrade_v4":           output.get("degrade_level", 1),
            "retry_count":          output.get("retry_count", 0),
        }

        # Build citations_doc_ids cho T2 scoring
        # Normalize: "68/2026/NĐ-CP" → "68_2026_NDCP" (calc path dùng display format)
        def _norm_doc_id(raw: str) -> str:
            if not raw or "/" not in raw:
                return raw
            return raw.replace("/", "_").replace("-", "").replace("Đ", "D").replace("đ", "d")

        citations = output.get("citations", []) or []
        citations_doc_ids = list({
            _norm_doc_id(c.get("doc_number") or c.get("doc_id", ""))
            for c in citations
            if c.get("doc_number") or c.get("doc_id")
        })

        return {
            "answer":             output.get("answer", ""),
            "sources":            _build_sources(output),
            "tool_calls":         _build_tool_calls(output),
            "model":              self.model,
            "retrieved_doc_ids":  output.get("doc_ids", []),
            "citations_doc_ids":  citations_doc_ids,
            "degrade_level":      _map_degrade_level(output.get("degrade_level", 1)),
            "fm_breakdown":       fm_breakdown,
            "error":              output.get("error"),
        }
