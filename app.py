"""
app.py — TaxAI Legal Chatbot — Streamlit UI

Chạy:
    streamlit run app.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

TZ_VN           = timezone(timedelta(hours=7))
HISTORY_DIR     = Path("data/chat_history")
PARSED_DIR      = Path("data/parsed")

# Map doc_number → doc_id (file name)
DOC_NUMBER_MAP: dict[str, str] = {
    "108/2025/QH15":          "108_2025_QH15",
    "109/2025/QH15":          "109_2025_QH15",
    "68/2026/NĐ-CP":          "68_2026_NDCP",
    "117/2025/NĐ-CP":         "117_2025_NDCP",
    "126/2020/NĐ-CP":         "126_2020_NDCP",
    "125/2020/NĐ-CP":         "125_2020_NDCP",
    "310/2025/NĐ-CP":         "310_2025_NDCP",
    "373/2025/NĐ-CP":         "373_2025_NDCP",
    "20/2026/NĐ-CP":          "20_2026_NDCP",
    "152/2025/TT-BTC":        "152_2025_TTBTC",
    "18/2026/TT-BTC":         "18_2026_TTBTC",
    "111/2013/TT-BTC":        "111_2013_TTBTC",
    "92/2015/TT-BTC":         "92_2015_TTBTC",
    "86/2024/TT-BTC":         "86_2024_TTBTC",
    "110/2025/UBTVQH15":      "110_2025_UBTVQH15",
    "110/2025/NQ-UBTVQH15":   "110_2025_UBTVQH15",
    "149/2025/QH15":          "149_2025_QH15",
    "198/2025/QH15":          "198_2025_QH15",
}

# Regex: "Điều X" + optional "Khoản Y" + optional doc reference
# Xử lý cả:
#   "Điều 7 Nghị định 68/2026"           (space trước tên VB)
#   "Điều 7 Luật ..., số 109/2025/QH15"  (space + dấu phẩy)
#   "Điều 1, Nghị quyết số 110/2025/..."  (dấu phẩy trước tên VB)
_CITATION_RE = re.compile(
    r'(?:khoản\s+\d+\s+)?'
    r'Điều\s+(\d+)'
    r'(?:\s+Khoản\s+\d+)?'
    r'(?:[,\s]\s*'                          # dấu phẩy HOẶC space trước tên VB
        r'(?:Luật(?:\s+Thuế[^,;\n]{0,60})?|Nghị\s+định|Thông\s+tư|Nghị\s+quyết|Quyết\s+định)'
        r'[^;\n]{0,80}?'
        r'(?:số\s+)?(\d+/\d{4}/[A-Z0-9\-Đ]+)'
    r')?',
    re.UNICODE,
)


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
/* Chat bubbles */
.msg-user {
    background: #e8f4fd; border-radius: 12px 12px 4px 12px;
    padding: 10px 14px; margin: 6px 0 6px 15%;
    border-left: 3px solid #3498db;
}
.msg-ai {
    background: #f8f9fa; border-radius: 12px 12px 12px 4px;
    padding: 10px 14px; margin: 6px 15% 6px 0;
    border-left: 3px solid #27ae60;
}
.msg-time {
    font-size: 0.72em; color: #888; margin-top: 4px; text-align: right;
}
/* Inline citations */
.inline-cite {
    color: #1a6fa3; text-decoration: underline dotted; font-weight: 500;
    cursor: default;
}
.cite-sup {
    color: #1a6fa3; font-size: 0.72em; font-weight: 700;
    vertical-align: super; line-height: 0;
}
/* Sources */
.source-item  { background: #f8f9fa; border-left: 3px solid #3498db;
                padding: 6px 10px; margin: 4px 0; border-radius: 3px; font-size: 0.85em; }
.source-calc  { border-left-color: #27ae60; }
.source-validity { border-left-color: #8e44ad; }
/* Badges */
.iter-badge   { background: #16a085; color: white; border-radius: 12px;
                padding: 2px 8px; font-size: 0.75em; margin-left: 6px; }
.cache-badge  { background: #f39c12; color: white; border-radius: 12px;
                padding: 2px 8px; font-size: 0.75em; margin-left: 6px; }
/* Legal popup */
.law-title    { font-size: 1.05em; font-weight: 700; color: #1a6fa3; margin-bottom: 8px; }
.law-content  { line-height: 1.7; }
.law-khoan    { background: #f0f7ff; border-left: 3px solid #3498db;
                padding: 8px 12px; margin: 6px 0; border-radius: 4px; }
/* Citation / Answer section labels */
.section-label {
    display: inline-block;
    font-size: 0.75em; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase; border-radius: 4px;
    padding: 3px 10px; margin: 10px 0 6px 0;
}
.citation-label {
    background: #dbeafe; color: #1e40af;
    border: 1px solid #93c5fd;
}
.answer-label {
    background: #dcfce7; color: #166534;
    border: 1px solid #86efac;
}
.section-divider {
    border: none; border-top: 1px dashed #d1d5db;
    margin: 12px 0 4px 0;
}
</style>
""", unsafe_allow_html=True)


# ── Legal content lookup ──────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_parsed(doc_id: str) -> list:
    path = PARSED_DIR / f"{doc_id}.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("data", [])


def _find_article(nodes: list, dieu_num: str) -> Optional[dict]:
    for node in nodes:
        if node.get("node_type") == "Điều" and str(node.get("node_index", "")) == dieu_num:
            return node
        found = _find_article(node.get("children", []), dieu_num)
        if found:
            return found
    return None


def fetch_article_content(doc_number: str, dieu_num: str) -> Optional[dict]:
    """Lấy nội dung Điều từ parsed JSON. Trả về dict node hoặc None."""
    doc_id = DOC_NUMBER_MAP.get(doc_number)
    if not doc_id:
        return None
    nodes = _load_parsed(doc_id)
    if not nodes:
        return None
    return _find_article(nodes, dieu_num)


# ── Citation parser ───────────────────────────────────────────────────────────

def parse_citations(text: str) -> tuple[str, list[dict]]:
    """
    Tìm các trích dẫn pháp luật trong text.
    Trả về (annotated_text, citations_list).
    annotated_text: citation text được bọc trong <span class="inline-cite"> + số thứ tự superscript.
    """
    citations: list[dict] = []
    seen: dict[str, int] = {}   # key → index in citations list

    def _replace(m: re.Match) -> str:
        full    = m.group(0)
        dieu    = m.group(1)
        doc_num = m.group(2)
        if not doc_num:
            return full  # không có số văn bản → không wrap
        key = f"{doc_num}#{dieu}"
        if key not in seen:
            idx = len(citations)
            seen[key] = idx
            citations.append({"text": full, "dieu": dieu, "doc_number": doc_num})
        else:
            idx = seen[key]
        num = idx + 1
        return (
            f'<span class="inline-cite">{full}</span>'
            f'<span class="cite-sup">[{num}]</span>'
        )

    annotated = _CITATION_RE.sub(_replace, text)
    return annotated, citations


# ── Chat history persistence ──────────────────────────────────────────────────

def _history_path(session_id: str) -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR / f"{session_id}.json"


def save_session(session_id: str, messages: list) -> None:
    if not messages:
        return
    data = {
        "session_id": session_id,
        "created_at": messages[0].get("time", ""),
        "updated_at": messages[-1].get("time", ""),
        "title": messages[0].get("content", "")[:60] if messages else "",
        "messages": messages,
    }
    _history_path(session_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_session(session_id: str) -> list:
    path = _history_path(session_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("messages", [])
    except Exception:
        return []


def list_sessions() -> list[dict]:
    """Danh sách tất cả sessions, sort theo updated_at mới nhất."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    for p in HISTORY_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            sessions.append({
                "session_id": d.get("session_id", p.stem),
                "title":      d.get("title", "(Không có tiêu đề)"),
                "updated_at": d.get("updated_at", ""),
            })
        except Exception:
            pass
    return sorted(sessions, key=lambda x: x["updated_at"], reverse=True)


# ── Load agent + cache ────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def load_agent():
    logging.disable(logging.WARNING)
    from src.agent.planner import TaxAIAgent
    return TaxAIAgent()


@st.cache_resource(show_spinner=False)
def load_qa_cache():
    from src.retrieval.qa_cache import QACache
    return QACache()


@st.cache_resource(show_spinner=False)
def _prewarm_resources():
    """
    Load agent + embedding model lúc startup, không đợi câu hỏi đầu tiên.
    Embedding model chỉ load 1 lần nhờ _MODEL_CACHE trong embedder.py.
    """
    load_qa_cache()   # load embedding model (~13s, 1 lần duy nhất)
    load_agent()      # load TaxAIAgent (~1s)
    return True


@st.cache_data(show_spinner=False)
def get_docs_list() -> list[dict]:
    try:
        from src.utils.config import DOCUMENT_REGISTRY
        return [
            {
                "doc_id":    key,
                "num":       getattr(doc, "number", key),
                "status":    "active" if getattr(doc, "effective_from", None) else "pending",
                "valid_from": str(getattr(doc, "effective_from", "")),
            }
            for key, doc in DOCUMENT_REGISTRY.items()
        ]
    except Exception:
        return []


# ── Legal article dialog ──────────────────────────────────────────────────────

@st.dialog("Nội dung văn bản pháp luật", width="large")
def show_article_dialog(citation: dict) -> None:
    doc_num  = citation["doc_number"]
    dieu_num = citation["dieu"]
    full_ref = citation["text"]

    st.markdown(f'<div class="law-title">📄 {full_ref}</div>', unsafe_allow_html=True)

    node = fetch_article_content(doc_num, dieu_num)
    if not node:
        st.info(f"Không tìm thấy nội dung Điều {dieu_num} trong văn bản {doc_num}.")
        return

    title = node.get("title") or ""
    content = node.get("content") or ""

    if title:
        st.markdown(f"**Điều {dieu_num}. {title}**")
    if content:
        st.markdown(f'<div class="law-content">{content}</div>', unsafe_allow_html=True)

    for khoan in node.get("children", []):
        k_idx     = khoan.get("node_index", "")
        k_content = khoan.get("content") or ""
        if k_content:
            st.markdown(
                f'<div class="law-khoan"><b>{k_idx}.</b> {k_content}</div>',
                unsafe_allow_html=True,
            )
        for diem in khoan.get("children", []):
            d_idx     = diem.get("node_index", "")
            d_content = diem.get("content") or ""
            if d_content:
                st.markdown(f"&nbsp;&nbsp;&nbsp;**{d_idx})** {d_content}")


# ── Source rendering ──────────────────────────────────────────────────────────

def _breadcrumb(s: dict) -> str:
    src_type = s.get("type", "search")
    if src_type == "article":
        parts = [s.get("doc_id", ""), s.get("article_id", ""), s.get("title", "")]
    elif src_type == "calculation":
        parts = [s.get("doc_number", s.get("doc_id", "")), s.get("reference", "")]
    elif src_type == "validity":
        parts = [s.get("doc_number", s.get("doc_id", "")), f"Trạng thái: {s.get('status','')}"]
    else:
        parts = [s.get("doc_number", s.get("doc_id", "")), s.get("breadcrumb", "")]
    return " › ".join(p for p in parts if p)


def _render_sources(sources: list, iterations: int = 0) -> None:
    if not sources:
        return
    label = f"📚 Nguồn tham khảo ({len(sources)})"
    if iterations > 0:
        label += f" · {iterations} bước"
    with st.expander(label, expanded=False):
        for s in sources:
            src_type  = s.get("type", "search")
            css_extra = "source-calc" if src_type == "calculation" else (
                        "source-validity" if src_type == "validity" else "")
            validity_warn = ""
            if src_type == "validity" and s.get("status") not in ("active", ""):
                validity_warn = " ⚠️ Cần kiểm tra hiệu lực"
            bc = _breadcrumb(s)
            st.markdown(
                f"<div class='source-item {css_extra}'>{bc}{validity_warn}</div>",
                unsafe_allow_html=True,
            )


# ── Off-topic guard ───────────────────────────────────────────────────────────

_TAX_KEYWORDS = re.compile(
    r'thuế|tncn|gtgt|vat|hkd|hộ\s*kinh\s*doanh|khai\s*thuế|nộp\s*thuế|'
    r'hoàn\s*thuế|quyết\s*toán|giảm\s*trừ|gia\s*cảnh|mã\s*số\s*thuế|mst|'
    r'thu\s*nhập|lương|thưởng|hoa\s*hồng|doanh\s*thu|'
    r'nghị\s*định|thông\s*tư|luật|điều\s*\d+|khoản\s*\d+|'
    r'kế\s*toán|hóa\s*đơn|chứng\s*từ|sổ\s*sách|'
    r'cưỡng\s*chế|xử\s*phạt|tiền\s*chậm\s*nộp|thanh\s*tra|kiểm\s*tra\s*thuế|'
    r'đăng\s*ký\s*thuế|đại\s*lý\s*thuế|thương\s*mại\s*điện\s*tử|tmđt|'
    r'kinh\s*doanh|khấu\s*trừ|tính\s*thuế|'
    r'người\s*phụ\s*thuộc|npt|người\s*lao\s*động|tổ\s*chức\s*chi\s*trả|'
    r'cơ\s*quan\s*thuế|cqt|tờ\s*khai|hồ\s*sơ\s*thuế|'
    r'ủy\s*quyền|đăng\s*ký\s*người|mẫu\s*\d+|'
    r'kê\s*khai|khấu\s*trừ\s*tại\s*nguồn|phụ\s*thuộc',
    re.IGNORECASE | re.UNICODE,
)

_OFFTOPIC_RESPONSE = (
    "Xin lỗi, tôi là **TaxAI** — trợ lý chuyên về pháp luật thuế Việt Nam. "
    "Tôi chỉ có thể hỗ trợ các câu hỏi liên quan đến **thuế TNCN, thuế GTGT, "
    "hộ kinh doanh** và các thủ tục thuế.\n\n"
    "Bạn có câu hỏi nào về thuế không? 😊"
)

def _is_offtopic(question: str) -> bool:
    """Trả về True nếu câu hỏi không liên quan đến thuế."""
    return not bool(_TAX_KEYWORDS.search(question))


# ── Format answer sections (📌 Trích dẫn / 💬 Trả lời) ──────────────────────

def _format_answer_sections(content: str) -> str:
    """Thay thế marker 📌/💬 bằng styled HTML label để tách 2 phần rõ ràng.
    Dùng \n\n trước div để đảm bảo paragraph break trong Streamlit markdown."""
    content = re.sub(
        r"\*{0,2}\s*📌\s*Trích\s*dẫn\s*\*{0,2}[\s:>]*",
        '\n\n<div class="section-label citation-label">📌 &nbsp;Trích dẫn pháp luật</div>\n\n',
        content, count=1, flags=re.IGNORECASE,
    )
    content = re.sub(
        r"\*{0,2}\s*💬\s*Trả\s*lời\s*\*{0,2}[\s:]*",
        '\n\n<div class="section-divider"></div><div class="section-label answer-label">💬 &nbsp;Phân tích & Trả lời</div>\n\n',
        content, count=1, flags=re.IGNORECASE,
    )
    return content


# ── Streaming helper ──────────────────────────────────────────────────────────

def _render_streaming(msg: dict, show_sources: bool, show_iters: bool) -> None:
    """
    P3 — Streaming display cho NEW AI messages.

    Dùng st.write_stream() để hiển thị từng từ thay vì toàn bộ cùng lúc.
    Chỉ gọi cho messages mới vừa generate (không phải history re-render).

    Sau khi stream xong, hiển thị metadata (cache badge, iterations, time)
    và sources dưới dạng st.expander.
    """
    answer     = msg.get("content", "")
    ts         = msg.get("time", "")
    from_cache = msg.get("from_cache", False)
    iterations = msg.get("iterations", 0)
    citations  = msg.get("citations", [])
    sources    = msg.get("sources", [])
    latency_ms = msg.get("latency_ms", 0)

    # Annotate citations inline
    display_content = answer
    if citations and '<span class="inline-cite">' not in answer:
        display_content, _ = parse_citations(answer)
    display_content = _format_answer_sections(display_content)

    # Stream từng từ vào chat message container
    with st.chat_message("assistant", avatar="⚖️"):
        # st.write_stream nhận generator, trả về full text
        def _word_gen():
            for word in display_content.split(" "):
                yield word + " "

        st.write_stream(_word_gen())

        # Metadata badges
        badges: list[str] = []
        if from_cache:
            badges.append("⚡ Cache")
        if show_iters and iterations > 1:
            badges.append(f"🔄 {iterations} bước")
        if latency_ms:
            badges.append(f"⏱️ {latency_ms}ms")
        if ts:
            badges.append(ts)
        st.caption("  ·  ".join(badges))

        # Citations footnotes
        if citations and show_sources:
            msg_id_key = msg.get("_id", str(id(msg)))
            btn_cols = st.columns(min(len(citations), 4))
            for i, cit in enumerate(citations):
                with btn_cols[i % len(btn_cols)]:
                    label = f"[{i+1}] Điều {cit['dieu']} · {cit['doc_number']}"
                    if st.button(label, key=f"cit_stream_{msg_id_key}_{i}",
                                 use_container_width=True, type="secondary"):
                        show_article_dialog(cit)

        # Sources expander
        if show_sources and sources:
            with st.expander(f"📚 {len(sources)} nguồn tham khảo", expanded=False):
                for src in sources:
                    st.markdown(f"- {src.get('breadcrumb', src.get('reference', ''))}")


# ── Render one message ────────────────────────────────────────────────────────

def _render_message(msg: dict, show_sources: bool, show_iters: bool) -> None:
    role       = msg["role"]
    content    = msg["content"]
    ts         = msg.get("time", "")
    from_cache = msg.get("from_cache", False)
    iterations = msg.get("iterations", 0)
    citations  = msg.get("citations", [])

    if role == "user":
        st.markdown(
            f'<div class="msg-user">{content}'
            f'<div class="msg-time">{ts}</div></div>',
            unsafe_allow_html=True,
        )
    else:
        # AI message — annotate citations inline if not already annotated
        display_content = content
        if citations and '<span class="inline-cite">' not in content:
            display_content, _ = parse_citations(content)
        display_content = _format_answer_sections(display_content)

        cache_html = " <span class='cache-badge'>⚡ Cache</span>" if from_cache else ""
        iter_html  = (
            f" <span class='iter-badge'>🔄 {iterations} bước</span>"
            if show_iters and iterations > 1 else ""
        )
        st.markdown(
            f'<div class="msg-ai">{display_content}'
            f'<div class="msg-time">{ts}{cache_html}{iter_html}</div></div>',
            unsafe_allow_html=True,
        )

        # Footnote buttons — compact, numbered, one row
        if citations:
            msg_id_key = msg.get("_id", str(id(msg)))
            btn_cols = st.columns(min(len(citations), 4))
            for i, cit in enumerate(citations):
                with btn_cols[i % len(btn_cols)]:
                    label = f"[{i+1}] Điều {cit['dieu']} · {cit['doc_number']}"
                    if st.button(label, key=f"cit_{msg_id_key}_{i}",
                                 use_container_width=True, type="secondary"):
                        show_article_dialog(cit)

        if show_sources and msg.get("sources"):
            _render_sources(msg["sources"], iterations)


# ── Pre-warm: load embedding model + agent tại startup ───────────────────────

with st.spinner("⏳ Đang khởi động TaxAI (lần đầu ~15s)..."):
    _prewarm_resources()

# ── Session state init ────────────────────────────────────────────────────────

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())[:8]
if "messages" not in st.session_state:
    st.session_state.messages = []


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚖️ TaxAI")
    st.caption("Tư vấn pháp luật thuế Việt Nam")
    st.divider()

    # Doc filter
    docs = get_docs_list()
    doc_options = {"Tất cả văn bản": None}
    for d in docs:
        doc_options[d.get("num", "?")] = d.get("doc_id")
    selected_label = st.selectbox("Tìm trong văn bản:", list(doc_options.keys()), index=0)
    filter_doc_id  = doc_options[selected_label]

    st.divider()

    # Toggles
    show_sources    = st.toggle("Hiển thị nguồn trích dẫn", value=True)
    show_iters      = st.toggle("Hiển thị số bước suy luận", value=True)
    use_cache       = st.toggle("Dùng cache câu hỏi", value=True,
                                help="Trả kết quả ngay nếu câu hỏi tương tự đã được hỏi (similarity ≥ 0.88)")
    use_streaming   = st.toggle("Streaming response", value=False,
                                help="Hiển thị câu trả lời từng từ thay vì hiện toàn bộ cùng lúc")

    st.divider()

    # Actions
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗑️ Xóa chat", use_container_width=True):
            st.session_state.messages = []
            st.session_state.session_id = str(uuid.uuid4())[:8]
            st.rerun()
    with col2:
        if st.button("💾 Lưu", use_container_width=True):
            save_session(st.session_state.session_id, st.session_state.messages)
            st.success("Đã lưu!")

    st.divider()

    # Chat history
    st.markdown("**📂 Lịch sử cuộc hội thoại**")
    sessions = list_sessions()
    if not sessions:
        st.caption("Chưa có cuộc hội thoại nào được lưu.")
    else:
        for s in sessions[:8]:
            sid   = s["session_id"]
            title = s["title"] or "(Không có tiêu đề)"
            ts    = s["updated_at"][:16] if s["updated_at"] else ""
            label = f"{title[:28]}…" if len(title) > 30 else title
            if st.button(f"💬 {label}", key=f"hist_{sid}", help=f"{ts}  |  ID: {sid}",
                         use_container_width=True):
                st.session_state.session_id = sid
                st.session_state.messages   = load_session(sid)
                st.rerun()


# ── Main ──────────────────────────────────────────────────────────────────────

st.title("TaxAI — Tư vấn pháp luật thuế Việt Nam")
st.caption(
    "Dựa trên Luật Quản lý thuế 108/2025/QH15, Luật TNCN 109/2025/QH15, "
    "NĐ 68/2026/NĐ-CP, NĐ 117/2025/NĐ-CP và các văn bản liên quan. "
    "Thông tin chỉ mang tính tham khảo."
)

# Hiển thị lịch sử chat
for msg in st.session_state.messages:
    _render_message(msg, show_sources, show_iters)

# Gợi ý câu hỏi
if not st.session_state.messages:
    st.markdown("**Câu hỏi gợi ý:**")
    suggestions = [
        "Mức giảm trừ gia cảnh cho bản thân và người phụ thuộc năm 2026 là bao nhiêu?",
        "Hộ kinh doanh doanh thu 1 tỷ/năm phải nộp thuế GTGT và TNCN bao nhiêu?",
        "Tôi có lương 20 triệu, 2 con nhỏ, thuế TNCN phải nộp bao nhiêu?",
        "Sàn TMĐT như Shopee, TikTok Shop có tự khấu trừ thuế thay người bán không?",
        "Cơ quan thuế thanh tra tại cửa hàng, tôi có quyền gì và nghĩa vụ gì?",
        "Hộ kinh doanh bán hàng online qua Facebook có phải đóng thuế không?",
    ]
    cols = st.columns(2)
    for i, q in enumerate(suggestions):
        if cols[i % 2].button(q, key=f"sug_{i}", use_container_width=True):
            st.session_state._pending_question = q
            st.rerun()

# Pending question từ suggestion button
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
    now_str = datetime.now(TZ_VN).strftime("%H:%M:%S %d/%m/%Y")
    msg_id  = str(uuid.uuid4())[:8]

    # Lưu tin nhắn user
    user_msg = {"role": "user", "content": question, "time": now_str, "_id": msg_id + "_u"}
    st.session_state.messages.append(user_msg)
    _render_message(user_msg, show_sources, show_iters)

    # Gọi agent
    with st.spinner("🔍 Đang tra cứu điều khoản..."):
        try:
            t0         = time.perf_counter()
            from_cache = False
            answer     = ""
            sources    = []
            iterations = 0
            model_name = ""

            # P2 — Multi-turn DST: build context từ lịch sử trước câu hỏi hiện tại
            from src.agent.dialogue_state import DialogueStateTracker as _DST
            _tracker = _DST()
            _history_before = [m for m in st.session_state.messages if m.get("role") == "user"
                               and m.get("content") != question]
            _tracker.process_history(_history_before)
            _tracker.process_current_turn(question)
            # Chỉ inject context nếu đã có ít nhất 1 lượt trước (không phải câu đầu tiên)
            _context_hint = _tracker.build_context_string() if _history_before else None

            # Off-topic guard (trước cache — tránh serve cached response lạc đề)
            if _is_offtopic(question):
                answer     = _OFFTOPIC_RESPONSE
                from_cache = False
                latency    = int((time.perf_counter() - t0) * 1000)
                _, citations = parse_citations(answer)
                ai_msg = {
                    "role":       "assistant",
                    "content":    answer,
                    "time":       datetime.now(TZ_VN).strftime("%H:%M:%S %d/%m/%Y"),
                    "latency_ms": latency,
                    "from_cache": False,
                    "iterations": 0,
                    "citations":  citations,
                    "sources":    [],
                }
                st.session_state.messages.append(ai_msg)
                save_session(st.session_state.session_id, st.session_state.messages)
                st.rerun()

            # Cache lookup — semantic similarity, không cần preliminary retrieval
            top_doc_ids: list[str] = []
            if use_cache and not filter_doc_id:
                try:
                    hit = load_qa_cache().lookup(question)
                    if hit:
                        answer     = hit.answer
                        from_cache = True
                except Exception as _ce:
                    import logging as _log
                    _log.getLogger(__name__).debug(f"[Cache] lookup error: {_ce}")

            # Full pipeline
            if not from_cache:
                result     = load_agent().answer(
                    question=question, filter_doc_id=filter_doc_id, show_sources=True,
                    context_hint=_context_hint,
                )
                answer     = result["answer"]
                sources    = result.get("sources", [])
                iterations = result.get("iterations", 0)
                model_name = result.get("model", "")

                # Store vào cache nếu hợp lệ — lấy doc_ids từ pipeline result
                if use_cache and answer and iterations <= 5 and not filter_doc_id:
                    key_facts = [
                        s.get("breadcrumb", s.get("reference", ""))
                        for s in sources if s.get("type") == "search"
                    ]
                    top_doc_ids = list({
                        s.get("doc_id") for s in sources
                        if s.get("type") == "search" and s.get("doc_id")
                    })
                    load_qa_cache().store(
                        question=question, answer=answer,
                        key_facts=[kf for kf in key_facts if kf],
                        source_round="user",
                        top_doc_ids=top_doc_ids or None,
                    )

            latency = int((time.perf_counter() - t0) * 1000)

            # Parse citations
            _, citations = parse_citations(answer)

            # Lưu AI message
            ai_msg = {
                "role":       "assistant",
                "content":    answer,
                "time":       datetime.now(TZ_VN).strftime("%H:%M:%S %d/%m/%Y"),
                "_id":        msg_id + "_a",
                "sources":    sources,
                "iterations": iterations,
                "from_cache": from_cache,
                "citations":  citations,
                "latency_ms": latency,
                "model":      model_name,
            }
            st.session_state.messages.append(ai_msg)

            # Auto-save session sau mỗi câu
            save_session(st.session_state.session_id, st.session_state.messages)

            # Render AI message — streaming nếu bật, ngược lại render thường
            if use_streaming and not from_cache:
                _render_streaming(ai_msg, show_sources, show_iters)
            else:
                _render_message(ai_msg, show_sources, show_iters)

        except Exception as e:
            raw = str(e)
            if "RATE_LIMITED" in raw or "RESOURCE_EXHAUSTED" in raw or "429" in raw:
                err = (
                    "⏳ **Hệ thống đang bận** — API đạt giới hạn tạm thời (429). "
                    "Vui lòng chờ **30-60 giây** rồi hỏi lại. "
                    "Câu hỏi của bạn không bị mất."
                )
                st.warning(err)
            elif "UNAVAILABLE" in raw or "503" in raw:
                err = (
                    "⏳ **Model đang quá tải** — Gemini đang có lượng truy cập cao (503). "
                    "Vui lòng chờ **10-30 giây** rồi hỏi lại."
                )
                st.warning(err)
            elif "GOOGLE_API_KEY" in raw or "API_KEY_INVALID" in raw:
                err = "🔑 **Lỗi API key** — Kiểm tra lại `GOOGLE_API_KEY` trong file `.env`."
                st.error(err)
            else:
                err = f"❌ Lỗi hệ thống: {raw}"
                st.error(err)
            st.session_state.messages.append({"role": "assistant", "content": err, "time": now_str})
