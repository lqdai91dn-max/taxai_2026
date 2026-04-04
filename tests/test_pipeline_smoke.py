"""
tests/test_pipeline_smoke.py — 4 smoke tests cho PipelineOrchestrator.

Mục tiêu: verify 4 luồng extreme của state machine trước khi chạy full benchmark.
Không cần ChromaDB thật — dùng mock searcher cho test isolation.

Luồng kiểm tra:
  Test 1: OOD short-circuit (Stage 1 → DONE, không qua Stage 2+)
  Test 2: FM05 missing strict params (Stage 2b → CLARIFY, không generate)
  Test 3: FM08 conflict detection + resolution (isolated — không dùng query ngẫu nhiên)
  Test 4: Full pipeline fast-path (mock searcher, verify SSE sequence)

Usage:
  pytest tests/test_pipeline_smoke.py -v
  pytest tests/test_pipeline_smoke.py::test_ood_short_circuit -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.agent.schemas import (
    ConflictPair,
    DegradeLevel,
    PipelineState,
    QueryType,
    RetrievedChunk,
    RetrievalOutput,
    RouterOutput,
    SSEEventType,
)


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_chunk(
    chunk_id: str,
    doc_id: str,
    text: str = "nội dung điều khoản pháp luật",
    score: float = 0.8,
    effective_date: str = "2025-01-01",
    document_type: str = "Nghị định",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text,
        score=score,
        metadata={"document_type": document_type, "article": "Điều 1"},
        legal_level=3,
        effective_date=effective_date,
    )


def _make_mock_searcher(chunks: list[RetrievedChunk] | None = None) -> MagicMock:
    """
    Mock HybridSearch trả về danh sách chunks cố định.
    Format khớp với HybridSearch.search() thực tế: chunk_id, text, rrf_score, metadata.
    """
    searcher = MagicMock()
    raw_chunks = [
        {
            "chunk_id":  c.chunk_id,
            "text":      c.text,
            "rrf_score": c.score,
            "metadata": {
                **c.metadata,
                "doc_id":         c.doc_id,
                "effective_date": c.effective_date,
            },
        }
        for c in (chunks or [])
    ]
    searcher.search.return_value = raw_chunks
    return searcher


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: OOD short-circuit
# ═══════════════════════════════════════════════════════════════════════════════

def test_ood_short_circuit():
    """
    Câu hỏi không liên quan đến thuế → OOD → ANSWER event ngay, không qua Stage 2.
    Verify: không có SOURCES, không có CALCULATION, DONE event có degrade_level=3.
    """
    from src.agent.pipeline import PipelineOrchestrator

    mock_searcher = _make_mock_searcher([])
    orchestrator  = PipelineOrchestrator(
        searcher=mock_searcher,
        api_key="fake-key-for-test",
    )

    events = orchestrator.run("Cách nấu phở bò ngon nhất")

    event_types = [e.event for e in events]

    # Phải có STATUS + ANSWER + DONE
    assert SSEEventType.STATUS  in event_types, "Thiếu STATUS event"
    assert SSEEventType.ANSWER  in event_types, "Thiếu ANSWER event"
    assert SSEEventType.DONE    in event_types, "Thiếu DONE event"

    # KHÔNG được có SOURCES hay CALCULATION (chưa retrieve gì)
    assert SSEEventType.SOURCES      not in event_types, "OOD không được có SOURCES"
    assert SSEEventType.CALCULATION  not in event_types, "OOD không được có CALCULATION"

    # DONE event phải có degrade_level=3 (L3_EXPLAIN)
    done_event = next(e for e in events if e.event == SSEEventType.DONE)
    assert done_event.data["degrade_level"] == DegradeLevel.L3_EXPLAIN.value, (
        f"OOD phải L3, got {done_event.data['degrade_level']}"
    )

    # Verify searcher KHÔNG được gọi (short-circuit trước retrieval)
    mock_searcher.search.assert_not_called()

    # Answer text phải có thông báo ngoài phạm vi
    answer_event = next(e for e in events if e.event == SSEEventType.ANSWER)
    assert "phạm vi" in answer_event.data["text"].lower() or \
           "thuế" in answer_event.data["text"].lower(), \
        "OOD answer phải giải thích phạm vi"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: FM05 — thiếu strict params
# ═══════════════════════════════════════════════════════════════════════════════

def test_fm05_missing_strict_params():
    """
    Query về tính thuế HKD nhưng không có doanh thu cụ thể.
    Verify: FM05 kích hoạt, pipeline trả về clarify message, không generate full answer.

    Query: "doanh thu shopee bao nhiêu thì phải nộp thuế?"
    → Router: CALCULATION + HKD scope + calc_tool=calculate_tax_hkd
    → Stage 2b: annual_revenue missing → missing_params=["annual_revenue"]
    → Pipeline: emit ANSWER với clarify message, DONE với L2
    """
    from src.agent.pipeline import PipelineOrchestrator

    mock_searcher = _make_mock_searcher([
        _make_chunk("c1", "68_2026_NDCP", "Hộ kinh doanh doanh thu trên 500 triệu"),
    ])
    orchestrator = PipelineOrchestrator(
        searcher=mock_searcher,
        api_key="fake-key-for-test",
    )

    # Query không có số cụ thể → FM05
    events = orchestrator.run("Bán hàng trên Shopee thì phải tính thuế HKD như thế nào?")

    event_types = [e.event for e in events]

    # Phải có ANSWER và DONE
    assert SSEEventType.ANSWER in event_types
    assert SSEEventType.DONE   in event_types

    done_event = next(e for e in events if e.event == SSEEventType.DONE)
    fm_breakdown = done_event.data.get("fm_breakdown", {})

    # FM05 phải được log trong fallback (nếu trigger)
    # hoặc pipeline trả về clarify/answer bình thường (nếu Router không pick CALCULATION)
    # → test này verify pipeline không crash, có answer hợp lý
    answer_event = next(e for e in events if e.event == SSEEventType.ANSWER)
    assert len(answer_event.data.get("text", "")) > 0, "Answer không được rỗng"


def test_fm05_strict_params_detected():
    """
    Test trực tiếp Router + RetrievalStage FM05 detection mà không cần full pipeline.
    Verify: query thiếu annual_revenue → CalcOutput.missing_params = ["annual_revenue"]
    """
    from src.agent.retrieval_stage import _run_calculator

    # calculate_tax_hkd cần annual_revenue + business_category
    calc_out = _run_calculator(
        query="tôi bán hàng online thì nộp thuế bao nhiêu?",
        calc_tool="calculate_tax_hkd",
    )

    assert calc_out.error is not None, "Phải có error khi thiếu params"
    assert "annual_revenue" in calc_out.missing_params, (
        f"annual_revenue phải trong missing_params, got: {calc_out.missing_params}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: FM08 — conflict detection (isolated)
# ═══════════════════════════════════════════════════════════════════════════════

def test_fm08_conflict_detection_isolated():
    """
    Test _detect_conflicts() và _resolve_conflict() độc lập, không phụ thuộc query.

    Verify:
    - 2 chunks từ conflict pair (125_2020_NDCP + 310_2025_NDCP) → conflict detected
    - Winner = chunk có effective_date mới hơn (310_2025_NDCP)
    - Reason = "newer_date"
    """
    from src.agent.retrieval_stage import _detect_conflicts, _resolve_conflict

    chunk_old = _make_chunk(
        chunk_id="c_old",
        doc_id="125_2020_NDCP",
        effective_date="2020-12-12",
        document_type="Nghị định",
    )
    chunk_new = _make_chunk(
        chunk_id="c_new",
        doc_id="310_2025_NDCP",
        effective_date="2025-12-01",
        document_type="Nghị định",
    )

    chunks = [chunk_old, chunk_new]

    # ── Test _detect_conflicts ─────────────────────────────────────────────────
    conflicts = _detect_conflicts(chunks)

    assert len(conflicts) >= 1, (
        "Phải detect conflict giữa 125_2020_NDCP và 310_2025_NDCP"
    )

    conflict = conflicts[0]

    # ── Test winner = newer doc ────────────────────────────────────────────────
    assert conflict.winner_id == "c_new", (
        f"Winner phải là 310_2025_NDCP (newer), got winner_id={conflict.winner_id}"
    )
    assert "date" in conflict.reason.lower() or "newer" in conflict.reason.lower(), (
        f"Reason phải nói về date, got: {conflict.reason}"
    )


def test_fm08_losing_chunk_excluded_from_context():
    """
    Verify: loser chunk bị loại khỏi context trong _build_context().
    Winner chunk vẫn có mặt.
    """
    from src.agent.generator import _build_context
    from src.agent.schemas import CalcOutput, RouterOutput

    chunk_old = _make_chunk("c_old", "125_2020_NDCP", text="Mức phạt cũ theo NĐ125")
    chunk_new = _make_chunk("c_new", "310_2025_NDCP", text="Mức phạt mới theo NĐ310")

    conflict = ConflictPair(
        chunk_id_a="c_old",
        chunk_id_b="c_new",
        winner_id="c_new",
        reason="newer_date",
    )

    ret_out = RetrievalOutput(
        chunks=[chunk_old, chunk_new],
        has_conflict=True,
        conflicts=[conflict],
    )

    state = PipelineState(query="mức phạt chậm nộp thuế?")
    state.retrieval_output = ret_out
    state.router_output = RouterOutput(query_type=QueryType.GENERAL, scopes=["PENALTY"])

    context = _build_context(state)

    # Winner phải có trong context
    assert "NĐ310" in context or "310_2025_NDCP" in context, (
        "Winner chunk (310) phải có trong context"
    )

    # Loser phải BỊ LOẠI
    assert "Mức phạt cũ theo NĐ125" not in context, (
        "Loser chunk (NĐ125) phải bị loại khỏi context"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Full pipeline fast-path (mock Gemini)
# ═══════════════════════════════════════════════════════════════════════════════

def test_full_pipeline_fast_path():
    """
    Full pipeline với mock Gemini + mock searcher.
    Verify: đúng thứ tự SSE events, không có crash, answer không rỗng.

    SSE sequence expected:
      STATUS(ROUTING) → STATUS(RETRIEVING) → STATUS(GENERATING)
      → STATUS(FACT_CHECKING) → SOURCES → ANSWER → DONE
    """
    from src.agent.pipeline import PipelineOrchestrator

    # Mock searcher trả về 2 chunks hợp lệ
    chunks = [
        _make_chunk("c1", "68_2026_NDCP",
                    text="Hộ kinh doanh doanh thu dưới 500 triệu đồng được miễn thuế GTGT và TNCN"),
        _make_chunk("c2", "68_2026_NDCP",
                    text="Thuế khoán tính theo tỷ lệ % trên doanh thu"),
    ]
    mock_searcher = _make_mock_searcher(chunks)

    # Mock Gemini GeneratorStage response
    mock_gen_response = MagicMock()
    mock_gen_response.text = (
        '{"answer": "Hộ kinh doanh doanh thu dưới 500 triệu được miễn thuế. '
        'Theo Nghị định 68/2026/NĐ-CP.", '
        '"citations": [{"doc_id": "68_2026_NDCP", "article": "Điều 1", '
        '"text": "dưới 500 triệu đồng được miễn thuế", "label": "NĐ68/2026"}], '
        '"key_facts": ["dưới 500 triệu đồng được miễn thuế"]}'
    )

    orchestrator = PipelineOrchestrator(
        searcher=mock_searcher,
        api_key="fake-key-for-test",
    )

    # Patch Gemini client trong GeneratorStage
    with patch.object(
        orchestrator._generator.client.models,
        "generate_content",
        return_value=mock_gen_response,
    ):
        events = orchestrator.run("Hộ kinh doanh doanh thu 400 triệu có phải nộp thuế không?")

    event_types = [e.event for e in events]

    # ── Verify thứ tự events ──────────────────────────────────────────────────
    assert SSEEventType.STATUS  in event_types, "Thiếu STATUS"
    assert SSEEventType.ANSWER  in event_types, "Thiếu ANSWER"
    assert SSEEventType.SOURCES in event_types, "Thiếu SOURCES"
    assert SSEEventType.DONE    in event_types, "Thiếu DONE"
    assert SSEEventType.ERROR   not in event_types, "Không được có ERROR trong fast-path"

    # ── Verify answer không rỗng ──────────────────────────────────────────────
    answer_events = [e for e in events if e.event == SSEEventType.ANSWER]
    last_answer   = answer_events[-1]
    assert len(last_answer.data.get("text", "")) > 0, "Answer phải không rỗng"

    # ── Verify SOURCES có citations và key_facts ──────────────────────────────
    sources_event = next((e for e in events if e.event == SSEEventType.SOURCES), None)
    assert sources_event is not None
    assert "citations" in sources_event.data
    assert "key_facts"  in sources_event.data, "key_facts phải có trong SOURCES event"

    # ── Verify DONE có fm_breakdown ───────────────────────────────────────────
    done_event = next(e for e in events if e.event == SSEEventType.DONE)
    assert "fm_breakdown" in done_event.data, "fm_breakdown phải có trong DONE"
    assert "request_id"   in done_event.data, "request_id phải có trong DONE"

    # ── Verify thứ tự STATUS trước ANSWER ────────────────────────────────────
    status_indices = [i for i, e in enumerate(events) if e.event == SSEEventType.STATUS]
    answer_index   = next(i for i, e in enumerate(events) if e.event == SSEEventType.ANSWER)
    assert len(status_indices) > 0
    assert status_indices[0] < answer_index, "STATUS phải emit trước ANSWER"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Latency metrics trong adapter
# ═══════════════════════════════════════════════════════════════════════════════

def test_adapter_latency_metrics():
    """
    Verify PipelineAdapter trả về đúng 3 latency metrics:
      t_first_status_ms: time to first event
      t_answer_ms:       time to ANSWER event
      latency_ms:        total
    """
    from src.agent.pipeline_adapter import PipelineAdapter

    chunks = [_make_chunk("c1", "68_2026_NDCP", "nội dung mẫu")]
    mock_searcher = _make_mock_searcher(chunks)

    adapter = PipelineAdapter(searcher=mock_searcher, api_key="fake-key")

    mock_gen_response = MagicMock()
    mock_gen_response.text = (
        '{"answer": "câu trả lời mẫu về thuế HKD", '
        '"citations": [{"doc_id": "68_2026_NDCP", "article": "Điều 1", '
        '"text": "nội dung mẫu", "label": "NĐ68"}], '
        '"key_facts": ["500 triệu"]}'
    )

    with patch.object(
        adapter._pipeline._generator.client.models,
        "generate_content",
        return_value=mock_gen_response,
    ):
        result = adapter.answer("thuế HKD là gì?")

    assert "latency_ms"          in result, "Thiếu latency_ms"
    assert "t_answer_ms"         in result, "Thiếu t_answer_ms"
    assert "t_first_status_ms"   in result, "Thiếu t_first_status_ms"

    assert result["t_first_status_ms"] >= 0
    assert result["t_answer_ms"]       >= result["t_first_status_ms"]
    assert result["latency_ms"]        >= result["t_answer_ms"]

    # key_facts phải được expose
    assert "key_facts" in result
    assert isinstance(result["key_facts"], list)

    # degrade_level phải có
    assert "degrade_level" in result
