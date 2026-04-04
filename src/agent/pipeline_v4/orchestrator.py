"""
src/agent/pipeline_v4/orchestrator.py — Pipeline v4 Orchestrator (MVP)

Full flow:
  [INPUT]  query
      ↓
  [RETRIEVAL]  Vector search Top-K=20 (no hard filter)
      ↓
  [6.1]  LLM Legal Reasoner   → template_type + params_validated + assumptions
      ↓
  [VALIDATION LAYER]  Python  → check coverage + template consistency
      ↓  clarification_needed → short-circuit → user
      ↓
  [6.2]  Python Calculator    → run_template() → CalcOutput (LOCKED)
      ↓
  [6.3]  LLM Synthesizer      → format answer (read-only locked state)
      ↓
  [FINAL VALIDATOR]  Python   → VND present + assumption mention + citation
      ↓  fail → retry 6.3 (max 2x) → Rollback
      ↓
  [AUDIT]  log_pipeline_run()
      ↓
  [OUTPUT]  answer + tax_amount + citations + audit_ref

Rollback:
  Level 1: retry same template (max 2x via retry_count)
  Level 2: fallback simpler template (HKD_profit → HKD_percentage)
  Level 3: reset + re-retrieve   [TODO Full Production]
  Level 4: degrade gracefully (answer + strong disclaimer)

Strategy: chạy song song với R25. Switch khi benchmark v4 ≥ R25.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)


# ─── Prompt assembly: được handle bởi P6 prompt_assembler.py ─────────────────
# _LEGAL_REASONER_SYSTEM đã được thay thế bởi assemble_reasoner_prompt()
# Xem: src/agent/pipeline_v4/prompt_assembler.py


# ─── Calculation templates — các template cần Python Calculator ──────────────

_CALC_TEMPLATES = frozenset({
    "PIT_full", "PIT_progressive", "PIT_flat_20",
    "HKD_percentage", "HKD_profit", "deduction_calc",
})


# ─── Fallback template mapping (Rollback Level 2) ────────────────────────────

_FALLBACK_TEMPLATES: Dict[str, Optional[str]] = {
    "HKD_profit":      "HKD_percentage",   # nếu expenses unknown → fallback simple
    "PIT_progressive": "PIT_full",          # nếu taxable_income không tính được
    "PIT_full":        None,                # không có fallback → degrade
    "HKD_percentage":  None,
    "deduction_calc":  None,
}


# ─── Orchestrator ─────────────────────────────────────────────────────────────

class PipelineV4:
    """
    Pipeline v4 Orchestrator (MVP).

    Usage:
        pipeline = PipelineV4(searcher=hybrid_search, api_key="...")
        result = pipeline.run("Tôi bán hàng doanh thu 1.2 tỷ, thuế bao nhiêu?")
        # result["answer"], result["tax_amount"], result["citations"], ...
    """

    MAX_SYNTHESIZER_RETRIES = 2
    MAX_ROLLBACK_RETRIES    = 2

    def __init__(
        self,
        searcher,
        api_key:   Optional[str] = None,
        model:     str = "gemini-2.5-flash",
        log_audit: bool = True,
    ):
        self.searcher  = searcher
        self.api_key   = api_key
        self.model     = model
        self.log_audit = log_audit
        self._llm      = None   # lazy init

    def run(
        self,
        question:   str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Chạy full pipeline v4.

        Returns dict với:
          answer, tax_amount, citations, template, degrade_level,
          retry_count, assumptions, warnings, latency_ms, error
        """
        from src.agent.pipeline_v4.state import PipelineState
        from src.agent.pipeline_v4.audit import log_pipeline_run

        t_start = time.perf_counter()
        state = PipelineState(
            question   = question,
            session_id = session_id or str(uuid.uuid4())[:8],
        )
        state._audit("PIPELINE_START", {"model": self.model})

        try:
            # ── Step P5: QueryIntent Builder ──────────────────────────────────
            query_intent = self._step_build_intent(state)
            state._query_intent = query_intent   # P6: dùng trong prompt assembly

            # ── Step 1: Retrieval + NodeMetadata Reranker (P5.3) ─────────────
            self._step_retrieve(state, query_intent=query_intent)

            # ── Step 6.1: Legal Reasoner → Validation → 6.2: Calculator ──────
            success = self._step_reason_validate_calc(state)

            if not success:
                # Rollback Level 4: degrade
                self._step_degrade(state, "reason_validate_calc failed after rollbacks")
            elif state.has_calc_result:
                # ── Step 6.3: Synthesizer + Final Validator ───────────────────
                self._step_synthesize_with_retry(state)
            elif state.rag_path:
                # ── Explain/RAG path: synthesize từ retrieved chunks ──────────
                self._step_rag_answer(state)
            # else: clarification short-circuit — answer đã được set, không cần synthesize

        except Exception as e:
            logger.exception("PipelineV4 unexpected error: %s", e)
            state.error = str(e)
            state.degrade_level = 4
            state.synthesized_answer = self._degrade_message(str(e))
            state._audit("PIPELINE_ERROR", {"error": str(e)})

        finally:
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            if self.log_audit:
                log_pipeline_run(state, latency_ms=latency_ms)

        return self._build_result(state, latency_ms)

    # ── Step implementations ───────────────────────────────────────────────────

    def _step_build_intent(self, state):
        """P5.1 + P5.3: Build QueryIntent từ query. None nếu fail (graceful)."""
        try:
            from src.agent.pipeline_v4.query_intent import build_query_intent
            qi = build_query_intent(
                query     = state.question,
                api_key   = self.api_key,
                model     = self.model,
            )
            state._audit("QUERY_INTENT_BUILT", {
                "who":          qi.who.value if qi.who else None,
                "tax_domain":   qi.tax_domain.value if qi.tax_domain else None,
                "confidence":   qi.overall_confidence(),
            })
            return qi
        except Exception as e:
            logger.warning("QueryIntent Builder failed (non-fatal): %s", e)
            return None

    def _step_retrieve(self, state, query_intent=None) -> None:
        """Hybrid search Top-K=20 + NodeMetadata reranker (P5.3) nếu có query_intent."""
        try:
            # Truncate long queries: câu hỏi >200 chars làm loãng semantic signal
            # → lấy đến dấu '?' đầu tiên (câu hỏi chính), hoặc cắt 200 chars
            raw_q = state.question
            if len(raw_q) > 200:
                idx = raw_q.find("?")
                retrieval_query = raw_q[: idx + 1] if 0 < idx < 200 else raw_q[:200]
            else:
                retrieval_query = raw_q

            # QueryIntent-based query augmentation: thêm domain terms vào BM25
            # Ví dụ: "quán phở miễn thuế" + who=HKD → thêm "hộ kinh doanh" để hit 68_2026_NDCP
            if query_intent is not None:
                tax_domains = getattr(getattr(query_intent, "tax_domain", None), "value", []) or []
                who_val = getattr(getattr(query_intent, "who", None), "value", None)
                raw_lower = retrieval_query.lower()
                extras = []
                if "HKD" in tax_domains and "hộ kinh doanh" not in raw_lower:
                    extras.append("hộ kinh doanh")
                if "TMDT" in tax_domains and "thương mại điện tử" not in raw_lower:
                    extras.append("thương mại điện tử")
                if "PIT" in tax_domains and "thu nhập cá nhân" not in raw_lower and "tncn" not in raw_lower:
                    extras.append("thu nhập cá nhân")
                # Fix8: thuế khoán (old term) → bridge to new 2026 HKD terminology
                if "thuế khoán" in raw_lower and "khai thuế" not in raw_lower:
                    extras.append("khai thuế hộ kinh doanh")
                if extras:
                    retrieval_query = retrieval_query + " " + " ".join(extras)
                    logger.debug("[P5.aug] Query augmented: +%s", extras)

            # R32c: multi-query disabled (caused 180/225 citation changes + net regression)
            # BM25 per-doc cap (group-then-select cap=2) is still active in hybrid_search.py
            hits = self.searcher.search(
                query        = retrieval_query,
                n_results    = 20,
                query_intent = query_intent,
            )
            state.retrieved_chunks  = hits
            state.retrieved_doc_ids = list({
                h.get("metadata", {}).get("doc_id", "")
                for h in hits
                if h.get("metadata", {}).get("doc_id")
            })
            state._audit("RETRIEVED", {
                "n_chunks": len(hits),
                "doc_ids":  state.retrieved_doc_ids,
            })
        except Exception as e:
            logger.error("Retrieval failed: %s", e)
            state._audit("RETRIEVAL_ERROR", {"error": str(e)})
            # Continue với empty chunks — Legal Reasoner sẽ xử lý

    def _step_reason_validate_calc(self, state, override_template: Optional[str] = None) -> bool:
        """
        Steps 6.1 + Validation + 6.2.
        Returns True nếu thành công, False nếu cần degrade.
        """
        from src.agent.pipeline_v4.validation import (
            validate_reasoner_output, extract_calc_params
        )
        from src.agent.template_registry import run_template

        # ── 6.1: Legal Reasoner ───────────────────────────────────────────────
        reasoner_out = self._call_legal_reasoner(state, override_template)
        if reasoner_out is None:
            state._audit("REASONER_FAIL", {})
            return False
        state.legal_reasoner_out = reasoner_out

        # ── Explain/non-calc short-circuit ───────────────────────────────────
        # Nếu LLM trả template_type không phải calc template → route sang RAG path
        # (kể cả khi reasoner muốn clarification — RAG synthesizer tự xử lý)
        if reasoner_out.template_type not in _CALC_TEMPLATES:
            state.rag_path = True
            state._audit("EXPLAIN_PATH", {"template": reasoner_out.template_type})
            logger.info("[Explain path] template=%s → RAG synthesizer", reasoner_out.template_type)
            return True

        # ── Clarification short-circuit ───────────────────────────────────────
        if reasoner_out.clarification_needed:
            # Trước khi hỏi user, thử Rollback Level 2 nếu có fallback template.
            # Ví dụ: LLM chọn HKD_profit nhưng thiếu annual_expenses
            #        → patch sang HKD_percentage dùng params đã có (annual_revenue, business_category).
            # KHÔNG re-call LLM — patch deterministic để tránh LLM tiếp tục bỏ qua hint.
            fallback = _FALLBACK_TEMPLATES.get(reasoner_out.template_type)
            if fallback and not override_template:
                from src.agent.pipeline_v4.state import LegalReasonerOutput as _LRO
                # Lấy params có sẵn (bỏ params null — không cần cho fallback template)
                patched_params = {
                    k: v for k, v in reasoner_out.params_validated.items()
                    if isinstance(v, dict) and v.get("value") is not None
                }
                patched = _LRO(
                    template_type          = fallback,
                    params_validated       = patched_params,
                    assumptions            = reasoner_out.assumptions + [
                        f"Áp dụng phương pháp khoán ({fallback}) do thiếu thông tin chi phí."
                    ],
                    clarification_needed   = False,
                    clarification_question = None,
                    scenarios              = [],
                    raw_json               = reasoner_out.raw_json,
                )
                state.legal_reasoner_out = patched
                state.degrade_level = 2
                state._audit("ROLLBACK_L2_PATCH", {
                    "from": reasoner_out.template_type, "to": fallback,
                    "patched_params": list(patched_params.keys()),
                })
                logger.info(
                    "[Rollback L2] Patch %s → %s, params=%s",
                    reasoner_out.template_type, fallback, list(patched_params.keys()),
                )
                # Tiếp tục với reasoner_out đã patch (gán lại biến cục bộ)
                reasoner_out = patched
            else:
                # Không có fallback (hoặc đang là lần override) → route sang RAG path
                # Thay vì hỏi user, tổng hợp câu trả lời từ retrieved docs.
                # Lý do: "thiếu params" thường là query rate-lookup (không cần tính toán),
                # RAG synthesizer có thể trả lời từ tài liệu pháp lý trực tiếp.
                state.rag_path = True
                state._audit("CLARIFICATION_FALLBACK_TO_RAG", {
                    "template": reasoner_out.template_type,
                    "clarification_q": reasoner_out.clarification_question,
                })
                logger.info(
                    "[Clarification→RAG] template=%s no fallback → RAG path",
                    reasoner_out.template_type,
                )
                return True

        # ── Validation ────────────────────────────────────────────────────────
        retrieved_ids = set()
        for chunk in state.retrieved_chunks:
            cid = chunk.get("chunk_id") or chunk.get("id")
            if cid:
                retrieved_ids.add(cid)

        val_result = validate_reasoner_output(
            template_type         = reasoner_out.template_type,
            params_validated      = reasoner_out.params_validated,
            clarification_needed  = False,
            clarification_question= None,
            retrieved_chunk_ids   = retrieved_ids or None,
        )

        if not val_result.valid:
            logger.warning("Validation failed: %s", val_result.errors)
            state._audit("VALIDATION_FAIL", {"errors": val_result.errors})

            # Rollback Level 2: try simpler template
            fallback = _FALLBACK_TEMPLATES.get(reasoner_out.template_type)
            if fallback and state.retry_count < self.MAX_ROLLBACK_RETRIES:
                state.unlock_for_rollback(f"Validation fail → fallback to {fallback}")
                state.degrade_level = 2
                return self._step_reason_validate_calc(state, override_template=fallback)
            return False

        # ── 6.2: Python Calculator ────────────────────────────────────────────
        calc_params = extract_calc_params(reasoner_out.params_validated)
        template_type = reasoner_out.template_type

        try:
            tmpl_result = run_template(
                template_name = template_type,
                params        = calc_params,
                assumptions   = reasoner_out.assumptions,
            )
        except (ValueError, KeyError) as e:
            logger.warning("Calculator failed for %s: %s", template_type, e)
            state._audit("CALCULATOR_FAIL", {"template": template_type, "error": str(e)})

            # Rollback Level 1: retry same template (max 2x)
            if state.retry_count < self.MAX_ROLLBACK_RETRIES:
                state.unlock_for_rollback(f"Calculator fail: {e}")
                state.degrade_level = 2
                return self._step_reason_validate_calc(state)
            return False

        from src.agent.pipeline_v4.state import CalcOutput
        state.set_calc_output(CalcOutput(
            template       = tmpl_result.template,
            version        = tmpl_result.version,
            tax_amount     = tmpl_result.tax_amount,
            breakdown      = tmpl_result.breakdown,
            effective_rate = tmpl_result.effective_rate,
            citations      = tmpl_result.citations,
            warnings       = tmpl_result.warnings,
            assumptions    = tmpl_result.assumptions,
        ))
        # state is now LOCKED
        return True

    def _step_synthesize_with_retry(self, state) -> None:
        """Step 6.3 + Final Validator với retry."""
        from src.agent.pipeline_v4.final_validator import validate_synthesized_answer

        for attempt in range(self.MAX_SYNTHESIZER_RETRIES + 1):
            answer = self._call_synthesizer(state)
            if not answer:
                continue

            # Validate
            has_assumptions = bool(
                state.calc_output and state.calc_output.assumptions
            )
            val = validate_synthesized_answer(
                answer          = answer,
                tax_amount      = state.tax_amount,
                has_assumptions = has_assumptions,
                assumption_risk = "medium" if has_assumptions else "low",
            )

            if val.valid:
                state.set_synthesized_answer(answer)
                state._audit("SYNTHESIZER_OK", {"attempt": attempt})
                return

            logger.warning(
                "Synthesizer validation fail (attempt %d/%d): %s",
                attempt + 1, self.MAX_SYNTHESIZER_RETRIES + 1, val.errors,
            )
            state._audit("SYNTHESIZER_RETRY", {
                "attempt": attempt,
                "errors":  val.errors,
            })

        # Max retries reached — degrade
        logger.error("Synthesizer failed after %d attempts", self.MAX_SYNTHESIZER_RETRIES + 1)
        self._step_degrade(state, "Synthesizer validation failed after max retries")

    def _step_rag_answer(self, state) -> None:
        """
        RAG-based answer cho explain/threshold queries.
        Synthesize trực tiếp từ retrieved chunks, không qua Calculator.
        """
        # Build citations từ chunks
        state.citations = self._build_rag_citations(state.retrieved_chunks)

        # Gọi RAG synthesizer
        answer = self._call_rag_synthesizer(state)
        if answer:
            state.synthesized_answer = answer
            state._audit("RAG_ANSWER_OK", {"n_citations": len(state.citations)})
        else:
            self._step_degrade(state, "RAG synthesizer returned empty")

    def _step_degrade(self, state, reason: str) -> None:
        """Level 4: degrade gracefully với disclaimer."""
        state.degrade_level = 4
        state._audit("DEGRADE", {"reason": reason})
        disclaimer = (
            "Tôi gặp khó khăn trong việc tính toán chính xác cho câu hỏi này. "
            "Vui lòng liên hệ tư vấn viên thuế để được hỗ trợ cụ thể. "
            "Thông tin tham khảo: "
        )
        if state.retrieved_chunks:
            # Trả về relevant snippets từ RAG
            snippets = [
                c.get("text", c.get("snippet", ""))[:200]
                for c in state.retrieved_chunks[:3]
                if c.get("text") or c.get("snippet")
            ]
            if snippets:
                disclaimer += "\n\n" + "\n\n".join(snippets)
        state.synthesized_answer = disclaimer

    # ── LLM calls ─────────────────────────────────────────────────────────────

    def _call_legal_reasoner(self, state, override_template: Optional[str] = None):
        """Gọi LLM Legal Reasoner (step 6.1) với P6 Dynamic Prompt Assembly."""
        from src.agent.pipeline_v4.state import LegalReasonerOutput
        from src.agent.pipeline_v4.prompt_assembler import (
            assemble_reasoner_prompt, estimate_token_count
        )

        # ── P6: Assemble prompt tất định từ QueryIntent + chunks ──────────
        query_intent = getattr(state, "_query_intent", None)
        system_prompt = assemble_reasoner_prompt(
            query_intent    = query_intent,
            retrieved_chunks= state.retrieved_chunks,
            raw_query       = state.question,
        )

        # Thêm hint nếu có override_template (từ rollback)
        if override_template:
            system_prompt += f"\n\nGợi ý rollback: ưu tiên sử dụng template_type='{override_template}'."

        est_tokens = estimate_token_count(system_prompt)
        state._audit("PROMPT_ASSEMBLED", {
            "domains":    getattr(getattr(query_intent, "tax_domain", None), "value", None),
            "est_tokens": est_tokens,
            "chunks_used": min(len(state.retrieved_chunks), 12),
        })
        logger.debug("[P6] Assembled prompt ~%d tokens", est_tokens)

        # [G3] Params quality guard — inject hint nếu cần
        from src.agent.pipeline_v4.llm_guard import check_params_quality

        guard = self._get_llm()
        data, llm_meta = guard.call_reasoner(
            system_prompt = system_prompt,
            user_msg      = "Trả lời JSON theo schema đã quy định.",
        )

        state._audit("LEGAL_REASONER_CALL", {
            "attempts":    llm_meta["attempts"],
            "parse_ok":    llm_meta["parse_ok"],
            "schema_ok":   llm_meta["schema_ok"],
            "latency_ms":  llm_meta["latency_ms"],
            "compute_violations": llm_meta.get("compute_violations", []),
        })

        if data is None:
            state._audit("LEGAL_REASONER_ERROR", {"errors": llm_meta["errors"]})
            return None

        # [G3] Params quality guard
        template_type = data.get("template_type", "")
        params        = data.get("params_validated", {})
        params_ok, clarify_q = check_params_quality(params, template_type)
        if not params_ok:
            # Nếu có fallback template (e.g. HKD_profit → HKD_percentage), KHÔNG trigger
            # clarification — để Validation Layer + Rollback Level 2 tự xử lý.
            # Chỉ trigger clarification khi không có fallback (không thể simplify hơn).
            has_fallback = _FALLBACK_TEMPLATES.get(template_type) is not None
            if not has_fallback:
                data["clarification_needed"]  = True
                data["clarification_question"] = clarify_q
            # Nếu has_fallback: để _step_reason_validate_calc xử lý rollback

        out = LegalReasonerOutput(
            template_type         = template_type,
            params_validated      = params,
            assumptions           = data.get("assumptions", []),
            clarification_needed  = bool(data.get("clarification_needed", False)),
            clarification_question= data.get("clarification_question"),
            scenarios             = data.get("scenarios", []),
            raw_json              = json.dumps(data),
        )
        state._audit("LEGAL_REASONER_OK", {
            "template":             out.template_type,
            "clarification_needed": out.clarification_needed,
            "params_count":         len(out.params_validated),
        })
        return out

    def _call_synthesizer(self, state) -> Optional[str]:
        """Gọi LLM Synthesizer (step 6.3) — read-only locked state."""
        if not state.has_calc_result:
            return None

        calc = state.calc_output
        breakdown_text = self._format_breakdown(calc.breakdown)
        assumptions_text = (
            "\n".join(f"- {a}" for a in calc.assumptions)
            if calc.assumptions else "Không có."
        )
        citations_text = self._format_citations(calc.citations)

        system = (
            "Bạn là tư vấn viên thuế Việt Nam. Trình bày kết quả tính thuế cho người dùng.\n\n"
            "Quy tắc:\n"
            "- KHÔNG tự tính lại thuế — chỉ dùng số liệu đã cung cấp\n"
            "- Trình bày rõ ràng, dễ hiểu (người dùng không phải chuyên gia)\n"
            "- Bắt buộc trích dẫn văn bản pháp luật nguồn\n"
            "- Nếu có giả định → đề cập rõ để người dùng tự đối chiếu\n"
        )

        user = (
            f"Câu hỏi: {state.question}\n\n"
            f"Kết quả tính thuế (KHÔNG được thay đổi):\n"
            f"  Template: {calc.template} ({calc.version})\n"
            f"  Tổng thuế phải nộp: {calc.tax_amount:,} VND\n"
            f"  Thuế suất hiệu dụng: {calc.effective_rate:.2%}\n\n"
            f"Chi tiết:\n{breakdown_text}\n\n"
            f"Giả định:\n{assumptions_text}\n\n"
            f"Nguồn pháp lý:\n{citations_text}\n\n"
            "Hãy viết câu trả lời tư vấn cho người dùng."
        )

        guard = self._get_llm()
        answer, synth_meta = guard.call_synthesizer(system_prompt=system, user_msg=user)
        state._audit("SYNTHESIZER_CALL", {
            "attempts":   synth_meta["attempts"],
            "latency_ms": synth_meta["latency_ms"],
        })
        return answer

    def _build_rag_citations(self, chunks: List[dict]) -> List[dict]:
        """Tổng hợp citations từ retrieved chunks cho RAG path.
        Lấy top-4 unique doc_ids theo thứ tự rank để tối ưu F1 precision/recall.
        Analysis: expected docs thường ở rank 1-3, cap=8 làm precision quá thấp.
        """
        seen: set = set()
        citations = []
        for chunk in chunks:  # iterate all chunks in rank order
            doc_id = chunk.get("metadata", {}).get("doc_id", "")
            if not doc_id or doc_id in seen:
                continue
            seen.add(doc_id)
            title = chunk.get("metadata", {}).get("title", "")
            citations.append({"doc_number": doc_id, "title": title, "note": "RAG"})
            if len(citations) >= 3:  # cap at 3 unique docs (R38: precision cut, Phase 2A)
                break
        return citations

    def _call_rag_synthesizer(self, state) -> Optional[str]:
        """
        Gọi LLM để tổng hợp câu trả lời từ RAG chunks (explain/threshold path).
        Không có calc result — trả lời trực tiếp từ tài liệu.
        """
        # Diverse context: đảm bảo mỗi retrieved doc có ít nhất 1 chunk trong context
        # Tránh trường hợp đúng doc được retrieve nhưng chunk đại diện bị đẩy ra ngoài top-8
        rag_context = self._format_rag_context_diverse(state.retrieved_chunks, query=state.question)

        system = (
            "Bạn là chuyên gia tư vấn thuế Việt Nam. Nhiệm vụ: trả lời câu hỏi pháp lý thuế "
            "dựa trên tài liệu pháp lý được cung cấp.\n\n"
            "Quy tắc BẮT BUỘC:\n"
            "- CHỈ sử dụng thông tin từ tài liệu pháp lý bên dưới — tuyệt đối không tự suy diễn\n"
            "- PHẢI đưa vào câu trả lời các thông tin CỤ THỂ có trong tài liệu:\n"
            "  + Con số chính xác (%, tỷ lệ, ngưỡng, thời hạn)\n"
            "  + Điều kiện pháp lý cụ thể (ai được/không được, khi nào, ở đâu)\n"
            "  + Nghĩa vụ cụ thể (phải làm gì, trong bao lâu)\n"
            "  → KHÔNG được gộp chung hay nói 'theo quy định' mà không nêu nội dung cụ thể\n"
            "- Khi trích dẫn, ghi tên văn bản + số điều ngay sau câu đó\n"
            "  Ví dụ: '...doanh thu ≤ 500 triệu/năm được miễn TNCN (Nghị định 68/2026/NĐ-CP, Điều 3).'\n"
            "- Nếu tài liệu không đủ thông tin → nêu rõ giới hạn, không bịa đặt\n"
            "- Ngôn ngữ rõ ràng, dễ hiểu cho người không phải chuyên gia thuế\n"
            "- P7.2: Nếu người dùng đưa ra một con số cụ thể (ví dụ: 20%, 8%) nhưng tài liệu "
            "cho thấy con số khác hoặc không có quy định đó → BẮT BUỘC nêu rõ con số của người "
            "dùng là SAI/không chính xác trước, sau đó trích dẫn thông tin đúng từ tài liệu\n"
        )

        user = (
            f"Câu hỏi: {state.question}\n\n"
            f"Tài liệu pháp lý:\n{rag_context}\n\n"
            "Hãy trả lời câu hỏi trên dựa vào tài liệu pháp lý đã cung cấp."
        )

        guard = self._get_llm()
        answer, meta = guard.call_synthesizer(system_prompt=system, user_msg=user)
        state._audit("RAG_SYNTHESIZER_CALL", {
            "attempts":   meta["attempts"],
            "latency_ms": meta["latency_ms"],
        })
        return answer

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_llm(self):
        """Lazy init LLMGuard (stabilization wrapper)."""
        if self._llm is None:
            from src.agent.pipeline_v4.llm_guard import LLMGuard
            self._llm = LLMGuard(api_key=self.api_key, model=self.model)
        return self._llm

    # P6.1: tăng từ 10→13 để đủ chỗ cho fact depth (tiết children) + doc breadth (2nd doc)
    # Đủ buffer mà không spam LLM: 3 slots 117 + 2 slots 68 + 2 slots khác + 6 tiết/guard
    MAX_CONTEXT_CHUNKS = 13

    # P7: patterns để phát hiện "số có nghĩa" trong context thuế
    # Loại trừ số thứ tự cấu trúc (Điều 1, Khoản 3, Năm 2026) — chỉ bắt con số mang giá trị thuế
    _P7_MEANINGFUL_NUM_PAT = re.compile(
        r'\d+[.,]?\d*\s*%'                     # tỷ lệ/thuế suất: 1%, 0.5%, 1,5%
        r'|\d+\s*(triệu|nghìn|tỷ|đồng)\b'      # số tiền
        r'|\d+\s*(ngày|tháng|giờ)\b'           # thời hạn
    )
    # P7: từ khoá query báo hiệu cần số liệu cụ thể
    _P7_NUMERIC_INTENT_PAT = re.compile(
        r'%|bao nhiêu|tỷ lệ|mức thuế|thuế suất|thuế khoán|phần trăm'
        r'|thời hạn|bao lâu|mấy ngày|mấy tháng|số tiền|doanh thu'
        r'|tối đa|tối thiểu|hạn mức|mức.*là|suất.*là',
        re.IGNORECASE,
    )
    # P7.1: skip guard khi query đang verify một claim (không hỏi "số là bao nhiêu?")
    # Case 1 — hearsay: user nghe tin đồn, đang verify existence
    _P7_HEARSAY_PAT = re.compile(
        r'nghe nói|tin đồn|mạng bảo|người ta bảo|nghe đồn|có người nói',
        re.IGNORECASE,
    )
    # Case 2 — verify claim: query tự chứa số cụ thể + signal xác minh
    _P7_VERIFY_CLAIM_NUM_PAT = re.compile(r'\d+\s*%')   # số % do user đưa ra
    _P7_VERIFY_SIGNAL_PAT = re.compile(
        r'đúng không|có phải|có đúng|như vậy không|vậy có đúng|thật không|có thật',
        re.IGNORECASE,
    )

    @staticmethod
    def _p7_has_meaningful_number(text: str) -> bool:
        """True nếu text chứa con số có nghĩa trong context thuế (tỷ lệ, tiền, thời hạn)."""
        return bool(PipelineV4._P7_MEANINGFUL_NUM_PAT.search(text))

    def _p7_should_skip_guard(self, query: str) -> bool:
        """P7.1: True nếu query đang verify một claim cụ thể — guard sẽ inject sai context.

        Case — verify specific claim: "Giảm 20% có đúng không?"
            → user đưa số cụ thể vào query + tín hiệu xác minh → guard inject số khác = confuse LLM

        NOTE: hearsay pattern ("nghe nói") đã bị bỏ vì false positive cao:
            Q76 "nghe nói bỏ lệ phí môn bài" — claim là TRUE → guard injection giúp LLM cite đúng từ.
            Bỏ hearsay skip để guard vẫn chạy cho các câu hearsay claim đúng.
        """
        if self._P7_VERIFY_CLAIM_NUM_PAT.search(query) and self._P7_VERIFY_SIGNAL_PAT.search(query):
            return True
        return False

    def _format_rag_context_diverse(
        self,
        chunks: List[dict],
        max_chunks: int = MAX_CONTEXT_CHUNKS,
        query: Optional[str] = None,
    ) -> str:
        """
        Tạo RAG context với doc diversity: đảm bảo mỗi retrieved doc có ít nhất 1 chunk.

        Strategy:
          1. Lấy best chunk (theo rank) từ mỗi unique doc → 1 representative per doc
          2. Điền slot còn lại bằng top-ranked chunks (bất kể doc nào)
          3. Tổng max_chunks chunks (P6.1: 13), đảm bảo diversity
          4. P6: child_expansion_of chunks bypass per-doc cap=2 (piggyback on parent)
          5. P7: nếu query cần số liệu mà selected thiếu → inject 1 meaningful chunk
        """
        if not chunks:
            return "(Không có tài liệu RAG)"

        # Step 1: best chunk per doc (theo rank)
        doc_repr: dict = {}
        for chunk in chunks:
            doc_id = chunk.get("metadata", {}).get("doc_id", "")
            if doc_id and doc_id not in doc_repr:
                doc_repr[doc_id] = chunk

        # Step 2: top-ranked chunks including representatives
        selected = []
        selected_ids: set = set()
        doc_chunk_counts: dict = {}   # R31: per-doc cap để tránh 1 doc chiếm nhiều slots
        child_slots = 0               # P6.1: track child expansion slots for logging
        # First pass: top chunks, stop at max_chunks (max 2 chunks per doc)
        # P6: child_expansion_of chunks bypass cap (họ piggyback trên parent đã selected)
        for chunk in chunks[:max_chunks * 2]:  # scan thêm để bù slots bị cap
            cid = chunk.get("chunk_id") or chunk.get("id")
            doc_id = chunk.get("metadata", {}).get("doc_id", "")
            if cid in selected_ids:
                continue
            is_child_expansion = bool(chunk.get("child_expansion_of"))
            if doc_chunk_counts.get(doc_id, 0) >= 2 and not is_child_expansion:  # R31 + P6
                continue
            selected.append(chunk)
            selected_ids.add(cid)
            if is_child_expansion:
                child_slots += 1
            else:
                doc_chunk_counts[doc_id] = doc_chunk_counts.get(doc_id, 0) + 1
            if len(selected) >= max_chunks:
                break

        # Second pass: inject representatives of docs not yet present
        for doc_id, repr_chunk in doc_repr.items():
            if len(selected) >= max_chunks:
                break
            cid = repr_chunk.get("chunk_id") or repr_chunk.get("id")
            if cid and cid not in selected_ids:
                selected.append(repr_chunk)
                selected_ids.add(cid)

        # P7 Guard: nếu query cần số nhưng selected không có → inject 1 chunk tốt nhất từ pool
        # Append cuối (không reorder) — chỉ bổ sung, không thay thế
        # Được phép vượt max_chunks thêm 1 slot (guard slot dự phòng)
        # P7.1: skip nếu query đang verify một claim (hearsay / user tự đưa số vào)
        guard_injected = False
        if query and self._P7_NUMERIC_INTENT_PAT.search(query) and not self._p7_should_skip_guard(query):
            has_num = any(self._p7_has_meaningful_number(c.get("text", "")) for c in selected)
            if not has_num:
                # Tìm chunk tốt nhất trong pool (đã RRF-ranked) chứa số có nghĩa
                for candidate in chunks:
                    cid = candidate.get("chunk_id") or candidate.get("id")
                    if cid in selected_ids:
                        continue
                    if self._p7_has_meaningful_number(candidate.get("text", "")):
                        selected.append(candidate)
                        selected_ids.add(cid)
                        guard_injected = True
                        logger.info(
                            "[P7-Guard] numeric intent detected, injected chunk: %s (total=%d)",
                            cid, len(selected),
                        )
                        break

        logger.debug(
            "[P6.1] context: max=%d selected=%d child_slots=%d docs=%d guard=%s",
            max_chunks, len(selected), child_slots, len(doc_chunk_counts), guard_injected,
        )
        return self._format_rag_context(selected)

    @staticmethod
    def _second_level_rrf(
        results_lists: List[List[dict]],
        k: int = 60,
        top_n: int = 25,
    ) -> List[dict]:
        """Second-Level RRF: merge N ranked hit-lists (R32 multi-query).

        Mỗi list đã được hybrid_search xử lý (RRF + reranker + expansions).
        Dùng rank position (không phải score value) để tránh double-counting.
        Chunk xuất hiện trong nhiều lists → cộng dồn RRF rank signal → ranked cao.

        Args:
            results_lists: list of ranked chunk lists (từ hybrid_search.search())
            k:             RRF constant (default 60)
            top_n:         số kết quả trả về

        Returns:
            Merged and re-ranked list, rrf_score là Second-Level RRF score.
        """
        chunk_scores: Dict[str, float] = {}
        chunk_data:   Dict[str, dict]  = {}

        for res_list in results_lists:
            for rank, chunk in enumerate(res_list):
                cid = chunk.get("chunk_id") or chunk.get("id", "")
                if not cid:
                    continue
                if cid not in chunk_data:
                    chunk_data[cid] = chunk
                    chunk_scores[cid] = 0.0
                chunk_scores[cid] += 1.0 / (k + rank + 1)

        sorted_ids = sorted(chunk_scores, key=lambda x: chunk_scores[x], reverse=True)

        merged = []
        for cid in sorted_ids[:top_n]:
            hit = chunk_data[cid].copy()
            hit["rrf_score"] = round(chunk_scores[cid], 6)
            merged.append(hit)

        return merged

    def _format_rag_context(self, chunks: List[dict]) -> str:
        if not chunks:
            return "(Không có tài liệu RAG)"
        parts = []
        for i, chunk in enumerate(chunks[:15], 1):
            chunk_id = chunk.get("chunk_id") or chunk.get("id") or f"chunk_{i}"
            text     = chunk.get("text") or chunk.get("snippet") or ""
            doc_id   = chunk.get("metadata", {}).get("doc_id", "")
            breadcrumb = chunk.get("metadata", {}).get("breadcrumb", "")
            parts.append(
                f"[{chunk_id}] ({doc_id} | {breadcrumb})\n{text[:500]}"
            )
        return "\n\n---\n\n".join(parts)

    def _format_breakdown(self, breakdown) -> str:
        if not breakdown:
            return "(Không có chi tiết)"
        if isinstance(breakdown, list):
            lines = []
            for item in breakdown:
                desc  = item.get("description", "")
                val   = item.get("value", item.get("tax_in_bracket", ""))
                formula = item.get("formula", "")
                lines.append(f"  - {desc}: {val:,}" if isinstance(val, int) else f"  - {desc}")
                if formula:
                    lines.append(f"    {formula}")
            return "\n".join(lines)
        return str(breakdown)

    def _format_citations(self, citations: List[dict]) -> str:
        if not citations:
            return "(Không có)"
        seen = set()
        lines = []
        for c in citations:
            doc_num = c.get("doc_number", c.get("doc_id", ""))
            if doc_num not in seen:
                seen.add(doc_num)
                title = c.get("title", "")[:60]
                note  = c.get("note", "")
                lines.append(f"  - {doc_num}: {title}" + (f" ({note})" if note else ""))
        return "\n".join(lines)

    def _degrade_message(self, error: str) -> str:
        return (
            "Xin lỗi, hệ thống gặp sự cố khi xử lý câu hỏi của bạn. "
            "Vui lòng thử lại hoặc liên hệ tư vấn viên thuế trực tiếp."
        )

    def _build_result(self, state, latency_ms: int) -> Dict[str, Any]:
        """Build output dict từ final state."""
        lr = state.legal_reasoner_out
        qi = getattr(state, "_query_intent", None)

        # Collect compute violations from audit trail
        compute_violations: List[str] = []
        for entry in state._audit_entries:
            v = entry.get("data", {}).get("compute_violations")
            if v:
                compute_violations.extend(v)

        # LLM meta from last reasoner call
        meta_reasoner: Dict[str, Any] = {}
        for entry in reversed(state._audit_entries):
            if entry.get("event") == "LEGAL_REASONER_CALL":
                meta_reasoner = entry.get("data", {})
                break

        return {
            # Core
            "answer":        state.synthesized_answer or "",
            "tax_amount":    state.tax_amount,
            "citations":     state.citations or (
                state.calc_output.citations if state.calc_output else []
            ),
            "template":      state.calc_output.template if state.calc_output else (
                lr.template_type if lr else None
            ),
            "template_ver":  state.calc_output.version  if state.calc_output else None,
            # Clarification
            "clarification_needed":   lr.clarification_needed   if lr else False,
            "clarification_question": lr.clarification_question if lr else None,
            # Quality
            "assumptions":   state.calc_output.assumptions if state.calc_output else [],
            "warnings":      state.calc_output.warnings   if state.calc_output else [],
            "breakdown":     state.calc_output.breakdown  if state.calc_output else None,
            # Guard meta
            "compute_violations": compute_violations,
            "meta_reasoner":      meta_reasoner,
            # Debug
            "intent_summary": qi.to_dict() if qi and hasattr(qi, "to_dict") else None,
            "top_chunks": [
                {
                    "chunk_id": c.get("chunk_id") or c.get("id"),
                    "doc_id":   c.get("metadata", {}).get("doc_id"),
                    "score":    c.get("final_score") or c.get("rrf_score"),
                    "snippet":  (c.get("text") or c.get("snippet") or "")[:300],
                }
                for c in state.retrieved_chunks[:10]   # store top-10 = max_chunks used by synthesizer
            ],
            # Meta
            "degrade_level": state.degrade_level,
            "retry_count":   state.retry_count,
            "doc_ids":       state.retrieved_doc_ids,
            "session_id":    state.session_id,
            "latency_ms":    latency_ms,
            "error":         state.error,
        }
