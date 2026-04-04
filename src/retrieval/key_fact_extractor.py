"""
src/retrieval/key_fact_extractor.py

Key Fact Extraction Pipeline — 5 stages:
  Stage 1: Seed retrieval     (HybridSearch — đã có sẵn)
  Stage 2: Graph expansion    (in-memory BFS, depth=2, internal refs only)
  Stage 3a: Regex extraction  (numbers, %, dates, money thresholds)
  Stage 3b: LLM extraction    (conditions, exceptions) via Gemini Flash
  Stage 4: Merge + classify   (deduplicate, tag type)
  Stage 5: Verify             (regex anchors LLM claims → reject hallucination)

Output: list[KeyFact]

Usage:
    graph   = LegalGraphIndex()                    # load once at startup
    extract = KeyFactExtractor(searcher, graph, llm_client)
    facts   = extract.extract("câu hỏi...", doc_filter="68_2026_NDCP")
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PARSED_DIR = Path(__file__).parent.parent.parent / "data" / "parsed"


# ── Cross-doc reference resolution ────────────────────────────────────────────
#
# Nhiều external_* references thực ra trỏ đến documents có trong corpus.
# Bảng này map keyword fragments (từ target_id) → doc_id trong corpus.
# Priority: more specific first.
_EXTERNAL_DOC_MAP: list[tuple[str, str]] = [
    # Luật TNCN
    ("luat_thue_thu_nhap_ca_nhan",  "109_2025_QH15"),
    ("109_2025_qh15",               "109_2025_QH15"),
    # NĐ 68/2026 (self-references)
    ("nghi_dinh_68_2026",           "68_2026_NDCP"),
    ("68_2026_nd_cp",               "68_2026_NDCP"),
    # NĐ 117/2025 (TMĐT)
    ("nghi_dinh_117_2025",          "117_2025_NDCP"),
    ("117_2025_nd_cp",              "117_2025_NDCP"),
    # NĐ 20/2026
    ("nghi_dinh_20_2026",           "20_2026_NDCP"),
    ("20_2026_nd_cp",               "20_2026_NDCP"),
    # NĐ 373/2025
    ("nghi_dinh_373_2025",          "373_2025_NDCP"),
    ("373_2025_nd_cp",              "373_2025_NDCP"),
    # NĐ 310/2025
    ("nghi_dinh_310_2025",          "310_2025_NDCP"),
    ("310_2025_nd_cp",              "310_2025_NDCP"),
    # TT 18/2026
    ("thong_tu_18_2026",            "18_2026_TTBTC"),
    ("18_2026_tt_btc",              "18_2026_TTBTC"),
    # TT 152/2025
    ("thong_tu_152_2025",           "152_2025_TTBTC"),
    ("152_2025_tt_btc",             "152_2025_TTBTC"),
    # Luật 109/2025 (QH15 sửa đổi)
    ("149_2025_qh15",               "149_2025_QH15"),
    # Luật 198/2025
    ("nghi_quyet_198_2025",         "198_2025_QH15"),
    ("198_2025_qh15",               "198_2025_QH15"),
    # TT/NĐ khác
    ("110_2025_ubtvqh15",           "110_2025_UBTVQH15"),
]

# Regex extract article-path suffix: _dieu_7, _dieu_7_khoan_3, _dieu_7_khoan_3_diem_a
_ARTICLE_SUFFIX_RE = re.compile(
    r"(_dieu_\d+(?:_khoan_\d+(?:_diem_[a-z\u0111\u0113]+)?)?)",
    re.IGNORECASE,
)


def _resolve_external_ref(target_id: str, node_map: dict[str, dict]) -> str | None:
    """
    Cố gắng resolve một external_* target_id sang node_id thực trong corpus.

    Cách hoạt động:
      1. Map keyword trong target_id → doc_id (theo _EXTERNAL_DOC_MAP)
      2. Extract article-path suffix (_dieu_X_khoan_Y...) từ target_id
      3. Tìm node trong node_map có doc_id đúng VÀ suffix match

    Trả về node_id nếu tìm được, None nếu không resolve được.
    """
    if not target_id.startswith("external_"):
        return None

    tid_lower = target_id.lower()

    # Step 1: tìm doc_id từ keyword map
    matched_doc_id: str | None = None
    for keyword, doc_id in _EXTERNAL_DOC_MAP:
        if keyword in tid_lower:
            matched_doc_id = doc_id
            break

    if not matched_doc_id:
        return None  # doc không có trong corpus

    # Step 2: extract article path suffix
    suffix_match = _ARTICLE_SUFFIX_RE.search(tid_lower)
    if not suffix_match:
        # Không có _dieu_ → chỉ có doc, không có article path
        # Trả về None (không đủ chính xác để pick 1 node)
        return None

    suffix = suffix_match.group(1)  # e.g. "_dieu_7_khoan_3"

    # Step 3: tìm node_id = doc_{doc_id}_*{suffix}
    # Normalize: doc_id có thể là "109_2025_QH15", node prefix là "doc_109_2025_QH15_"
    prefix = f"doc_{matched_doc_id}_".lower()

    best_match: str | None = None
    for node_id in node_map:
        nid_lower = node_id.lower()
        if nid_lower.startswith(prefix) and nid_lower.endswith(suffix):
            # Ưu tiên match chính xác nhất (ngắn nhất = ít intermediate path nhất)
            if best_match is None or len(node_id) < len(best_match):
                best_match = node_id

    if best_match:
        logger.debug(f"[Graph] Resolved '{target_id}' → '{best_match}'")
    return best_match


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class KeyFact:
    value: str          # chuỗi fact ("5%", "30 ngày", "không được trừ nếu...")
    fact_type: str      # "numeric" | "condition" | "citation"
    source: str         # "regex" | "llm"
    evidence_node: str  # node_id chứa evidence
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "fact_type": self.fact_type,
            "source": self.source,
            "evidence_node": self.evidence_node,
            "confidence": self.confidence,
        }


# ── Stage: In-memory Graph Index ──────────────────────────────────────────────

class LegalGraphIndex:
    """
    Flat index của tất cả nodes từ tất cả parsed JSONs trong data/parsed/*.json.
    Build một lần lúc startup, dùng chung cho mọi request.

    BFS traversal qua references[].target_id với cross-doc resolution:
      - Internal refs (doc_*): follow trực tiếp
      - External refs (external_*): resolve qua _EXTERNAL_DOC_MAP nếu doc có trong corpus
      - Unresolvable externals: bỏ qua
    """

    def __init__(self, parsed_dir: Path = PARSED_DIR):
        self.node_map: dict[str, dict] = {}   # node_id → node_data (flat)
        self._build(parsed_dir)

    def _build(self, parsed_dir: Path):
        file_count = 0
        for json_file in sorted(parsed_dir.glob("*.json")):
            try:
                with open(json_file, encoding="utf-8") as f:
                    doc = json.load(f)
                for root_node in doc.get("data", []):
                    self._index_recursive(root_node)
                file_count += 1
            except Exception as e:
                logger.warning(f"LegalGraphIndex: skip {json_file.name} — {e}")
        logger.info(
            f"LegalGraphIndex built: {len(self.node_map)} nodes from {file_count} files"
        )

    def _index_recursive(self, node: dict):
        """Đệ quy index node + tất cả children."""
        node_id = node.get("node_id")
        if node_id:
            self.node_map[node_id] = node
        for child in node.get("children", []):
            self._index_recursive(child)

    def get_node(self, node_id: str) -> dict | None:
        return self.node_map.get(node_id)

    def bfs_expand(self, seed_ids: list[str], depth: int = 2) -> list[dict]:
        """
        BFS từ seed nodes.
        Expand qua:
          - references[].target_id:
              * Internal (doc_*): follow ở depth+1
              * External → resolved internal: cross-doc hop, reset depth về 0
                (vì cross-doc refs rất relevant, cần explore subtree đầy đủ)
          - children trực tiếp: depth+1

        Returns danh sách nodes duy nhất (seeds + expanded), theo thứ tự BFS.
        """
        visited: set[str] = set()
        result: list[dict] = []
        # queue item: (node_id, current_depth)
        queue: deque[tuple[str, int]] = deque()

        for sid in seed_ids:
            if sid and sid not in visited:
                visited.add(sid)
                queue.append((sid, 0))

        while queue:
            node_id, d = queue.popleft()
            node = self.get_node(node_id)
            if not node:
                continue

            result.append(node)

            if d >= depth:
                continue

            # Follow references
            for ref in node.get("references", []):
                target = ref.get("target_id", "")
                if not target:
                    continue

                if target.startswith("doc_"):
                    # Same-corpus internal ref → depth+1
                    if target not in visited:
                        visited.add(target)
                        queue.append((target, d + 1))

                elif target.startswith("external_"):
                    # Cross-doc ref: resolve → reset depth về 0 (fresh subtree)
                    resolved = _resolve_external_ref(target, self.node_map)
                    if resolved and resolved not in visited:
                        visited.add(resolved)
                        queue.append((resolved, 0))  # depth reset!

            # Include direct children at depth+1
            for child in node.get("children", []):
                cid = child.get("node_id", "")
                if cid and cid not in visited:
                    visited.add(cid)
                    queue.append((cid, d + 1))

        return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _node_text(node: dict) -> str:
    """Ghép content + lead_in_text của node."""
    parts = []
    c = node.get("content", "")
    if c:
        parts.append(c)
    l = node.get("lead_in_text", "")
    if l:
        parts.append(l)
    return " ".join(parts)


# ── Stage 3a: Regex Extraction ────────────────────────────────────────────────

# (pattern, fact_type)
_REGEX_PATTERNS: list[tuple[str, str]] = [
    # Percentages: 5%, 0,5%, 1.5%, 1,5%
    (r"\b\d+(?:[.,]\d+)?\s*%", "numeric"),
    # Money — triệu/tỷ
    (r"\b\d+(?:[.,]\d+)?\s*(?:triệu|tỷ)\b", "numeric"),
    # Money — formatted: 50.000.000 hoặc 50,000,000
    (r"\b\d{1,3}(?:[.,]\d{3})+\s*(?:đồng|VNĐ|VND|₫)?", "numeric"),
    # Deadlines: 30 ngày, 3 tháng, 1 năm
    (r"\b\d+\s*(?:ngày|tháng|năm)\b", "numeric"),
    # Article citations: Điều 6, Khoản 2 Điều 7, Điểm d
    (r"\b(?:Điều|Khoản|Điểm|Mục|Chương)\s+\d+[a-zA-Zđ]?(?:\s+(?:Điều|Khoản|Điểm)\s+\d+[a-zA-Zđ]?)*", "citation"),
]


def _extract_regex_facts(nodes: list[dict]) -> list[KeyFact]:
    facts: list[KeyFact] = []
    seen: set[str] = set()

    for node in nodes:
        text = _node_text(node)
        if not text:
            continue
        node_id = node.get("node_id", "")
        for pattern, fact_type in _REGEX_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                val = m.group(0).strip()
                val_lower = val.lower()
                if val_lower not in seen:
                    seen.add(val_lower)
                    facts.append(KeyFact(
                        value=val,
                        fact_type=fact_type,
                        source="regex",
                        evidence_node=node_id,
                        confidence=1.0,
                    ))
    return facts


# ── Stage 3b: LLM Extraction ──────────────────────────────────────────────────

_LLM_EXTRACT_PROMPT = """\
Bạn là chuyên gia pháp luật thuế Việt Nam.

Câu hỏi: {question}

Các điều luật liên quan:
{context}

Nhiệm vụ: liệt kê TỐI ĐA 5 key facts quan trọng nhất để trả lời câu hỏi.
Mỗi fact là một cụm từ ngắn (tối đa 12 từ), tập trung vào:
- Điều kiện áp dụng ("nếu... thì...", "trường hợp...")
- Ngoại lệ ("trừ khi...", "không áp dụng khi...")
- Nghĩa vụ bắt buộc ("phải", "bắt buộc", "không được")
- Quyền được hưởng ("được phép", "được trừ", "được miễn")

KHÔNG lặp lại số liệu (số, %, tiền) — những con số đã được extract riêng.
KHÔNG hallucinate — chỉ extract từ văn bản được cung cấp phía trên.
Nếu không có fact nào rõ ràng, trả về array rỗng [].

Trả về JSON array of strings. Ví dụ:
["được trừ nếu có hóa đơn chứng từ", "không áp dụng với cá nhân không cư trú"]
"""


def _extract_llm_facts(
    question: str,
    top_nodes: list[dict],
    llm_client: Any,
    max_context_chars: int = 3000,
) -> list[KeyFact]:
    """Gọi Gemini Flash để extract điều kiện/ngoại lệ từ top nodes."""
    if not llm_client or not top_nodes:
        return []

    # Build context (giới hạn chars để tiết kiệm token)
    context_parts: list[str] = []
    total = 0
    for node in top_nodes[:8]:
        text = _node_text(node)
        if not text:
            continue
        breadcrumb = node.get("breadcrumb", "")
        snippet = f"[{breadcrumb}]\n{text[:400]}"
        total += len(snippet)
        if total > max_context_chars:
            break
        context_parts.append(snippet)

    if not context_parts:
        return []

    prompt = _LLM_EXTRACT_PROMPT.format(
        question=question,
        context="\n\n".join(context_parts),
    )

    try:
        response = llm_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"temperature": 0, "max_output_tokens": 300},
        )
        text = (response.text or "").strip()

        # Tìm JSON array trong response
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if not m:
            return []

        arr = json.loads(m.group(0))
        return [
            KeyFact(
                value=str(f).strip(),
                fact_type="condition",
                source="llm",
                evidence_node="",
                confidence=0.8,
            )
            for f in arr
            if isinstance(f, str) and f.strip()
        ]
    except Exception as e:
        logger.warning(f"LLM fact extraction failed: {e}")
        return []


# ── Stage 4: Merge + Classify ─────────────────────────────────────────────────

def _merge_and_classify(
    regex_facts: list[KeyFact],
    llm_facts: list[KeyFact],
) -> list[KeyFact]:
    """
    Merge regex + LLM facts, deduplicate.
    Regex facts được ưu tiên (confidence=1.0, source xác định).
    """
    seen: set[str] = set()
    merged: list[KeyFact] = []

    for f in regex_facts:
        key = f.value.lower().strip()
        if key not in seen:
            seen.add(key)
            merged.append(f)

    for f in llm_facts:
        key = f.value.lower().strip()
        if key not in seen:
            seen.add(key)
            merged.append(f)

    return merged


# ── Stage 5b: LLM Relevance Filter ──────────────────────────────────────────

_LLM_FILTER_PROMPT = """\
Câu hỏi: {question}

Danh sách facts đã extract:
{facts_list}

Nhiệm vụ: chọn TỐI ĐA 6 facts TRỰC TIẾP trả lời câu hỏi.

Quy tắc chọn:
- Ưu tiên facts có đơn vị phù hợp với câu hỏi:
  * Hỏi về "thuế suất", "tỷ lệ", "%" → ưu tiên facts chứa %
  * Hỏi về "thời hạn", "bao nhiêu ngày" → ưu tiên ngày/tháng
  * Hỏi về "mức tiền", "ngưỡng" → ưu tiên triệu/tỷ/đồng
- Loại bỏ facts không liên quan đến đại lượng được hỏi
- Loại bỏ citations (Điều X, Khoản Y) trừ khi câu hỏi hỏi về điều khoản cụ thể

Trả về JSON array of strings (chỉ các giá trị facts, giữ nguyên chuỗi gốc).
Ví dụ: ["0,5%", "1%", "2%", "5%"]
"""


def _llm_relevance_filter(
    question: str,
    facts: list[KeyFact],
    llm_client: Any,
) -> list[KeyFact]:
    """
    Stage 5b: Dùng LLM để chọn facts thực sự trả lời câu hỏi.
    Chỉ chạy nếu có llm_client VÀ số facts > threshold.
    Regex facts bị filter ra → re-merge với LLM-selected ones (giữ original objects).
    """
    if not llm_client or len(facts) <= 5:
        return facts  # đủ nhỏ, không cần filter

    facts_list = "\n".join(f"- {f.value}" for f in facts)
    prompt = _LLM_FILTER_PROMPT.format(question=question, facts_list=facts_list)

    try:
        response = llm_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"temperature": 0, "max_output_tokens": 200},
        )
        text = (response.text or "").strip()
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if not m:
            return facts

        selected_values: list[str] = json.loads(m.group(0))
        selected_lower = {v.lower().strip() for v in selected_values if isinstance(v, str)}

        # Giữ lại fact objects có value trong selected set
        filtered = [f for f in facts if f.value.lower().strip() in selected_lower]

        # Nếu LLM lọc quá ít (có thể hallucinate), fallback về tất cả
        if len(filtered) == 0:
            return facts
        return filtered

    except Exception as e:
        logger.warning(f"LLM relevance filter failed: {e}")
        return facts


# ── Stage 5: Verify ───────────────────────────────────────────────────────────

def _verify_facts(
    facts: list[KeyFact],
    expanded_nodes: list[dict],
    min_term_match_ratio: float = 0.6,
) -> list[KeyFact]:
    """
    Cross-check LLM facts với actual text trong expanded nodes.
    - Regex facts: luôn pass (đến từ actual text).
    - LLM facts: phải có ≥60% key terms xuất hiện trong corpus.
      Nếu pass, gán evidence_node = node có match tốt nhất.
    """
    if not expanded_nodes:
        return facts

    corpus = " ".join(_node_text(n) for n in expanded_nodes).lower()

    verified: list[KeyFact] = []
    for f in facts:
        if f.source == "regex":
            verified.append(f)
            continue

        # LLM fact: verify bằng term matching
        key_terms = [w for w in f.value.lower().split() if len(w) > 3]
        if not key_terms:
            continue

        matched_count = sum(1 for t in key_terms if t in corpus)
        ratio = matched_count / len(key_terms)

        if ratio >= min_term_match_ratio:
            # Tìm node có evidence tốt nhất (match nhiều terms nhất)
            best_node = ""
            best_score = 0
            for node in expanded_nodes:
                node_text = _node_text(node).lower()
                score = sum(1 for t in key_terms if t in node_text)
                if score > best_score:
                    best_score = score
                    best_node = node.get("node_id", "")
            f.evidence_node = best_node
            f.confidence = round(ratio * 0.8, 2)  # LLM max confidence = 0.8
            verified.append(f)

    return verified


# ── Main Extractor ─────────────────────────────────────────────────────────────

class KeyFactExtractor:
    """
    5-stage key fact extraction pipeline.

    Args:
        searcher:     HybridSearch instance
        graph:        LegalGraphIndex instance (shared, loaded once)
        llm_client:   Gemini client (optional — bỏ qua Stage 3b nếu None)
        graph_depth:  BFS depth cho Stage 2 (default=2)
        n_seeds:      số seed nodes từ Stage 1 (default=5)
    """

    def __init__(
        self,
        searcher,
        graph: LegalGraphIndex,
        llm_client=None,
        graph_depth: int = 2,
        n_seeds: int = 5,
    ):
        self.searcher = searcher
        self.graph = graph
        self.llm_client = llm_client
        self.graph_depth = graph_depth
        self.n_seeds = n_seeds

    def extract(
        self,
        question: str,
        doc_filter: str | None = None,
    ) -> list[KeyFact]:
        """
        Chạy toàn bộ pipeline cho một câu hỏi.

        Args:
            question:   câu hỏi tiếng Việt
            doc_filter: giới hạn retrieval trong một document (optional)

        Returns:
            list[KeyFact] đã được verify, theo thứ tự: numeric → condition → citation
        """

        # Stage 1: Seed retrieval
        hits = self.searcher.search(
            query=question,
            n_results=self.n_seeds,
            filter_doc_id=doc_filter,
        )
        # chunk_id có dạng "doc_X_chuong_Y_dieu_Z_chunk" — strip "_chunk" suffix
        # để map sang node_id trong graph
        seed_ids = [
            h.get("chunk_id", "").removesuffix("_chunk")
            for h in hits
        ]
        logger.debug(f"[KFE] Stage 1: {len(seed_ids)} seeds")

        # Stage 2: Graph expansion
        expanded = self.graph.bfs_expand(seed_ids, depth=self.graph_depth)
        logger.debug(f"[KFE] Stage 2: {len(expanded)} nodes after expansion")

        if not expanded:
            logger.warning("[KFE] No nodes found — returning empty")
            return []

        # Stage 3a + 3b: parallel extraction
        regex_facts = _extract_regex_facts(expanded)
        llm_facts   = _extract_llm_facts(question, expanded[:6], self.llm_client)
        logger.debug(f"[KFE] Stage 3: {len(regex_facts)} regex, {len(llm_facts)} llm")

        # Stage 4: Merge
        merged = _merge_and_classify(regex_facts, llm_facts)
        logger.debug(f"[KFE] Stage 4: {len(merged)} merged")

        # Stage 5: Verify (cross-check LLM claims against corpus)
        verified = _verify_facts(merged, expanded)
        logger.debug(f"[KFE] Stage 5: {len(verified)} verified")

        # Stage 5b: LLM relevance filter (chỉ chạy khi có llm_client + nhiều facts)
        filtered = _llm_relevance_filter(question, verified, self.llm_client)
        logger.debug(f"[KFE] Stage 5b: {len(filtered)} after relevance filter")

        # Sort: numeric first, then condition, then citation
        order = {"numeric": 0, "condition": 1, "citation": 2}
        filtered.sort(key=lambda f: (order.get(f.fact_type, 9), -f.confidence))

        return filtered

    def extract_values_only(
        self,
        question: str,
        doc_filter: str | None = None,
    ) -> list[str]:
        """Shortcut — trả về list[str] thay vì list[KeyFact]."""
        return [f.value for f in self.extract(question, doc_filter)]
