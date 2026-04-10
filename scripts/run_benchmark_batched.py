"""
scripts/run_benchmark_batched.py — Chạy benchmark Pipeline v2 theo batch.

Chia 225 câu thành 5 batch × 45 câu, chạy tuần tự, mỗi batch lưu kết quả riêng.
Sau khi tất cả batch xong, tổng hợp lỗi và tạo báo cáo so sánh.

Usage:
    # Chạy tất cả 5 batch:
    python scripts/run_benchmark_batched.py

    # Chỉ chạy batch cụ thể (0-indexed):
    python scripts/run_benchmark_batched.py --batch 2

    # Merge kết quả từ các batch đã chạy:
    python scripts/run_benchmark_batched.py --merge-only

    # Dùng planner cũ để tạo baseline (để so sánh):
    python scripts/run_benchmark_batched.py --agent planner --name baseline

Options:
    --batch N       Chỉ chạy batch N (0-4), default: tất cả
    --agent         planner | pipeline (default: pipeline)
    --name          Tên prefix cho output files (default: pipeline_v2)
    --merge-only    Chỉ merge kết quả từ các batch đã có, không chạy thêm
    --batch-size    Số câu mỗi batch (default: 45)
    --delay         Delay ms giữa requests (default: 1000)
    --llm-judge     Bật T4b LLM judge
    --dry-run       Kiểm tra setup, không chạy
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT        = Path(__file__).parent.parent
RESULTS_DIR = ROOT / "data" / "eval" / "results"
EVAL_RUNNER = ROOT / "tests" / "eval_runner.py"


# ── Batch runner ───────────────────────────────────────────────────────────────

def run_batch(
    batch_idx: int,
    offset: int,
    limit: int,
    agent: str,
    name: str,
    delay: int,
    llm_judge: bool,
    dry_run: bool,
) -> Path:
    """Chạy một batch, trả về path file kết quả."""
    output_name = f"{name}_batch{batch_idx + 1}"
    output_path = RESULTS_DIR / output_name

    cmd = [
        sys.executable, str(EVAL_RUNNER),
        "--agent",   agent,
        "--offset",  str(offset),
        "--limit",   str(limit),
        "--output",  output_name,
        "--delay",   str(delay),
    ]
    if llm_judge:
        cmd.append("--llm-judge")
    if dry_run:
        cmd.append("--dry-run")

    print(f"\n{'='*60}")
    print(f"BATCH {batch_idx + 1} — câu {offset + 1}–{offset + limit} | output: {output_name}")
    print(f"{'='*60}")
    print(f"CMD: {' '.join(cmd[2:])}")   # bỏ python path

    t0 = time.monotonic()
    result = subprocess.run(cmd, cwd=str(ROOT))
    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        print(f"\n⚠️  Batch {batch_idx + 1} exited với code {result.returncode} ({elapsed:.0f}s)")
    else:
        print(f"\n✅ Batch {batch_idx + 1} done ({elapsed:.0f}s)")

    return output_path


# ── Merge + aggregate ──────────────────────────────────────────────────────────

def merge_batches(batch_paths: list[Path], name: str) -> Path:
    """Merge tất cả batch results, tạo báo cáo tổng hợp."""
    all_results: list[dict] = []
    missing_batches: list[int] = []

    for i, path in enumerate(batch_paths):
        # Tìm file với suffix .json (eval_runner lưu không có extension hoặc có)
        candidates = [path, path.with_suffix(".json"), Path(str(path) + ".json")]
        found = next((p for p in candidates if p.exists()), None)

        if found is None:
            print(f"  ⚠️  Batch {i + 1}: file không tìm thấy ({path.name})")
            missing_batches.append(i + 1)
            continue

        with open(found, encoding="utf-8") as f:
            data = json.load(f)
        batch_results = data.get("results", [])
        all_results.extend(batch_results)
        print(f"  ✅ Batch {i + 1}: {len(batch_results)} câu loaded từ {found.name}")

    if not all_results:
        print("❌ Không có kết quả nào để merge")
        return Path()

    # ── Deduplicate theo question_id ──────────────────────────────────────────
    seen: dict[str, dict] = {}
    for r in all_results:
        qid = str(r.get("question_id", ""))
        if qid not in seen:
            seen[qid] = r
    unique_results = list(seen.values())
    print(f"\n  📊 Total unique: {len(unique_results)} câu (từ {len(all_results)} raw)")

    # ── Tính metrics tổng hợp ─────────────────────────────────────────────────
    def avg_tier(results: list[dict], tier: str) -> float | None:
        scores = [r[tier]["score"] for r in results
                  if r.get(tier) and r[tier].get("score") is not None]
        return round(sum(scores) / len(scores), 3) if scores else None

    total   = len(unique_results)
    errors  = sum(1 for r in unique_results if r.get("error"))
    passed  = sum(1 for r in unique_results if _is_passed(r))
    avg_score = round(
        sum(_overall_score(r) for r in unique_results) / total, 3
    ) if total > 0 else 0.0

    summary = {
        "name":          name,
        "generated_at":  datetime.now().isoformat(),
        "total":         total,
        "passed":        passed,
        "pass_rate":     round(passed / total, 3) if total else 0,
        "avg_score":     avg_score,
        "errors":        errors,
        "missing_batches": missing_batches,
        "tier_scores": {
            "T1": avg_tier(unique_results, "tier1"),
            "T2": avg_tier(unique_results, "tier2"),
            "T3": avg_tier(unique_results, "tier3"),
            "T4": avg_tier(unique_results, "tier4"),
        },
        "latency": _latency_stats(unique_results),
        "fm_breakdown": _aggregate_fm(unique_results),
    }

    merged = {"summary": summary, "results": unique_results}
    out_path = RESULTS_DIR / f"{name}_merged.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    return out_path


def _is_passed(r: dict) -> bool:
    scores = [r[t]["score"] for t in ["tier1", "tier2", "tier3", "tier4"]
              if r.get(t) and r[t].get("score") is not None]
    avg = sum(scores) / len(scores) if scores else 0.0
    return round(avg * 1000) >= 667


def _overall_score(r: dict) -> float:
    scores = [r[t]["score"] for t in ["tier1", "tier2", "tier3", "tier4"]
              if r.get(t) and r[t].get("score") is not None]
    return round(sum(scores) / len(scores), 3) if scores else 0.0


def _latency_stats(results: list[dict]) -> dict:
    lats = [r.get("latency_ms", 0) for r in results if r.get("latency_ms")]
    if not lats:
        return {}
    lats_sorted = sorted(lats)
    n = len(lats_sorted)
    return {
        "avg_ms":  round(sum(lats) / n),
        "p50_ms":  lats_sorted[n // 2],
        "p95_ms":  lats_sorted[int(n * 0.95)],
        "min_ms":  lats_sorted[0],
        "max_ms":  lats_sorted[-1],
    }


def _aggregate_fm(results: list[dict]) -> dict:
    """Tổng hợp FM breakdown từ tất cả kết quả."""
    fm_counts: dict[str, int] = defaultdict(int)
    for r in results:
        for fm_id, count in r.get("fm_breakdown", {}).items():
            fm_counts[fm_id] += count
    return dict(sorted(fm_counts.items()))


# ── Error report ───────────────────────────────────────────────────────────────

def print_error_report(merged_path: Path) -> None:
    """In báo cáo lỗi từ file merged."""
    if not merged_path.exists():
        print("⚠️  Merged file không tìm thấy")
        return

    with open(merged_path, encoding="utf-8") as f:
        data = json.load(f)

    summary = data.get("summary", {})
    results = data.get("results", [])

    # ── Print summary ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"BENCHMARK REPORT — {summary.get('name', '')}")
    print(f"{'='*60}")
    print(f"  Total:     {summary.get('total')}")
    print(f"  Passed:    {summary.get('passed')} ({summary.get('pass_rate', 0)*100:.1f}%)")
    print(f"  Avg score: {summary.get('avg_score')}")
    print(f"  Errors:    {summary.get('errors')}")

    ts = summary.get("tier_scores", {})
    print(f"\nTier Scores:")
    for t, s in ts.items():
        bar = "░" * 10 if s is None else "█" * int((s or 0) * 10)
        val = "N/A" if s is None else f"{s:.3f}"
        print(f"  {t}: {bar} {val}")

    lat = summary.get("latency", {})
    if lat:
        print(f"\nLatency: avg={lat.get('avg_ms')}ms  p50={lat.get('p50_ms')}ms  p95={lat.get('p95_ms')}ms")

    fm = summary.get("fm_breakdown", {})
    if fm:
        print(f"\nFM Breakdown:")
        for fm_id, count in fm.items():
            print(f"  {fm_id}: {count}")

    # ── Failed questions ───────────────────────────────────────────────────────
    failed = [r for r in results if not _is_passed(r) and not r.get("error")]
    errors = [r for r in results if r.get("error")]

    if errors:
        print(f"\n❌ ERRORS ({len(errors)}):")
        for r in errors[:10]:
            print(f"  [{r['question_id']}] {r['question'][:60]} → {r['error'][:80]}")
        if len(errors) > 10:
            print(f"  ... và {len(errors)-10} câu lỗi khác")

    if failed:
        print(f"\n⚠️  FAILED (score < 0.667): {len(failed)} câu")

        # Group by tier thấp nhất
        t2_fails = [r for r in failed
                    if r.get("tier2", {}).get("score", 1.0) is not None
                    and r.get("tier2", {}).get("score", 1.0) < 0.5]
        t4_fails = [r for r in failed
                    if r.get("tier4", {}).get("score") is not None
                    and r.get("tier4", {}).get("score", 1.0) < 0.5]

        if t2_fails:
            print(f"\n  T2 Citation failures ({len(t2_fails)}):")
            for r in t2_fails[:5]:
                print(f"    [{r['question_id']}] {r['question'][:60]}")
                print(f"    → {r.get('tier2', {}).get('reason', '')[:80]}")

        if t4_fails:
            print(f"\n  T4 Key Facts failures ({len(t4_fails)}):")
            for r in t4_fails[:5]:
                print(f"    [{r['question_id']}] {r['question'][:60]}")
                print(f"    → {r.get('tier4', {}).get('reason', '')[:80]}")

        # Remaining fails
        other_fails = [r for r in failed if r not in t2_fails and r not in t4_fails]
        if other_fails:
            print(f"\n  Other failures ({len(other_fails)}):")
            for r in other_fails[:5]:
                score = _overall_score(r)
                print(f"    [{r['question_id']}] score={score:.2f} | {r['question'][:60]}")

    print(f"\n💾 Full results: {merged_path}")
    print(f"{'='*60}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Batched benchmark runner cho TaxAI")
    p.add_argument("--batch",      type=int,  default=None,       help="Chỉ chạy batch N (0-indexed)")
    p.add_argument("--agent",      type=str,  default="pipeline", choices=["planner", "pipeline"])
    p.add_argument("--name",       type=str,  default=None,       help="Prefix tên output files")
    p.add_argument("--merge-only", action="store_true",           help="Chỉ merge, không chạy")
    p.add_argument("--batch-size", type=int,  default=45,         help="Số câu mỗi batch (default: 45)")
    p.add_argument("--delay",      type=int,  default=1000,       help="Delay ms giữa requests")
    p.add_argument("--llm-judge",  action="store_true",           help="Bật T4b LLM judge")
    p.add_argument("--dry-run",    action="store_true",           help="Dry run")
    args = p.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Default name theo agent + timestamp
    if args.name is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        args.name = f"{args.agent}_bench_{ts}"

    # Load total để tính batches
    with open(ROOT / "data" / "eval" / "questions.json", encoding="utf-8") as f:
        total_questions = len(json.load(f))

    batch_size = args.batch_size
    n_batches  = (total_questions + batch_size - 1) // batch_size  # ceil division

    batch_configs = [
        (i, i * batch_size, batch_size)  # (idx, offset, limit)
        for i in range(n_batches)
    ]

    # Batch paths
    batch_paths = [RESULTS_DIR / f"{args.name}_batch{i + 1}" for i in range(n_batches)]

    print(f"📋 Benchmark plan: {total_questions} câu → {n_batches} batches × {batch_size}")
    print(f"   Agent: {args.agent} | Name: {args.name}")
    for i, (_, offset, limit) in enumerate(batch_configs):
        actual_limit = min(limit, total_questions - offset)
        status = "  " if args.batch is None or args.batch == i else "(skip)"
        print(f"   Batch {i+1}: câu {offset+1}–{offset+actual_limit} {status}")

    if args.merge_only:
        print("\n🔀 Merge only mode...")
        merged = merge_batches(batch_paths, args.name)
        print_error_report(merged)
        return

    # ── Run batches ────────────────────────────────────────────────────────────
    batches_to_run = (
        [args.batch] if args.batch is not None
        else list(range(n_batches))
    )

    start = time.monotonic()
    for i in batches_to_run:
        if i >= len(batch_configs):
            print(f"⚠️  Batch {i} không tồn tại (max: {n_batches-1})")
            continue
        _, offset, limit = batch_configs[i]
        run_batch(
            batch_idx = i,
            offset    = offset,
            limit     = min(limit, total_questions - offset),
            agent     = args.agent,
            name      = args.name,
            delay     = args.delay,
            llm_judge = args.llm_judge,
            dry_run   = args.dry_run,
        )

    total_elapsed = time.monotonic() - start
    print(f"\n⏱  Total time: {total_elapsed/60:.1f} phút")

    # ── Auto-merge nếu chạy hết tất cả batches ────────────────────────────────
    if args.batch is None and not args.dry_run:
        print("\n🔀 Merging all batches...")
        merged = merge_batches(batch_paths, args.name)
        if merged.exists():
            print_error_report(merged)


if __name__ == "__main__":
    main()
