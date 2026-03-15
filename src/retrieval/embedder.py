"""
src/retrieval/embedder.py
Tạo embeddings từ parsed JSON documents cho TaxAI 2026
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Model nhẹ, hỗ trợ tiếng Việt tốt
EMBEDDING_MODEL = "keepitreal/vietnamese-sbert"
FALLBACK_MODEL  = "paraphrase-multilingual-MiniLM-L12-v2"


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


class DocumentEmbedder:
    """
    Đọc parsed JSON → tạo Chunk list → embed bằng sentence-transformers
    """

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        self.model_name = model_name
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

    # ── Build chunks từ parsed JSON ──────────────────────────────────────

    def build_chunks(self, parsed_json_path: Path) -> List[Chunk]:
        """Đọc file JSON đã parse → trả về list Chunk"""
        with open(parsed_json_path, encoding="utf-8") as f:
            doc = json.load(f)

        metadata = doc.get("metadata", {})
        doc_id   = metadata.get("document_id", parsed_json_path.stem)
        chunks   = []

        data_nodes = doc.get("data", [])
        if data_nodes:
            # Type A — structured legal hierarchy
            for node in data_nodes:
                self._extract_chunks(node, doc_id, metadata, chunks)
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
        """Type B docs — serialize each table into a searchable chunk."""
        for t in doc.get("tables", []):
            idx     = t.get("table_index", t.get("page_number", 0))
            headers = t.get("headers", [])
            rows    = t.get("rows", [])

            parts = []
            if headers:
                parts.append(" | ".join(str(h) for h in headers if h))
            for row in rows[:20]:
                if isinstance(row, list):
                    parts.append(" | ".join(str(c) for c in row if c))
                elif isinstance(row, dict):
                    parts.append(" | ".join(str(v) for v in row.values() if v))

            text = "\n".join(parts).strip()
            if len(text) < 20:
                continue

            chunk_id = f"{doc_id}_chunk_{idx}"
            chunks.append(Chunk(
                chunk_id   = chunk_id,
                doc_id     = doc_id,
                node_id    = chunk_id,
                node_type  = "GuidanceChunk",
                breadcrumb = f"{doc_meta.get('document_number','')}: Bảng {idx}",
                text       = text[:2000],
                metadata   = {
                    "doc_id":          doc_id,
                    "document_type":   doc_meta.get("document_type", ""),
                    "document_number": doc_meta.get("document_number", ""),
                    "node_type":       "GuidanceChunk",
                    "node_index":      str(idx),
                    "title":           " | ".join(str(h) for h in headers[:2] if h)[:80],
                    "breadcrumb":      f"{doc_meta.get('document_number','')}: Bảng {idx}",
                    "depth":           0,
                    "effective_date":  doc_meta.get("effective_date", ""),
                }
            ))

    def _extract_chunks(
        self,
        node: Dict[str, Any],
        doc_id: str,
        doc_meta: Dict[str, Any],
        chunks: List[Chunk],
        depth: int = 0,
        parent_context: str = "",  # context từ node cha, prepend vào Điểm chunks
    ):
        """Đệ quy qua cây node, tạo chunk cho mỗi node có nội dung"""

        node_id    = node.get("node_id", "")
        node_type  = node.get("node_type", "")
        node_index = node.get("node_index", "")
        title      = node.get("title") or ""
        content    = node.get("content") or ""
        lead_in    = node.get("lead_in_text") or ""
        trailing   = node.get("trailing_text") or ""
        breadcrumb = node.get("breadcrumb", "")

        # Gộp text cho chunk (content trước, lead_in sau)
        parts = []
        if title:
            parts.append(f"{node_type} {node_index}: {title}")
        if content:
            parts.append(content)
        if lead_in:
            parts.append(lead_in)
        if trailing:
            parts.append(trailing)

        text = "\n".join(parts).strip()

        # Điểm chunks: prepend parent context để tăng khả năng retrieval
        # Ví dụ: "Giảm trừ gia cảnh gồm:\na) Mức giảm trừ...15,5 triệu"
        if node_type == "Điểm" and parent_context and text:
            text = f"{parent_context}\n{text}"

        # Chỉ tạo chunk nếu có đủ nội dung (>= 20 ký tự)
        if text and len(text) >= 20:
            chunk_id = f"{node_id}_chunk"
            chunk = Chunk(
                chunk_id   = chunk_id,
                doc_id     = doc_id,
                node_id    = node_id,
                node_type  = node_type,
                breadcrumb = breadcrumb,
                text       = text,
                metadata   = {
                    "doc_id":          doc_id,
                    "document_type":   doc_meta.get("document_type", ""),
                    "document_number": doc_meta.get("document_number", ""),
                    "node_type":       node_type,
                    "node_index":      node_index,
                    "title":           title,
                    "breadcrumb":      breadcrumb,
                    "depth":           depth,
                    "effective_date":  doc_meta.get("effective_date", ""),
                }
            )
            chunks.append(chunk)

        # Tạo context cho children: dùng phần text của node này
        # (ưu tiên lead_in vì nó thường là "...bao gồm:" dẫn vào list)
        child_context = (lead_in or content or title)[:200].strip()

        # Đệ quy vào children
        for child in node.get("children", []):
            self._extract_chunks(
                child, doc_id, doc_meta, chunks,
                depth + 1,
                parent_context=child_context,
            )

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
            batch_size      = batch_size,
            show_progress_bar = True,
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

    for json_path in json_files:
        print(f"\n📄 Processing: {json_path.name}")
        chunks, embeddings = embedder.process_file(json_path)
        print(f"   Chunks: {len(chunks)}")
        print(f"   Embedding dim: {len(embeddings[0])}")
        print(f"   Sample chunk:")
        print(f"     breadcrumb: {chunks[0].breadcrumb}")
        print(f"     text[:80]: {chunks[0].text[:80]}")