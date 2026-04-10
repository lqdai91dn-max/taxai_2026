"""
Mini benchmark stratified — 20 câu phân tầng để đo delta sau B1+B2 fix.

Nhóm:
  A (8 câu) — TMĐT + Xử phạt: hit✅ → đo synthesizer fix
  B (4 câu) — Giảm trừ gia cảnh: hit✅ → cross-validate synthesizer
  C (4 câu) — Quyết toán TNCN: mixed hit/miss → đo retrieval improvement
  D (4 câu) — HKD calc: control (không regression)

Usage:
  python scripts/mini_benchmark_stratified.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Stratified question IDs (từ diagnostic + knowledge of benchmark)
STRATIFIED_IDS = {
    "A_tmdt_xuphat": [21, 22, 24, 25, 48, 50, 83, 84],   # hit✅ → synthesizer
    "B_giamtru":     [201, 218, 223, 224],                  # hit✅ → cross-validate
    "C_quyettoan":   [204, 205, 206, 208],                  # mixed → retrieval
    "D_hkd_control": [3, 5, 7, 10],                         # calc control
}

ALL_IDS = []
for ids in STRATIFIED_IDS.values():
    ALL_IDS.extend(ids)

IDS_STR = ",".join(str(i) for i in ALL_IDS)

if __name__ == "__main__":
    import subprocess, sys
    cmd = [
        sys.executable, "tests/eval_runner.py",
        "--agent", "v4",
        "--ids", IDS_STR,
        "--output", "benchmark_mini_stratified",
        "--verbose",
    ]
    print(f"Running: {' '.join(cmd)}")
    print(f"Groups: {STRATIFIED_IDS}\n")
    subprocess.run(cmd)
