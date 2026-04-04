"""
src/agent/pipeline_v4/audit.py — Audit Trail (JSONL per session)

Ghi log mọi pipeline run vào file JSONL để debug production.

Format mỗi dòng:
  {
    "ts":          float,        # unix timestamp
    "session_id":  str,
    "question":    str,
    "template":    str | null,
    "tax_amount":  int | null,
    "degrade_level": int,
    "retry_count": int,
    "assumptions": list[str],
    "doc_ids":     list[str],
    "error":       str | null,
    "latency_ms":  int,
    "audit_trail": list[dict],   # PipelineState._audit_entries
  }

Usage:
    audit_logger = AuditLogger("logs/audit.jsonl")
    audit_logger.log(state, latency_ms=1234)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path(__file__).parent.parent.parent.parent / "logs"


class AuditLogger:
    """
    Thread-safe (append-only) JSONL logger cho pipeline runs.

    Mỗi PipelineState → 1 dòng JSON trong audit file.
    File không bao giờ bị truncate — chỉ append.
    """

    def __init__(self, log_path: Optional[str] = None):
        if log_path is None:
            log_path = str(_DEFAULT_LOG_DIR / "pipeline_v4_audit.jsonl")
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, state, latency_ms: int = 0) -> None:
        """
        Ghi một PipelineState vào audit file.

        Args:
            state:       PipelineState đã hoàn thành (hoặc failed).
            latency_ms:  Thời gian xử lý tổng (ms).
        """
        from src.agent.pipeline_v4.state import PipelineState  # avoid circular
        assert isinstance(state, PipelineState)

        entry = {
            "ts":           time.time(),
            "session_id":   state.session_id,
            "question":     state.question[:200],   # truncate cho privacy
            "template":     state.calc_output.template if state.calc_output else None,
            "template_ver": state.calc_output.version if state.calc_output else None,
            "tax_amount":   state.tax_amount,
            "degrade_level": state.degrade_level,
            "retry_count":  state.retry_count,
            "assumptions":  state.calc_output.assumptions if state.calc_output else [],
            "doc_ids":      state.retrieved_doc_ids,
            "citation_count": len(state.citations),
            "error":        state.error,
            "latency_ms":   latency_ms,
            "audit_trail":  state.get_audit_trail(),
        }

        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            # Audit fail KHÔNG được làm crash pipeline
            logger.warning("AuditLogger: không thể ghi log: %s", e)


# Module-level default logger — dùng nếu không cần custom path
_default_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = AuditLogger()
    return _default_logger


def log_pipeline_run(state, latency_ms: int = 0) -> None:
    """Convenience function — ghi log với default logger."""
    get_audit_logger().log(state, latency_ms=latency_ms)
