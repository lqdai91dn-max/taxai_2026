"""
write_annotations_to_chroma.py — Write annotations từ progress.json → ChromaDB.

Dùng sau khi chạy node_annotator mà không có --write-chroma flag.

Chạy:
  python scripts/write_annotations_to_chroma.py --doc-id So_Tay_HKD
  python scripts/write_annotations_to_chroma.py --doc-id 310_2025_NDCP
  python scripts/write_annotations_to_chroma.py --all-pending
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import chromadb
from chromadb.config import Settings

from src.retrieval.node_annotator import (
    load_progress,
    save_annotations_to_chroma,
    COLLECTION_NAME,
    CHROMA_DIR,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def write_for_doc(doc_id: str, collection) -> int:
    """Write annotations cho 1 doc_id từ progress.json → ChromaDB. Returns count updated."""
    progress = load_progress()
    doc_annotations = {k: v for k, v in progress.items() if f"{doc_id}" in k}
    if not doc_annotations:
        logger.warning(f"No annotations found for {doc_id} in progress.json")
        return 0
    logger.info(f"{doc_id}: found {len(doc_annotations)} annotations in progress.json")
    summary = save_annotations_to_chroma(doc_annotations, collection)
    logger.info(f"{doc_id}: ChromaDB update → {summary}")
    return summary.get("updated", 0)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--doc-id", help="Doc ID để write")
    group.add_argument("--all-pending", action="store_true",
                       help="Write tất cả docs có annotation trong progress.json nhưng chưa 100% trong ChromaDB")
    args = parser.parse_args()

    client = chromadb.PersistentClient(
        path=CHROMA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_collection(COLLECTION_NAME)

    if args.doc_id:
        write_for_doc(args.doc_id, collection)
    else:
        # Find all doc_ids in progress.json
        progress = load_progress()
        doc_ids_in_progress = set()
        for k in progress.keys():
            # Key format: doc_<doc_id>_...
            # e.g. doc_1296_CTNVT_phan_0_chunk → 1296_CTNVT
            parts = k.replace("doc_", "", 1).split("_chunk")[0]
            # Try to match known doc_id patterns
            # The doc_id is embedded in the chunk_id
            for doc_id in ["125_2020_NDCP", "126_2020_NDCP", "So_Tay_HKD",
                           "310_2025_NDCP", "108_2025_QH15", "20_2026_NDCP",
                           "373_2025_NDCP", "198_2025_QH15", "149_2025_QH15",
                           "110_2025_UBTVQH15"]:
                if doc_id.replace("_", "") in k.replace("_", "") or doc_id in k:
                    doc_ids_in_progress.add(doc_id)

        # Check which docs need ChromaDB update
        for doc_id in sorted(doc_ids_in_progress):
            try:
                result = collection.get(
                    where={"doc_id": doc_id},
                    limit=1000,
                    include=["metadatas"],
                )
                total = len(result.get("ids", []))
                annotated = sum(1 for m in (result.get("metadatas") or [])
                                if m and m.get("nm_annotated", False))
                if annotated < total:
                    logger.info(f"→ {doc_id}: {annotated}/{total} annotated, writing...")
                    write_for_doc(doc_id, collection)
                else:
                    logger.info(f"✅ {doc_id}: {total}/{total} already annotated, skip")
            except Exception as e:
                logger.warning(f"  Could not check {doc_id}: {e}")


if __name__ == "__main__":
    main()
