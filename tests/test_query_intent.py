"""
tests/test_query_intent.py — Rule Parser tests for QueryIntent Builder (P5.1)

Tests only the Rule Parser (no API key required, ~0ms).
Covers 10 representative queries.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent.pipeline_v4.query_intent import build_query_intent, QueryIntent

# ---------------------------------------------------------------------------
# Test cases definition
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "id": 1,
        "query": "Tôi bán hàng doanh thu 1.2 tỷ/năm, thuế bao nhiêu?",
        "desc": "HKD goods — revenue + calculate intent",
        "expect_who": "HKD",
        "expect_activity_contains": "goods_distribution",
        "expect_domain_contains": "HKD",
        "expect_revenue": 1_200_000_000,
        "expect_intent": "calculate",
        "expect_requires_calc": True,
        "min_overall_conf": 0.45,
    },
    {
        "id": 2,
        "query": "Lương 30 triệu/tháng, 1 con nhỏ, đóng thuế TNCN bao nhiêu?",
        "desc": "PIT salary + dependent + calculate",
        "expect_who": "individual",
        "expect_activity_contains": "salary_wages",
        "expect_domain_contains": "PIT",
        "expect_income": 30_000_000,
        "expect_dependent_count": 1,
        "expect_intent": "calculate",
        "min_overall_conf": 0.45,
    },
    {
        "id": 3,
        "query": "Tiệm vàng của tôi doanh thu 60 tỷ, phải nộp thuế gì?",
        "desc": "HKD goods (jewelry) — high revenue",
        "expect_who": "HKD",
        "expect_activity_contains": "goods_distribution",
        "expect_domain_contains": "HKD",
        "expect_revenue": 60_000_000_000,
        "expect_intent": "calculate",
        "min_overall_conf": 0.45,
    },
    {
        "id": 4,
        "query": "Trúng xổ số 500 triệu, thuế bao nhiêu?",
        "desc": "PIT lottery — calculate",
        "expect_who": "individual",
        "expect_activity_contains": "lottery_prizes",
        "expect_domain_contains": "PIT",
        "expect_intent": "calculate",
        "min_overall_conf": 0.45,
    },
    {
        "id": 5,
        "query": "Tôi muốn cho thuê nhà 15 triệu/tháng, có phải đóng thuế không?",
        "desc": "Asset rental — explain/conditions",
        "expect_activity_contains": "asset_rental",
        "expect_intent_in": ("explain", "calculate"),
        "min_overall_conf": 0.30,
    },
    {
        "id": 6,
        "query": "Kinh doanh online trên Shopee, doanh thu 800 triệu, thuế?",
        "desc": "e_commerce_platform — calculate",
        "expect_who": "HKD",
        "expect_activity_contains": "e_commerce_platform",
        "expect_domain_contains": "HKD",
        "expect_revenue": 800_000_000,
        "expect_intent": "calculate",
        "expect_online": True,
        "min_overall_conf": 0.45,
    },
    {
        "id": 7,
        "query": "Điều kiện để được miễn thuế TNCN là gì?",
        "desc": "Explain exempt conditions — no calculation",
        "expect_domain_contains": "PIT",
        "expect_intent": "explain",
        "expect_requires_calc": False,
        "expect_requires_conditions": True,
        "min_overall_conf": 0.20,
    },
    {
        "id": 8,
        "query": "Quán cà phê doanh thu 400 triệu, có phải đóng thuế không?",
        "desc": "HKD café (manufacturing_transport) — below threshold check",
        "expect_who": "HKD",
        "expect_activity_contains": "manufacturing_transport",
        "expect_domain_contains": "HKD",
        "expect_revenue": 400_000_000,
        "min_overall_conf": 0.35,
    },
    {
        "id": 9,
        "query": "Công ty tôi trả lương 50 triệu, khấu trừ tại nguồn bao nhiêu?",
        "desc": "Employer PIT withholding — calculate",
        "expect_who": "employer",
        "expect_activity_contains": "salary_wages",
        "expect_domain_contains": "PIT",
        "expect_intent": "calculate",
        "min_overall_conf": 0.35,
    },
    {
        "id": 10,
        "query": "Năm 2026 luật thuế TNCN mới có gì thay đổi?",
        "desc": "Policy explain — no calculation",
        "expect_domain_contains": "PIT",
        "expect_intent": "explain",
        "expect_requires_calc": False,
        "expect_year": 2026,
        "min_overall_conf": 0.15,
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_tests():
    total = 0
    passed = 0
    failed_cases = []

    print("\n" + "=" * 70)
    print("  QueryIntent Rule Parser — Test Suite (10 queries, no API)")
    print("=" * 70)

    for tc in TEST_CASES:
        total += 1
        qi: QueryIntent = build_query_intent(tc["query"])  # no api_key → rule-only
        overall = qi.overall_confidence()
        errors = []

        # ── Assertions ──────────────────────────────────────────────────
        if "expect_who" in tc and qi.who.value != tc["expect_who"]:
            errors.append(f"who: got {qi.who.value!r}, expected {tc['expect_who']!r}")

        if "expect_activity_contains" in tc:
            ag = qi.activity_group.value
            if tc["expect_activity_contains"] not in ag:
                errors.append(
                    f"activity_group: {tc['expect_activity_contains']!r} not in {ag}"
                )

        if "expect_domain_contains" in tc:
            td = qi.tax_domain.value
            if tc["expect_domain_contains"] not in td:
                errors.append(
                    f"tax_domain: {tc['expect_domain_contains']!r} not in {td}"
                )

        if "expect_revenue" in tc:
            rev = qi.financials.value.get("revenue")
            if rev != tc["expect_revenue"]:
                errors.append(f"revenue: got {rev}, expected {tc['expect_revenue']}")

        if "expect_income" in tc:
            inc = qi.financials.value.get("income_value")
            if inc != tc["expect_income"]:
                errors.append(f"income_value: got {inc}, expected {tc['expect_income']}")

        if "expect_dependent_count" in tc:
            dep = qi.financials.value.get("dependent_count")
            if dep != tc["expect_dependent_count"]:
                errors.append(
                    f"dependent_count: got {dep}, expected {tc['expect_dependent_count']}"
                )

        if "expect_intent" in tc:
            primary = qi.intent.value.get("primary")
            if primary != tc["expect_intent"]:
                errors.append(f"intent.primary: got {primary!r}, expected {tc['expect_intent']!r}")

        if "expect_intent_in" in tc:
            primary = qi.intent.value.get("primary")
            if primary not in tc["expect_intent_in"]:
                errors.append(
                    f"intent.primary: got {primary!r}, expected one of {tc['expect_intent_in']}"
                )

        if "expect_requires_calc" in tc:
            rc = qi.intent.value.get("requires_calculation")
            if rc != tc["expect_requires_calc"]:
                errors.append(
                    f"requires_calculation: got {rc}, expected {tc['expect_requires_calc']}"
                )

        if "expect_requires_conditions" in tc:
            cond = qi.intent.value.get("requires_conditions")
            if cond != tc["expect_requires_conditions"]:
                errors.append(
                    f"requires_conditions: got {cond}, expected {tc['expect_requires_conditions']}"
                )

        if "expect_year" in tc:
            year = qi.time.value.get("year")
            if year != tc["expect_year"]:
                errors.append(f"time.year: got {year}, expected {tc['expect_year']}")

        if "expect_online" in tc:
            online = qi.flags.value.get("is_online_platform")
            if online != tc["expect_online"]:
                errors.append(
                    f"is_online_platform: got {online}, expected {tc['expect_online']}"
                )

        if overall < tc.get("min_overall_conf", 0.0):
            errors.append(
                f"overall_confidence {overall:.3f} < {tc['min_overall_conf']}"
            )

        # ── Report ───────────────────────────────────────────────────────
        status = "PASS" if not errors else "FAIL"
        if not errors:
            passed += 1
        else:
            failed_cases.append(tc["id"])

        print(f"\n[{status}] Q{tc['id']:02d}: {tc['desc']}")
        print(f"  Query:          {tc['query']}")
        print(f"  who:            {qi.who.value!r:20s}  (conf={qi.who.confidence:.2f}, src={qi.who.source})")
        print(f"  activity_group: {qi.activity_group.value}  (conf={qi.activity_group.confidence:.2f})")
        print(f"  tax_domain:     {qi.tax_domain.value}  (conf={qi.tax_domain.confidence:.2f})")
        print(f"  financials:     {qi.financials.value}")
        print(f"  intent:         {qi.intent.value}")
        print(f"  flags:          {qi.flags.value}")
        print(f"  time:           {qi.time.value}")
        print(f"  overall_conf:   {overall:.3f}")
        if errors:
            for err in errors:
                print(f"  !! {err}")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  Results: {passed}/{total} passed", end="")
    if failed_cases:
        print(f"  |  FAILED: Q{failed_cases}")
    else:
        print("  |  ALL PASS")
    print("=" * 70 + "\n")

    return passed, total


if __name__ == "__main__":
    passed, total = run_tests()
    sys.exit(0 if passed == total else 1)
