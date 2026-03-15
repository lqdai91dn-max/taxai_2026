"""
src/agent/planner.py — Agentic Tax AI với Gemini function calling.

Architecture:
  User question
    → Gemini (reasoning + tool selection)
    → Tool execution (deterministic)
    → Gemini (tiếp tục hoặc trả lời cuối)
    → Final answer với citations

Rules được enforce qua system prompt:
  - LLM KHÔNG tự tính toán thuế → PHẢI gọi calculator tools
  - Mọi citation phải có nguồn từ tool output
  - Tối đa MAX_ITERATIONS vòng lặp tool calls
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types, errors as genai_errors

from src.tools import TOOL_DEFINITIONS, TOOL_REGISTRY

load_dotenv()
logger = logging.getLogger(__name__)

GEMINI_MODEL   = "gemini-2.5-flash"
MAX_ITERATIONS = 6  # max vòng lặp tool calling

# ── System prompt ─────────────────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = f"""Bạn là TaxAI — trợ lý tư vấn pháp luật thuế Việt Nam chuyên nghiệp.
Ngày hôm nay: {{today}}.

## Nhiệm vụ
Trả lời câu hỏi về thuế TNCN, thuế GTGT, thuế hộ kinh doanh (HKD) dựa trên văn bản pháp luật Việt Nam.

## QUY TẮC BẮT BUỘC

### 1. LUÔN GỌI TOOL TRƯỚC KHI TRẢ LỜI
- **BẤT KỲ câu hỏi nào liên quan đến con số, tỷ lệ %, thuế phải nộp** → PHẢI gọi tool
- KHÔNG tự nhân, chia, tính % mà không có tool output làm căn cứ
- Nếu không chắc tool nào dùng → gọi `search_legal_docs` để tìm căn cứ pháp lý trước
- KHÔNG hỏi người dùng thêm thông tin nếu câu hỏi đã đủ dữ liệu để tính

### 2. KIỂM TRA HIỆU LỰC
- Luật 109/2025/QH15 chưa có hiệu lực đến 01/07/2026 — dùng mức giảm trừ mới khi tính từ ngày đó
- Nghị định 68/2026/NĐ-CP: hiệu lực từ 05/03/2026

### 3. CITATION ĐẦY ĐỦ
- Mọi số liệu phải có nguồn từ tool output
- Định dạng: "Theo [tên văn bản số XX/XXXX], Điều X..."

## Workflow theo loại câu hỏi

**HKD / cá nhân kinh doanh — tính thuế GTGT + TNCN:**
1. `calculate_tax_hkd(annual_revenue, business_category)` → PP tỷ lệ % doanh thu
   - goods=hàng hóa, services=dịch vụ, manufacturing=sản xuất/xây dựng, real_estate=cho thuê BĐS
2. Hoặc `calculate_tax_hkd_profit(revenue, expenses, category)` nếu có chi phí (PP lợi nhuận)

**HKD — kiểm tra nghĩa vụ tổng hợp (kê khai, HĐĐT, sàn TMĐT):**
1. `evaluate_tax_obligation(annual_revenue, has_online_sales, platform_has_payment)`

**HKD bán hàng trên sàn TMĐT (Shopee, Lazada, TikTok...):**
1. `calculate_tax_hkd` → tính thuế theo ngành
2. `evaluate_tax_obligation(has_online_sales=True, platform_has_payment=True)` → xác định sàn có khấu trừ không
3. `search_legal_docs` → tìm quy định cụ thể nếu cần

**TNCN cá nhân — lương, tiền công:**
1. `calculate_deduction(dependents, months)` → tính giảm trừ gia cảnh trước
2. `calculate_tncn_progressive(annual_taxable_income)` → tính thuế lũy tiến

**TNCN từ chuyển nhượng BĐS, trúng thưởng, cổ tức:**
1. `search_legal_docs` → tìm quy định thuế suất áp dụng
2. `get_article` → lấy toàn văn điều khoản
→ BĐS: 2% × giá bán; Trúng thưởng: 10% × (giải thưởng − 10 triệu); Cổ tức: 5%

**Tra cứu điều luật:**
1. `resolve_legal_reference` → parse tên văn bản
2. `get_article` hoặc `get_article_with_amendments` → toàn văn

**Kiểm tra hiệu lực:**
1. `check_doc_validity` → status + amended_by
2. `get_impl_chain` → hierarchy pháp lý

**Câu hỏi tổng quát / thủ tục / hướng dẫn:**
1. `search_legal_docs` → tìm điều khoản liên quan
2. `get_guidance` → hướng dẫn thực tế từ Sổ tay HKD / Công văn

## Định dạng câu trả lời cuối
1. Trả lời trực tiếp (con số, quy định)
2. Căn cứ pháp lý (tên văn bản + số Điều)
3. Lưu ý / cảnh báo nếu có (hiệu lực, sửa đổi, v.v.)
"""


# ═══════════════════════════════════════════════════════════════════════════════
# TaxAIAgent
# ═══════════════════════════════════════════════════════════════════════════════

class TaxAIAgent:
    """
    Agentic Tax AI — Gemini function calling loop.

    Thay thế pipeline cứng của AnswerGenerator bằng LLM-driven tool orchestration.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = GEMINI_MODEL,
        max_iterations: int = MAX_ITERATIONS,
    ):
        api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Cần GOOGLE_API_KEY — set env hoặc truyền vào constructor")

        self.client         = genai.Client(api_key=api_key)
        self.model          = model
        self.max_iterations = max_iterations

        # Build Gemini tool spec từ TOOL_DEFINITIONS
        self._gemini_tools = [types.Tool(function_declarations=TOOL_DEFINITIONS)]

        logger.info(f"✅ TaxAIAgent initialized — model: {model}, tools: {len(TOOL_DEFINITIONS)}")

    # ── Tool execution ────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> Any:
        """Gọi tool từ TOOL_REGISTRY với args từ Gemini."""
        if name not in TOOL_REGISTRY:
            return {"error": f"Tool '{name}' không tồn tại trong registry."}
        try:
            logger.info(f"  🔧 Executing tool: {name}({_summarize_args(args)})")
            result = TOOL_REGISTRY[name](**args)
            logger.info(f"  ✅ Tool {name} done")
            return result
        except Exception as e:
            logger.error(f"  ❌ Tool {name} failed: {e}")
            return {"error": str(e), "tool": name, "args": args}

    # ── Main agentic loop ─────────────────────────────────────────────────────

    def answer(
        self,
        question: str,
        filter_doc_id: str | None = None,
        show_sources: bool = True,
    ) -> dict[str, Any]:
        """
        Trả lời câu hỏi thuế qua agentic loop.

        Args:
            question:      Câu hỏi của user.
            filter_doc_id: Giới hạn tìm kiếm trong 1 văn bản (optional).
            show_sources:  Trả về tool call log hay không.

        Returns:
            {answer, sources, tool_calls, model, iterations}
        """
        today = date.today().isoformat()
        system_prompt = AGENT_SYSTEM_PROMPT.format(today=today)

        # Nếu có filter_doc_id → thêm vào system prompt
        if filter_doc_id:
            system_prompt += f"\n\nLưu ý: Người dùng muốn tìm trong văn bản '{filter_doc_id}'."

        # ── Khởi tạo conversation ─────────────────────────────────────────────
        contents: list[types.Content] = [
            types.Content(
                role="user",
                parts=[types.Part(text=question)],
            )
        ]

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=self._gemini_tools,
            temperature=0.1,   # thấp để đảm bảo deterministic reasoning
        )

        tool_calls_log: list[dict] = []
        answer_text   = ""

        # ── Agentic loop ──────────────────────────────────────────────────────
        for iteration in range(1, self.max_iterations + 1):
            logger.info(f"🔄 Iteration {iteration}/{self.max_iterations}")

            try:
                response = self.client.models.generate_content(
                    model    = self.model,
                    contents = contents,
                    config   = config,
                )
            except genai_errors.ClientError as e:
                msg = str(e)
                if "API_KEY_INVALID" in msg or "API key expired" in msg:
                    raise RuntimeError(
                        "❌ GOOGLE_API_KEY không hợp lệ hoặc hết hạn."
                    ) from None
                raise RuntimeError(f"❌ Gemini API lỗi: {msg}") from None

            candidate = response.candidates[0]
            parts      = candidate.content.parts

            # ── Tách function_calls và text parts ────────────────────────────
            fn_call_parts  = [p for p in parts if p.function_call is not None]
            text_parts     = [p for p in parts if p.text]

            # Không có function call → Gemini đã trả lời xong
            if not fn_call_parts:
                answer_text = "\n".join(p.text for p in text_parts if p.text).strip()
                logger.info(f"✅ Final answer after {iteration} iteration(s)")
                break

            # ── Thêm assistant turn vào conversation ─────────────────────────
            contents.append(
                types.Content(role="model", parts=parts)
            )

            # ── Execute từng tool call, build function_response parts ─────────
            response_parts: list[types.Part] = []

            for part in fn_call_parts:
                fc   = part.function_call
                name = fc.name
                args = dict(fc.args) if fc.args else {}

                result = self._execute_tool(name, args)

                tool_calls_log.append({
                    "tool":   name,
                    "args":   args,
                    "result": _truncate_result(result),
                })

                response_parts.append(
                    types.Part.from_function_response(
                        name=name,
                        response={"result": result},
                    )
                )

            # ── Thêm tool results vào conversation ───────────────────────────
            contents.append(
                types.Content(role="user", parts=response_parts)
            )

        else:
            # Hết iterations — yêu cầu Gemini tổng hợp best-effort từ những gì đã tìm được
            logger.warning(f"⚠️ Max iterations ({self.max_iterations}) reached")
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=(
                    "Bạn đã sử dụng hết số vòng tìm kiếm. "
                    "Hãy tổng hợp câu trả lời tốt nhất có thể dựa trên thông tin đã tìm được. "
                    "Nếu không tìm được điều khoản cụ thể, hãy nêu nguyên tắc chung và hướng người dùng tra cứu thêm."
                ))],
            ))
            try:
                synth = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=self._gemini_tools,
                        temperature=0.1,
                    ),
                )
                answer_text = "\n".join(
                    p.text for p in synth.candidates[0].content.parts if p.text
                ).strip() or "Không tìm được thông tin phù hợp cho câu hỏi này trong cơ sở dữ liệu."
            except Exception:
                answer_text = "Không tìm được thông tin phù hợp cho câu hỏi này trong cơ sở dữ liệu."

        # ── Build sources từ tool call log ────────────────────────────────────
        sources = _extract_sources_from_tool_log(tool_calls_log) if show_sources else []

        return {
            "answer":     answer_text,
            "sources":    sources,
            "tool_calls": tool_calls_log if show_sources else [],
            "model":      self.model,
            "iterations": iteration,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _summarize_args(args: dict) -> str:
    """Log-friendly summary của tool args."""
    parts = []
    for k, v in args.items():
        if isinstance(v, (int, float)) and v > 1_000_000:
            parts.append(f"{k}={v/1e6:.0f}M")
        else:
            parts.append(f"{k}={str(v)[:30]}")
    return ", ".join(parts)


def _truncate_result(result: Any, max_chars: int = 800) -> Any:
    """Truncate tool result cho log (tránh log quá dài)."""
    if isinstance(result, dict):
        # Giữ summary/message fields, truncate full_text/content
        truncated = {}
        for k, v in result.items():
            if k in ("full_text", "content") and isinstance(v, str) and len(v) > 200:
                truncated[k] = v[:200] + "..."
            elif k == "breakdown" and isinstance(v, list):
                truncated[k] = v  # giữ nguyên breakdown
            else:
                truncated[k] = v
        return truncated
    return result


def _extract_sources_from_tool_log(tool_calls: list[dict]) -> list[dict]:
    """
    Gom citations từ tất cả tool call results.
    Dedup theo doc_id + article_id.
    """
    seen: set[str] = set()
    sources: list[dict] = []

    for call in tool_calls:
        result = call.get("result", {})
        if not isinstance(result, dict):
            continue

        # Calculator tools → citation trong breakdown items
        for item in result.get("breakdown", []):
            c = item.get("citation", {})
            key = c.get("doc_id", "") + "|" + c.get("note", "")
            if key and key not in seen:
                seen.add(key)
                sources.append({
                    "tool":        call["tool"],
                    "doc_id":      c.get("doc_id", ""),
                    "doc_number":  c.get("doc_number", ""),
                    "reference":   c.get("note", ""),
                    "type":        "calculation",
                })

        # get_article / get_article_with_amendments
        if "citation" in result and isinstance(result["citation"], dict):
            c   = result["citation"]
            key = c.get("doc_id", "") + "|" + c.get("article_id", "")
            if key and key not in seen:
                seen.add(key)
                sources.append({
                    "tool":       call["tool"],
                    "doc_id":     c.get("doc_id", ""),
                    "article_id": c.get("article_id", ""),
                    "title":      c.get("title", ""),
                    "type":       "article",
                })

        # search_legal_docs → citations trong results list
        for hit in result.get("results", []):
            c   = hit.get("citation", {})
            key = c.get("doc_id", "") + "|" + c.get("breadcrumb", "")
            if key and key not in seen:
                seen.add(key)
                sources.append({
                    "tool":       call["tool"],
                    "doc_id":     c.get("doc_id", ""),
                    "doc_number": c.get("doc_number", ""),
                    "breadcrumb": c.get("breadcrumb", ""),
                    "type":       "search",
                })

        # check_doc_validity
        if call["tool"] == "check_doc_validity" and result.get("found"):
            key = result.get("doc_id", "") + "|validity"
            if key not in seen:
                seen.add(key)
                sources.append({
                    "tool":       call["tool"],
                    "doc_id":     result.get("doc_id", ""),
                    "doc_number": result.get("doc_number", ""),
                    "status":     result.get("status", ""),
                    "type":       "validity",
                })

    return sources
