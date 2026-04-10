"""
scripts/validate_key_fact_extractor.py

Validate chất lượng KeyFactExtractor vs manual annotations trong questions.json.

Bước 1: Smoke test (5 câu, Stage 1+2+3a, không LLM)
Bước 2: Systematic eval Stage 3a trên toàn bộ câu có key_facts
Bước 3: LLM stage eval trên 20-30 câu fail Stage 3a (--llm flag)

Metrics:
  recall        = manual_facts_found / total_manual_facts
  precision     = manual_facts_found / total_auto_facts
  critical_miss = fraction của numeric/threshold facts bị miss

Usage:
  python scripts/validate_key_fact_extractor.py          # Bước 1+2 (no LLM)
  python scripts/validate_key_fact_extractor.py --llm    # Thêm Bước 3 (LLM)
  python scripts/validate_key_fact_extractor.py --smoke  # Chỉ Bước 1
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.retrieval.hybrid_search import HybridSearch as HybridSearcher
from src.retrieval.key_fact_extractor import KeyFactExtractor, LegalGraphIndex


# ── Helpers ───────────────────────────────────────────────────────────────────

_NUM_RE = re.compile(
    r"""
    \d+(?:[.,]\d+)?   # số
    \s*
    (?:%              # %
    |triệu|tỷ|nghìn   # đơn vị tiền
    |ngày|tháng|năm   # thời gian
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _normalize(s: str) -> str:
    """Chuẩn hóa để so sánh: lowercase, bỏ dấu câu thừa."""
    s = s.lower().strip()
    s = re.sub(r"[.,;:!?\"']", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


_UNIT_MAP = {
    # Tiền
    "tỷ đồng": "tỷ", "tỷ vnd": "tỷ", "tỷ vnđ": "tỷ",
    "triệu đồng": "triệu", "triệu vnd": "triệu",
    "nghìn đồng": "nghìn",
    # Loại bỏ leading zeros cho số nguyên: "01" → "1", "03" → "3"
}


def _numeric_canonical(s: str) -> str:
    """
    Chuẩn hoá biểu diễn số để so sánh:
      "01 tỷ"          → "1 tỷ"
      "1 tỷ đồng"      → "1 tỷ"
      "4.000.000"       → "4 triệu"
      "4,000,000"       → "4 triệu"
      "15,5 triệu"      → "15.5 triệu"
      "500.000.000 VND" → "500 triệu"
    """
    s = s.lower().strip()

    # Unify units
    for long, short in _UNIT_MAP.items():
        s = s.replace(long, short)

    # Chuẩn hóa dấu thập phân: "15,5" → "15.5"
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)

    # Xử lý số dạng 4.000.000 hoặc 4,000,000 (dấu phân cách hàng nghìn)
    def _expand_formatted(m):
        raw = m.group(0).replace(".", "").replace(",", "")
        val = int(raw)
        if val >= 1_000_000_000:
            t = val / 1_000_000_000
            return f"{t:.0f} tỷ" if t == int(t) else f"{t:.1f} tỷ"
        if val >= 1_000_000:
            t = val / 1_000_000
            return f"{t:.0f} triệu" if t == int(t) else f"{t:.1f} triệu"
        return raw

    s = re.sub(r"\b\d{1,3}(?:[.,]\d{3})+\b", _expand_formatted, s)

    # Bỏ leading zeros: "01 " → "1 "
    s = re.sub(r"\b0+(\d)", r"\1", s)

    # Bỏ đơn vị tiền tệ thừa
    s = re.sub(r"\b(vnd|vnđ|đồng)\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fact_in_list(fact: str, auto_facts: list[str], threshold: float = 0.7) -> bool:
    """
    Kiểm tra 1 manual fact có được cover bởi auto_facts không.
    So sánh cả raw + numeric-normalized để bắt "01 tỷ" == "1 tỷ đồng".
    """
    norm_fact  = _normalize(fact)
    canon_fact = _numeric_canonical(fact)

    for af in auto_facts:
        norm_af  = _normalize(af)
        canon_af = _numeric_canonical(af)

        # Substring match (raw normalized)
        if norm_fact in norm_af or norm_af in norm_fact:
            return True
        # Substring match (numeric canonical)
        if canon_fact and canon_af:
            if canon_fact in canon_af or canon_af in canon_fact:
                return True

        # Token overlap >= threshold (raw)
        tokens_f = set(norm_fact.split())
        tokens_a = set(norm_af.split())
        if tokens_f and tokens_a:
            overlap = len(tokens_f & tokens_a) / len(tokens_f)
            if overlap >= threshold:
                return True
    return False


def _is_critical(fact: str) -> bool:
    """Fact là numeric/threshold (% / tiền / ngày) — miss là nghiêm trọng nhất."""
    return bool(_NUM_RE.search(fact))


# ── Evaluation ────────────────────────────────────────────────────────────────

def eval_one(
    question: dict,
    extractor: KeyFactExtractor,
    use_llm: bool = False,
) -> dict:
    """
    So sánh auto-extracted facts với manual key_facts cho 1 câu hỏi.
    Trả về dict kết quả.
    """
    q_text    = question["question"]
    q_id      = question.get("id", "?")
    topic     = question.get("topic", "")
    doc_filter = question.get("doc_filter")
    manual_kf  = question.get("key_facts", [])

    if not manual_kf:
        return {"id": q_id, "skipped": True, "reason": "no key_facts"}

    # Extract
    try:
        auto_kf_objs = extractor.extract(
            question=q_text,
            doc_filter=doc_filter,
        )
        # Nếu use_llm=False, bỏ các facts từ source 'llm'
        if not use_llm:
            auto_kf_objs = [kf for kf in auto_kf_objs if kf.source != "llm"]
        auto_facts = [kf.value for kf in auto_kf_objs]
    except Exception as e:
        return {"id": q_id, "error": str(e), "question": q_text[:80]}

    # Recall: manual facts được cover bởi auto_facts
    matched     = [f for f in manual_kf if _fact_in_list(f, auto_facts)]
    missed      = [f for f in manual_kf if not _fact_in_list(f, auto_facts)]
    critical_missed = [f for f in missed if _is_critical(f)]

    recall    = len(matched) / len(manual_kf) if manual_kf else 1.0
    precision = len(matched) / len(auto_facts) if auto_facts else 0.0

    return {
        "id":               q_id,
        "topic":            topic,
        "question":         q_text[:100],
        "manual_facts":     manual_kf,
        "auto_facts":       auto_facts,
        "matched":          matched,
        "missed":           missed,
        "critical_missed":  critical_missed,
        "recall":           round(recall, 3),
        "precision":        round(precision, 3),
        "n_auto":           len(auto_facts),
        "n_manual":         len(manual_kf),
        "has_critical_miss": len(critical_missed) > 0,
    }


def print_result(r: dict, verbose: bool = False):
    """In kết quả 1 câu."""
    if r.get("skipped"):
        return
    if r.get("error"):
        print(f"  ❌ ERROR  Q{r['id']}: {r['error'][:60]}")
        return

    recall  = r["recall"]
    prec    = r["precision"]
    crit    = "🔴" if r["has_critical_miss"] else "🟢"
    status  = "✅" if recall >= 0.75 else ("⚠️" if recall >= 0.5 else "❌")
    print(f"  {status} Q{r['id']:3d} R={recall:.2f} P={prec:.2f} {crit} "
          f"auto={r['n_auto']:2d}/manual={r['n_manual']} | {r['question'][:60]}")

    if verbose or recall < 0.5:
        for f in r.get("missed", []):
            crit_tag = " ⚡CRITICAL" if _is_critical(f) else ""
            print(f"       MISS: {f}{crit_tag}")
        if r.get("auto_facts") and prec < 0.3:
            print(f"       AUTO (first 3): {r['auto_facts'][:3]}")


def print_summary(results: list[dict], label: str = ""):
    """In tổng kết."""
    valid = [r for r in results if not r.get("skipped") and not r.get("error")]
    if not valid:
        print("No valid results.")
        return

    recalls    = [r["recall"] for r in valid]
    precs      = [r["precision"] for r in valid]
    crit_miss  = sum(1 for r in valid if r["has_critical_miss"])
    errors     = sum(1 for r in results if r.get("error"))

    avg_recall = sum(recalls) / len(recalls)
    avg_prec   = sum(precs) / len(precs)
    n_ok       = sum(1 for r in valid if r["recall"] >= 0.75)
    n_partial  = sum(1 for r in valid if 0.5 <= r["recall"] < 0.75)
    n_fail     = sum(1 for r in valid if r["recall"] < 0.5)

    print()
    print("=" * 60)
    print(f"  SUMMARY{f' — {label}' if label else ''}")
    print("=" * 60)
    print(f"  Questions evaluated: {len(valid)}  (errors: {errors})")
    print(f"  Avg Recall:          {avg_recall:.3f}")
    print(f"  Avg Precision:       {avg_prec:.3f}")
    print(f"  Critical miss rate:  {crit_miss}/{len(valid)} = {crit_miss/len(valid):.1%}")
    print()
    print(f"  Recall ≥ 0.75  (OK):      {n_ok:3d} ({n_ok/len(valid):.0%})")
    print(f"  Recall 0.5-0.75 (partial): {n_partial:3d} ({n_partial/len(valid):.0%})")
    print(f"  Recall < 0.5   (fail):    {n_fail:3d} ({n_fail/len(valid):.0%})")
    print()

    # Gate check
    gate_ok = avg_recall >= 0.75 and (crit_miss / len(valid)) < 0.1
    gate_msg = "✅ PASS — có thể wire vào API" if gate_ok else "❌ FAIL — cần fix pipeline trước"
    print(f"  Gatekeeper: {gate_msg}")
    print("=" * 60)

    # Per-topic
    topics: dict[str, list[float]] = {}
    for r in valid:
        topics.setdefault(r.get("topic", "?"), []).append(r["recall"])
    if len(topics) > 1:
        print("\n  By topic:")
        for t, recs in sorted(topics.items()):
            avg = sum(recs) / len(recs)
            bar = "█" * int(avg * 10) + "░" * (10 - int(avg * 10))
            print(f"    {t:30s} {bar} {avg:.2f} (n={len(recs)})")

    # Worst 5
    worst = sorted(valid, key=lambda r: r["recall"])[:5]
    if worst:
        print("\n  Worst 5 (by recall):")
        for r in worst:
            print(f"    Q{r['id']:3d} R={r['recall']:.2f} | {r['question'][:60]}")
            for f in r.get("critical_missed", []):
                print(f"           ⚡ {f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke",   action="store_true", help="Chỉ chạy smoke test 5 câu")
    parser.add_argument("--llm",     action="store_true", help="Bật Stage 3b+5b LLM")
    parser.add_argument("--verbose", action="store_true", help="In chi tiết mọi câu")
    parser.add_argument("--limit",   type=int, default=0, help="Giới hạn số câu (0=tất cả)")
    parser.add_argument("--fail-only", action="store_true", help="Chỉ in câu fail")
    args = parser.parse_args()

    # Load questions
    q_path = ROOT / "data" / "eval" / "questions.json"
    with open(q_path, encoding="utf-8") as f:
        all_questions = json.load(f)

    questions_with_kf = [q for q in all_questions if q.get("key_facts")]
    print(f"Loaded {len(all_questions)} questions, {len(questions_with_kf)} have key_facts")

    # Build components
    print("Building graph index...", end=" ", flush=True)
    graph = LegalGraphIndex()
    print(f"✅ {len(graph.node_map)} nodes")

    print("Building searcher...", end=" ", flush=True)
    searcher = HybridSearcher()
    print("✅")

    llm_client = None
    if args.llm:
        print("Building LLM client...", end=" ", flush=True)
        from google import genai
        from src.utils.config import config as cfg
        llm_client = genai.Client(api_key=cfg.GOOGLE_API_KEY)
        print("✅")

    extractor = KeyFactExtractor(
        searcher=searcher,
        graph=graph,
        llm_client=llm_client,
        n_seeds=25,       # tăng từ 5 → 25 để bắt được nodes ở rank 30+
        graph_depth=1,    # giảm từ 2 → 1 để tránh BFS explosion với n_seeds lớn
    )

    # ── Bước 1: Smoke test ────────────────────────────────────────────────────
    SMOKE_IDS = {1, 5, 10, 50, 100}  # câu đa dạng topic
    smoke_qs  = [q for q in questions_with_kf if q.get("id") in SMOKE_IDS][:5]
    if not smoke_qs:
        smoke_qs = questions_with_kf[:5]

    print("\n" + "─" * 60)
    print("  BƯỚC 1: SMOKE TEST (5 câu, Stage 1+2+3a)")
    print("─" * 60)
    smoke_results = []
    for q in smoke_qs:
        print(f"  Q{q.get('id','?')}: {q['question'][:70]}...")
        r = eval_one(q, extractor, use_llm=False)
        smoke_results.append(r)
        print_result(r, verbose=True)

    smoke_errors = [r for r in smoke_results if r.get("error")]
    if smoke_errors:
        print(f"\n❌ Smoke test failed: {len(smoke_errors)} errors. Stopping.")
        sys.exit(1)
    print("\n✅ Smoke test passed — no crashes")

    if args.smoke:
        return

    # ── Bước 2: Systematic eval (Stage 3a, toàn bộ) ─────────────────────────
    eval_qs = questions_with_kf
    if args.limit > 0:
        eval_qs = eval_qs[:args.limit]

    print(f"\n{'─'*60}")
    print(f"  BƯỚC 2: SYSTEMATIC EVAL — {len(eval_qs)} câu (Stage 3a, no LLM)")
    print("─" * 60)

    results = []
    for i, q in enumerate(eval_qs, 1):
        r = eval_one(q, extractor, use_llm=False)
        results.append(r)

        show = (not args.fail_only) or (not r.get("skipped") and not r.get("error")
                                         and r.get("recall", 1.0) < 0.75)
        if show:
            print_result(r, verbose=args.verbose)

        if i % 20 == 0:
            done = [x for x in results if not x.get("skipped") and not x.get("error")]
            if done:
                avg_r = sum(x["recall"] for x in done) / len(done)
                print(f"  [{i}/{len(eval_qs)}] avg_recall so far: {avg_r:.3f}")

    print_summary(results, "Stage 3a (Regex only)")

    # ── Bước 3: LLM eval trên các câu fail ────────────────────────────────────
    if args.llm and llm_client:
        fail_qs_ids = {r["id"] for r in results
                       if not r.get("skipped") and not r.get("error")
                       and r.get("recall", 1.0) < 0.75}
        llm_qs = [q for q in eval_qs if q.get("id") in fail_qs_ids][:30]

        if llm_qs:
            print(f"\n{'─'*60}")
            print(f"  BƯỚC 3: LLM STAGE EVAL — {len(llm_qs)} câu fail (Stage 3a+3b+5b)")
            print("─" * 60)
            llm_results = []
            for q in llm_qs:
                r = eval_one(q, extractor, use_llm=True)
                llm_results.append(r)
                print_result(r, verbose=args.verbose)
            print_summary(llm_results, "Stage 3a+3b (Regex + LLM)")

            # Delta
            base_by_id = {r["id"]: r["recall"] for r in results if not r.get("skipped")}
            llm_by_id  = {r["id"]: r["recall"] for r in llm_results if not r.get("skipped")}
            gains = [(qid, llm_by_id[qid] - base_by_id.get(qid, 0))
                     for qid in llm_by_id if qid in base_by_id]
            if gains:
                avg_gain = sum(g for _, g in gains) / len(gains)
                print(f"\n  LLM lift (avg over {len(gains)} câu): +{avg_gain:.3f} recall")


if __name__ == "__main__":
    main()
