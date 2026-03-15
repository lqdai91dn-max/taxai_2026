"""
app.py — TaxAI Legal Chatbot — Streamlit UI

Chạy:
    streamlit run app.py
"""

from __future__ import annotations

import os
import sys
import time
import logging
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TaxAI — Tư vấn pháp luật thuế",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────

st.markdown("""
<style>
.validity-warning { color: #e67e22; font-size: 0.85em; }
.validity-pending  { color: #e74c3c; font-weight: bold; }
.source-item { background: #f8f9fa; border-left: 3px solid #3498db;
               padding: 6px 10px; margin: 4px 0; border-radius: 3px;
               font-size: 0.85em; }
.intent-badge { background: #2c3e50; color: white; border-radius: 12px;
                padding: 2px 10px; font-size: 0.75em; }
</style>
""", unsafe_allow_html=True)


# ── Load generator (cached) ───────────────────────────────────────────────────

@st.cache_resource(show_spinner="⏳ Đang tải TaxAI (lần đầu ~30s)...")
def load_generator():
    logging.disable(logging.WARNING)
    from src.generation.answer_generator import AnswerGenerator
    return AnswerGenerator()


@st.cache_data(ttl=60, show_spinner=False)
def get_docs_list():
    """Lấy danh sách văn bản từ Neo4j (cache 60s)."""
    try:
        from src.graph.neo4j_client import Neo4jClient
        with Neo4jClient() as c:
            rows = c.run("""
MATCH (d) WHERE d:Document OR d:GuidanceDocument
RETURN d.doc_id AS doc_id, d.doc_number AS num,
       d.doc_type AS dtype, d.status AS status,
       d.valid_from AS valid_from
ORDER BY d.hierarchy_rank, d.doc_id
""")
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚖️ TaxAI")
    st.caption("Tư vấn pháp luật thuế Việt Nam")
    st.divider()

    # Filter by doc
    docs = get_docs_list()
    doc_options = {"Tất cả văn bản": None}
    for d in docs:
        label = f"{d.get('num','?')} ({d.get('status','')})"
        doc_options[label] = d.get("doc_id")

    selected_label = st.selectbox(
        "Tìm trong văn bản:",
        options=list(doc_options.keys()),
        index=0,
    )
    filter_doc_id = doc_options[selected_label]

    st.divider()
    st.markdown("**Văn bản đang có hiệu lực:**")
    active_docs = [d for d in docs if d.get("status") == "active"]
    pending_docs = [d for d in docs if d.get("status") == "pending"]

    for d in active_docs:
        st.markdown(f"✅ `{d.get('num','?')}`")
    for d in pending_docs:
        st.markdown(
            f"<span class='validity-pending'>⏳ {d.get('num','?')} "
            f"(chờ hiệu lực {d.get('valid_from','')})</span>",
            unsafe_allow_html=True,
        )

    st.divider()
    if st.button("🗑️ Xóa lịch sử chat"):
        st.session_state.messages = []
        st.rerun()

    show_sources = st.toggle("Hiển thị nguồn trích dẫn", value=True)
    show_intent  = st.toggle("Hiển thị intent classifier", value=False)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _render_sources(sources: list, intent: str = ""):
    if not sources:
        return
    with st.expander(f"📚 Nguồn tham khảo ({len(sources)})", expanded=False):
        if intent and show_intent:
            st.markdown(
                f"<span class='intent-badge'>🎯 {intent}</span>",
                unsafe_allow_html=True,
            )
        for s in sources:
            validity_warn = ""
            if s.get("validity_ok") is False:
                validity_warn = " ⚠️ <span class='validity-warning'>Có thể hết hiệu lực</span>"
            st.markdown(
                f"<div class='source-item'>"
                f"<b>{s.get('document_number','')}</b> — {s.get('breadcrumb','')}"
                f"<br><small>Score: {s.get('score',0):.4f}{validity_warn}</small>"
                f"</div>",
                unsafe_allow_html=True,
            )


# ── Main ──────────────────────────────────────────────────────────────────────

st.title("TaxAI — Tư vấn pháp luật thuế Việt Nam")
st.caption(
    "Dựa trên Luật 109/2025/QH15, NĐ 68/2026/NĐ-CP, TT 152/2025/TT-BTC "
    "và các văn bản liên quan. Thông tin chỉ mang tính tham khảo."
)

# Khởi tạo chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Hiển thị lịch sử
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and "sources" in msg and show_sources:
            _render_sources(msg["sources"], msg.get("intent", ""))


# Gợi ý câu hỏi
if not st.session_state.messages:
    st.markdown("**Câu hỏi gợi ý:**")
    suggestions = [
        "Mức giảm trừ gia cảnh cho bản thân và người phụ thuộc là bao nhiêu?",
        "Hộ kinh doanh phải nộp những loại thuế gì?",
        "Thu nhập từ chuyển nhượng bất động sản bị đánh thuế thế nào?",
        "Nghị định nào hướng dẫn Luật Thuế TNCN 109/2025?",
        "Tôi có lương 20 triệu, 2 con, thuế TNCN phải nộp bao nhiêu?",
    ]
    cols = st.columns(2)
    for i, q in enumerate(suggestions):
        if cols[i % 2].button(q, key=f"sug_{i}", use_container_width=True):
            st.session_state._pending_question = q
            st.rerun()


# Xử lý câu hỏi từ suggestion button
question = None
if hasattr(st.session_state, "_pending_question"):
    question = st.session_state._pending_question
    del st.session_state._pending_question

# Chat input
chat_input = st.chat_input("Nhập câu hỏi về pháp luật thuế...")
if chat_input:
    question = chat_input

# Xử lý câu hỏi
if question:
    # Hiển thị câu hỏi
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    # Gọi TaxAI
    with st.chat_message("assistant"):
        with st.spinner("🔍 Đang tra cứu điều khoản..."):
            try:
                gen = load_generator()
                t0 = time.perf_counter()

                from src.retrieval.query_classifier import classify
                cq = classify(question)

                result = gen.answer(
                    question      = question,
                    filter_doc_id = filter_doc_id,
                    show_sources  = True,
                )
                latency = int((time.perf_counter() - t0) * 1000)

                answer = result["answer"]
                sources = result.get("sources", [])

                st.markdown(answer)
                st.caption(f"⏱️ {latency}ms | 🤖 {result['model']}")

                if show_sources and sources:
                    _render_sources(sources, cq.intent.value if show_intent else "")

                # Lưu vào history
                st.session_state.messages.append({
                    "role":    "assistant",
                    "content": answer,
                    "sources": sources,
                    "intent":  cq.intent.value,
                })

            except Exception as e:
                err = f"❌ Lỗi: {e}"
                st.error(err)
                st.session_state.messages.append({
                    "role": "assistant", "content": err
                })
