"""
src/agent/pipeline.py — Pipeline Orchestrator (Stage 5: State Machine)

Điều phối 5 stages theo state machine:
  Stage 1  Router        (TaxRouter)
  Stage 2  Retrieval     (RetrievalStage)
  Stage 2b Calculation   (RetrievalStage._run_calculator)
  Stage 3  Generator     (GeneratorStage)
  Stage 4  Fact Checker  (FactCheckerStage)

SSE streaming:
  status(ROUTING) → status(RETRIEVING) → calculation → status(GENERATING)
  → status(FACT_CHECKING) → answer → sources → done

Failure modes handled:
  FM02a  OOD             → L3 explain, emit error event, short-circuit
  FM02b  Corpus gap      → L3 explain, emit corpus_gap event, short-circuit
  FM03   Hallucination   → regenerate (max 1 lần)
  FM04   Fact check x2   → L2 caveat (run_degrade_l2)
  FM07   API timeout     → L3 explain after stage timeout
  FM08   Conflict        → handled in Retrieval, flagged in context

Usage:
    orchestrator = PipelineOrchestrator(api_key="...", searcher=hybrid_search)

    # Sync (collect all SSE events)
    events = orchestrator.run(query="thuế TNCN là gì?")

    # Streaming (async generator for SSE)
    async for event in orchestrator.stream(query="..."):
        yield event.to_sse_str()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import AsyncIterator, Iterator, List, Optional

from src.agent.fact_checker_stage import FactCheckerStage
from src.agent.generator import GeneratorStage
from src.agent.retrieval_stage import RetrievalStage
from src.agent.router import TaxRouter
from src.agent.schemas import (
    CancelledError,
    DegradeLevel,
    PipelineStage,
    PipelineState,
    QueryType,
    REQUEST_TIMEOUT,
    SSEEvent,
    SSEEventType,
    StageTimeoutError,
    make_corpus_gap_event,
    make_done_event,
    make_error_event,
    make_status_event,
)

logger = logging.getLogger(__name__)

CORPUS_DATE = "03/2026"


# ── OOD canned responses ────────────────────────────────────────────────────────

_OOD_RESPONSE = (
    "Câu hỏi này nằm ngoài phạm vi tư vấn của TaxAI. "
    "Tôi chuyên về pháp luật thuế Việt Nam (TNCN, GTGT, HKD). "
    "Vui lòng hỏi các vấn đề liên quan đến thuế để được hỗ trợ tốt hơn."
)

_CLARIFY_PREFIX = (
    "Để tính toán chính xác, tôi cần thêm thông tin:\n"
)


# ── PipelineOrchestrator ────────────────────────────────────────────────────────

class PipelineOrchestrator:
    """
    Điều phối toàn bộ pipeline từ query → SSE events.

    Args:
        api_key:  Google API key cho Gemini (hoặc từ GOOGLE_API_KEY env)
        searcher: HybridSearch instance (đã init với ChromaDB + BM25)
        model:    Gemini model ID (default: gemini-2.5-flash)
        request_timeout: Total timeout cho toàn pipeline (giây)
    """

    def __init__(
        self,
        searcher,                           # HybridSearch
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash",
        request_timeout: float = REQUEST_TIMEOUT,
    ):
        api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Cần GOOGLE_API_KEY")

        self._router    = TaxRouter(llm_client=None)   # rule-based by default
        self._retrieval = RetrievalStage(searcher=searcher)
        self._generator = GeneratorStage(api_key=api_key, model=model)
        self._checker   = FactCheckerStage()
        self._request_timeout = request_timeout

    # ── Public API: sync ────────────────────────────────────────────────────────

    def run(
        self,
        query: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[SSEEvent]:
        """
        Chạy pipeline synchronously, trả về danh sách tất cả SSE events.

        Dùng cho testing / internal calls.
        """
        events: List[SSEEvent] = []
        for event in self._run_sync(query, session_id, user_id):
            events.append(event)
        return events

    def stream(
        self,
        query: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Iterator[SSEEvent]:
        """
        Sync iterator — yields SSE events as they are generated.

        Dùng cho Flask/sync web frameworks.
        """
        return self._run_sync(query, session_id, user_id)

    async def astream(
        self,
        query: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> AsyncIterator[SSEEvent]:
        """
        Async generator — yields SSE events.

        Dùng cho FastAPI / async frameworks.
        Generator stages chạy trong executor để không block event loop.
        """
        loop = asyncio.get_event_loop()
        # Collect events in background thread (Gemini SDK is sync)
        events = await loop.run_in_executor(
            None,
            lambda: list(self._run_sync(query, session_id, user_id)),
        )
        for event in events:
            yield event

    # ── Core pipeline ────────────────────────────────────────────────────────────

    def _run_sync(
        self,
        query: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Iterator[SSEEvent]:
        """
        Core pipeline runner — yields SSE events.

        Stage transitions follow strict state machine in PipelineState.
        Each FM has a specific handler that emits appropriate events.
        """
        state = PipelineState(
            query=query,
            session_id=session_id,
            user_id=user_id,
        )
        # Set deadline
        state.deadline = time.monotonic() + self._request_timeout

        # ── Stage 1: Router ──────────────────────────────────────────────────
        yield make_status_event(PipelineStage.ROUTING, "Phân tích câu hỏi...")
        t0 = time.monotonic()
        try:
            router_out = self._router.route(query)
            state.router_output = router_out
        except Exception as exc:
            logger.error("Router failed: %s", exc)
            yield make_error_event(str(exc), stage="ROUTING", fm_id="FM07")
            state.fail(str(exc), DegradeLevel.L3_EXPLAIN)
            yield make_done_event(state)
            return

        state.record_stage_latency("ROUTING", time.monotonic() - t0)

        # FM02a: OOD — short circuit
        if router_out.query_type == QueryType.OOD:
            state.log_fallback("FM02a", "ood_short_circuit", success=True)
            state.degrade_level = DegradeLevel.L3_EXPLAIN
            yield SSEEvent(
                event=SSEEventType.ANSWER,
                data={"text": _OOD_RESPONSE},
            )
            state.finished_at = time.monotonic()
            yield make_done_event(state)
            return

        # CLARIFY_HARD: thiếu strict params → hỏi lại trước khi retrieve
        if router_out.clarify_needed and router_out.clarify_question:
            state.degrade_level = DegradeLevel.L2_CAVEAT
            yield SSEEvent(
                event=SSEEventType.ANSWER,
                data={"text": _CLARIFY_PREFIX + router_out.clarify_question},
            )
            state.finished_at = time.monotonic()
            yield make_done_event(state)
            return

        # ── Stage 2: Retrieval + 2b Calculation ─────────────────────────────
        state.transition(PipelineStage.RETRIEVING)
        yield make_status_event(PipelineStage.RETRIEVING, "Tra cứu văn bản pháp luật...")

        t0 = time.monotonic()
        try:
            ret_out, calc_out = self._retrieval.run(state)
            state.retrieval_output = ret_out
            if calc_out is not None:
                state.calc_output = calc_out
        except StageTimeoutError as exc:
            logger.warning("Retrieval timeout: %s", exc)
            state.log_fallback("FM07", "retrieval_timeout", success=False)
            yield make_error_event(str(exc), stage="RETRIEVING", fm_id="FM07")
            state.fail(str(exc), DegradeLevel.L3_EXPLAIN)
            yield make_done_event(state)
            return
        except Exception as exc:
            logger.error("Retrieval failed: %s", exc)
            yield make_error_event(str(exc), stage="RETRIEVING", fm_id="FM07")
            state.fail(str(exc), DegradeLevel.L3_EXPLAIN)
            yield make_done_event(state)
            return

        state.record_stage_latency("RETRIEVING", time.monotonic() - t0)

        # FM02b: zero results — corpus gap
        if ret_out.zero_results:
            state.log_fallback("FM02b", "corpus_gap", success=False)
            scope_str = ", ".join(router_out.scopes) or "thuế"
            yield make_corpus_gap_event(scope_str, CORPUS_DATE)
            state.degrade_level = DegradeLevel.L3_EXPLAIN
            state.finished_at = time.monotonic()
            yield make_done_event(state)
            return

        # FM05: calculation error với missing strict params → clarify
        if calc_out and calc_out.missing_params:
            state.log_fallback("FM05", "missing_strict_params", success=False)
            param_names = ", ".join(calc_out.missing_params)
            clarify_msg = (
                f"Để tính toán chính xác, vui lòng cung cấp thêm: **{param_names}**.\n\n"
                "Bạn có thể mô tả tình huống cụ thể hơn không?"
            )
            state.degrade_level = DegradeLevel.L2_CAVEAT
            yield SSEEvent(
                event=SSEEventType.ANSWER,
                data={"text": clarify_msg},
            )
            state.finished_at = time.monotonic()
            yield make_done_event(state)
            return

        # Stream calculation kết quả sớm (UX: user thấy số trước khi có full answer)
        if calc_out and calc_out.formatted and not calc_out.error:
            state.record_stage_latency("CALCULATING", 0.0)   # already done in retrieval
            yield SSEEvent(
                event=SSEEventType.CALCULATION,
                data={
                    "tool":      calc_out.tool_name,
                    "formatted": calc_out.formatted,
                    "assumed":   calc_out.assumed_params,
                },
            )

        # FM08: log conflict nếu có
        if ret_out.has_conflict:
            state.log_fallback(
                "FM08", "conflict_resolution", success=True,
                conflict_count=len(ret_out.conflicts),
            )

        # ── Stage 3: Generator (với retry loop) ─────────────────────────────
        state.transition(PipelineStage.GENERATING)
        yield make_status_event(PipelineStage.GENERATING, "Soạn câu trả lời...")

        gen_out = self._run_generator_with_retry(state)

        if gen_out is None:
            # FM07 cứng — generator fail hoàn toàn
            err = state.error or "Generator failed"
            yield make_error_event(err, stage="GENERATING", fm_id="FM07")
            yield make_done_event(state)
            return

        state.generator_output = gen_out

        # ── Emit answer ──────────────────────────────────────────────────────
        yield SSEEvent(
            event=SSEEventType.ANSWER,
            data={"text": gen_out.answer_text},
        )

        # Emit citations + key_facts (sources)
        # key_facts phải có ở đây để adapter eval có thể lấy mà không cần truy cập state
        yield SSEEvent(
            event=SSEEventType.SOURCES,
            data={
                "citations": [
                    {
                        "doc_id":   c.doc_id,
                        "article":  c.article,
                        "text":     c.text,
                        "label":    c.label,
                        "chunk_id": c.chunk_id,
                    }
                    for c in gen_out.citations
                ],
                "key_facts": gen_out.key_facts,
            },
        )

        # ── Stage 4: Fact Checker ────────────────────────────────────────────
        state.transition(PipelineStage.FACT_CHECKING)
        yield make_status_event(PipelineStage.FACT_CHECKING, "Xác minh dữ liệu...")

        t0 = time.monotonic()
        fc_out = self._checker.run(state)
        state.fact_check_output = fc_out
        state.record_stage_latency("FACT_CHECKING", time.monotonic() - t0)

        if fc_out.passed:
            # L1 Full — all good
            state.degrade_level = DegradeLevel.L1_FULL
        elif fc_out.has_critical and state.regeneration_count == 0:
            # FM03: regenerate một lần với negative constraint
            logger.info(
                "FM03: hallucination detected — regenerating (request_id=%s)",
                state.request_id,
            )
            state.log_fallback(
                "FM03", "regenerate",
                success=False,   # sẽ update sau
                issue_count=len([i for i in fc_out.issues if i.severity == "critical"]),
            )
            # Set correction instruction cho generator khi regenerate
            state.generator_output.correction_instruction = fc_out.build_correction_instruction()
            state.regeneration_count = 1
            state.transition(PipelineStage.GENERATING)
            yield make_status_event(PipelineStage.GENERATING, "Cập nhật câu trả lời...")

            regen_out = self._run_generator_with_retry(state, is_regen=True)

            if regen_out is None:
                # FM07 trong regeneration
                state.degrade_level = DegradeLevel.L2_CAVEAT
            else:
                state.generator_output = regen_out
                # Không chạy lại Fact Check sau regen — FM04 kích hoạt nếu lần này cũng fail
                state.transition(PipelineStage.FACT_CHECKING)
                fc_out2 = self._checker.run(state)
                state.fact_check_output = fc_out2

                if fc_out2.has_critical:
                    # FM04: fail x2 → L2 degrade với caveat
                    logger.warning(
                        "FM04: fact check failed twice — L2 degrade (request_id=%s)",
                        state.request_id,
                    )
                    state.log_fallback("FM04", "l2_degrade", success=False)
                    state.degrade_level = DegradeLevel.L2_CAVEAT
                    # Chạy lại generator với caveat instruction
                    state.transition(PipelineStage.GENERATING)
                    l2_out = self._generator.run_degrade_l2(state)
                    state.generator_output = l2_out
                    state.transition(PipelineStage.FACT_CHECKING)
                    # Không check thêm lần nữa — accept L2 output
                    state.degrade_level = DegradeLevel.L2_CAVEAT
                    # Re-emit answer với caveat
                    yield SSEEvent(
                        event=SSEEventType.ANSWER,
                        data={"text": l2_out.answer_text, "degrade": "L2"},
                    )
                else:
                    state.degrade_level = DegradeLevel.L1_FULL
                    state.log_fallback("FM03", "regenerate", success=True)
                    # Re-emit updated answer
                    yield SSEEvent(
                        event=SSEEventType.ANSWER,
                        data={"text": regen_out.answer_text, "regenerated": True},
                    )

        elif fc_out.has_critical:
            # FM04: đây là lần regenerate thứ 2 (regeneration_count đã = 1)
            # → L2 degrade
            state.log_fallback("FM04", "l2_degrade", success=False)
            state.degrade_level = DegradeLevel.L2_CAVEAT
            l2_out = self._generator.run_degrade_l2(state)
            state.generator_output = l2_out
            yield SSEEvent(
                event=SSEEventType.ANSWER,
                data={"text": l2_out.answer_text, "degrade": "L2"},
            )
        else:
            # Only warnings — L1 Full
            state.degrade_level = DegradeLevel.L1_FULL

        # ── DONE ────────────────────────────────────────────────────────────
        state.current_stage = PipelineStage.DONE
        state.finished_at = time.monotonic()
        yield make_done_event(state)

        logger.info(
            "Pipeline DONE | request_id=%s | latency=%dms | degrade=%s | fallbacks=%d",
            state.request_id,
            state.total_latency_ms(),
            state.degrade_level.name,
            len(state.fallback_log),
        )

    # ── Generator runner ─────────────────────────────────────────────────────────

    def _run_generator_with_retry(
        self,
        state: PipelineState,
        is_regen: bool = False,
    ) -> Optional[object]:
        """
        Chạy GeneratorStage với timeout handling.

        Returns GeneratorOutput hoặc None nếu fail.
        """
        t0 = time.monotonic()
        try:
            gen_out = self._generator.run(state)
            state.record_stage_latency(
                "GENERATING" if not is_regen else "REGENERATING",
                time.monotonic() - t0,
            )
            return gen_out
        except TimeoutError as exc:
            logger.error("Generator timeout: %s", exc)
            state.log_fallback("FM07", "generator_timeout", success=False)
            state.fail(str(exc), DegradeLevel.L3_EXPLAIN)
            return None
        except RuntimeError as exc:
            logger.error("Generator runtime error: %s", exc)
            state.log_fallback("FM07", "generator_error", success=False)
            state.fail(str(exc), DegradeLevel.L3_EXPLAIN)
            return None
        except Exception as exc:
            logger.error("Generator unexpected error: %s", exc)
            state.log_fallback("FM07", "generator_error", success=False)
            state.fail(str(exc), DegradeLevel.L3_EXPLAIN)
            return None
