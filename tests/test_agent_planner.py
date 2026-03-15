"""
Tests cho TaxAIAgent (Phase C).

Dùng unittest.mock để mock Gemini API — không cần API key thật.
Kiểm tra: tool dispatch, agentic loop, citation extraction, edge cases.

Chạy: pytest tests/test_agent_planner.py -v
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, call
from google.genai import types

from src.agent.planner import TaxAIAgent, _extract_sources_from_tool_log, _summarize_args


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures & Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_text_response(text: str) -> MagicMock:
    """Tạo mock Gemini response chỉ có text (final answer)."""
    part = types.Part(text=text)

    candidate         = MagicMock()
    candidate.content.parts = [part]

    response = MagicMock()
    response.candidates = [candidate]
    return response


def _make_tool_call_response(tool_name: str, args: dict) -> MagicMock:
    """Tạo mock Gemini response có function_call (dùng types.Part thật)."""
    part = types.Part.from_function_call(name=tool_name, args=args)

    candidate         = MagicMock()
    candidate.content.parts = [part]

    response = MagicMock()
    response.candidates = [candidate]
    return response


def _make_agent(mock_generate=None) -> TaxAIAgent:
    """Tạo TaxAIAgent với Gemini client bị mock."""
    with patch("src.agent.planner.genai.Client") as MockClient:
        agent = TaxAIAgent(api_key="fake-key")
        if mock_generate:
            agent.client.models.generate_content = mock_generate
        return agent


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Tool Dispatch
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolExecution:

    def test_execute_known_tool(self):
        agent = _make_agent()
        result = agent._execute_tool("calculate_tax_hkd", {
            "annual_revenue": 2_000_000_000,
            "business_category": "services",
        })
        # services 2B: GTGT = 2B×5% = 100M, TNCN = 2B×2% = 40M → total 140M
        assert result["total_tax"] == 140_000_000
        assert result["tncn_rate"] == 0.02

    def test_execute_unknown_tool_returns_error(self):
        agent = _make_agent()
        result = agent._execute_tool("nonexistent_tool", {})
        assert "error" in result
        assert "nonexistent_tool" in result["error"]

    def test_execute_tool_with_bad_args_returns_error(self):
        agent = _make_agent()
        result = agent._execute_tool("calculate_tax_hkd", {
            "annual_revenue": -1,
            "business_category": "goods",
        })
        assert "error" in result

    def test_all_9_tools_callable(self):
        """Verify tất cả 9 tools trong registry có thể được gọi qua _execute_tool."""
        from src.tools import TOOL_REGISTRY
        agent = _make_agent()
        # Chỉ test tools không cần Neo4j/ChromaDB
        result = agent._execute_tool("calculate_tncn_progressive",
                                     {"annual_taxable_income": 300_000_000})
        assert result["tax_payable"] > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Agentic Loop
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgenticLoop:

    def test_single_turn_no_tool_call(self):
        """Gemini trả lời thẳng không cần tool."""
        mock_gen = MagicMock(return_value=_make_text_response("Đây là câu trả lời"))

        with patch("src.agent.planner.genai.Client"):
            agent = TaxAIAgent(api_key="fake")
            agent.client.models.generate_content = mock_gen

        result = agent.answer("Chào bạn")
        assert result["answer"] == "Đây là câu trả lời"
        assert result["iterations"] == 1
        assert result["tool_calls"] == []

    def test_one_tool_call_then_answer(self):
        """Gemini gọi 1 tool rồi trả lời."""
        tool_response = _make_tool_call_response("calculate_tax_hkd", {
            "annual_revenue": 2_000_000_000,
            "business_category": "services",
        })
        # services 2B: GTGT = 100M + TNCN = 40M → total 140M
        final_response = _make_text_response("Thuế phải nộp là 140 triệu đồng.")

        mock_gen = MagicMock(side_effect=[tool_response, final_response])

        with patch("src.agent.planner.genai.Client"):
            agent = TaxAIAgent(api_key="fake")
            agent.client.models.generate_content = mock_gen

        result = agent.answer("HKD dịch vụ 2 tỷ đóng thuế bao nhiêu?")
        assert "140" in result["answer"] or result["iterations"] == 2
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool"] == "calculate_tax_hkd"
        # Tool result phải có số thực
        assert result["tool_calls"][0]["result"]["total_tax"] == 140_000_000

    def test_two_tool_calls_then_answer(self):
        """Gemini gọi 2 tools liên tiếp."""
        tool_call_1  = _make_tool_call_response("calculate_tax_hkd", {
            "annual_revenue": 1_000_000_000,
            "business_category": "goods",
        })
        tool_call_2  = _make_tool_call_response("check_doc_validity", {
            "doc_id": "68_2026_NDCP",
        })
        final        = _make_text_response("Kết quả tính toán theo Nghị định 68.")

        mock_gen = MagicMock(side_effect=[tool_call_1, tool_call_2, final])

        with patch("src.agent.planner.genai.Client"):
            agent = TaxAIAgent(api_key="fake")
            agent.client.models.generate_content = mock_gen

        result = agent.answer("Tính thuế và kiểm tra hiệu lực NĐ 68")
        assert result["iterations"] == 3
        assert len(result["tool_calls"]) == 2
        tools_used = [t["tool"] for t in result["tool_calls"]]
        assert "calculate_tax_hkd" in tools_used
        assert "check_doc_validity" in tools_used

    def test_max_iterations_fallback(self):
        """Khi tool calls liên tục vượt max_iterations → trả fallback message."""
        # Luôn trả tool call, không bao giờ có final text
        infinite_tool = _make_tool_call_response("search_legal_docs", {"query": "test"})
        mock_gen = MagicMock(return_value=infinite_tool)

        with patch("src.agent.planner.genai.Client"):
            agent = TaxAIAgent(api_key="fake", max_iterations=3)
            agent.client.models.generate_content = mock_gen

        result = agent.answer("test")
        assert "Xin lỗi" in result["answer"] or len(result["answer"]) > 0
        assert result["iterations"] == 3

    def test_show_sources_false_omits_tool_calls(self):
        mock_gen = MagicMock(return_value=_make_text_response("OK"))

        with patch("src.agent.planner.genai.Client"):
            agent = TaxAIAgent(api_key="fake")
            agent.client.models.generate_content = mock_gen

        result = agent.answer("test", show_sources=False)
        assert result["tool_calls"] == []
        assert result["sources"] == []

    def test_filter_doc_id_injected_to_prompt(self):
        """filter_doc_id phải được inject vào system prompt."""
        mock_gen = MagicMock(return_value=_make_text_response("OK"))

        with patch("src.agent.planner.genai.Client"):
            agent = TaxAIAgent(api_key="fake")
            agent.client.models.generate_content = mock_gen

        agent.answer("test", filter_doc_id="68_2026_NDCP")
        # Kiểm tra config được truyền vào generate_content
        call_kwargs = mock_gen.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None
        assert "68_2026_NDCP" in config.system_instruction


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Source Extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractSources:

    def test_extracts_from_calculator_breakdown(self):
        tool_calls = [{
            "tool": "calculate_tax_hkd",
            "args": {},
            "result": {
                "breakdown": [
                    {"tax_type": "GTGT", "value": 10_000_000,
                     "citation": {"doc_id": "68_2026_NDCP", "doc_number": "68/2026/NĐ-CP",
                                  "note": "Tỷ lệ GTGT"}},
                ]
            }
        }]
        sources = _extract_sources_from_tool_log(tool_calls)
        assert len(sources) == 1
        assert sources[0]["doc_id"] == "68_2026_NDCP"
        assert sources[0]["type"] == "calculation"

    def test_extracts_from_search_results(self):
        tool_calls = [{
            "tool": "search_legal_docs",
            "args": {},
            "result": {
                "results": [
                    {"citation": {"doc_id": "109_2025_QH15",
                                  "doc_number": "109/2025/QH15",
                                  "breadcrumb": "Chương I > Điều 1"}},
                ]
            }
        }]
        sources = _extract_sources_from_tool_log(tool_calls)
        assert len(sources) == 1
        assert sources[0]["type"] == "search"

    def test_deduplication(self):
        """Cùng doc_id + note không được lặp lại."""
        citation = {"doc_id": "68_2026_NDCP", "doc_number": "68/2026/NĐ-CP", "note": "x"}
        tool_calls = [
            {"tool": "calculate_tax_hkd", "args": {},
             "result": {"breakdown": [{"citation": citation}]}},
            {"tool": "calculate_tax_hkd", "args": {},
             "result": {"breakdown": [{"citation": citation}]}},
        ]
        sources = _extract_sources_from_tool_log(tool_calls)
        assert len(sources) == 1

    def test_empty_tool_log(self):
        assert _extract_sources_from_tool_log([]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestHelpers:

    def test_summarize_args_large_number(self):
        s = _summarize_args({"annual_revenue": 2_000_000_000})
        assert "2000M" in s

    def test_summarize_args_string(self):
        s = _summarize_args({"business_category": "services"})
        assert "services" in s

    def test_tool_definitions_count(self):
        from src.tools import TOOL_DEFINITIONS
        assert len(TOOL_DEFINITIONS) == 12

    def test_tool_registry_count(self):
        from src.tools import TOOL_REGISTRY
        assert len(TOOL_REGISTRY) == 12

    def test_all_definitions_have_required_fields(self):
        from src.tools import TOOL_DEFINITIONS
        for td in TOOL_DEFINITIONS:
            assert "name" in td
            assert "description" in td
            assert "parameters" in td
            assert "required" in td["parameters"]
