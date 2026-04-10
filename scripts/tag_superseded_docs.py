"""
scripts/tag_superseded_docs.py

Phase 1: Tag superseded=True vào ChromaDB metadata cho các docs đã bị thay thế.

Supersession map (từ Điều khoản thi hành):
  111_2013_TTBTC → superseded by 109_2025_QH15 (từ kỳ tính thuế 2026)
  92_2015_TTBTC  → superseded by 109_2025_QH15 (từ kỳ tính thuế 2026)

Rule 3 (transitional): Các chunks này vẫn cần cho queries về năm 2024/2025.
→ Tag superseded=True nhưng KHÔNG xóa — hybrid_search sẽ kiểm tra query year.
"""
import chromadb
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SUPERSEDED_DOCS = {
    "111_2013_TTBTC": {
        "superseded_by": "109_2025_QH15",
        "superseded_from_year": 2026,
        "reason": "Luật Thuế TNCN 04/2007 hết hiệu lực từ kỳ tính thuế 2026 (Điều 29K3 Luật 109/2025/QH15)",
    },
    "92_2015_TTBTC": {
        "superseded_by": "109_2025_QH15",
        "superseded_from_year": 2026,
        "reason": "Thông tư sửa đổi TT111 — superseded cùng với 111/2013 từ kỳ tính thuế 2026",
    },
}

CHROMA_DIR = "data/chroma"


def tag_doc(col, doc_id: str, info: dict) -> int:
    """Tag all chunks of a doc as superseded. Returns count updated."""
    # Fetch all chunks for this doc
    res = col.get(
        where={"doc_id": doc_id},
        include=["metadatas"],
        limit=10000,
    )
    ids = res["ids"]
    if not ids:
        logger.warning(f"No chunks found for {doc_id}")
        return 0

    # Check if already tagged
    already = sum(1 for m in res["metadatas"] if m.get("superseded") is True)
    if already == len(ids):
        logger.info(f"✅ {doc_id}: already fully tagged ({len(ids)} chunks)")
        return 0

    # Update metadata — add superseded fields
    new_metas = []
    for meta in res["metadatas"]:
        m = dict(meta)
        m["superseded"] = True
        m["superseded_by"] = info["superseded_by"]
        m["superseded_from_year"] = info["superseded_from_year"]
        new_metas.append(m)

    # ChromaDB update in batches of 500
    batch_size = 500
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i : i + batch_size]
        batch_metas = new_metas[i : i + batch_size]
        col.update(ids=batch_ids, metadatas=batch_metas)

    logger.info(f"✅ {doc_id}: tagged {len(ids)} chunks as superseded")
    return len(ids)


def main():
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    col = client.get_collection("taxai_legal_docs")

    total = 0
    for doc_id, info in SUPERSEDED_DOCS.items():
        count = tag_doc(col, doc_id, info)
        total += count

    logger.info(f"\n✅ Done. Tagged {total} chunks total.")

    # Verify
    for doc_id in SUPERSEDED_DOCS:
        res = col.get(where={"doc_id": doc_id, "superseded": True}, limit=1)
        status = "✅ tagged" if res["ids"] else "❌ NOT tagged"
        logger.info(f"  {doc_id}: {status}")


if __name__ == "__main__":
    main()
