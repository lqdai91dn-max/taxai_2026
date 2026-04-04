"""
src/agent/schemas.py — Data Contracts cho TaxAI Pipeline

Pydantic models định nghĩa cấu trúc dữ liệu luân chuyển giữa các stage.
Mọi inter-stage communication phải qua các schemas này — không truyền raw dict.

Pipeline stages:
  Stage 1  Router        → RouterOutput
  Stage 2  Retrieval     → RetrievalOutput
  Stage 2b Calculation   → CalcOutput
  Stage 3  Generator     → GeneratorOutput
  Stage 4  Fact Checker  → FactCheckOutput
  Overall  State Machine → PipelineState (bao gồm tất cả)

Failure Modes được xử lý:
  FM01 Router sai scope          FM05 Calculator extraction fail
  FM02a True OOD                 FM06 Miss scope phụ (phrase safety net)
  FM02b Corpus Gap               FM07 API timeout
  FM03 Generator hallucination   FM08 Conflicting chunks
  FM04 Fact check fail x2
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Timeout constants ─────────────────────────────────────────────────────────

REQUEST_TIMEOUT = 35.0  # giây — toàn bộ pipeline

DEFAULT_STAGE_TIMEOUTS: Dict[str, float] = {
    "ROUTING":       5.0,   # rule-based nhanh; LLM fallback cần thêm buffer
    "RETRIEVING":    5.0,   # BM25 + vector + RRF
    "CALCULATING":   3.0,   # deterministic tool call
    "GENERATING":   20.0,   # Gemini 2.5 Flash: thực tế 5–20s
    "FACT_CHECKING": 3.0,   # deterministic substring match
}

# legal_level từ document_type — dùng cho FM08 conflict resolution
# Không cần re-index: document_type đã có sẵn trong mọi chunk metadata
DOCUMENT_TYPE_LEVEL: Dict[str, int] = {
    "Luật":       1,
    "Nghị quyết": 2,
    "Nghị định":  3,
    "Thông tư":   4,
    "Công văn":   5,
}


def legal_level_from_doc_type(doc_type: str) -> int:
    """Derive legal hierarchy level từ document_type metadata."""
    return DOCUMENT_TYPE_LEVEL.get(doc_type, 99)


# ── Stage 0: Pipeline State Machine ──────────────────────────────────────────

class PipelineStage(str, Enum):
    """Formal state machine — transitions chỉ theo chiều tiến (không backward)."""
    ROUTING       = "ROUTING"
    RETRIEVING    = "RETRIEVING"
    CALCULATING   = "CALCULATING"
    GENERATING    = "GENERATING"
    FACT_CHECKING = "FACT_CHECKING"
    DONE          = "DONE"
    FAILED        = "FAILED"


# Allowed transitions: stage → next stages
STAGE_TRANSITIONS: Dict[PipelineStage, List[PipelineStage]] = {
    PipelineStage.ROUTING:       [PipelineStage.RETRIEVING, PipelineStage.FAILED],
    PipelineStage.RETRIEVING:    [PipelineStage.CALCULATING, PipelineStage.GENERATING, PipelineStage.FAILED],
    PipelineStage.CALCULATING:   [PipelineStage.GENERATING, PipelineStage.FAILED],
    PipelineStage.GENERATING:    [PipelineStage.FACT_CHECKING, PipelineStage.FAILED],
    PipelineStage.FACT_CHECKING: [PipelineStage.DONE, PipelineStage.GENERATING, PipelineStage.FAILED],
    PipelineStage.DONE:          [],
    PipelineStage.FAILED:        [],
}


# ── Stage 1: Router ───────────────────────────────────────────────────────────

class QueryType(str, Enum):
    """
    Loại câu hỏi — ảnh hưởng đến retrieval strategy và specialist prompt.

    OOD      → FM02a: short-circuit ngay tại Stage 1, không qua Retrieval
    AMBIGUOUS → trả lời cả hai interpretation, không CLARIFY_HARD
               trừ khi hai interpretation dẫn đến nghĩa vụ khác nhau căn bản
    """
    CALCULATION  = "CALCULATION"   # Cần tính số cụ thể
    LEGAL_LOOKUP = "LEGAL_LOOKUP"  # Tra cứu điều luật cụ thể
    PROCEDURE    = "PROCEDURE"     # Hỏi thủ tục, hồ sơ, deadline
    ELIGIBILITY  = "ELIGIBILITY"   # "Có phải/có được/có bị...?" → cần mandatory pairs
    GENERAL      = "GENERAL"       # Giải thích quy định chung
    AMBIGUOUS    = "AMBIGUOUS"     # Nhiều interpretation → cover cả hai
    OOD          = "OOD"           # Ngoài phạm vi thuế VN → FM02a


class RetrievalQuery(BaseModel):
    """Một query retrieval kèm tham số tùy chọn."""
    query:      str
    doc_filter: Optional[str] = None   # doc_id cụ thể nếu biết rõ nguồn
    top_k:      int = 5


class RouterOutput(BaseModel):
    """
    Output của Stage 1 Router.

    Quyết định:
    - Loại câu hỏi (query_type)
    - Phạm vi pháp luật (scopes) — sau khi áp FM06 phrase safety net
    - Các query cần chạy (retrieval_queries)
    - Có cần clarification không (CLARIFY_SOFT chỉ dùng cho CALCULATION thiếu số)
    - Mandatory pair retrieval (cho ELIGIBILITY)
    - Calculator tool cần gọi (nếu CALCULATION)
    - Ambiguous interpretations (nếu AMBIGUOUS)

    Không dùng LLM confidence score — sử dụng reasoning string để debug.
    """
    query_type:               QueryType
    scopes:                   List[str]            = Field(default_factory=list)
    retrieval_queries:        List[RetrievalQuery] = Field(default_factory=list)
    mandatory_pairs:          bool                 = False
    clarify_needed:           bool                 = False
    clarify_question:         Optional[str]        = None
    calc_tool:                Optional[str]        = None
    ambiguous_interpretations: List[str]           = Field(default_factory=list)
    reasoning:                str                  = ""


# ── Stage 2: Retrieval ────────────────────────────────────────────────────────

class RetrievedChunk(BaseModel):
    """
    Một chunk kết quả từ hybrid search.

    legal_level và effective_date được populate từ metadata để dùng
    cho FM08 conflict resolution mà không cần truy cập metadata dict.
    """
    chunk_id:       str
    doc_id:         str
    text:           str
    score:          float
    metadata:       Dict[str, Any] = Field(default_factory=dict)
    # Convenience fields cho FM08 — populate khi build chunk
    legal_level:    int  = 99    # derive từ document_type; 99 = unknown
    effective_date: str  = ""    # "YYYY-MM-DD" hoặc rỗng


class ConflictPair(BaseModel):
    """Một cặp chunks mâu thuẫn nhau (FM08)."""
    chunk_id_a:     str
    chunk_id_b:     str
    winner_id:      str   # chunk_id thắng sau resolution
    reason:         str   # "newer_date" | "higher_level" | "unresolved"


class RetrievalOutput(BaseModel):
    """
    Output của Stage 2 Retrieval.

    Gom tất cả chunks từ nhiều queries (deduplicated by chunk_id).

    FM01: scope_mismatch=True → Stage 2 đã expand scope
    FM02b: zero_results=True → không tìm thấy gì, dừng pipeline
    FM08: conflicts → danh sách cặp mâu thuẫn đã resolve
    """
    chunks:         List[RetrievedChunk] = Field(default_factory=list)
    query_count:    int  = 0
    zero_results:   bool = False
    # FM01
    scope_mismatch: bool = False   # Router scopes ≠ retrieved doc_ids
    scope_expanded: bool = False   # Đã retry với broader scope
    # FM08
    conflicts:      List[ConflictPair] = Field(default_factory=list)
    has_conflict:   bool = False


# ── Stage 2b: Calculation ─────────────────────────────────────────────────────

class CalcParamStatus(str, Enum):
    """Trạng thái của tham số calculator."""
    OK        = "OK"       # Parse được, hợp lệ
    ASSUMED   = "ASSUMED"  # Default assumption (FLEXIBLE param)
    MISSING   = "MISSING"  # Thiếu tham số STRICT → cần clarify


class CalcOutput(BaseModel):
    """
    Output của calculator tools.

    Được stream sớm qua SSE event "calculation" trước khi Generator chạy xong.
    FM05: error + missing_params → CLARIFY_SOFT
    """
    tool_name:      str
    result:         Dict[str, Any] = Field(default_factory=dict)
    formatted:      str  = ""
    error:          Optional[str] = None
    # FM05
    missing_params: List[str]          = Field(default_factory=list)
    assumed_params: Dict[str, Any]     = Field(default_factory=dict)
    param_status:   Dict[str, CalcParamStatus] = Field(default_factory=dict)


# ── Stage 3: Generator ────────────────────────────────────────────────────────

class Citation(BaseModel):
    """Một trích dẫn nguồn pháp lý trong câu trả lời."""
    doc_id:   str
    article:  str        # "Điều 8 Khoản 3"
    text:     str        # Snippet nội dung
    label:    str        # "Luật 109/2025/QH15"
    chunk_id: str = ""   # chunk_id từ retrieved chunks — dùng cho citation validation


class GeneratorOutput(BaseModel):
    """
    Output của Stage 3 Generator.

    key_facts: danh sách fact substring cần Stage 4 verify trong chunks.
               Chỉ include facts LẤY TỪ chunks — không include facts Generator tự biết.
    FM03: nếu key_fact không tìm thấy trong chunks → hallucination → regenerate.
    """
    answer_text:            str
    citations:              List[Citation] = Field(default_factory=list)
    key_facts:              List[str]      = Field(default_factory=list)
    correction_instruction: Optional[str] = None   # Populated bởi Fact Checker


# ── Stage 4: Fact Checker ─────────────────────────────────────────────────────

class FactIssue(BaseModel):
    """Một lỗi fact được phát hiện bởi Fact Checker."""
    severity:    str            # "critical" | "warning"
    key_fact:    str            # Fact bị lỗi
    description: str
    chunk_id:    Optional[str] = None


class FactCheckOutput(BaseModel):
    """
    Output của Stage 4 Fact Checker.

    passed=True            → DONE (Level 1)
    has_critical + regen=0 → regenerate với negative constraint (FM03)
    has_critical + regen=1 → Level 2 degrade (FM04)
    only warnings          → DONE với warnings logged (Level 1)
    """
    passed:       bool
    issues:       List[FactIssue] = Field(default_factory=list)
    has_critical: bool = False

    @property
    def needs_regeneration(self) -> bool:
        return not self.passed and self.has_critical

    def build_correction_instruction(self) -> str:
        """Tạo negative constraint instruction cho regeneration (FM03)."""
        bad_facts = [i.key_fact for i in self.issues if i.severity == "critical"]
        if not bad_facts:
            return ""
        fact_list = "\n".join(f"  - {f}" for f in bad_facts)
        return (
            "BẢN NHÁP TRƯỚC BỊ LOẠI DO VI PHẠM QUY TẮC.\n"
            f"TUYỆT ĐỐI KHÔNG đề cập các thông tin sau:\n{fact_list}\n"
            "Chỉ sử dụng dữ liệu có trong chunks được cung cấp.\n"
            "Nếu không đủ cơ sở pháp lý → viết đúng câu: "
            "'Không đủ cơ sở pháp lý để xác định [vấn đề].'"
        )


# ── Degrade level ─────────────────────────────────────────────────────────────

class DegradeLevel(int, Enum):
    """
    3-level degrade framework — mọi FM đều map về một trong 3 level này.

    L1  Full answer       — Fact Check passed
    L2  Answer + caveat   — thiếu data / FM04 sau retry
    L3  No answer         — FM02a OOD / FM02b Corpus Gap / FM07 API fail hoàn toàn
    """
    L1_FULL    = 1
    L2_CAVEAT  = 2
    L3_EXPLAIN = 3


# ── Structured Fallback Logging ───────────────────────────────────────────────

class FallbackLog(BaseModel):
    """
    Log chuẩn hóa cho mọi FM trigger.
    Dùng để phân tích FM nào xảy ra nhiều nhất sau khi deploy.
    """
    fm_id:      str             # "FM01", "FM02b", ...
    stage:      str             # PipelineStage value
    action:     str             # "scope_expand", "retry_no_filter", "regenerate", ...
    success:    bool
    latency_ms: float = 0.0
    details:    Dict[str, Any] = Field(default_factory=dict)


# ── Overall Pipeline State ────────────────────────────────────────────────────

class PipelineState(BaseModel):
    """
    Trạng thái tổng thể của pipeline — single source of truth.

    Truyền qua tất cả stages, mỗi stage populate field của mình.
    Không dùng global state — mỗi request có PipelineState riêng.
    """
    # ── Input ────────────────────────────────────────────────────────────
    query:      str
    request_id: str  = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id: Optional[str] = None
    user_id:    Optional[str] = None

    # ── State machine ────────────────────────────────────────────────────
    current_stage: PipelineStage = PipelineStage.ROUTING
    stage_history: List[str]     = Field(default_factory=list)

    # ── Stage outputs ────────────────────────────────────────────────────
    router_output:     Optional[RouterOutput]    = None
    retrieval_output:  Optional[RetrievalOutput] = None
    calc_output:       Optional[CalcOutput]      = None
    generator_output:  Optional[GeneratorOutput] = None
    fact_check_output: Optional[FactCheckOutput] = None

    # ── Control flow ─────────────────────────────────────────────────────
    regeneration_count: int           = 0      # max 1
    degrade_level:      DegradeLevel  = DegradeLevel.L1_FULL
    error:              Optional[str] = None
    error_stage:        Optional[str] = None

    # ── Timeout & cancellation ───────────────────────────────────────────
    started_at:     float = Field(default_factory=time.monotonic)
    finished_at:    float = 0.0
    deadline:       float = 0.0   # monotonic timestamp; 0 = no deadline
    stage_timeouts: Dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_STAGE_TIMEOUTS)
    )
    is_cancelled:   bool = False

    # ── Observability ─────────────────────────────────────────────────────
    latency_per_stage: Dict[str, float] = Field(default_factory=dict)
    tokens_used:       Optional[int]    = None   # best-effort từ Gemini usage_metadata
    fallback_log:      List[FallbackLog] = Field(default_factory=list)
    retrieved_doc_ids: List[str]        = Field(default_factory=list)  # Stage 2 output → eval diagnostic

    # ── State machine methods ─────────────────────────────────────────────

    def transition(self, next_stage: PipelineStage) -> None:
        """Validate và thực hiện state transition."""
        if self.is_cancelled:
            raise CancelledError(f"Pipeline cancelled at {self.current_stage}")
        allowed = STAGE_TRANSITIONS.get(self.current_stage, [])
        if next_stage not in allowed:
            raise ValueError(
                f"Invalid transition: {self.current_stage} → {next_stage}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        self.stage_history.append(self.current_stage.value)
        self.current_stage = next_stage

    def fail(self, error: str, degrade: DegradeLevel = DegradeLevel.L3_EXPLAIN) -> None:
        """Chuyển pipeline sang FAILED."""
        self.error         = error
        self.error_stage   = self.current_stage.value
        self.degrade_level = degrade
        self.stage_history.append(self.current_stage.value)
        self.current_stage = PipelineStage.FAILED
        self.finished_at   = time.monotonic()

    def cancel(self) -> None:
        """Huỷ pipeline — dùng khi client disconnect."""
        self.is_cancelled = True

    def is_timed_out(self) -> bool:
        """Kiểm tra request đã quá deadline chưa."""
        if self.deadline == 0.0:
            return False
        return time.monotonic() > self.deadline

    def stage_timed_out(self) -> bool:
        """Kiểm tra stage hiện tại đã quá stage_timeout chưa."""
        stage_name = self.current_stage.value
        timeout    = self.stage_timeouts.get(stage_name, 0.0)
        if timeout == 0.0:
            return False
        stage_start = self._stage_start_time()
        return (time.monotonic() - stage_start) > timeout

    def _stage_start_time(self) -> float:
        """Estimate thời điểm stage hiện tại bắt đầu từ latency_per_stage."""
        elapsed = sum(self.latency_per_stage.values())
        return self.started_at + elapsed

    # ── Observability helpers ─────────────────────────────────────────────

    def record_stage_latency(self, stage: str, latency_s: float) -> None:
        self.latency_per_stage[stage] = round(latency_s, 3)

    def log_fallback(
        self,
        fm_id: str,
        action: str,
        success: bool,
        latency_ms: float = 0.0,
        **details: Any,
    ) -> None:
        """Ghi nhận một FM trigger vào fallback_log."""
        self.fallback_log.append(FallbackLog(
            fm_id      = fm_id,
            stage      = self.current_stage.value,
            action     = action,
            success    = success,
            latency_ms = latency_ms,
            details    = details,
        ))

    def total_latency_ms(self) -> int:
        if self.finished_at > 0:
            return int((self.finished_at - self.started_at) * 1000)
        return int((time.monotonic() - self.started_at) * 1000)


# ── Custom exceptions ─────────────────────────────────────────────────────────

class CancelledError(Exception):
    """Pipeline bị huỷ do client disconnect hoặc timeout."""


class StageTimeoutError(Exception):
    """Stage vượt quá stage_timeout."""
    def __init__(self, stage: str, limit_s: float):
        super().__init__(f"Stage {stage} exceeded {limit_s}s timeout")
        self.stage   = stage
        self.limit_s = limit_s


# ── SSE Event Protocol ────────────────────────────────────────────────────────

class SSEEventType(str, Enum):
    """
    Server-Sent Events protocol cho Progressive Disclosure UI.

    Thứ tự emit chuẩn:
      status(ROUTING)
      → status(RETRIEVING)
      → calculation  (nếu có — stream sớm trước answer)
      → status(GENERATING)
      → status(FACT_CHECKING)
      → answer
      → sources
      → done
    """
    STATUS      = "status"       # Pipeline stage update (FM debug + UX)
    CALCULATION = "calculation"  # Kết quả tính toán — stream sớm
    ANSWER      = "answer"       # Final answer text
    SOURCES     = "sources"      # Citations
    ERROR       = "error"        # FM02a, FM02b, FM07
    DONE        = "done"         # Kèm latency + degrade_level


class SSEEvent(BaseModel):
    """Một SSE event emit ra client."""
    event: SSEEventType
    data:  Dict[str, Any]

    def to_sse_str(self) -> str:
        """Format chuẩn SSE: 'event: X\\ndata: {...}\\n\\n'"""
        import json
        return (
            f"event: {self.event.value}\n"
            f"data: {json.dumps(self.data, ensure_ascii=False)}\n\n"
        )


# ── SSE Factory helpers ───────────────────────────────────────────────────────

def make_status_event(stage: PipelineStage, message: str = "") -> SSEEvent:
    return SSEEvent(
        event=SSEEventType.STATUS,
        data={"stage": stage.value, "message": message},
    )


def make_error_event(error: str, stage: str = "", fm_id: str = "") -> SSEEvent:
    return SSEEvent(
        event=SSEEventType.ERROR,
        data={"error": error, "stage": stage, "fm_id": fm_id},
    )


def make_corpus_gap_event(scope: str, corpus_date: str) -> SSEEvent:
    """FM02b — corpus gap, không phải OOD."""
    return SSEEvent(
        event=SSEEventType.ERROR,
        data={
            "error": (
                f"Câu hỏi thuộc phạm vi {scope}, nhưng cơ sở dữ liệu "
                f"hiện tại (cập nhật đến {corpus_date}) chưa có văn bản "
                "chi tiết cho trường hợp này."
            ),
            "fm_id": "FM02b",
            "stage": "RETRIEVING",
        },
    )


def make_done_event(state: PipelineState) -> SSEEvent:
    # FM breakdown: {fm_id: count} — dùng để debug post-benchmark
    fm_breakdown: Dict[str, int] = {}
    for entry in state.fallback_log:
        fm_breakdown[entry.fm_id] = fm_breakdown.get(entry.fm_id, 0) + 1

    return SSEEvent(
        event=SSEEventType.DONE,
        data={
            "latency_ms":        state.total_latency_ms(),
            "stages":            state.stage_history,
            "degrade_level":     state.degrade_level.value,
            "latency_per_stage": state.latency_per_stage,
            "fallback_count":    len(state.fallback_log),
            "fm_breakdown":      fm_breakdown,
            "request_id":        state.request_id,
            "retrieved_doc_ids": state.retrieved_doc_ids,
        },
    )
