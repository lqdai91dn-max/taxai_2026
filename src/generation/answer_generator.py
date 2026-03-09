"""
src/generation/answer_generator.py
Kết nối Hybrid Search + Gemini 2.5 Flash để trả lời câu hỏi pháp luật thuế
"""

from __future__ import annotations
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors

from src.retrieval.hybrid_search import HybridSearch

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_MODEL   = "gemini-2.5-flash"
CHROMA_DIR     = "data/chroma"
EMBEDDING_MODEL = "keepitreal/vietnamese-sbert"

SYSTEM_PROMPT = """Bạn là TaxAI — trợ lý tư vấn pháp luật thuế Việt Nam chuyên nghiệp.

Nhiệm vụ:
- Trả lời câu hỏi về thuế dựa trên các điều khoản pháp luật được cung cấp
- Trích dẫn chính xác số Điều, Khoản, văn bản pháp luật liên quan
- Giải thích rõ ràng, dễ hiểu cho người dùng không chuyên

Nguyên tắc:
- Nếu có mục "SỐ LIỆU THUẾ ĐÃ XÁC NHẬN", hãy ưu tiên sử dụng các con số trong đó vì đây là nguồn chính xác nhất
- CHỈ trả lời dựa trên nội dung pháp luật được cung cấp trong context
- Nếu context không đủ thông tin, nói rõ "Tôi không tìm thấy quy định cụ thể về vấn đề này"
- KHÔNG bịa đặt hoặc suy đoán các con số, tỷ lệ, mức thuế
- Luôn ghi rõ nguồn: tên văn bản + số Điều

Định dạng trả lời:
1. Trả lời trực tiếp câu hỏi (nêu rõ con số nếu có)
2. Trích dẫn điều khoản liên quan
3. Giải thích thêm nếu cần
"""


class AnswerGenerator:
    """
    Pipeline hoàn chỉnh: Query → Hybrid Search → Gemini → Answer
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = GEMINI_MODEL,
        chroma_dir: str = CHROMA_DIR,
        embedding_model: str = EMBEDDING_MODEL,
        n_results: int = 8,
    ):
        # Gemini client
        api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("❌ Cần GOOGLE_API_KEY — set env hoặc truyền vào constructor")

        self.client = genai.Client(api_key=api_key)
        self.model  = model
        self.n_results = n_results

        # Hybrid search
        self.searcher = HybridSearch(
            chroma_dir   = chroma_dir,
            model_name   = embedding_model,
        )

        logger.info(f"✅ AnswerGenerator initialized — model: {model}")

    # ── Build config context ──────────────────────────────────────────────

    def _build_config_context(self) -> str:
        """
        Inject các số liệu thuế đã xác nhận từ Config hệ thống.
        Giúp model trả lời chính xác ngay cả khi RAG không tìm đủ chunk.
        """
        try:
            from src.utils.config import Config

            parts = []

            # GTGC (NQ 110/2025/UBTVQH15)
            self_d = getattr(Config, "FAMILY_DEDUCTION_SELF", None)
            dep_d  = getattr(Config, "FAMILY_DEDUCTION_DEPENDENT", None)
            if self_d and dep_d:
                parts.append(
                    "[Giảm trừ gia cảnh — NQ 110/2025/UBTVQH15, hiệu lực 01/07/2026]\n"
                    f"• Bản thân người nộp thuế : {self_d:,} VND/tháng"
                    f" ({self_d * 12:,} VND/năm)\n"
                    f"• Mỗi người phụ thuộc     : {dep_d:,} VND/tháng"
                    f" ({dep_d * 12:,} VND/năm)"
                )

            # Biểu thuế lũy tiến (Luật 109/2025/QH15)
            brackets = getattr(Config, "PROGRESSIVE_TAX_BRACKETS", None)
            if brackets:
                rows = [
                    "[Biểu thuế TNCN lũy tiến — Luật 109/2025/QH15, hiệu lực 01/07/2026]"
                ]
                for b in brackets:
                    rows.append(f"• Bậc {b['bracket']}: {b['description']} → {int(b['rate']*100)}%")
                parts.append("\n".join(rows))

            # Thuế TNCN từ chuyển nhượng bất động sản (Luật 109/2025/QH15)
            # (Dữ liệu từ Điều 14 & 24 — thêm vào đây vì OCR bị lỗi ghép từ trong JSON)
            parts.append(
                "[Thuế TNCN từ chuyển nhượng bất động sản — Luật 109/2025/QH15]\n"
                "• Cá nhân CƯ TRÚ   : thuế = giá chuyển nhượng × 2%  (Điều 14, Khoản 1)\n"
                "• Cá nhân KHÔNG CƯ TRÚ: thuế = giá chuyển nhượng × 2%  (Điều 24, Khoản 1)\n"
                "• Thời điểm tính thuế: khi hợp đồng có hiệu lực hoặc khi đăng ký"
                " quyền sở hữu/sử dụng (Điều 14, Khoản 2)\n"
                "• MIỄN THUẾ: chuyển nhượng giữa vợ/chồng; cha mẹ/con đẻ; cha mẹ nuôi/con nuôi;"
                " cha mẹ chồng/vợ với con dâu/rể; ông bà nội ngoại với cháu;"
                " anh chị em ruột (Điều 4, Khoản 1)"
            )

            # Ngưỡng đăng ký thuế GTGT (Luật GTGT 2025)
            vat_thresh = getattr(Config, "VAT_REGISTRATION_THRESHOLD", None)
            if vat_thresh:
                parts.append(
                    "[Ngưỡng đăng ký thuế GTGT — Luật GTGT 2025, hiệu lực 01/01/2026]\n"
                    f"• Doanh thu trên {vat_thresh:,} VND/năm phải đăng ký nộp thuế GTGT"
                )

            return "\n\n".join(parts)

        except Exception:
            return ""

    # ── Build context từ search results ──────────────────────────────────

    def _build_context(self, search_results: List[Dict[str, Any]]) -> str:
        """Chuyển search results thành context string cho LLM"""
        if not search_results:
            return "Không tìm thấy điều khoản liên quan."

        parts = []
        for i, r in enumerate(search_results, 1):
            meta       = r.get("metadata", {})
            breadcrumb = meta.get("breadcrumb", "")
            doc_number = meta.get("document_number", "")
            text       = r.get("text", "")

            parts.append(
                f"[{i}] {breadcrumb}\n"
                f"Nguồn: {doc_number}\n"
                f"Nội dung: {text}\n"
            )

        return "\n---\n".join(parts)

    # ── Main answer pipeline ──────────────────────────────────────────────

    def answer(
        self,
        question: str,
        filter_doc_id: Optional[str] = None,
        show_sources: bool = True,
    ) -> Dict[str, Any]:
        """
        Trả lời câu hỏi thuế

        Args:
            question: câu hỏi của user
            filter_doc_id: giới hạn tìm trong 1 văn bản cụ thể
            show_sources: có trả về nguồn tham khảo không

        Returns:
            {
                "answer": str,
                "sources": List[Dict],
                "model": str,
            }
        """

        # 1. Hybrid Search
        logger.info(f"🔍 Searching: {question[:60]}...")
        search_results = self.searcher.search(
            query          = question,
            n_results      = self.n_results,
            filter_doc_id  = filter_doc_id,
        )

        # 2. Build context
        context = self._build_context(search_results)
        config_context = self._build_config_context()

        # 3. Build prompt
        config_section = (
            f"\n===== SỐ LIỆU THUẾ ĐÃ XÁC NHẬN =====\n{config_context}\n"
            if config_context else ""
        )

        prompt = f"""{SYSTEM_PROMPT}{config_section}
===== CÁC ĐIỀU KHOẢN PHÁP LUẬT LIÊN QUAN =====
{context}

===== CÂU HỎI =====
{question}

===== TRẢ LỜI ====="""

        # 4. Gọi Gemini
        logger.info(f"🤖 Calling {self.model}...")
        try:
            response = self.client.models.generate_content(
                model    = self.model,
                contents = prompt,
            )
            answer_text = response.text
        except genai_errors.ClientError as e:
            status = e.status_code if hasattr(e, "status_code") else 0
            msg = str(e)
            if "API_KEY_INVALID" in msg or "API key expired" in msg:
                raise RuntimeError(
                    "❌ GOOGLE_API_KEY không hợp lệ hoặc đã hết hạn. "
                    "Vui lòng tạo key mới tại https://aistudio.google.com/apikey"
                ) from None
            raise RuntimeError(f"❌ GOOGLE API lỗi ({status}): {msg}") from None

        # 5. Build sources
        sources = []
        if show_sources:
            for r in search_results:
                meta = r.get("metadata", {})
                sources.append({
                    "breadcrumb":      meta.get("breadcrumb", ""),
                    "document_number": meta.get("document_number", ""),
                    "score":           r.get("rrf_score", 0),
                })

        return {
            "answer":  answer_text,
            "sources": sources,
            "model":   self.model,
        }


# ── CLI test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    generator = AnswerGenerator()  # tự đọc GOOGLE_API_KEY từ .env

    test_questions = [
        "Mức giảm trừ gia cảnh cho bản thân người nộp thuế là bao nhiêu?",
        "Thu nhập từ chuyển nhượng bất động sản bị đánh thuế như thế nào?",
        "Tổ chức quản lý sàn thương mại điện tử có nghĩa vụ khấu trừ thuế gì?",
        "Ngưỡng doanh thu phải đăng ký thuế GTGT là bao nhiêu?",
        "Thuế suất thuế TNCN theo biểu lũy tiến từng bậc là bao nhiêu?",
        "Tôi đang có lương hàng tháng là 20 triệu VND, gia đình gồm tôi + vợ và 2 con + cha mẹ của tôi, vậy thuế TNCN phải nộp hàng tháng là bao nhiêu?"
    ]

    for question in test_questions:
        print(f"\n{'='*60}")
        print(f"❓ {question}")
        print('='*60)

        result = generator.answer(question)

        print(f"\n💬 Trả lời:\n{result['answer']}")
        print(f"\n📚 Nguồn tham khảo:")
        for s in result['sources']:
            print(f"   • {s['breadcrumb']} (score: {s['score']:.4f})")