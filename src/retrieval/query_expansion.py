"""
src/retrieval/query_expansion.py — Legal-Term Query Expansion (R32)

Sinh query variant: legal-term rewrite của câu hỏi user.
Dùng Gemini Flash để dịch từ vựng dân dã → thuật ngữ pháp lý chính xác.

Mục đích: cải thiện recall cho docs có phrasing khác với câu hỏi user.
Ví dụ: "tiết kiệm ngân hàng" → "lãi tiền gửi thu nhập từ đầu tư vốn"

Thiết kế:
  - Non-blocking: nếu LLM fail → trả None, caller bỏ qua gracefully
  - Constraint: không invent số điều/khoản cụ thể (tránh hallucination drift)
  - Temperature 0.1: deterministic, tránh random variation giữa runs
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_EXPANSION_SYSTEM = (
    "Bạn là chuyên gia pháp lý thuế Việt Nam. "
    "Nhiệm vụ: viết lại câu hỏi của người dùng bằng thuật ngữ pháp lý chính xác "
    "như cách diễn đạt trong văn bản pháp luật.\n\n"
    "Quy tắc bắt buộc:\n"
    "- Chỉ thay từ vựng dân dã → thuật ngữ pháp lý, giữ nguyên ý nghĩa\n"
    "- Dùng thuật ngữ xuất hiện trong văn bản pháp luật thuế Việt Nam\n"
    "- KHÔNG thêm số điều, số khoản, số văn bản cụ thể\n"
    "- Trả lời chỉ là câu hỏi đã viết lại, không giải thích thêm"
)

_EXPANSION_EXAMPLES = (
    "Ví dụ:\n"
    "- \"tiết kiệm ngân hàng có chịu thuế không\" → "
    "\"lãi tiền gửi ngân hàng có thuộc diện thu nhập chịu thuế TNCN không\"\n"
    "- \"bán hàng online trên shopee nộp thuế gì\" → "
    "\"hoạt động kinh doanh trên sàn thương mại điện tử kê khai nộp thuế như thế nào\"\n"
    "- \"quán phở doanh thu bao nhiêu miễn thuế\" → "
    "\"hộ kinh doanh ngưỡng doanh thu được miễn thuế giá trị gia tăng thu nhập cá nhân\"\n"
    "- \"lương tháng mấy triệu thì phải nộp thuế\" → "
    "\"mức thu nhập chịu thuế TNCN từ tiền lương tiền công sau khi trừ giảm trừ gia cảnh\"\n"
)


def expand_to_legal_query(
    query: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> Optional[str]:
    """
    Sinh legal-term rewrite của query bằng Gemini Flash.

    Args:
        query:   câu hỏi gốc (đã augment nếu có)
        api_key: Gemini API key
        model:   tên model (default gemini-2.5-flash)

    Returns:
        Legal variant string, hoặc None nếu LLM fail / result không hợp lệ.
    """
    if not query or not api_key:
        return None

    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=api_key)

        client = genai.GenerativeModel(
            model_name=model,
            system_instruction=_EXPANSION_SYSTEM,
        )

        prompt = f"{_EXPANSION_EXAMPLES}\nCâu hỏi: {query}\n\nCâu hỏi viết lại:"

        response = client.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.1,
                max_output_tokens=300,
            ),
        )

        expanded = response.text.strip()

        # Sanity checks: không quá ngắn, không giống hệt original
        if len(expanded) < 15:
            logger.debug("[QueryExpansion] result too short, skipping")
            return None
        if expanded.lower().strip("?. ") == query.lower().strip("?. "):
            logger.debug("[QueryExpansion] result identical to original, skipping")
            return None

        logger.debug("[QueryExpansion] legal_variant: %s", expanded[:120])
        return expanded

    except Exception as e:
        logger.warning("[QueryExpansion] LLM fail (non-fatal): %s", e)
        return None
