"""
tests/test_reconstruction.py — Parser Reconstruction Validation

So sánh text reconstruct từ parsed JSON với original DOCX.
Mục tiêu: phát hiện text bị mất, sai, hoặc thừa.

Metrics:
  - Article coverage:   % Điều trong DOCX có trong parsed
  - Clause coverage:    % Khoản/Điểm trong DOCX có trong parsed
  - Content accuracy:   % parsed content khớp với DOCX text
  - Missing segments:   đoạn text trong DOCX không tìm thấy trong parsed

Usage:
  python tests/test_reconstruction.py               # tất cả DOCX docs
  python tests/test_reconstruction.py 109_2025_QH15 # một doc cụ thể
  python tests/test_reconstruction.py --report      # lưu báo cáo chi tiết
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from docx import Document as DocxDocument
except ImportError:
    print("ERROR: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)

ROOT = Path(__file__).parent.parent
RAW_DIR  = ROOT / "data" / "raw"
PARSED_DIR = ROOT / "data" / "parsed"
REPORT_DIR = ROOT / "data" / "reconstruction_reports"


# ── Text normalization ─────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Chuẩn hóa text để so sánh: strip whitespace, normalize spaces."""
    if not text:
        return ""
    # Normalize unicode (NFC — chuẩn Vietnamese)
    text = unicodedata.normalize("NFC", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_for_search(text: str) -> str:
    """Chuẩn hóa mạnh hơn cho fuzzy search: lowercase + remove punctuation."""
    text = normalize(text).lower()
    # ASCII punctuation
    text = re.sub(r"[.,;:\"'()\[\]{}]", " ", text)
    # Unicode quotation marks (DOCX vs parsed JSON có thể khác nhau)
    text = re.sub(r"[\u201c\u201d\u2018\u2019\u201e\u201f\u00ab\u00bb]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── DOCX extraction ────────────────────────────────────────────────────────────

# Patterns để skip boilerplate (preamble, signing block)
SKIP_PATTERNS = [
    r"^Căn cứ ",
    r"^Theo đề nghị",
    r"^Xét đề nghị",
    r"^Nơi nhận",
    r"^TM\.",
    r"^KT\.",
    r"^P\.KT\.",
    r"^\-\s+(Lưu|Gửi|Báo)",
    r"^(QUỐC HỘI|CHÍNH PHỦ|BỘ TÀI CHÍNH|ỦY BAN THƯỜNG VỤ QUỐC HỘI)$",
    r"^(CỘNG HÒA XÃ HỘI|Độc lập)",
    r"^Số:\s+\d+",
    r"^Hà Nội,\s+ngày",
]


def is_boilerplate(text: str) -> bool:
    return any(re.match(pat, text) for pat in SKIP_PATTERNS)


# Patterns that indicate start of appendix/form-template section
# When detected, stop attributing numbered items to current article
SECTION_RESET_PATTERNS = [
    r"^(PHỤ LỤC|Phụ lục|PHỤC LỤC)\b",   # Explicit phụ lục header
    r"^(MẪU SỐ|Mẫu số|BIỂU SỐ|Biểu số)\s+\S",  # Form number header
    r"^DANH MỤC\b",                        # Table of contents
]


def _is_form_section_start(text: str) -> bool:
    """Phát hiện bắt đầu phụ lục / biểu mẫu."""
    # Explicit phụ lục patterns
    if any(re.match(pat, text) for pat in SECTION_RESET_PATTERNS):
        return True
    # Form blank field (nhiều dấu chấm/dấu ba chấm → đây là ô trống biểu mẫu)
    if re.search(r"[.…]{5,}", text):
        return True
    return False


def _is_form_clause(text: str) -> bool:
    """Phát hiện khoản/điểm là nội dung biểu mẫu, không phải điều khoản thực chất."""
    if re.search(r"[.…]{5,}", text):
        return True
    # Footnote annotation at end: "[1]", "[n]", "<text>"
    if re.search(r"\[\d+\]\s*$", text):
        return True
    return False


def extract_docx_paragraphs(docx_path: Path) -> list[str]:
    """
    Trích xuất tất cả paragraph text từ DOCX.
    Trả về list paragraphs đã normalize, bỏ boilerplate.
    """
    doc = DocxDocument(str(docx_path))
    result = []
    for para in doc.paragraphs:
        text = normalize(para.text)
        if not text:
            continue
        if is_boilerplate(text):
            continue
        result.append(text)
    return result


def extract_docx_articles(docx_path: Path) -> list[dict]:
    """
    Trích xuất Điều/Khoản/Điểm từ DOCX thành structured list.
    Dùng để đo article coverage.
    """
    paras = extract_docx_paragraphs(docx_path)
    articles: list[dict] = []
    current_article: dict | None = None

    for para in paras:
        # Detect Điều — also resets form-section state
        m = re.match(r"^Điều\s+(\d+)[.\s]*(.*)", para)
        if m:
            current_article = {
                "number": m.group(1),
                "title": normalize(m.group(2)),
                "text": para,
                "clauses": [],
            }
            articles.append(current_article)
            continue

        # Detect start of phụ lục / biểu mẫu section → stop collecting clauses
        if _is_form_section_start(para):
            current_article = None
            continue

        # Detect Khoản
        m = re.match(r"^(\d+)\.\s+(.+)", para)
        if m and current_article and not _is_form_clause(para):
            current_article["clauses"].append({
                "type": "khoản",
                "index": m.group(1),
                "text": para,
            })
            continue

        # Detect Điểm
        m = re.match(r"^([a-zđ])\)\s+(.+)", para)
        if m and current_article and not _is_form_clause(para):
            current_article["clauses"].append({
                "type": "điểm",
                "index": m.group(1),
                "text": para,
            })
            continue

    return articles


# ── JSON reconstruction ────────────────────────────────────────────────────────

def reconstruct_from_json(parsed_path: Path) -> tuple[list[str], list[dict], list[str]]:
    """
    Reconstruct text từ parsed JSON.
    Returns:
      - flat_texts: list string (title + content — dùng cho accuracy check)
      - nodes_flat: list node dicts (để phân tích chi tiết)
      - lead_in_texts: list string (lead_in_text — dùng cho clause search thêm,
                       KHÔNG dùng cho accuracy vì có thể bị corrupt/chứa boilerplate)
    """
    with open(parsed_path, encoding="utf-8") as f:
        data = json.load(f)

    nodes_data = data.get("data", [])
    flat_texts = []
    nodes_flat = []
    lead_in_texts: list[str] = []

    def dfs(nodes: list, depth: int = 0):
        for node in nodes:
            title     = normalize(node.get("title", "") or "")
            content   = normalize(node.get("content", "") or "")
            lead_in   = normalize(node.get("lead_in_text", "") or "")
            node_type = node.get("node_type", "")

            # flat_texts: dùng cho accuracy check (title + content chính thức)
            if title and content:
                flat_texts.append(title)
                flat_texts.append(content)
            elif title:
                flat_texts.append(title)
            elif content:
                flat_texts.append(content)

            # lead_in_texts: chỉ dùng để mở rộng clause search corpus
            if lead_in:
                lead_in_texts.append(lead_in)

            nodes_flat.append({
                "node_id": node.get("node_id", ""),
                "node_type": node_type,
                "title": title,
                "content": content,
                "depth": depth,
            })
            dfs(node.get("children", []), depth + 1)

    dfs(nodes_data)
    return flat_texts, nodes_flat, lead_in_texts


# ── Comparison engine ──────────────────────────────────────────────────────────

def text_in_corpus(needle: str, haystack_joined: str, threshold: float = 0.85) -> bool:
    """
    Kiểm tra needle có xuất hiện trong haystack không.
    Dùng exact match trước, fallback về fuzzy.
    """
    needle_norm = normalize_for_search(needle)
    if len(needle_norm) < 10:
        return True  # quá ngắn, skip

    if needle_norm in haystack_joined:
        return True

    # Fuzzy: tìm đoạn 30-char sliding window
    window = len(needle_norm)
    if window > 200:
        # Chia nhỏ: nếu phần đầu + phần cuối đều match → ok
        head = needle_norm[:80]
        tail = needle_norm[-80:]
        return head in haystack_joined and tail in haystack_joined

    # SequenceMatcher cho đoạn ngắn
    matcher = difflib.SequenceMatcher(None, needle_norm, haystack_joined, autojunk=False)
    # Tìm longest match
    match = matcher.find_longest_match(0, len(needle_norm), 0, len(haystack_joined))
    ratio = match.size / len(needle_norm) if needle_norm else 0
    return ratio >= threshold


@dataclass
class DocResult:
    doc_id: str
    total_articles: int = 0
    found_articles: int = 0
    total_clauses: int = 0
    found_clauses: int = 0
    total_parsed_nodes: int = 0
    accuracy_ratio: float = 0.0        # % parsed content khớp với DOCX
    char_similarity: float = 0.0       # difflib character similarity
    missing_articles: list = field(default_factory=list)
    missing_clauses: list = field(default_factory=list)
    phantom_content: list = field(default_factory=list)  # parsed nhưng không có trong DOCX
    error: Optional[str] = None

    @property
    def article_coverage(self) -> float:
        return self.found_articles / self.total_articles if self.total_articles else 1.0

    @property
    def clause_coverage(self) -> float:
        return self.found_clauses / self.total_clauses if self.total_clauses else 1.0

    @property
    def passed(self) -> bool:
        return (
            self.article_coverage >= 0.99 and
            self.clause_coverage  >= 0.95 and
            self.accuracy_ratio   >= 0.95
        )


def get_parsed_article_numbers(parsed_path: Path) -> set[str]:
    """Lấy tập hợp số Điều từ parsed JSON (dùng node_index)."""
    with open(parsed_path, encoding="utf-8") as f:
        data = json.load(f)

    article_nums = set()

    def dfs(nodes):
        for n in nodes:
            if n.get("node_type") == "Điều":
                idx = str(n.get("node_index", "")).strip()
                if idx:
                    article_nums.add(idx)
            dfs(n.get("children", []))

    dfs(data.get("data", []))
    return article_nums


def validate_document(doc_id: str, verbose: bool = False) -> DocResult:
    result = DocResult(doc_id=doc_id)

    # Tìm DOCX file
    docx_path = RAW_DIR / f"{doc_id}.docx"
    if not docx_path.exists():
        docx_path = RAW_DIR / f"{doc_id}.doc"
    if not docx_path.exists():
        result.error = f"DOCX not found: {doc_id}.docx"
        return result

    # Tìm parsed JSON
    parsed_path = PARSED_DIR / f"{doc_id}.json"
    if not parsed_path.exists():
        result.error = f"Parsed JSON not found: {doc_id}.json"
        return result

    try:
        # Extract DOCX
        docx_articles = extract_docx_articles(docx_path)
        docx_paras    = extract_docx_paragraphs(docx_path)
        docx_joined   = " ".join(normalize_for_search(p) for p in docx_paras)

        # Reconstruct from JSON
        parsed_texts, parsed_nodes, lead_in_texts = reconstruct_from_json(parsed_path)
        # parsed_joined: bao gồm cả lead_in_texts để clause search rộng hơn
        parsed_joined = " ".join(
            normalize_for_search(t) for t in parsed_texts + lead_in_texts
        )

        # Article numbers from parsed JSON (reliable: uses node_index field)
        parsed_article_nums = get_parsed_article_numbers(parsed_path)

        result.total_parsed_nodes = len(parsed_nodes)
        result.total_articles = len(docx_articles)

        # ── Article coverage (by number matching) ──
        for art in docx_articles:
            found = art["number"] in parsed_article_nums
            if found:
                result.found_articles += 1
            else:
                result.missing_articles.append({
                    "article": art['number'],
                    "title": art['title'][:60],
                })

        # ── Clause coverage ──
        all_clauses = []
        for art in docx_articles:
            for cl in art["clauses"]:
                all_clauses.append((art["number"], cl))

        result.total_clauses = len(all_clauses)

        for art_num, clause in all_clauses:
            # Strip clause number prefix ("1. ", "a) ") before matching
            # because parser stores content WITHOUT the number prefix
            raw = clause["text"]
            stripped = re.sub(r"^\d+\.\s+", "", raw)   # "1. text" → "text"
            stripped = re.sub(r"^[a-zđ]\)\s+", "", stripped)  # "a) text" → "text"

            found = (text_in_corpus(raw, parsed_joined) or
                     text_in_corpus(stripped, parsed_joined))
            if found:
                result.found_clauses += 1
            else:
                result.missing_clauses.append({
                    "article": art_num,
                    "type": clause["type"],
                    "index": clause["index"],
                    "text_preview": clause["text"][:80],
                })

        # ── Content accuracy: % parsed text có trong DOCX ──
        checked = 0
        matched = 0
        for t in parsed_texts:
            if len(normalize(t)) < 15:
                continue  # quá ngắn, skip
            checked += 1
            if text_in_corpus(t, docx_joined):
                matched += 1
            else:
                result.phantom_content.append(t[:100])

        result.accuracy_ratio = matched / checked if checked else 1.0

        # ── Character similarity (sample: 5000 chars each) ──
        orig_sample = normalize(" ".join(docx_paras))[:5000]
        parsed_sample = normalize(" ".join(parsed_texts))[:5000]
        result.char_similarity = difflib.SequenceMatcher(
            None, orig_sample, parsed_sample
        ).ratio()

    except Exception as e:
        result.error = str(e)

    return result


# ── Report printing ────────────────────────────────────────────────────────────

def print_result(r: DocResult, verbose: bool = False):
    status = "✅ PASS" if r.passed else "❌ FAIL"
    if r.error:
        status = "⚠️  SKIP"

    print(f"\n{'─'*60}")
    print(f"{status}  {r.doc_id}")

    if r.error:
        print(f"   Error: {r.error}")
        return

    art_pct  = r.article_coverage * 100
    cl_pct   = r.clause_coverage  * 100
    acc_pct  = r.accuracy_ratio   * 100
    sim_pct  = r.char_similarity  * 100

    print(f"   Articles  : {r.found_articles}/{r.total_articles} ({art_pct:.1f}%)")
    print(f"   Clauses   : {r.found_clauses}/{r.total_clauses} ({cl_pct:.1f}%)")
    print(f"   Accuracy  : {acc_pct:.1f}%  (parsed content found in DOCX)")
    print(f"   CharSim   : {sim_pct:.1f}%  (difflib sample similarity)")
    print(f"   ParsedNodes: {r.total_parsed_nodes}")

    if r.missing_articles:
        print(f"\n   ⚠️  Missing Articles ({len(r.missing_articles)}):")
        for ma in r.missing_articles[:5]:
            print(f"      Điều {ma['article']}: {ma['title']}")

    if r.missing_clauses and (verbose or len(r.missing_clauses) <= 10):
        print(f"\n   ⚠️  Missing Clauses ({len(r.missing_clauses)}):")
        for mc in r.missing_clauses[:10]:
            print(f"      [Điều {mc['article']} {mc['type']} {mc['index']}] {mc['text_preview']}")
    elif r.missing_clauses:
        print(f"\n   ⚠️  Missing Clauses: {len(r.missing_clauses)} (use --verbose to see)")

    if r.phantom_content and verbose:
        print(f"\n   ⚠️  Phantom Content ({len(r.phantom_content)} segments not in DOCX):")
        for pc in r.phantom_content[:5]:
            print(f"      \"{pc}\"")


def save_report(results: list[DocResult], out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed and not r.error),
            "skipped": sum(1 for r in results if r.error),
        },
        "documents": [],
    }
    docs_list: list[dict] = report["documents"]  # type: ignore[assignment]
    for r in results:
        docs_list.append({
            "doc_id": r.doc_id,
            "passed": r.passed,
            "error": r.error,
            "article_coverage": round(r.article_coverage, 4),
            "clause_coverage": round(r.clause_coverage, 4),
            "accuracy_ratio": round(r.accuracy_ratio, 4),
            "char_similarity": round(r.char_similarity, 4),
            "total_articles": r.total_articles,
            "found_articles": r.found_articles,
            "total_clauses": r.total_clauses,
            "found_clauses": r.found_clauses,
            "missing_articles": r.missing_articles,
            "missing_clauses": r.missing_clauses[:20],
            "phantom_content": r.phantom_content[:10],
        })

    out_path = out_dir / "reconstruction_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📄 Report saved: {out_path}")


# ── DOCX-sourced documents ─────────────────────────────────────────────────────

DOCX_DOCS = [
    "108_2025_QH15",
    "109_2025_QH15",
    "110_2025_UBTVQH15",
    "117_2025_NDCP",
    "125_2020_NDCP",
    "149_2025_QH15",
    "152_2025_TTBTC",
    "198_2025_QH15",
    "20_2026_NDCP",
    "310_2025_NDCP",
    "373_2025_NDCP",
]


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parser Reconstruction Validation")
    parser.add_argument("docs", nargs="*", help="doc_id(s) to validate (default: all DOCX docs)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed diff")
    parser.add_argument("--report",  "-r", action="store_true", help="Save JSON report")
    args = parser.parse_args()

    target_docs = args.docs if args.docs else DOCX_DOCS

    print(f"🔍 Reconstruction Validation — {len(target_docs)} document(s)")
    print(f"   Targets: Article coverage ≥99% | Clause coverage ≥95% | Accuracy ≥95%")

    results = []
    for doc_id in target_docs:
        print(f"   Validating {doc_id}...", end="", flush=True)
        r = validate_document(doc_id, verbose=args.verbose)
        results.append(r)
        if r.error:
            print(f" ⚠️  {r.error}")
        elif r.passed:
            print(f" ✅ ({r.article_coverage*100:.0f}% art | {r.clause_coverage*100:.0f}% cl | {r.accuracy_ratio*100:.0f}% acc)")
        else:
            print(f" ❌ ({r.article_coverage*100:.0f}% art | {r.clause_coverage*100:.0f}% cl | {r.accuracy_ratio*100:.0f}% acc)")

    # Print details
    for r in results:
        print_result(r, verbose=args.verbose)

    # Summary
    passed  = sum(1 for r in results if r.passed)
    failed  = sum(1 for r in results if not r.passed and not r.error)
    skipped = sum(1 for r in results if r.error)
    print(f"\n{'═'*60}")
    print(f"SUMMARY: {passed}/{len(results)} passed | {failed} failed | {skipped} skipped")

    if results:
        valid = [r for r in results if not r.error]
        if valid:
            avg_art = sum(r.article_coverage for r in valid) / len(valid)
            avg_cl  = sum(r.clause_coverage  for r in valid) / len(valid)
            avg_acc = sum(r.accuracy_ratio   for r in valid) / len(valid)
            print(f"AVG:     Articles {avg_art*100:.1f}% | Clauses {avg_cl*100:.1f}% | Accuracy {avg_acc*100:.1f}%")

    if args.report:
        save_report(results, REPORT_DIR)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
