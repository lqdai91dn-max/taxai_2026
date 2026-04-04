"""
src/agent/pipeline_v4/llm_guard.py — E2E Stabilization Layer

Wraps mọi LLM call trong Pipeline v4 với:
  [G1] JSON auto-retry (3x): strip markdown, fix trailing comma, parse
  [G2] Schema validator: REQUIRED_FIELDS check trước khi xuống Validation Layer
  [G3] Params quality guard: reject empty params, flag missing sources
  [G4] Computation scanner: detect nếu LLM tự tính toán (vi phạm AP3)
  [G5] Structured output: dùng response_schema của Gemini khi có thể

Dùng google.genai SDK (new SDK) nhất quán với generator.py hiện tại.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── JSON Schema cho Legal Reasoner output (step 6.1) ────────────────────────

REQUIRED_FIELDS = ["template_type", "params_validated", "clarification_needed"]

# _NUM: chữ số + suffix tiếng Việt phổ biến (M=triệu, tỷ, nghìn, k)
# Dùng chung cho cả G4-Critical và G4-Warn
_NUM = r"[\d,\.]+(?:\s*(?:triệu|tỷ|nghìn|[MmBbKk]))?"

# ── G4-Critical: formula-as-string trong params_validated.value ──────────────
# Mục tiêu: bắt trường hợp LLM trả "value": "30M × 12" thay vì "value": 360000000
# Chỉ match khi value là string chứa biểu thức toán học — integer/float thực sự không bị block.
_CRITICAL_PATTERNS = [
    rf"{_NUM}\s*[×xX\*]\s*{_NUM}",         # "30M × 12", "1,200,000 * 0.01"
    rf"{_NUM}\s*\/\s*{_NUM}",              # "360 / 12"
    rf"{_NUM}\s*[\+]\s*{_NUM}\s*=",        # "11 + 4.4 ="
    rf"{_NUM}\s*\-\s*{_NUM}\s*=",          # "360 - 132 ="
]
_CRITICAL_RES = [re.compile(p, re.IGNORECASE) for p in _CRITICAL_PATTERNS]

# ── G4-Warn: equation patterns trong assumptions / clarification_question ─────
# Mục tiêu: detect LLM viết bước tính trung gian vào text field (không block, chỉ log)
# Pattern yêu cầu: DIGIT(+suffix) + OPERATOR + DIGIT(+suffix) + DẤU BẰNG
# → tránh false positive: "Xuất - Nhập khẩu", "TNHH 1 thành viên"
_WARN_PATTERNS = [
    rf"{_NUM}\s*[×xX\*]\s*{_NUM}\s*=\s*{_NUM}",   # "30M × 12 = 360M"
    rf"{_NUM}\s*[\+\-]\s*{_NUM}\s*=\s*{_NUM}",     # "11M + 4.4M = 15.4M", "300 - 186 = 114"
    r"tổng\s+thuế\s*[=:]\s*[\d,\.]+",              # "tổng thuế = 18 triệu"
    r"thuế\s+phải\s+nộp\s*[=:]\s*[\d,\.]+",        # "thuế phải nộp = ..."
]
_WARN_RES = [re.compile(p, re.IGNORECASE) for p in _WARN_PATTERNS]


# ─── JSON cleanup helpers ─────────────────────────────────────────────────────

def _strip_markdown(raw: str) -> str:
    """Strip ```json ... ``` hoặc ``` ... ``` wrapper."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _fix_trailing_commas(raw: str) -> str:
    """Xóa trailing comma trước } hoặc ] — lỗi phổ biến của LLM."""
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return raw


def _try_parse_json(raw: str) -> Optional[dict]:
    """Parse JSON với cleanup. Trả về None nếu fail."""
    cleaned = _fix_trailing_commas(_strip_markdown(raw))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Thử extract JSON object từ giữa text
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None


# ─── Schema validator [G2] ────────────────────────────────────────────────────

def validate_reasoner_schema(obj: dict) -> Tuple[bool, List[str]]:
    """
    Check REQUIRED_FIELDS có mặt và đúng type.

    Returns:
        (valid: bool, errors: list[str])
    """
    errors: List[str] = []

    for field in REQUIRED_FIELDS:
        if field not in obj:
            errors.append(f"Thiếu field bắt buộc: '{field}'")

    # Type checks
    if "template_type" in obj and not isinstance(obj["template_type"], str):
        errors.append("template_type phải là string")
    if "params_validated" in obj and not isinstance(obj["params_validated"], dict):
        errors.append("params_validated phải là object/dict")
    if "clarification_needed" in obj and not isinstance(obj["clarification_needed"], bool):
        # Gemini đôi khi trả "true"/"false" string
        val = obj["clarification_needed"]
        if isinstance(val, str) and val.lower() in ("true", "false"):
            obj["clarification_needed"] = val.lower() == "true"
        else:
            errors.append("clarification_needed phải là boolean")

    return len(errors) == 0, errors


# ─── Params quality guard [G3] ───────────────────────────────────────────────

def check_params_quality(
    params_validated: dict,
    template_type: str,
) -> Tuple[bool, str]:
    """
    Kiểm tra chất lượng params từ LLM Legal Reasoner.

    Returns:
        (ok: bool, clarification_question: str nếu cần clarify)
    """
    # Non-calculation templates: params có thể rỗng (explain queries)
    _CALC_TEMPLATES = {"PIT_full", "PIT_progressive", "HKD_percentage", "HKD_profit"}

    if not params_validated:
        if template_type in _CALC_TEMPLATES:
            return False, (
                "Vui lòng cung cấp thêm thông tin: "
                + {
                    "PIT_full":        "thu nhập hàng tháng hoặc hàng năm của bạn là bao nhiêu?",
                    "PIT_progressive": "thu nhập tính thuế của bạn là bao nhiêu?",
                    "HKD_percentage":  "doanh thu hàng năm và ngành nghề kinh doanh của bạn là gì?",
                    "HKD_profit":      "doanh thu, chi phí và ngành nghề kinh doanh của bạn là gì?",
                }.get(template_type, "thông tin để tính toán.")
            )

    # Check mọi value không phải None cho required params
    null_params = [
        k for k, v in params_validated.items()
        if isinstance(v, dict) and v.get("value") is None
    ]
    if null_params and template_type in _CALC_TEMPLATES:
        return False, f"Thiếu giá trị cho: {', '.join(null_params)}. Bạn có thể cung cấp thêm không?"

    return True, ""


# ─── Computation scanner [G4] — 2 cấp độ ─────────────────────────────────────

def scan_params_critical(params_validated: dict) -> List[str]:
    """
    [G4-Critical] Detect formula-as-string trong params_validated.value.

    Chỉ scan các value là string — integer/float hợp lệ không bị block.
    Ví dụ bắt được: "value": "30M × 12", "value": "360000000 / 12"
    Ví dụ KHÔNG bắt: "value": 360000000  (integer — ok)

    Returns: List violations (empty = OK, không cần retry nếu rỗng).
    """
    violations = []
    for param_name, pdata in params_validated.items():
        if not isinstance(pdata, dict):
            continue
        val = pdata.get("value")
        if not isinstance(val, str):
            continue   # integer/float/null → bỏ qua
        for pat in _CRITICAL_RES:
            m = pat.search(val)
            if m:
                violations.append(f"{param_name}.value: '{m.group(0)}'")
                break  # 1 violation per param là đủ
    return violations


def scan_assumptions_warn(obj: dict) -> List[str]:
    """
    [G4-Warn] Detect equation patterns trong assumptions + clarification_question.

    KHÔNG block pipeline — chỉ dùng để log và metrics.
    Pattern yêu cầu: DIGIT + OPERATOR + DIGIT + DẤU BẰNG
    → tránh false positive với text thông thường có dấu gạch ngang/dấu bằng.

    Returns: List matches tìm được (empty = clean).
    """
    text_fields: List[str] = []
    for key in ("assumptions", "clarification_question"):
        val = obj.get(key)
        if isinstance(val, str):
            text_fields.append(val)
        elif isinstance(val, list):
            text_fields.extend(str(v) for v in val if v)

    violations = []
    for text in text_fields:
        for pat in _WARN_RES:
            m = pat.search(text)
            if m:
                violations.append(m.group(0))
    return violations


# Backward-compat alias (dùng trong orchestrator cũ nếu có)
def scan_for_computation(obj: dict) -> List[str]:
    """Deprecated: dùng scan_assumptions_warn() trực tiếp."""
    return scan_assumptions_warn(obj)


# ─── LLM Guard — Main class ───────────────────────────────────────────────────

class LLMGuard:
    """
    Wrapper quanh google.genai để call LLM với stabilization.

    Usage:
        guard = LLMGuard(api_key="...", model="gemini-2.5-flash")

        # Call Legal Reasoner (step 6.1)
        result, meta = guard.call_reasoner(system_prompt=..., user_msg=...)
        # result: dict với template_type, params_validated, ...
        # meta:   {attempts, parse_ok, schema_ok, compute_violations, latency_ms}
    """

    MAX_RETRIES = 3
    TEMPERATURE = 0.1   # low → deterministic

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash"):
        from google import genai
        api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Cần GOOGLE_API_KEY cho LLMGuard")
        self._client = genai.Client(api_key=api_key)
        self.model   = model
        logger.debug("LLMGuard initialized: model=%s", model)

    def call_reasoner(
        self,
        system_prompt: str,
        user_msg: str = "Trả lời JSON theo schema đã quy định.",
    ) -> Tuple[Optional[dict], dict]:
        """
        Gọi LLM Legal Reasoner (step 6.1) với JSON retry + validation.

        Returns:
            (parsed_dict | None, meta_dict)
            meta: {attempts, parse_ok, schema_ok, compute_violations,
                   latency_ms, errors}
        """
        from google.genai import types

        t_start = time.perf_counter()
        meta: Dict[str, Any] = {
            "attempts": 0, "parse_ok": False, "schema_ok": False,
            "compute_violations": [], "latency_ms": 0, "errors": [],
        }

        # [G5] Dùng response_mime_type="application/json" (không dùng response_schema cho
        # params_validated vì Gemini trả {} khi schema là generic OBJECT — params phải
        # free-form để LLM điền theo text prompt hướng dẫn).

        last_error = ""
        for attempt in range(1, self.MAX_RETRIES + 1):
            meta["attempts"] = attempt
            try:
                response = self._client.models.generate_content(
                    model    = self.model,
                    contents = [types.Part.from_text(text=user_msg)],
                    config   = types.GenerateContentConfig(
                        system_instruction  = system_prompt,
                        response_mime_type  = "application/json",
                        temperature         = self.TEMPERATURE,
                        thinking_config     = types.ThinkingConfig(thinkingBudget=0),
                    ),
                )
                raw = response.text or ""

            except Exception as exc:
                last_error = f"API error attempt {attempt}: {exc}"
                meta["errors"].append(last_error)
                logger.warning("[LLMGuard] %s", last_error)
                if attempt < self.MAX_RETRIES:
                    time.sleep(1.0)
                continue

            # [G1] Parse JSON
            parsed = _try_parse_json(raw)
            if parsed is None:
                last_error = f"JSON parse fail attempt {attempt}: {raw[:100]}"
                meta["errors"].append(last_error)
                logger.warning("[LLMGuard] %s", last_error)
                if attempt < self.MAX_RETRIES:
                    time.sleep(0.5)
                continue

            meta["parse_ok"] = True

            # [G2] Schema validation
            schema_ok, schema_errors = validate_reasoner_schema(parsed)
            if not schema_ok:
                last_error = f"Schema fail attempt {attempt}: {schema_errors}"
                meta["errors"].append(last_error)
                logger.warning("[LLMGuard] %s", last_error)
                if attempt < self.MAX_RETRIES:
                    time.sleep(0.5)
                continue

            meta["schema_ok"] = True

            # [G4-Critical] Formula-as-string trong params_validated → retry
            params = parsed.get("params_validated", {})
            critical_violations = scan_params_critical(params)
            if critical_violations:
                last_error = (
                    f"G4-Critical: formula trong params attempt {attempt}: "
                    f"{critical_violations}"
                )
                meta["errors"].append(last_error)
                logger.warning("[LLMGuard] %s", last_error)
                if attempt < self.MAX_RETRIES:
                    time.sleep(0.5)
                continue   # retry

            # [G4-Warn] Equation trong assumptions → log + metrics, KHÔNG block
            warn_violations = scan_assumptions_warn(parsed)
            if warn_violations:
                meta["compute_violations"] = warn_violations
                logger.warning(
                    "[LLMGuard][G4-Warn] Computation trong assumptions (non-blocking): %s",
                    warn_violations[:3],
                )
            else:
                meta["compute_violations"] = []

            meta["latency_ms"] = int((time.perf_counter() - t_start) * 1000)
            logger.debug(
                "[LLMGuard] OK attempt=%d latency=%dms template=%s",
                attempt, meta["latency_ms"], parsed.get("template_type"),
            )
            return parsed, meta

        # Tất cả retries fail
        meta["latency_ms"] = int((time.perf_counter() - t_start) * 1000)
        logger.error("[LLMGuard] All %d attempts failed. Last: %s", self.MAX_RETRIES, last_error)
        return None, meta

    def call_synthesizer(
        self,
        system_prompt: str,
        user_msg: str,
    ) -> Tuple[Optional[str], dict]:
        """
        Gọi LLM Synthesizer (step 6.3) — free text output.

        Returns:
            (answer_text | None, meta_dict)
        """
        from google.genai import types

        t_start = time.perf_counter()
        meta: Dict[str, Any] = {"attempts": 0, "latency_ms": 0, "errors": []}

        for attempt in range(1, self.MAX_RETRIES + 1):
            meta["attempts"] = attempt
            try:
                response = self._client.models.generate_content(
                    model    = self.model,
                    contents = [types.Part.from_text(text=user_msg)],
                    config   = types.GenerateContentConfig(
                        system_instruction = system_prompt,
                        temperature        = 0.0,
                        thinking_config    = types.ThinkingConfig(thinkingBudget=0),
                    ),
                )
                text = response.text or ""
                if text.strip():
                    meta["latency_ms"] = int((time.perf_counter() - t_start) * 1000)
                    return text.strip(), meta

            except Exception as exc:
                meta["errors"].append(f"attempt {attempt}: {exc}")
                logger.warning("[LLMGuard synthesizer] attempt %d: %s", attempt, exc)
                if attempt < self.MAX_RETRIES:
                    time.sleep(1.0)

        meta["latency_ms"] = int((time.perf_counter() - t_start) * 1000)
        return None, meta
