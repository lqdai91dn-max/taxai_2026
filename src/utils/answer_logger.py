"""
src/utils/answer_logger.py — Structured Logging cho mỗi câu trả lời của TaxAI.

Mỗi log entry là 1 dòng JSON (JSON Lines format) ghi vào data/logs/answers.jsonl.
Mục đích:
  - Phân tích chất lượng câu trả lời offline
  - Phát hiện pattern lỗi (confidence=fail, fact_check=warning)
  - Foundation cho fine-tuning / evaluation sau này

Chi phí: 0 API call, <1ms latency.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Thư mục log — tạo nếu chưa có
_LOG_DIR  = Path(__file__).resolve().parents[2] / "data" / "logs"
_LOG_FILE = _LOG_DIR / "answers.jsonl"


def log_answer(
    question: str,
    answer:   str,
    tool_calls: list[dict],
    confidence: dict,
    fact_check: dict,
    *,
    model:      str = "",
    iterations: int = 0,
    latency_ms: float = 0.0,
) -> None:
    """
    Ghi 1 dòng JSON vào answers.jsonl.

    Entry schema:
        ts          — ISO-8601 UTC timestamp
        question    — câu hỏi gốc
        answer_len  — độ dài câu trả lời (ký tự)
        model       — model ID
        iterations  — số vòng lặp tool calling
        latency_ms  — thời gian trả lời (ms)
        confidence  — {level, tool_searched, found_results, has_citation}
        fact_check  — {level, passed, issues, numeric_checked, numeric_matched}
        tools_used  — danh sách tên tool đã gọi
        top_chunks  — top 3 chunk id từ search_legal_docs (nếu có)
        top_scores  — RRF scores tương ứng
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Lấy danh sách tools đã gọi
    tools_used = [tc.get("tool", "") for tc in tool_calls]

    # Lấy top chunks từ search_legal_docs results
    top_chunks: list[str]  = []
    top_scores: list[float] = []
    for tc in tool_calls:
        if tc.get("tool") != "search_legal_docs":
            continue
        for hit in tc.get("result", {}).get("results", [])[:3]:
            cid   = hit.get("citation", {}).get("breadcrumb", hit.get("chunk_id", ""))
            score = hit.get("score", 0.0)
            top_chunks.append(str(cid))
            top_scores.append(round(float(score), 4))
        break  # chỉ lấy từ search call đầu tiên

    entry = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "question":    question,
        "answer_len":  len(answer),
        "model":       model,
        "iterations":  iterations,
        "latency_ms":  round(latency_ms, 1),
        "confidence":  confidence,
        "fact_check":  fact_check,
        "tools_used":  tools_used,
        "top_chunks":  top_chunks[:3],
        "top_scores":  top_scores[:3],
    }

    try:
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"⚠️ answer_logger: không ghi được log — {e}")


def get_log_path() -> Path:
    """Trả về path file log hiện tại."""
    return _LOG_FILE
