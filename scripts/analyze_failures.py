"""
Phân tích 52 câu fail từ R56, phân loại theo error_type:
  - annotation : model trả đúng nhưng key_facts mismatch
  - routing    : retrieved_docs không chứa expected_docs
  - corpus_gap : expected_docs không có trong parsed/ hoặc parse thiếu section
  - hard       : cần multi-hop / reasoning phức tạp, không rõ ràng

Chạy: python scripts/analyze_failures.py [fail_result_file]
"""
import json
import sys
import re
from pathlib import Path

PARSED_DIR = Path("data/parsed")
EXPECTED_IN_PARSED = {f.stem for f in PARSED_DIR.glob("*.json") if not f.name.startswith("documents")}

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_questions():
    qs = load_json("data/eval/questions.json")
    return {str(q["id"]): q for q in qs}

def get_score(tier_val):
    """Extract float score from tier dict or direct float."""
    if isinstance(tier_val, dict):
        return tier_val.get("score")
    return tier_val


def get_reason(tier_val):
    """Extract reason string from tier dict."""
    if isinstance(tier_val, dict):
        return tier_val.get("reason", "")
    return ""


def classify(r, q_meta):
    """
    Returns error_type: 'annotation' | 'routing' | 'corpus_gap' | 'hard'
    + brief reason string.
    """
    qid = str(r.get("question_id", ""))
    answer = (r.get("answer") or "").lower()
    retrieved = set(r.get("retrieved_doc_ids") or [])
    expected_docs = set(q_meta.get("expected_docs") or [])
    key_facts = q_meta.get("key_facts") or []
    t2 = get_score(r.get("tier2"))
    t4 = get_score(r.get("tier4"))
    t3 = get_score(r.get("tier3"))
    t4_reason = get_reason(r.get("tier4"))
    error = r.get("error")

    # API error → can't classify meaningfully
    if error:
        return "hard", f"API error: {error[:80]}"

    # 1. Corpus gap: expected doc không có trong parsed/
    missing_docs = expected_docs - EXPECTED_IN_PARSED
    if missing_docs:
        return "corpus_gap", f"Missing parsed: {missing_docs}"

    # 2. Routing sai: expected_docs không xuất hiện trong retrieved
    if expected_docs and not (expected_docs & retrieved):
        return "routing", f"Expected {sorted(expected_docs)}, got {sorted(retrieved)[:4]}"

    # 3. Partial routing: retrieved một phần expected_docs
    partial_match = expected_docs & retrieved
    if expected_docs and partial_match and len(partial_match) < len(expected_docs):
        missing = expected_docs - retrieved
        if t4 is not None and t4 < 0.5:
            return "routing", f"Partial: missing {sorted(missing)}"

    # 4. Annotation: retrieved đúng doc, T4 fail → phrasing mismatch
    if expected_docs and (expected_docs & retrieved):
        if t4 is not None and t4 < 0.5 and len(answer) > 100:
            # T4b_rescued = LLM judge cũng fail → really annotation or hard
            t4_detail = r.get("tier4", {})
            t4b = t4_detail.get("details", {}).get("t4b_rescued", []) if isinstance(t4_detail, dict) else []
            if "Thiếu" in t4_reason or "phrasing" in t4_reason.lower() or not t4b:
                return "annotation", f"Right docs retrieved, T4={t4:.2f}, key_facts={key_facts}"

    # 5. T2 fail (citation) with docs retrieved
    if t2 is not None and t2 < 0.5:
        if not expected_docs:
            return "annotation", f"No expected_docs set, T2={t2:.2f}"
        return "routing", f"T2={t2:.2f}, expected={sorted(expected_docs)}, got={sorted(retrieved)[:4]}"

    # 6. Hard: T4 vẫn fail dù retrieved đúng và T2 ok
    if t4 is not None and t4 < 0.5:
        return "hard", f"T4={t4:.2f}: {t4_reason[:80]}"

    return "hard", f"T2={t2} T4={t4}, overall below threshold"


def main():
    result_file = sys.argv[1] if len(sys.argv) > 1 else "data/eval/result/r56_fail_analysis.json"
    r56_file = "data/eval/result/r56_full.json"

    # Load R56 full để lấy tất cả câu fail
    r56 = load_json(r56_file)
    questions = load_questions()

    # Xác định câu fail từ R56 (dùng is_fail helper)

    # Tính overall_pass từ score
    def is_fail(r):
        scores = []
        for t in ["tier2", "tier3", "tier4"]:
            v = r.get(t)
            s = get_score(v)
            if s is not None:
                scores.append(s)
        t1 = get_score(r.get("tier1"))
        if t1 is not None:
            scores.append(t1)
        if not scores:
            return False
        avg = sum(scores) / len(scores)
        return avg < 0.667

    fail_results = [r for r in r56["results"] if is_fail(r)]

    # Load rerun file nếu có (để lấy verbose answer)
    rerun_by_id = {}
    if Path(result_file).exists():
        rerun = load_json(result_file)
        for r in rerun.get("results", []):
            rerun_by_id[str(r["question_id"])] = r

    print(f"\n{'='*70}")
    print(f"PHÂN TÍCH {len(fail_results)} CÂU FAIL — R56")
    print(f"{'='*70}\n")

    buckets = {"annotation": [], "routing": [], "corpus_gap": [], "hard": []}

    for r in sorted(fail_results, key=lambda x: x["question_id"]):
        qid = str(r["question_id"])
        q = questions.get(qid, {})
        topic = r.get("topic", "?")
        diff = r.get("difficulty", "?")

        # Use rerun result if available (has fresh answer)
        r_data = rerun_by_id.get(qid, r)
        etype, reason = classify(r_data, q)
        buckets[etype].append({
            "id": qid,
            "topic": topic,
            "difficulty": diff,
            "reason": reason,
            "question": r.get("question", "")[:100],
            "key_facts": q.get("key_facts", []),
            "expected_docs": q.get("expected_docs", []),
            "retrieved_docs": list(r_data.get("retrieved_doc_ids") or [])[:5],
            "t2": get_score(r_data.get("tier2")),
            "t4": get_score(r_data.get("tier4")),
        })

    # Print by bucket
    for etype in ["routing", "annotation", "corpus_gap", "hard"]:
        items = buckets[etype]
        pct = len(items) / max(len(fail_results), 1) * 100
        print(f"\n{'─'*70}")
        print(f"[{etype.upper()}] — {len(items)} câu ({pct:.0f}% of fails)")
        print(f"{'─'*70}")
        for item in sorted(items, key=lambda x: x["topic"]):
            t2s = f"{item['t2']:.2f}" if isinstance(item['t2'], float) else "?"
            t4s = f"{item['t4']:.2f}" if isinstance(item['t4'], float) else "?"
            print(f"  Q{item['id']:>3} | {item['topic']:<30} | {item['difficulty']:<6} | T2={t2s} T4={t4s}")
            print(f"       {item['question'][:90]}")
            print(f"       ⇒ {item['reason'][:100]}")
            print()

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    total = len(fail_results)
    for etype, items in sorted(buckets.items(), key=lambda x: -len(x[1])):
        pct = len(items) / max(total, 1) * 100
        print(f"  {etype:<12}: {len(items):>3} / {total}  ({pct:.0f}%)")

    print(f"\nTổng fail: {total}")
    quick_wins = len(buckets["annotation"]) + len(buckets["routing"])
    print(f"Quick wins (annotation + routing): {quick_wins} / {total}  ({quick_wins/total*100:.0f}%)")
    expected_gain = quick_wins / 225 * 100
    print(f"Expected gain nếu fix hết: +{expected_gain:.1f}pp → {76.9 + expected_gain:.1f}%")

    # Save to file
    out = {
        "total_fails": total,
        "buckets": {k: len(v) for k, v in buckets.items()},
        "details": {k: v for k, v in buckets.items()},
    }
    out_path = "data/eval/result/fail_analysis_r56.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nSaved → {out_path}")


if __name__ == "__main__":
    main()
