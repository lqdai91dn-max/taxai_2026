"""
src/agent/pipeline_adapter.py — Adapter giữa PipelineOrchestrator và eval runner.

Vấn đề:
  - eval_runner.py gọi agent.answer(question=...) → trả về dict chuẩn
  - PipelineOrchestrator.run() → trả về List[SSEEvent]
  - Format không tương thích → T2/T3/T4 score rớt về 0 nếu không có adapter

Adapter này:
  1. Buffer toàn bộ SSE events từ pipeline.run()
  2. Extract answer text từ ANSWER event CUỐI CÙNG (đúng khi có regeneration)
  3. Extract citations từ SOURCES event (structured, không parse text)
  4. Build synthetic tool_calls để T1/T3 scorer hoạt động đúng
  5. Log latency metrics: t_first_status, t_answer, t_total
  6. Expose FM breakdown từ DONE event

Output dict khớp với format mà eval_runner.py mong đợi:
  {
    "answer":      str,
    "sources":     list[dict],     # citations
    "tool_calls":  list[dict],     # synthetic từ calc_output + retrieval
    "key_facts":   list[str],      # từ SOURCES event
    "model":       str,
    "degrade_level": int,          # 1/2/3
    "fm_breakdown":  dict,         # {"FM03": 1, "FM05": 2}
    "latency_ms":    int,          # t_total
    "t_answer_ms":   int,          # time to ANSWER event (UX latency)
    "t_first_status_ms": int,      # verify streaming
  }
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from src.agent.pipeline import PipelineOrchestrator
from src.agent.schemas import SSEEventType

logger = logging.getLogger(__name__)


class PipelineAdapter:
    """
    Wrapper quanh PipelineOrchestrator để tương thích với eval_runner.py.

    Usage:
        adapter = PipelineAdapter(searcher=hybrid_search, api_key="...")
        result = adapter.answer(question="thuế HKD tính thế nào?")
        # result["answer"], result["tool_calls"], result["sources"], ...
    """

    def __init__(
        self,
        searcher,
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash",
    ):
        self._pipeline = PipelineOrchestrator(
            searcher=searcher,
            api_key=api_key or os.environ.get("GOOGLE_API_KEY"),
            model=model,
        )
        self.model = model

    def answer(
        self,
        question: str,
        show_sources: bool = True,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Gọi pipeline và trả về dict chuẩn cho eval runner.

        Critical contracts:
        - answer: LAST ANSWER event (correct for regeneration cases)
        - sources: từ SOURCES event structured data (không parse text)
        - tool_calls: synthetic — bao gồm cả calc tool và search_legal_docs
        - key_facts: từ SOURCES event (populated by Generator)
        """
        t_start = time.perf_counter()
        t_first_status_ms = 0
        t_answer_ms = 0

        events = []
        first_event_recorded = False

        # Buffer tất cả events — pipeline.run() là sync
        for event in self._pipeline.stream(question, session_id=session_id):
            events.append(event)
            elapsed_ms = int((time.perf_counter() - t_start) * 1000)

            if not first_event_recorded:
                t_first_status_ms = elapsed_ms
                first_event_recorded = True

            if event.event == SSEEventType.ANSWER:
                t_answer_ms = elapsed_ms  # ghi đè mỗi lần, giữ lần cuối

        t_total_ms = int((time.perf_counter() - t_start) * 1000)

        # ── Extract answer (LAST ANSWER event) ────────────────────────────────
        answer_text = ""
        for event in reversed(events):
            if event.event == SSEEventType.ANSWER:
                answer_text = event.data.get("text", "")
                break

        # ── Extract sources + key_facts (LAST SOURCES event) ──────────────────
        citations: List[Dict] = []
        key_facts: List[str]  = []
        for event in reversed(events):
            if event.event == SSEEventType.SOURCES:
                citations = event.data.get("citations", [])
                key_facts = event.data.get("key_facts", [])
                break

        # ── Extract DONE event metadata ────────────────────────────────────────
        degrade_level = 1
        fm_breakdown: Dict[str, int] = {}
        retrieved_doc_ids: List[str] = []
        for event in reversed(events):
            if event.event == SSEEventType.DONE:
                degrade_level     = event.data.get("degrade_level", 1)
                fm_breakdown      = event.data.get("fm_breakdown", {})
                retrieved_doc_ids = event.data.get("retrieved_doc_ids", [])
                break

        # ── Extract error ──────────────────────────────────────────────────────
        error_msg: Optional[str] = None
        for event in events:
            if event.event == SSEEventType.ERROR:
                error_msg = event.data.get("error", "pipeline error")
                break

        # ── Build synthetic tool_calls ─────────────────────────────────────────
        # T1/T3 scorer cần tool_calls để verify calc tool usage và topic tools.
        # Pipeline không dùng tool_calls trực tiếp, nhưng ta có thể reconstruct:
        #   - search_legal_docs: luôn có (pipeline luôn retrieve)
        #   - calculator tool: nếu có CALCULATION event
        tool_calls = _build_synthetic_tool_calls(events, citations)

        return {
            # ── Core output (required by eval_runner) ──────────────────────────
            "answer":     answer_text,
            "sources":    citations,
            "tool_calls": tool_calls,
            "model":      self.model,
            "iterations": 1,   # pipeline là single-pass (không phải agentic loop)

            # ── Extended output ────────────────────────────────────────────────
            "key_facts":           key_facts,
            "degrade_level":       degrade_level,
            "fm_breakdown":        fm_breakdown,
            "retrieved_doc_ids":   retrieved_doc_ids,
            "citations_doc_ids":   list({c.get("doc_id", "") for c in citations if c.get("doc_id")}),
            "error":               error_msg,

            # ── Latency metrics ────────────────────────────────────────────────
            "latency_ms":          t_total_ms,
            "t_answer_ms":         t_answer_ms,          # UX latency (compare vs planner)
            "t_first_status_ms":   t_first_status_ms,    # verify streaming không block
        }


# ── Synthetic tool_calls builder ───────────────────────────────────────────────

def _build_synthetic_tool_calls(events: list, citations: List[Dict]) -> List[Dict]:
    """
    Reconstruct tool_calls list từ SSE events để T1/T3 scorer hoạt động.

    Mapping:
      CALCULATION event  → calc tool call (với result dict)
      Retrieval (always) → search_legal_docs call (với snippets từ citations)
    """
    tool_calls: List[Dict] = []

    # ── Calculator tool ────────────────────────────────────────────────────────
    for event in events:
        if event.event == SSEEventType.CALCULATION:
            data = event.data
            tool_name = data.get("tool", "")
            if tool_name:
                tool_calls.append({
                    "tool":   tool_name,
                    "result": {
                        # Format khớp với expected_value keys trong eval
                        "formatted":    data.get("formatted", ""),
                        "assumed":      data.get("assumed", {}),
                        # Placeholder — T1 scorer sẽ tìm trong answer text
                        "total_tax":    _extract_total_from_formatted(data.get("formatted", "")),
                    },
                    "input":  {},
                })
            break  # chỉ có 1 calculation event

    # ── Search tool (always present nếu có citations hoặc có ANSWER) ──────────
    has_answer = any(e.event == SSEEventType.ANSWER for e in events)
    has_error  = any(e.event == SSEEventType.ERROR for e in events)
    ood_event  = any(
        e.event == SSEEventType.ANSWER
        and "nằm ngoài phạm vi" in e.data.get("text", "")
        for e in events
    )

    if has_answer and not has_error and not ood_event:
        # Build snippets từ citations để fact_checker legacy có thể dùng
        snippets = [
            {"snippet": c.get("text", "")}
            for c in citations if c.get("text")
        ]
        tool_calls.append({
            "tool":   "search_legal_docs",
            "result": {"results": snippets},
            "input":  {},
        })

    return tool_calls


def _extract_total_from_formatted(formatted_text: str) -> Optional[float]:
    """
    Trích xuất tổng số thuế từ formatted calculator output (best-effort).
    T1 scorer sẽ tự parse từ answer text, đây chỉ là fallback metadata.
    """
    import re
    # Tìm pattern "X triệu" hoặc "X,XXX,XXX" trong formatted text
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:triệu|đồng|VNĐ)?", formatted_text)
    if m:
        try:
            val_str = m.group(1).replace(",", "")
            val = float(val_str)
            if "triệu" in formatted_text[m.start():m.end() + 10]:
                val *= 1_000_000
            return val
        except ValueError:
            pass
    return None
