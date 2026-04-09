"""
Re-score existing benchmark results with updated expected_docs and key_facts
without making new API calls.

Usage:
    python scripts/rescore_with_new_annotations.py [input_result_file] [output_name]

Default:
    input:  data/eval/result/r56_full.json
    output: data/eval/results/r57_rescored
"""
import json
import sys
from pathlib import Path
from rapidfuzz import fuzz

ROOT = Path(__file__).parent.parent
QUESTIONS_FILE = ROOT / "data" / "eval" / "questions.json"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_t2(expected_docs, citations_got):
    """Recompute T2 with new expected_docs."""
    if not expected_docs:
        return None, "N/A — không có expected_docs"
    if not citations_got:
        return 0.0, "FAIL — không có citations (sources rỗng)"

    expected_set = set(expected_docs)
    citations_set = set(citations_got)
    overlap = len(citations_set & expected_set)
    recall = overlap / len(expected_set)
    precision = overlap / len(citations_set)
    penalty = min(1.0, precision / 0.5)
    t2 = round(recall * penalty, 3)

    if t2 >= 0.9:
        reason = f"PASS — overlap {overlap}/{len(expected_set)} docs, precision={precision:.2f}"
    elif t2 >= 0.4:
        reason = f"PARTIAL — overlap {overlap}/{len(expected_set)}, precision={precision:.2f}, penalty={penalty:.2f}"
    else:
        reason = f"FAIL — overlap {overlap}/{len(expected_set)} docs"
    return t2, reason


def compute_t4(key_facts, answer):
    """Recompute T4 with new key_facts using partial_ratio matching."""
    if not key_facts:
        return None, "N/A — không có key_facts"
    if not answer:
        return 0.0, "FAIL — answer rỗng"

    answer_lower = answer.lower()
    matched = []
    missing = []
    for kf in key_facts:
        kf_lower = kf.lower()
        # partial_ratio: sliding window match
        score = fuzz.partial_ratio(kf_lower, answer_lower)
        if score >= 80:
            matched.append(kf)
        else:
            missing.append(kf)

    n_match = len(matched)
    total = len(key_facts)
    t4 = round(n_match / total, 3)

    if n_match == total:
        reason = f"PASS — {n_match}/{total} key facts"
    elif n_match > 0:
        reason = f"PARTIAL — {n_match}/{total} key facts, thiếu: {missing}"
    else:
        reason = f"FAIL — chỉ {n_match}/{total} key facts, thiếu: {missing}"
    return t4, reason


def overall_score(t2, t4, degrade_level=1):
    """Compute overall score (T1+T2+T4 only, excluding T3)."""
    scores = [s for s in [t2, t4] if s is not None]
    if not scores:
        return 0.0
    base = sum(scores) / len(scores)
    multiplier = {1: 1.0, 2: 0.85, 3: 0.0}.get(degrade_level or 1, 1.0)
    return round(base * multiplier, 3)


def main():
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data/eval/result/r56_full.json"
    output_name = sys.argv[2] if len(sys.argv) > 2 else "r57_rescored"
    output_path = ROOT / "data" / "eval" / "results" / output_name

    results_in = load_json(input_path)
    questions = load_json(QUESTIONS_FILE)
    qmap = {str(q["id"]): q for q in questions}

    rescored = []
    changes = []

    for r in results_in["results"]:
        qid = str(r["question_id"])
        q = qmap.get(qid, {})
        new_expected = q.get("expected_docs", [])
        new_keyfacts = q.get("key_facts", [])
        answer = r.get("answer", "")

        # Get existing citations_doc_ids from tier2 details
        old_t2 = r.get("tier2", {})
        if isinstance(old_t2, dict) and "details" in old_t2:
            det = old_t2["details"]
            # PASS/PARTIAL format: matched + extra; FAIL format: got
            citations = list(set(det.get("matched", []) + det.get("extra", [])))
            if not citations:
                citations = det.get("got", [])
        else:
            citations = []
        # Fallback to retrieved_doc_ids if still empty
        if not citations:
            citations = r.get("citations_doc_ids", []) or r.get("retrieved_doc_ids", [])

        # Recompute T2
        new_t2_score, new_t2_reason = compute_t2(new_expected, citations)

        # Recompute T4
        new_t4_score, new_t4_reason = compute_t4(new_keyfacts, answer)

        old_t2_score = old_t2.get("score") if isinstance(old_t2, dict) else old_t2
        old_t4 = r.get("tier4", {})
        old_t4_score = old_t4.get("score") if isinstance(old_t4, dict) else old_t4

        # Build new result
        new_r = dict(r)
        new_r["tier2"] = {"score": new_t2_score, "reason": new_t2_reason, "details": {
            "expected_docs": new_expected, "citations": citations
        }}
        new_r["tier4"] = {"score": new_t4_score, "reason": new_t4_reason, "details": {
            "key_facts": new_keyfacts
        }}

        # Check if changed
        t2_changed = old_t2_score != new_t2_score
        t4_changed = old_t4_score != new_t4_score
        if t2_changed or t4_changed:
            old_score = overall_score(old_t2_score, old_t4_score, r.get("degrade_level", 1))
            new_score = overall_score(new_t2_score, new_t4_score, r.get("degrade_level", 1))
            old_pass = round(old_score * 1000) >= 667
            new_pass = round(new_score * 1000) >= 667
            if old_pass != new_pass or t2_changed or t4_changed:
                changes.append({
                    "id": qid,
                    "t2_old": f"{old_t2_score}" if old_t2_score is not None else "N/A",
                    "t2_new": f"{new_t2_score}" if new_t2_score is not None else "N/A",
                    "t4_old": f"{old_t4_score}" if old_t4_score is not None else "N/A",
                    "t4_new": f"{new_t4_score}" if new_t4_score is not None else "N/A",
                    "score_old": old_score,
                    "score_new": new_score,
                    "was_pass": old_pass,
                    "now_pass": new_pass,
                    "flip": "PASS" if (not old_pass and new_pass) else ("FAIL" if (old_pass and not new_pass) else "same"),
                })

        rescored.append(new_r)

    # Compute summary
    n_pass = sum(1 for r in rescored if round(
        overall_score(
            r["tier2"].get("score") if isinstance(r.get("tier2"), dict) else None,
            r["tier4"].get("score") if isinstance(r.get("tier4"), dict) else None,
            r.get("degrade_level", 1)
        ) * 1000
    ) >= 667)

    print(f"\n{'='*60}")
    print(f"RE-SCORE: {input_path.name} → {output_name}")
    print(f"{'='*60}")
    print(f"Total: {len(rescored)} questions")
    print(f"Pass (rescored): {n_pass} ({n_pass/len(rescored)*100:.1f}%)")
    print(f"Changed questions: {len(changes)}")

    gains = [c for c in changes if c["flip"] == "PASS"]
    losses = [c for c in changes if c["flip"] == "FAIL"]
    print(f"  Gain (fail→pass): {len(gains)}")
    print(f"  Loss (pass→fail): {len(losses)}")

    print(f"\n{'─'*60}")
    print("GAINS (fail → pass):")
    for c in sorted(gains, key=lambda x: x["id"]):
        print(f"  Q{c['id']:>3}: T2 {c['t2_old']}→{c['t2_new']} T4 {c['t4_old']}→{c['t4_new']} score {c['score_old']:.2f}→{c['score_new']:.2f}")

    if losses:
        print(f"\nLOSSES (pass → fail):")
        for c in sorted(losses, key=lambda x: x["id"]):
            print(f"  Q{c['id']:>3}: T2 {c['t2_old']}→{c['t2_new']} T4 {c['t4_old']}→{c['t4_new']} score {c['score_old']:.2f}→{c['score_new']:.2f}")

    # Save
    out_data = {"results": rescored, "report": {
        "summary": {
            "total": len(rescored),
            "passed": n_pass,
            "pass_rate": round(n_pass / len(rescored), 3),
        },
        "changes": changes,
    }}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)
    print(f"\nSaved → {output_path}")


if __name__ == "__main__":
    main()
