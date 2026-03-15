"""
Parser Validator — chạy trước khi ingest vào Graph DB.
Kiểm tra tất cả items trong Group 0 của pre_ingest_checklist.md.

Usage:
    python src/parsing/parser_validator.py                  # validate all parsed docs
    python src/parsing/parser_validator.py 109_2025_QH15    # validate 1 doc
"""

import json
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent.parent
PARSED_DIR = PROJECT_ROOT / "data" / "parsed"

REFERENCE_ID_PATTERN = re.compile(r"^(doc_|external_)")
MULTI_REF_PATTERN = re.compile(
    r"(Điều|Khoản|Điểm)\s+\d+(?:\s*,\s*(Điều|Khoản|Điểm)?\s*\d+){1,}"
)


@dataclass
class ValidationIssue:
    check_id: str       # e.g. "0.1"
    severity: str       # "error" | "warning"
    node_id: str
    message: str


@dataclass
class ValidationResult:
    doc_id: str
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def passed(self):
        return len(self.errors) == 0


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _walk(nodes: list[dict], parent_path: str = "") -> list[tuple[dict, str]]:
    """Yield (node, parent_path) for every node recursively."""
    result = []
    for n in nodes:
        result.append((n, parent_path))
        children = n.get("children", [])
        if children:
            result.extend(_walk(children, n.get("node_id", "")))
    return result


# ─────────────────────────────────────────────────────────────
# Check 0.1 — Duplicate node_index trong cùng parent
# ─────────────────────────────────────────────────────────────

def check_duplicate_node_index(nodes: list[dict], issues: list, doc_id: str):
    """
    Phát hiện node_index bị trùng trong cùng parent.

    Severity rules:
    - ERROR: node_id không có suffix _N (true duplicate — parser bug)
    - WARNING: node_id có suffix _2, _3... (amendment document flattened content —
               known limitation, data vẫn accessible qua unique node_id)
    """
    _AMENDMENT_SUFFIX = re.compile(r"_\d+$")

    def _check(children: list[dict], parent_id: str):
        seen: dict[str, str] = {}
        for n in children:
            idx = str(n.get("node_index", ""))
            ntype = n.get("node_type", "")
            key = f"{ntype}_{idx}"
            nid = n.get("node_id", "?")
            if idx and key in seen:
                # Nếu node_id kết thúc bằng _N → amendment inline content
                is_amendment_content = bool(_AMENDMENT_SUFFIX.search(
                    nid.split("_khoan_")[-1] if "_khoan_" in nid
                    else nid.split("_diem_")[-1] if "_diem_" in nid
                    else nid
                ))
                severity = "warning" if is_amendment_content else "error"
                issues.append(ValidationIssue(
                    check_id="0.1",
                    severity=severity,
                    node_id=nid,
                    message=f"Duplicate {ntype} index '{idx}' under parent '{parent_id}' "
                            f"(first: {seen[key]})"
                            + (" [amendment inline content — known]" if is_amendment_content else ""),
                ))
            else:
                seen[key] = nid
            _check(n.get("children", []), nid)

    _check(nodes, f"doc_{doc_id}")


# ─────────────────────────────────────────────────────────────
# Check 0.2 — Missing node_id
# ─────────────────────────────────────────────────────────────

def check_missing_node_id(nodes: list[dict], issues: list, doc_id: str):
    for node, parent_id in _walk(nodes):
        nid = node.get("node_id", "").strip()
        if not nid:
            issues.append(ValidationIssue(
                check_id="0.2",
                severity="error",
                node_id="(missing)",
                message=f"Node under '{parent_id}' has no node_id "
                        f"(type={node.get('node_type')}, index={node.get('node_index')})",
            ))


# ─────────────────────────────────────────────────────────────
# Check 0.3 — Breadcrumb không khớp hierarchy
# ─────────────────────────────────────────────────────────────

def check_breadcrumb(nodes: list[dict], issues: list, doc_id: str):
    """
    Breadcrumb phải bắt đầu bằng title document (từ metadata),
    và mỗi level phải là ancestor của level sau.
    Chỉ check format cơ bản: không được rỗng, phải có '>'.
    """
    for node, _ in _walk(nodes):
        nid = node.get("node_id", "?")
        bc = node.get("breadcrumb", "")
        if not bc:
            issues.append(ValidationIssue(
                check_id="0.3",
                severity="warning",
                node_id=nid,
                message="Breadcrumb is empty",
            ))
            continue
        parts = [p.strip() for p in bc.split(">")]
        if len(parts) < 2:
            issues.append(ValidationIssue(
                check_id="0.3",
                severity="warning",
                node_id=nid,
                message=f"Breadcrumb has only 1 part (no '>'): '{bc}'",
            ))


# ─────────────────────────────────────────────────────────────
# Check 0.4 — reference target_id format
# ─────────────────────────────────────────────────────────────

def check_reference_format(nodes: list[dict], issues: list, doc_id: str):
    for node, _ in _walk(nodes):
        nid = node.get("node_id", "?")
        for ref in node.get("references", []):
            tid = ref.get("target_id", "")
            if not tid:
                issues.append(ValidationIssue(
                    check_id="0.4",
                    severity="error",
                    node_id=nid,
                    message=f"Reference missing target_id: text_match='{ref.get('text_match')}'",
                ))
            elif not REFERENCE_ID_PATTERN.match(tid):
                issues.append(ValidationIssue(
                    check_id="0.4",
                    severity="error",
                    node_id=nid,
                    message=f"Reference target_id must start with 'doc_' or 'external_': '{tid}'",
                ))


# ─────────────────────────────────────────────────────────────
# Check 0.5 — Điều node rỗng hoàn toàn
# ─────────────────────────────────────────────────────────────

def check_empty_dieu(nodes: list[dict], issues: list, doc_id: str):
    for node, _ in _walk(nodes):
        if node.get("node_type") != "Điều":
            continue
        has_content = bool(node.get("content", "").strip())
        has_children = bool(node.get("children"))
        has_lead_in = bool(node.get("lead_in_text", "").strip())
        if not has_content and not has_children and not has_lead_in:
            issues.append(ValidationIssue(
                check_id="0.5",
                severity="warning",
                node_id=node.get("node_id", "?"),
                message="Điều node has no content, no children, and no lead_in_text",
            ))


# ─────────────────────────────────────────────────────────────
# Check 0.6 — Multi-reference chưa split
# ─────────────────────────────────────────────────────────────

def check_multi_reference_split(nodes: list[dict], issues: list, doc_id: str):
    """
    Phát hiện text như "Điều 8, Điều 11, Điều 12" mà chỉ có 1 reference object.
    Đây là dấu hiệu multi-ref chưa được split thành nhiều edges.
    """
    for node, _ in _walk(nodes):
        refs = node.get("references", [])
        for ref in refs:
            text = ref.get("text_match", "")
            # Nếu text match chứa nhiều số với dấu phẩy
            numbers = re.findall(r"\d+", text)
            if len(numbers) > 1 and "," in text:
                issues.append(ValidationIssue(
                    check_id="0.6",
                    severity="warning",
                    node_id=node.get("node_id", "?"),
                    message=f"Possible unsplit multi-reference: '{text}' "
                            f"→ should be {len(numbers)} separate edges",
                ))


# ─────────────────────────────────────────────────────────────
# Main validator
# ─────────────────────────────────────────────────────────────

CHECKS = [
    check_duplicate_node_index,
    check_missing_node_id,
    check_breadcrumb,
    check_reference_format,
    check_empty_dieu,
    check_multi_reference_split,
]


def validate_document(doc_id: str) -> ValidationResult:
    path = PARSED_DIR / f"{doc_id}.json"
    result = ValidationResult(doc_id=doc_id)

    if not path.exists():
        result.issues.append(ValidationIssue(
            check_id="0.0",
            severity="error",
            node_id="",
            message=f"Parsed file not found: {path}",
        ))
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        result.issues.append(ValidationIssue(
            check_id="0.0",
            severity="error",
            node_id="",
            message=f"Invalid JSON: {e}",
        ))
        return result

    nodes = data.get("data", [])

    # Type B documents (no nodes) — skip structural checks
    if not nodes:
        return result

    for check_fn in CHECKS:
        check_fn(nodes, result.issues, doc_id)

    return result


def validate_all() -> list[ValidationResult]:
    doc_ids = [
        p.stem for p in sorted(PARSED_DIR.glob("*.json"))
        if p.stem not in (".gitkeep",)
    ]
    return [validate_document(doc_id) for doc_id in doc_ids]


def print_report(results: list[ValidationResult]):
    total_errors = sum(len(r.errors) for r in results)
    total_warnings = sum(len(r.warnings) for r in results)

    print("=" * 65)
    print("PARSER VALIDATOR REPORT")
    print("=" * 65)

    for r in results:
        if r.passed and not r.warnings:
            print(f"  ✅  {r.doc_id}")
            continue

        status = "✅ " if r.passed else "❌ "
        print(f"\n  {status} {r.doc_id}  "
              f"[{len(r.errors)} error(s), {len(r.warnings)} warning(s)]")

        for issue in sorted(r.issues, key=lambda x: (x.severity, x.check_id)):
            icon = "  ERROR  " if issue.severity == "error" else "  WARN   "
            print(f"    [{issue.check_id}]{icon}{issue.node_id}")
            print(f"           {issue.message}")

    print("\n" + "=" * 65)
    print(f"SUMMARY: {len(results)} documents | "
          f"{total_errors} errors | {total_warnings} warnings")

    if total_errors == 0:
        print("✅ INGEST GATE: PASS — no blocking errors")
    else:
        print("❌ INGEST GATE: FAIL — fix errors before ingesting")
    print("=" * 65)

    return total_errors


if __name__ == "__main__":
    if len(sys.argv) > 1:
        results = [validate_document(sys.argv[1])]
    else:
        results = validate_all()

    exit_code = print_report(results)
    sys.exit(1 if exit_code > 0 else 0)
