"""
Diagnostic T4 failures v2 — dùng ChromaDB get() theo retrieved_doc_ids
thay vì chỉ check top_chunks[:3] như diagnostic v1.

Phân loại 4 tầng:
  synthesis      — phrase có trong chunks của retrieved_docs → LLM bỏ qua
  retrieval_miss — phrase có trong ChromaDB nhưng không được retrieve
  chunk_missing  — phrase có trong parsed JSON nhưng không được embed vào ChromaDB
  doc_missing    — phrase không xuất hiện ở bất kỳ đâu trong corpus

Chạy:
  python scripts/diagnose_t4_v2.py
  python scripts/diagnose_t4_v2.py --result benchmark_round29
"""
import argparse
import json
import re
import sys
from pathlib import Path
from collections import defaultdict

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import chromadb
from chromadb.config import Settings

CHROMA_DIR       = str(ROOT / "data" / "chroma")
COLLECTION_NAME  = "taxai_legal_docs"
PARSED_DIR       = ROOT / "data" / "parsed"
QUESTIONS_PATH   = ROOT / "data" / "eval" / "questions.json"
RESULTS_DIR      = ROOT / "data" / "eval" / "results"

# ── Load ChromaDB ─────────────────────────────────────────────────────────────
def load_chroma():
    client = chromadb.PersistentClient(
        path=CHROMA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_collection(COLLECTION_NAME)

def get_all_chunks_for_doc(collection, doc_id: str) -> list[str]:
    """Lấy toàn bộ text chunks của 1 doc từ ChromaDB."""
    try:
        result = collection.get(
            where   = {"doc_id": doc_id},
            limit   = 2000,                # đủ cho cả doc lớn nhất (111_2013: 441 chunks)
            include = ["documents"],
        )
        return result.get("documents", []) or []
    except Exception as e:
        print(f"  [WARN] ChromaDB error for {doc_id}: {e}")
        return []

def get_all_doc_ids(collection) -> set[str]:
    """Lấy danh sách tất cả doc_id đang có trong ChromaDB."""
    try:
        result = collection.get(include=["metadatas"], limit=100_000)
        ids = set()
        for m in (result.get("metadatas") or []):
            if m and m.get("doc_id"):
                ids.add(m["doc_id"])
        return ids
    except Exception as e:
        print(f"  [WARN] Cannot list doc_ids: {e}")
        return set()

# ── Load parsed JSON ──────────────────────────────────────────────────────────
def collect_all_text_from_json(doc_id: str) -> str:
    """Trả về toàn bộ text content trong parsed JSON của doc_id."""
    path = PARSED_DIR / f"{doc_id}.json"
    if not path.exists():
        # Try documents/ subdirectory
        path = PARSED_DIR / "documents" / f"{doc_id}.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    texts = []

    def walk(node):
        if isinstance(node, dict):
            if "content" in node and node["content"]:
                texts.append(str(node["content"]))
            for child in node.get("children", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if "nodes" in data:
        for n in data["nodes"]:
            walk(n)
    elif "chunks" in data:
        for c in data["chunks"]:
            if isinstance(c, dict) and c.get("text"):
                texts.append(c["text"])
    else:
        walk(data)

    return " ".join(texts)

# ── Phrase match helper ───────────────────────────────────────────────────────
def phrase_in_text(phrase: str, text: str) -> bool:
    """Case-insensitive, normalize whitespace."""
    p = re.sub(r"\s+", " ", phrase.strip().lower())
    t = re.sub(r"\s+", " ", text.lower())
    return p in t

def phrase_in_chunks(phrase: str, chunks: list[str]) -> bool:
    return any(phrase_in_text(phrase, c) for c in chunks)

# ── Main diagnostic ───────────────────────────────────────────────────────────
def run(result_name: str):
    result_path = RESULTS_DIR / result_name
    # Support: folder (JSON files), file without extension, file with .json extension
    if result_path.is_dir():
        files = sorted(result_path.glob("*.json"))
    elif result_path.exists() and result_path.is_file():
        files = [result_path]
    elif result_path.with_suffix(".json").exists():
        files = [result_path.with_suffix(".json")]
    else:
        print(f"[ERROR] Cannot find result: {result_path}")
        sys.exit(1)

    # Load questions
    questions = {q["id"]: q for q in json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))}

    # Load collection
    print("⏳ Loading ChromaDB...")
    collection = load_chroma()
    all_chroma_doc_ids = get_all_doc_ids(collection)
    print(f"   ChromaDB: {collection.count()} chunks, {len(all_chroma_doc_ids)} docs")

    # Cache: doc_id → all chunk texts in ChromaDB
    chroma_chunks_cache: dict[str, list[str]] = {}
    # Cache: doc_id → full parsed JSON text
    json_text_cache: dict[str, str] = {}

    # Counters
    counts = defaultdict(int)  # category → count
    details: list[dict] = []

    # Load all result rows
    rows = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows.extend(data)
            elif isinstance(data, dict):
                # Handle format: {"report": {...}, "results": [...]}
                if "results" in data and isinstance(data["results"], list):
                    rows.extend(data["results"])
                else:
                    rows.append(data)
        except Exception as e:
            print(f"  [WARN] Cannot read {f}: {e}")

    print(f"   Loaded {len(rows)} result rows from '{result_name}'")

    # Filter to T4 failures only
    t4_failures = []
    for row in rows:
        qid = row.get("question_id") or row.get("id")
        t4 = row.get("tier4") or {}
        if isinstance(t4, dict):
            score = t4.get("score", 1.0)
            missing = t4.get("details", {}).get("missing", [])
        else:
            score = 1.0
            missing = []

        if score < 1.0 and missing:
            fm = row.get("fm_breakdown", {}) or {}
            top_chunks_data = fm.get("top_chunks", []) or []
            # Extract snippets from top_chunks (works for both old [:3] and new [:10] data)
            top_chunk_snippets = [
                c.get("snippet", "") for c in top_chunks_data if c.get("snippet")
            ]
            t4_failures.append({
                "qid": qid,
                "missing_phrases": missing,
                "retrieved_doc_ids": fm.get("doc_ids") or row.get("retrieved_doc_ids") or [],
                "top_chunk_snippets": top_chunk_snippets,
            })

    print(f"   T4 failures: {len(t4_failures)} questions with missing phrases\n")

    for item in t4_failures:
        qid = item["qid"]
        q = questions.get(qid, {})
        retrieved_doc_ids = [d for d in item["retrieved_doc_ids"] if d]
        top_chunk_snippets = item.get("top_chunk_snippets", [])

        for phrase in item["missing_phrases"]:
            # Step 1a: Check if phrase is in top_chunks stored in fm_breakdown
            # These are exactly the chunks passed to synthesizer context (top-10)
            if top_chunk_snippets and phrase_in_chunks(phrase, top_chunk_snippets):
                cat = "synthesis"
                counts["synthesis"] += 1
                details.append({"qid": qid, "phrase": phrase, "cat": "synthesis",
                                 "note": "in top_chunks"})
                continue

            # Step 1b: If no top_chunks data (old R29 has only 3), fallback:
            # check all ChromaDB chunks of retrieved docs (upper bound estimate)
            if not top_chunk_snippets:
                in_retrieved_chunks = False
                found_in_doc = None
                for doc_id in retrieved_doc_ids:
                    if doc_id not in chroma_chunks_cache:
                        chroma_chunks_cache[doc_id] = get_all_chunks_for_doc(collection, doc_id)
                    if phrase_in_chunks(phrase, chroma_chunks_cache[doc_id]):
                        in_retrieved_chunks = True
                        found_in_doc = doc_id
                        break

                if in_retrieved_chunks:
                    cat = "synthesis_or_retrieval_within_doc"
                    counts["synthesis"] += 1  # count as synthesis (upper bound)
                    details.append({"qid": qid, "phrase": phrase, "cat": "synthesis",
                                     "found_in_doc": found_in_doc,
                                     "note": "upper_bound (old top_chunks[:3] data)"})
                    continue

            # Step 2: Check if phrase is in ANY ChromaDB chunk
            in_any_chroma = False
            found_chroma_doc = None
            for doc_id in all_chroma_doc_ids:
                if doc_id not in chroma_chunks_cache:
                    chroma_chunks_cache[doc_id] = get_all_chunks_for_doc(collection, doc_id)
                if phrase_in_chunks(phrase, chroma_chunks_cache[doc_id]):
                    in_any_chroma = True
                    found_chroma_doc = doc_id
                    break

            if in_any_chroma:
                cat = "retrieval_miss"
                counts["retrieval_miss"] += 1
                details.append({"qid": qid, "phrase": phrase, "cat": "retrieval_miss",
                                 "found_in_doc": found_chroma_doc,
                                 "retrieved": retrieved_doc_ids})
                continue

            # Step 3: Check if phrase is in parsed JSON (any doc)
            in_any_json = False
            found_json_doc = None
            # First check expected docs from question
            expected_docs = []
            for kf in q.get("key_facts", []):
                if isinstance(kf, dict):
                    src = kf.get("source_doc") or ""
                    if src:
                        expected_docs.append(src)
            check_docs = list(set(expected_docs + list(all_chroma_doc_ids)))

            for doc_id in check_docs:
                if doc_id not in json_text_cache:
                    json_text_cache[doc_id] = collect_all_text_from_json(doc_id)
                if phrase_in_text(phrase, json_text_cache[doc_id]):
                    in_any_json = True
                    found_json_doc = doc_id
                    break

            if in_any_json:
                cat = "chunk_missing"
                counts["chunk_missing"] += 1
                details.append({"qid": qid, "phrase": phrase, "cat": "chunk_missing",
                                 "found_in_doc": found_json_doc})
            else:
                cat = "doc_missing"
                counts["doc_missing"] += 1
                details.append({"qid": qid, "phrase": phrase, "cat": "doc_missing",
                                 "retrieved": retrieved_doc_ids})

    # ── Report ────────────────────────────────────────────────────────────────
    total_phrases = sum(counts.values())
    print("=" * 60)
    print(f"T4 FAILURE DIAGNOSTIC v2 — {result_name}")
    print("=" * 60)
    print(f"Total missing phrases: {total_phrases}")
    print()
    for cat in ["synthesis", "retrieval_miss", "chunk_missing", "doc_missing"]:
        n = counts[cat]
        pct = 100 * n / total_phrases if total_phrases else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {cat:<18} {bar} {n:3d} ({pct:.0f}%)")
    print()

    # Per-category details
    for cat in ["synthesis", "retrieval_miss", "chunk_missing", "doc_missing"]:
        cat_items = [d for d in details if d["cat"] == cat]
        if not cat_items:
            continue
        print(f"── {cat.upper()} ({len(cat_items)}) ──")
        # Group by question
        by_q: dict[int, list] = defaultdict(list)
        for d in cat_items:
            by_q[d["qid"]].append(d)
        for qid, items in sorted(by_q.items()):
            q = questions.get(qid, {})
            print(f"  Q{qid} [{q.get('topic','')}] — {len(items)} phrase(s)")
            for d in items[:3]:  # show max 3 phrases per question
                p = d["phrase"][:70]
                extra = f" (in {d.get('found_in_doc', '?')})" if "found_in_doc" in d else ""
                print(f"    • {p}{extra}")
            if len(items) > 3:
                print(f"    … +{len(items)-3} more")
        print()

    # ── Actionable summary ────────────────────────────────────────────────────
    print("=" * 60)
    print("ACTIONABLE SUMMARY")
    print("=" * 60)
    if counts["synthesis"] > 0:
        print(f"🔴 synthesis={counts['synthesis']}: LLM có context nhưng drop phrase → cần prompt fix")
    if counts["retrieval_miss"] > 0:
        print(f"🟠 retrieval_miss={counts['retrieval_miss']}: chunk tồn tại trong ChromaDB nhưng không được retrieve → cần annotation/reranker")
    if counts["chunk_missing"] > 0:
        print(f"🟡 chunk_missing={counts['chunk_missing']}: phrase có trong JSON nhưng không được embed → cần re-embed / chunking fix")
    if counts["doc_missing"] > 0:
        print(f"⚫ doc_missing={counts['doc_missing']}: phrase không có trong bất kỳ corpus nào → cần thêm corpus")

    if counts["synthesis"] == 0 and counts["retrieval_miss"] == 0:
        print("\n✅ Synthesis và retrieval OK — bottleneck là chunking/corpus gap")
        print("   → Focus: re-embed với chunking tốt hơn + thêm corpus còn thiếu")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", default="benchmark_round29",
                        help="Tên result folder hoặc file trong data/eval/results/")
    args = parser.parse_args()
    run(args.result)
