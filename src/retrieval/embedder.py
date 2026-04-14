"""
src/retrieval/embedder.py
Tạo embeddings từ parsed JSON documents cho TaxAI 2026

A3 — Retrieval Enrichment:
  - Breadcrumb header: [VB: ... | Đ1 (title) | K1]
  - Khoản chunks include direct children (≤800 chars, ≤5 items)
  - Guidance chunks include document title
  - validate_chunks(): 6-check metadata consistency
"""

from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Model nhẹ, hỗ trợ tiếng Việt tốt
EMBEDDING_MODEL = "keepitreal/vietnamese-sbert"
FALLBACK_MODEL  = "paraphrase-multilingual-MiniLM-L12-v2"

# Children expansion limits
MAX_CHILD_CHARS = 800
MAX_CHILD_COUNT = 5

# Table chunk split limit (A4)
MAX_TABLE_CHUNK_CHARS = 1200

# B3 — Amendment mapping: doc_id → list of doc_ids that amend it
# Used to inject `amended_by_doc_ids` metadata so hybrid_search can expand
AMENDED_BY: dict[str, list[str]] = {
    "125_2020_NDCP": ["310_2025_NDCP"],
}


@dataclass
class Chunk:
    """Một chunk sẵn sàng để embed và index"""
    chunk_id:   str
    doc_id:     str
    node_id:    str
    node_type:  str
    breadcrumb: str
    text:       str                    # text dùng để embed
    metadata:   Dict[str, Any] = field(default_factory=dict)


# Module-level model cache — tránh load lại model nhiều lần (~13s/lần)
# QACache + HybridSearcher cùng dùng EMBEDDING_MODEL → chỉ load 1 lần
_MODEL_CACHE: dict[str, Any] = {}


class DocumentEmbedder:
    """
    Đọc parsed JSON → tạo Chunk list → embed bằng sentence-transformers
    """

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        self.model_name = model_name
        if model_name in _MODEL_CACHE:
            self.model = _MODEL_CACHE[model_name]
            logger.debug(f"[Embedder] Reusing cached model: {model_name}")
        else:
            self.model = None
            self._load_model()

    def _load_model(self):
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"⏳ Loading model: {self.model_name}")
            self.model = SentenceTransformer(self.model_name)
            logger.info(f"✅ Model loaded: {self.model_name}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load {self.model_name}: {e}")
            logger.info(f"🔄 Trying fallback: {FALLBACK_MODEL}")
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(FALLBACK_MODEL)
            self.model_name = FALLBACK_MODEL
            logger.info(f"✅ Fallback model loaded")
        _MODEL_CACHE[self.model_name] = self.model

    # ── Header builder ────────────────────────────────────────────────────

    def _build_header(
        self,
        node_type:   str,
        node_index:  str,
        node_title:  str,
        doc_number:  str,
        dieu_index:  str = "",
        dieu_title:  str = "",
        khoan_index: str = "",
    ) -> str:
        """Build short structured header: [VB: ... | Đ1 (title) | K1]

        Format conventions:
          Điều  → Đ{index} ({title[:50]})
          Khoản → K{index}
          Điểm  → Đ.{index}
          Other → {node_type} {index}
        """
        parts = [f"VB: {doc_number}"]

        if node_type == "Điều":
            dieu_part = (
                f"Đ{node_index} ({node_title[:50]})" if node_title
                else f"Đ{node_index}"
            )
            parts.append(dieu_part)

        elif node_type == "Khoản":
            if dieu_index:
                dieu_part = (
                    f"Đ{dieu_index} ({dieu_title[:40]})" if dieu_title
                    else f"Đ{dieu_index}"
                )
                parts.append(dieu_part)
            parts.append(f"K{node_index}")

        elif node_type == "Điểm":
            if dieu_index:
                dieu_part = (
                    f"Đ{dieu_index} ({dieu_title[:30]})" if dieu_title
                    else f"Đ{dieu_index}"
                )
                parts.append(dieu_part)
            if khoan_index:
                parts.append(f"K{khoan_index}")
            parts.append(f"Đ.{node_index}")

        else:
            # Chương, Mục, Phần, PhụLục…
            label = (
                f"{node_type} {node_index} ({node_title[:50]})" if node_title
                else f"{node_type} {node_index}"
            )
            parts.append(label)

        return "[" + " | ".join(p for p in parts if p) + "]"

    # ── Children expansion ────────────────────────────────────────────────

    def _collect_children_text(
        self,
        children: list,
        max_chars: int = MAX_CHILD_CHARS,
        max_count: int = MAX_CHILD_COUNT,
    ) -> str:
        """Collect direct children text with structure preserved.

        Rules:
          - Direct children only (no grandchildren)
          - ≤ MAX_CHILD_COUNT items
          - ≤ MAX_CHILD_CHARS total
          - Preserve newlines (a) ... \\n b) ...)
          - Truncate marker: [... còn N điểm nữa]
        """
        parts: list[str] = []
        total = 0

        for i, child in enumerate(children):
            if i >= max_count:
                remaining = len(children) - max_count
                parts.append(f"[... còn {remaining} điểm nữa]")
                break

            child_type    = child.get("node_type", "")
            child_index   = child.get("node_index", "")
            child_content = (child.get("content") or "").strip()
            child_lead_in = (child.get("lead_in_text") or "").strip()

            body_parts = []
            if child_content:
                body_parts.append(child_content)
            if child_lead_in:
                body_parts.append(child_lead_in)
            body = "\n".join(body_parts).strip()

            if not body:
                continue

            if child_type == "Điểm":
                line = f"{child_index}) {body}"
            elif child_type == "Khoản":
                line = f"{child_index}. {body}"
            else:
                line = body

            if total + len(line) > max_chars:
                remaining = len(children) - i
                parts.append(f"[... còn {remaining} điểm nữa]")
                break

            parts.append(line)
            total += len(line)

        return "\n".join(parts)

    # ── Reference extraction ─────────────────────────────────────────────

    def _build_node_registry(
        self,
        nodes: list,
        registry: dict,
        dieu_idx: str = "",
        khoan_idx: str = "",
    ):
        """Traverse document tree → registry[(dieu, khoan, point)] = chunk_id.

        Used to resolve reference strings to actual chunk_ids before storing
        in metadata. Only Điều/Khoản/Điểm nodes are registered.
        """
        for node in nodes:
            ntype  = node.get("node_type", "")
            nidx   = str(node.get("node_index", ""))
            nid    = node.get("node_id", "")
            cid    = f"{nid}_chunk"

            if ntype == "Điều":
                dieu_idx  = nidx
                khoan_idx = ""
                registry[(nidx, "", "")] = cid
            elif ntype == "Khoản":
                khoan_idx = nidx
                registry[(dieu_idx, nidx, "")] = cid
            elif ntype == "Điểm":
                registry[(dieu_idx, khoan_idx, nidx)] = cid

            self._build_node_registry(
                node.get("children", []), registry, dieu_idx, khoan_idx
            )

    def _extract_refs(
        self,
        text: str,
        dieu_index: str,
        khoan_index: str,
        node_registry: dict,
        max_refs: int = 5,
    ) -> list[str]:
        """Extract + resolve references from node text.

        Handles:
          Absolute: "điểm a khoản 3 Điều 4", "khoản 2 Điều 5", "Điều 8"
          Relative: "Điều này" → dieu_index, "khoản này" → khoan_index

        Returns list of resolved chunk_ids (verified to exist in registry).
        """
        refs: list[str] = []
        seen: set[str] = set()

        def resolve(di: str, kh: str = "", pt: str = "") -> Optional[str]:
            """Try most specific key first, fall back to less specific."""
            for key in [(di, kh, pt), (di, kh, ""), (di, "", "")]:
                cid = node_registry.get(key)
                if cid:
                    return cid
            return None

        def add(chunk_id: Optional[str]):
            if chunk_id and chunk_id not in seen:
                seen.add(chunk_id)
                refs.append(chunk_id)

        # ── Absolute references (most specific first) ─────────────────────

        # "điểm a khoản 3 Điều 4"
        for m in re.finditer(
            r'điểm\s+([a-zđ])\s+khoản\s+(\d+)\s+[Ðđ]iều\s+(\d+)',
            text, re.IGNORECASE
        ):
            add(resolve(m.group(3), m.group(2), m.group(1).lower()))

        # "khoản X Điều Y" or "Điều Y khoản X"
        for m in re.finditer(
            r'khoản\s+(\d+)\s+[Ðđ]iều\s+(\d+)', text, re.IGNORECASE
        ):
            add(resolve(m.group(2), m.group(1)))

        for m in re.finditer(
            r'[Ðđ]iều\s+(\d+)\s+khoản\s+(\d+)', text, re.IGNORECASE
        ):
            add(resolve(m.group(1), m.group(2)))

        # "Điều X" standalone (not followed by "khoản" — already handled above)
        for m in re.finditer(
            r'[Ðđ]iều\s+(\d+)(?!\s+khoản)(?!\s+này)', text, re.IGNORECASE
        ):
            di = m.group(1)
            if di != dieu_index:        # skip self-reference
                add(resolve(di))

        # ── Relative references ───────────────────────────────────────────

        if re.search(r'[Ðđ]iều\s+này', text, re.IGNORECASE) and dieu_index:
            add(resolve(dieu_index))

        if re.search(r'khoản\s+này', text, re.IGNORECASE) and dieu_index and khoan_index:
            add(resolve(dieu_index, khoan_index))

        return refs[:max_refs]

    # ── Build chunks từ parsed JSON ──────────────────────────────────────

    def build_chunks(self, parsed_json_path: Path) -> List[Chunk]:
        """Đọc file JSON đã parse → trả về list Chunk"""
        with open(parsed_json_path, encoding="utf-8") as f:
            doc = json.load(f)

        metadata = doc.get("metadata", {})
        doc_id   = metadata.get("document_id", parsed_json_path.stem)
        chunks: List[Chunk] = []

        data_nodes = doc.get("data", [])
        if data_nodes:
            # Build node registry for reference resolution
            node_registry: dict = {}
            self._build_node_registry(data_nodes, node_registry)

            # Type A — structured legal hierarchy
            for node in data_nodes:
                self._extract_chunks(node, doc_id, metadata, chunks,
                                     node_registry=node_registry)
        else:
            # Type B — table-based guidance docs
            self._extract_table_chunks(doc, doc_id, metadata, chunks)

        # Dedup chunk_ids (can happen with amendment docs that have flattened nodes)
        seen: dict[str, int] = {}
        for c in chunks:
            if c.chunk_id in seen:
                seen[c.chunk_id] += 1
                c.chunk_id = f"{c.chunk_id}_{seen[c.chunk_id]}"
            else:
                seen[c.chunk_id] = 0

        logger.info(f"📦 Built {len(chunks)} chunks from {doc_id}")
        return chunks

    def _extract_table_chunks(
        self,
        doc: Dict[str, Any],
        doc_id: str,
        doc_meta: Dict[str, Any],
        chunks: List[Chunk],
    ):
        """Type B docs — serialize each table into searchable chunk(s).

        A4: Large tables (>MAX_TABLE_CHUNK_CHARS) are split into row-group
        sub-chunks, each repeating the header for self-containedness.
        Small tables produce a single chunk (unchanged behavior).
        """
        doc_title  = doc_meta.get("title", "")
        doc_number = doc_meta.get("document_number", "")

        for t in doc.get("tables", []):
            idx     = t.get("table_index", t.get("page_number", 0))
            headers = t.get("headers", [])
            rows    = t.get("rows", [])

            # --- Serialize rows ---
            header_line = " | ".join(str(h) for h in headers if h) if headers else ""
            # Truncate title to avoid inflating the fixed prefix
            _short_title = doc_title[:120] if doc_title else ""
            context_line = (
                f"{_short_title} — Bảng {idx}" if _short_title
                else f"{doc_number} — Bảng {idx}" if doc_number
                else f"Bảng {idx}"
            )

            def row_to_line(row) -> str:
                if isinstance(row, list):
                    return " | ".join(str(c) for c in row if c)
                if isinstance(row, dict):
                    return " | ".join(str(v) for v in row.values() if v)
                return str(row)

            row_lines = [row_to_line(r) for r in rows]

            # --- Build sub-chunk(s) ---
            breadcrumb_base = f"{doc_number}: Bảng {idx}"
            title_short     = " | ".join(str(h) for h in headers[:2] if h)[:80]
            base_meta = {
                "doc_id":          doc_id,
                "document_type":   doc_meta.get("document_type", ""),
                "document_number": doc_number,
                "node_type":       "GuidanceChunk",
                "node_index":      str(idx),
                "title":           title_short,
                "depth":           0,
                "effective_date":  doc_meta.get("effective_date", ""),
                "effective_to":    doc_meta.get("effective_to", ""),
                "status":          doc_meta.get("status", ""),
                "superseded_by":   doc_meta.get("superseded_by", ""),
                "parent_dieu_index":  "",
                "parent_khoan_index": "",
            }

            # Assemble one chunk, split if it exceeds MAX_TABLE_CHUNK_CHARS
            fixed_prefix = context_line + ("\n" + header_line if header_line else "")
            fixed_len    = len(fixed_prefix) + 1  # +1 for '\n'

            part_rows:  List[str] = []
            part_chars: int       = fixed_len
            part_num:   int       = 1

            def _flush(rows_slice: List[str], pnum: int):
                if not rows_slice:
                    return
                suffix    = f"_p{pnum}" if pnum > 1 else ""
                cid       = f"{doc_id}_chunk_{idx}{suffix}"
                bc        = f"{breadcrumb_base}{' (phần ' + str(pnum) + ')' if pnum > 1 else ''}"
                text_parts = [context_line]
                if header_line:
                    text_parts.append(header_line)
                text_parts.extend(rows_slice)
                text = "\n".join(text_parts).strip()
                if len(text) < 20:
                    return
                chunks.append(Chunk(
                    chunk_id   = cid,
                    doc_id     = doc_id,
                    node_id    = cid,
                    node_type  = "GuidanceChunk",
                    breadcrumb = bc,
                    text       = text,
                    metadata   = {**base_meta, "breadcrumb": bc, "node_index": f"{idx}_{pnum}" if pnum > 1 else str(idx)},
                ))

            for line in row_lines:
                line_len = len(line) + 1  # +1 for '\n'
                if part_chars + line_len > MAX_TABLE_CHUNK_CHARS and part_rows:
                    # Flush current part, start new
                    _flush(part_rows, part_num)
                    part_num  += 1
                    part_rows  = []
                    part_chars = fixed_len
                part_rows.append(line)
                part_chars += line_len

            # Flush remaining rows (always — even if only 1 part)
            _flush(part_rows, part_num if part_num > 1 or part_rows else 1)

    def _extract_chunks(
        self,
        node: Dict[str, Any],
        doc_id: str,
        doc_meta: Dict[str, Any],
        chunks: List[Chunk],
        depth: int = 0,
        parent_context: str = "",   # lead_in/content từ node cha, prepend vào Điểm
        dieu_index:  str = "",      # index Điều đang chứa node này
        dieu_title:  str = "",      # title Điều đang chứa node này
        khoan_index: str = "",      # index Khoản đang chứa node này (cho Điểm)
        node_registry: Optional[dict] = None,  # (dieu,khoan,point) → chunk_id
    ):
        """Đệ quy qua cây node, tạo chunk có breadcrumb header cho mỗi node."""

        node_id    = node.get("node_id", "")
        node_type  = node.get("node_type", "")
        node_index = node.get("node_index", "")
        title      = node.get("title") or ""
        content    = node.get("content") or ""
        lead_in    = node.get("lead_in_text") or ""
        trailing   = node.get("trailing_text") or ""
        breadcrumb = node.get("breadcrumb", "")
        doc_number = doc_meta.get("document_number", "")

        # ── Update hierarchy for children ────────────────────────────────
        child_dieu_index  = dieu_index
        child_dieu_title  = dieu_title
        child_khoan_index = khoan_index

        if node_type == "Điều":
            child_dieu_index  = node_index
            child_dieu_title  = title
            child_khoan_index = ""          # reset khi vào Điều mới
        elif node_type == "Khoản":
            child_khoan_index = node_index

        # ── Build breadcrumb header ───────────────────────────────────────
        header = self._build_header(
            node_type, node_index, title, doc_number,
            dieu_index, dieu_title, khoan_index,
        )

        # ── Build body text ───────────────────────────────────────────────
        # Title đã nằm trong header → không lặp lại trong body
        # Chỉ include content, lead_in, trailing
        body_parts = []
        if content:
            body_parts.append(content)
        if lead_in:
            body_parts.append(lead_in)
        if trailing:
            body_parts.append(trailing)

        body = "\n".join(filter(None, body_parts)).strip()

        # Children expansion cho Khoản (không áp dụng cho Điểm)
        if node_type == "Khoản" and node.get("children"):
            children_text = self._collect_children_text(node["children"])
            if children_text:
                body = (body + "\n" + children_text) if body else children_text

        # Điểm: prepend parent context để tăng khả năng retrieval
        if node_type == "Điểm" and parent_context and body:
            body = f"{parent_context}\n{body}"

        # Final: header + body
        text = f"{header}\n{body}".strip() if body else header

        # ── Create chunk ──────────────────────────────────────────────────
        if text and len(text) >= 20:
            # Extract references from full body text
            ref_text = " ".join(filter(None, [content, lead_in, trailing]))
            ref_ids  = (
                self._extract_refs(ref_text, dieu_index, khoan_index, node_registry)
                if node_registry else []
            )

            chunk = Chunk(
                chunk_id   = f"{node_id}_chunk",
                doc_id     = doc_id,
                node_id    = node_id,
                node_type  = node_type,
                breadcrumb = breadcrumb,
                text       = text,
                metadata   = {
                    "doc_id":               doc_id,
                    "document_type":        doc_meta.get("document_type", ""),
                    "document_number":      doc_number,
                    "node_type":            node_type,
                    "node_index":           node_index,
                    "title":                title,
                    "breadcrumb":           breadcrumb,
                    "depth":                depth,
                    "effective_date":       doc_meta.get("effective_date", ""),
                    # Hierarchy for cross-field validation & retrieval
                    "parent_dieu_index":    dieu_index  if node_type in ("Khoản", "Điểm") else "",
                    "parent_khoan_index":   khoan_index if node_type == "Điểm" else "",
                    # A2 — Reference expansion
                    "referenced_node_ids":  json.dumps(ref_ids),
                    # B3 — Amendment expansion
                    "amended_by_doc_ids":   json.dumps(AMENDED_BY.get(doc_id, [])),
                    # Validity window
                    "effective_to":         doc_meta.get("effective_to", ""),
                    "status":               doc_meta.get("status", ""),
                    "superseded_by":        doc_meta.get("superseded_by", ""),
                }
            )
            chunks.append(chunk)

        # ── Context for children ──────────────────────────────────────────
        child_context = (lead_in or content or title)[:200].strip()

        for child in node.get("children", []):
            self._extract_chunks(
                child, doc_id, doc_meta, chunks,
                depth + 1,
                parent_context = child_context,
                dieu_index     = child_dieu_index,
                dieu_title     = child_dieu_title,
                khoan_index    = child_khoan_index,
                node_registry  = node_registry,
            )

    # ── Metadata consistency validation ──────────────────────────────────

    def validate_chunks(self, chunks: List[Chunk]) -> Dict[str, Any]:
        """6-check metadata consistency (A–F per spec).

        Returns:
            {total, errors, warnings, ok}
        """
        errors:   list[str] = []
        warnings: list[str] = []
        seen_node_ids: dict[str, int] = {}

        for i, chunk in enumerate(chunks):
            meta       = chunk.metadata
            text       = chunk.text
            node_type  = meta.get("node_type", "")
            node_index = str(meta.get("node_index", ""))
            nid        = chunk.node_id

            # (D) Duplicate node_id
            if nid in seen_node_ids:
                errors.append(
                    f"[D] Duplicate node_id '{nid}' "
                    f"at chunk {i} and {seen_node_ids[nid]}"
                )
            else:
                seen_node_ids[nid] = i

            # (B) Required fields
            for req in ("doc_id", "node_type"):
                if not meta.get(req):
                    errors.append(f"[B] Chunk {i} ({nid}): missing required field '{req}'")
            if not nid:
                errors.append(f"[B] Chunk {i}: missing node_id")

            # (C) Size validation
            if len(text) < 30:
                warnings.append(
                    f"[C] Chunk {i} ({nid}): text too short ({len(text)} chars)"
                )
            elif len(text) > 1500:
                warnings.append(
                    f"[C] Chunk {i} ({nid}): text too long ({len(text)} chars)"
                )

            # (A) Text ↔ metadata match — parse header
            header_match = re.match(r"^\[([^\]]+)\]", text)
            if header_match:
                header = header_match.group(1)
                if node_type == "Khoản" and node_index:
                    if f"K{node_index}" not in header:
                        errors.append(
                            f"[A] Chunk {i} ({nid}): expected 'K{node_index}' "
                            f"in header, got: {header[:60]}"
                        )
                elif node_type == "Điểm" and node_index:
                    if f"Đ.{node_index}" not in header:
                        errors.append(
                            f"[A] Chunk {i} ({nid}): expected 'Đ.{node_index}' "
                            f"in header, got: {header[:60]}"
                        )
                elif node_type == "Điều" and node_index:
                    if f"Đ{node_index} " not in header and f"Đ{node_index})" not in header:
                        errors.append(
                            f"[A] Chunk {i} ({nid}): expected 'Đ{node_index}' "
                            f"in header, got: {header[:60]}"
                        )

            # (E) Empty semantic Khoản
            if node_type == "Khoản":
                body = re.sub(r"^\[[^\]]+\]\n?", "", text).strip()
                if len(body) < 10:
                    warnings.append(
                        f"[E] Chunk {i} ({nid}): Khoản has near-empty body"
                    )

            # (F) Cross-field: node_type vs node_index
            if node_type in ("Khoản", "Điểm") and not node_index:
                errors.append(
                    f"[F] Chunk {i} ({nid}): node_type={node_type} but node_index empty"
                )

        return {
            "total":    len(chunks),
            "errors":   errors,
            "warnings": warnings,
            "ok":       len(errors) == 0,
        }

    # ── Embed ────────────────────────────────────────────────────────────

    def embed_chunks(
        self,
        chunks: List[Chunk],
        batch_size: int = 32
    ) -> List[List[float]]:
        """Embed list chunks → trả về list vectors"""
        if not chunks:
            return []
        texts = [c.text for c in chunks]
        logger.info(f"🔢 Embedding {len(texts)} chunks (batch={batch_size})...")

        embeddings = self.model.encode(
            texts,
            batch_size           = batch_size,
            show_progress_bar    = True,
            normalize_embeddings = True,  # cosine similarity
        )

        logger.info(f"✅ Embedded {len(embeddings)} vectors, dim={len(embeddings[0])}")
        return embeddings.tolist()

    # ── Pipeline hoàn chỉnh ──────────────────────────────────────────────

    def process_file(self, json_path: Path) -> tuple[List[Chunk], List[List[float]]]:
        """Full pipeline: JSON → chunks → embeddings"""
        chunks     = self.build_chunks(json_path)
        embeddings = self.embed_chunks(chunks)
        return chunks, embeddings


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    json_files = list(Path("data/parsed").glob("*.json"))
    if not json_files:
        print("❌ Không có file JSON trong data/parsed/")
        sys.exit(1)

    embedder = DocumentEmbedder()

    for json_path in json_files[:2]:
        print(f"\n📄 Processing: {json_path.name}")
        chunks = embedder.build_chunks(json_path)
        print(f"   Chunks: {len(chunks)}")

        # Show sample
        for c in chunks[:3]:
            print(f"   [{c.node_type}] {c.text[:120]!r}")

        # Validate
        result = embedder.validate_chunks(chunks)
        print(f"   Validate: {result['total']} chunks, "
              f"{len(result['errors'])} errors, {len(result['warnings'])} warnings")
        for e in result["errors"][:5]:
            print(f"     ❌ {e}")
        for w in result["warnings"][:5]:
            print(f"     ⚠️  {w}")
