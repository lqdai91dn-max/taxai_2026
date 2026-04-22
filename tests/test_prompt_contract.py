"""
tests/test_prompt_contract.py — Contract tests cho AGENT_SYSTEM_PROMPT.

Đảm bảo prompt:
  1. Không chứa dead tool references
  2. Section 3 được generate từ law_registry (không hard-code)
  3. Tất cả active docs từ law_validity.json đều xuất hiện trong prompt
  4. Không còn conflict giữa Section 3 và Section 6+ về 111/92

Chạy: pytest tests/test_prompt_contract.py -v
"""

import pytest
from src.agent.planner import AGENT_SYSTEM_PROMPT
from src.utils.law_registry import get_active_documents, get_superseded_documents


DEAD_TOOLS = [
    "resolve_legal_reference",
    "get_impl_chain",
    "get_article_with_amendments",  # removed from _GROUNDING_TOOLS
]

FORMATTED_PROMPT = AGENT_SYSTEM_PROMPT.format(today="2026-04-20")


class TestNoDeadTools:

    def test_no_resolve_legal_reference(self):
        assert "resolve_legal_reference" not in FORMATTED_PROMPT

    def test_no_get_impl_chain(self):
        assert "get_impl_chain" not in FORMATTED_PROMPT

    def test_no_get_article_in_tool_call_context(self):
        # "get_article" may appear in text as doc name, but NOT as tool call
        # Check it doesn't appear as `get_article(` or `get_article_with_amendments(`
        assert "`get_article`" not in FORMATTED_PROMPT
        assert "get_article_with_amendments" not in FORMATTED_PROMPT


class TestSection3GeneratedFromRegistry:

    def test_section3_header_present(self):
        assert "LUẬT ÁP DỤNG — VĂN BẢN HIỆN HÀNH" in FORMATTED_PROMPT

    def test_active_docs_listed(self):
        """Mọi doc active phải có doc_number trong prompt."""
        active = get_active_documents()
        for doc_id, doc in active.items():
            doc_num = doc.get("doc_number", "")
            assert doc_num in FORMATTED_PROMPT, (
                f"Active doc {doc_id} ({doc_num}) missing from prompt Section 3"
            )

    def test_superseded_docs_listed(self):
        """Mọi doc superseded phải có doc_number trong prompt (với note về hết hiệu lực)."""
        superseded = get_superseded_documents()
        for doc_id, doc in superseded.items():
            doc_num = doc.get("doc_number", "")
            assert doc_num in FORMATTED_PROMPT, (
                f"Superseded doc {doc_id} ({doc_num}) missing from prompt"
            )

    def test_exception_notice_present(self):
        assert "Ngoại lệ" in FORMATTED_PROMPT

    def test_111_exception_reason_in_prompt(self):
        assert "111/2013/TT-BTC" in FORMATTED_PROMPT

    def test_92_exception_reason_in_prompt(self):
        assert "92/2015/TT-BTC" in FORMATTED_PROMPT


class TestNoHardcodedSection3:

    def test_old_moc_header_gone(self):
        """Old Section 3 header 'MỐC 01/07/2026' should not exist."""
        assert "MỐC 01/07/2026" not in FORMATTED_PROMPT

    def test_old_hardcoded_giamtru_numbers_gone(self):
        """Old hardcoded mức giảm trừ in Section 3 should come from registry now."""
        # The old Section 3 had these as bullet points — now they come via law_validity notes
        # Just verify Section 3 is not the old hard-coded text
        assert "### 3. LUẬT ÁP DỤNG — MỐC 01/07/2026" not in FORMATTED_PROMPT


class TestModelConfig:

    def test_gemini_model_is_stable(self):
        from src.agent.planner import GEMINI_MODEL
        assert GEMINI_MODEL == "gemini-2.5-flash"
        assert "preview" not in GEMINI_MODEL

    def test_est_tokens_per_call(self):
        from src.agent.planner import EST_TOKENS_PER_CALL
        assert EST_TOKENS_PER_CALL >= 20_000
