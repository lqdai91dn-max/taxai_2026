"""
src/agent/pipeline_v4/state.py — PipelineState + State Immutability Lock

PipelineState giữ toàn bộ kết quả từng bước của pipeline v4.
State Immutability Lock: sau khi step 6.2 (Python Calculator) hoàn thành,
field tax_amount bị LOCK — LLM Synthesizer (6.3) KHÔNG được thay đổi.

Design:
  - Dataclass với finalized: bool flag
  - Mọi attempt ghi vào tax_amount sau finalized=True → raise ImmutableStateError
  - Audit trail ghi mọi state transition
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ImmutableStateError(RuntimeError):
    """Raised khi cố ghi vào locked state."""


@dataclass
class CalcOutput:
    """Output từ Python Calculator (step 6.2) — LOCKED sau khi set."""
    template:       str
    version:        str
    tax_amount:     int          # VND, rounded per law
    breakdown:      Any          # list[dict]
    effective_rate: float
    citations:      List[dict]   = field(default_factory=list)
    warnings:       List[str]    = field(default_factory=list)
    assumptions:    List[str]    = field(default_factory=list)


@dataclass
class LegalReasonerOutput:
    """Output từ LLM Legal Reasoner (step 6.1)."""
    template_type:        str                    # e.g. "HKD_percentage"
    params_validated:     Dict[str, Any]         # {param: {value, source}}
    assumptions:          List[str]              = field(default_factory=list)
    clarification_needed: bool                   = False
    clarification_question: Optional[str]        = None
    scenarios:            List[dict]             = field(default_factory=list)  # max 2
    raw_json:             Optional[str]          = None


@dataclass
class PipelineState:
    """
    Toàn bộ trạng thái pipeline v4 cho một request.

    Lifecycle:
        INIT → RETRIEVED → REASONED → CALCULATED (LOCKED) → SYNTHESIZED → DONE
        Bất kỳ bước nào có thể → FAILED (với degrade_level)

    State Immutability:
        Sau khi finalize() được gọi, calc_output KHÔNG thể thay đổi.
        LLM Synthesizer CHỈ được đọc calc_output để format text.
    """
    # ── Request ──────────────────────────────────────────────────────────────
    question:       str
    session_id:     Optional[str]          = None
    created_at:     float                  = field(default_factory=time.time)

    # ── Stage outputs ─────────────────────────────────────────────────────────
    retrieved_chunks:   List[dict]         = field(default_factory=list)
    retrieved_doc_ids:  List[str]          = field(default_factory=list)
    legal_reasoner_out: Optional[LegalReasonerOutput] = None
    calc_output:        Optional[CalcOutput]           = None
    synthesized_answer: Optional[str]                  = None
    citations:          List[dict]         = field(default_factory=list)

    # ── State control ─────────────────────────────────────────────────────────
    finalized:      bool                   = False   # True sau step 6.2
    degrade_level:  int                    = 1       # 1=normal, 2=simpler, 3=reset, 4=degrade
    retry_count:    int                    = 0
    error:          Optional[str]          = None
    rag_path:       bool                   = False   # True khi query là explain/non-calc

    # ── Audit log entries (chronological) ────────────────────────────────────
    _audit_entries: List[dict]             = field(default_factory=list)

    # ── State Immutability Lock ───────────────────────────────────────────────

    def set_calc_output(self, output: CalcOutput) -> None:
        """
        Set kết quả từ Python Calculator và LOCK state.
        Chỉ được gọi đúng 1 lần (từ step 6.2).
        """
        if self.finalized:
            raise ImmutableStateError(
                "calc_output đã được LOCK — không thể thay đổi tax_amount sau step 6.2. "
                "LLM Synthesizer chỉ được đọc, không được recompute."
            )
        self.calc_output = output
        self.finalized = True
        self._audit("CALC_OUTPUT_LOCKED", {
            "template":   output.template,
            "tax_amount": output.tax_amount,
            "finalized":  True,
        })

    def set_synthesized_answer(self, text: str) -> None:
        """
        Set answer text từ LLM Synthesizer (step 6.3).
        Cho phép set (và retry), nhưng KHÔNG được thay đổi tax_amount.
        """
        if not self.finalized:
            raise RuntimeError(
                "Không thể set synthesized_answer trước khi calc_output được LOCK."
            )
        self.synthesized_answer = text
        self._audit("ANSWER_SYNTHESIZED", {"answer_len": len(text)})

    def unlock_for_rollback(self, reason: str) -> None:
        """
        Mở lock để Rollback Level 1-2 có thể retry với template khác.
        Chỉ được gọi từ Rollback logic, KHÔNG phải từ LLM.
        """
        self.finalized = False
        self.calc_output = None
        self.synthesized_answer = None
        self.retry_count += 1
        self._audit("STATE_UNLOCKED_FOR_ROLLBACK", {
            "reason":      reason,
            "retry_count": self.retry_count,
        })

    # ── Audit ─────────────────────────────────────────────────────────────────

    def _audit(self, event: str, data: Optional[dict] = None) -> None:
        self._audit_entries.append({
            "ts":    time.time(),
            "event": event,
            "data":  data or {},
        })

    def get_audit_trail(self) -> List[dict]:
        return list(self._audit_entries)

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def has_calc_result(self) -> bool:
        return self.calc_output is not None and self.finalized

    @property
    def tax_amount(self) -> Optional[int]:
        """Shortcut cho final tax amount (None nếu chưa calculated)."""
        return self.calc_output.tax_amount if self.calc_output else None

    def to_summary_dict(self) -> dict:
        """Compact summary cho logging + DONE event."""
        return {
            "session_id":     self.session_id,
            "question_len":   len(self.question),
            "finalized":      self.finalized,
            "degrade_level":  self.degrade_level,
            "retry_count":    self.retry_count,
            "tax_amount":     self.tax_amount,
            "template":       self.calc_output.template if self.calc_output else None,
            "doc_ids":        self.retrieved_doc_ids,
            "error":          self.error,
        }
