"""
TaxAI 2026 — Parse All Documents (v7 Production)
=================================================
Parses all registered tax documents using Parser v7.0.

Changes from previous version:
  - Uses ParserV7 (state machine + v4 extractor) instead of old PDFParser
  - Adds v7 stats: clauses, points, subpoints, chapters, tax tables
  - Better error reporting with OCR quality warnings
  - Supports both project-mode (src.* imports) and standalone-mode
  - json_serializer for date objects

Usage:
  python parse_all_documents.py              # project mode
  python parse_all_documents.py --standalone  # standalone mode (no src.*)
"""

import json
import sys
from pathlib import Path
from datetime import date, datetime

# ═══════════════════════════════════════════════════════════
# IMPORT STRATEGY: try project imports, fall back to standalone
# ═══════════════════════════════════════════════════════════

_STANDALONE = "--standalone" in sys.argv

if not _STANDALONE:
    try:
        from src.utils.logger import logger
        from src.utils.config import config, DOCUMENT_REGISTRY
        _PROJECT_MODE = True
    except ImportError:
        _PROJECT_MODE = False
else:
    _PROJECT_MODE = False

# Import ParserV7 — try project path first, then local
try:
    from src.parsing.pdf_parser import ParserV7, json_serializer
except ImportError:
    try:
        from pdf_parser import ParserV7, json_serializer
    except ImportError:
        # Last resort: inline the path
        sys.path.insert(0, str(Path(__file__).parent))
        from pdf_parser import ParserV7, json_serializer


# ═══════════════════════════════════════════════════════════
# STANDALONE CONFIG (when src.utils.config not available)
# ═══════════════════════════════════════════════════════════

if not _PROJECT_MODE:
    class _StandaloneConfig:
        """Minimal config for standalone mode"""
        def __init__(self):
            base = Path(__file__).parent
            self.RAW_DIR    = base / "data" / "raw"
            self.PARSED_DIR = base / "data" / "parsed"
            self.LOG_DIR    = base / "logs"

            # Create directories
            self.PARSED_DIR.mkdir(parents=True, exist_ok=True)
            self.LOG_DIR.mkdir(parents=True, exist_ok=True)

    config = _StandaloneConfig()

    # Auto-discover PDFs if no registry
    DOCUMENT_REGISTRY = {}
    if config.RAW_DIR.exists():
        for pdf in sorted(config.RAW_DIR.glob("*.pdf")):
            DOCUMENT_REGISTRY[pdf.stem] = {"filename": pdf.name}

    class _Logger:
        def info(self, msg):  print(f"ℹ️  {msg}")
        def warning(self, msg): print(f"⚠️  {msg}")
        def error(self, msg): print(f"❌ {msg}")

    logger = _Logger()


# ═══════════════════════════════════════════════════════════
# MAIN FUNCTION
# ═══════════════════════════════════════════════════════════

def parse_all_documents():
    """Parse all registered documents with Parser v7.0"""

    print("\n" + "=" * 70)
    print(" " * 10 + "TAXAI 2026 — PARSING ALL DOCUMENTS (v7.0)")
    print("=" * 70)

    all_docs = list(DOCUMENT_REGISTRY.keys())

    print(f"Documents in registry: {len(all_docs)}")
    print(f"PDF directory:         {config.RAW_DIR}")
    print(f"Output directory:      {config.PARSED_DIR}")
    print("=" * 70)

    if not all_docs:
        print("⚠️  No documents found in registry!")
        print(f"   Check that PDFs exist in: {config.RAW_DIR}")
        return {}

    # ─── Parse each document ───────────────────────────────

    parser = ParserV7()
    results = {}

    for i, doc_key in enumerate(all_docs, 1):

        print(f"\n{'─' * 70}")
        print(f"[{i}/{len(all_docs)}] {doc_key}")
        print("─" * 70)

        # Resolve PDF path
        pdf_path = config.RAW_DIR / f"{doc_key}.pdf"
        if not pdf_path.exists():
            # Try without extension in case key already has it
            alt = config.RAW_DIR / doc_key
            if alt.exists():
                pdf_path = alt
            else:
                print(f"⚠️  PDF not found: {pdf_path}")
                results[doc_key] = {"status": "SKIP", "reason": "PDF not found"}
                continue

        try:
            # ── PARSE ──────────────────────────────────────
            doc = parser.parse(str(pdf_path))

            # Check for error docs (e.g. no text extracted)
            if doc.get("error"):
                print(f"⚠️  Partial: {doc['error']}")
                results[doc_key] = {
                    "status": "PARTIAL",
                    "reason": doc["error"],
                    "doc_type": doc.get("doc_type"),
                }
                # Still save the partial result
                _save_json(doc, doc_key)
                continue

            # ── SAVE JSON ──────────────────────────────────
            _save_json(doc, doc_key)

            # ── COLLECT STATS ──────────────────────────────
            stats = doc.get("stats", {})
            articles = doc.get("articles", [])

            # Chapter info
            chapters = sorted(set(
                a["chapter"] for a in articles
                if a.get("chapter")
            ))

            # Tax table articles
            tax_articles = [
                a["number"] for a in articles
                if a.get("has_tax_table")
            ]

            results[doc_key] = {
                "status":         "SUCCESS",
                "doc_type":       doc.get("doc_type"),
                "number":         doc.get("number"),
                "title":          doc.get("title", "")[:100],
                "issued_by":      doc.get("issued_by"),
                "issued_date":    doc.get("issued_date"),
                "effective_from": doc.get("effective_from"),
                "articles":       stats.get("articles", 0),
                "clauses":        stats.get("clauses", 0),
                "points":         stats.get("points", 0),
                "subpoints":      stats.get("subpoints", 0),
                "appendices":     stats.get("appendices", 0),
                "chapters":       chapters,
                "tax_articles":   tax_articles,
                "references":     doc.get("references", []),
                "scanned_pages":  stats.get("scanned_pages", []),
                "parser_version": doc.get("parser_version"),
            }

            print(f"✅ {doc.get('doc_type', '?').upper()} {doc.get('number', '?')}")
            print(f"   {doc.get('title', '')[:70]}")
            print(f"   {stats.get('articles',0)} art | "
                  f"{stats.get('clauses',0)} cl | "
                  f"{stats.get('points',0)} pt | "
                  f"{stats.get('subpoints',0)} sp | "
                  f"{stats.get('appendices',0)} app")
            if chapters:
                print(f"   Chapters: {', '.join(chapters)}")
            if tax_articles:
                print(f"   📊 Tax tables in: Điều {', '.join(tax_articles)}")

        except Exception as e:
            msg = str(e)
            print(f"❌ FAILED: {msg[:200]}")
            logger.error(f"Parse failed {doc_key}: {msg}")
            results[doc_key] = {"status": "FAIL", "reason": msg[:500]}

    # ─── FINAL SUMMARY ─────────────────────────────────────

    _print_summary(results, all_docs)

    return results


# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def _save_json(doc: dict, doc_key: str):
    """Save parsed document to JSON"""
    output_path = config.PARSED_DIR / f"{doc_key}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, default=json_serializer)
    print(f"💾 {output_path}")


def _print_summary(results: dict, all_docs: list):
    """Print final parsing summary"""

    success = [k for k, v in results.items() if v["status"] == "SUCCESS"]
    partial = [k for k, v in results.items() if v["status"] == "PARTIAL"]
    skipped = [k for k, v in results.items() if v["status"] == "SKIP"]
    failed  = [k for k, v in results.items() if v["status"] == "FAIL"]

    total = len(all_docs)

    print("\n" + "=" * 70)
    print(" " * 20 + "PARSING SUMMARY (v7.0)")
    print("=" * 70)

    print(f"\n📊 Results: {len(success)}/{total} success, "
          f"{len(partial)} partial, {len(skipped)} skipped, {len(failed)} failed")

    # ── SUCCESS details ────────────────────────────────────
    if success:
        print(f"\n✅ SUCCESS ({len(success)}):")
        print(f"   {'Document':<30s} {'Type':<12s} {'Art':>4s} {'Cl':>4s} {'Pt':>4s} {'App':>4s} {'Effective':<12s}")
        print(f"   {'─'*29} {'─'*11} {'─'*4} {'─'*4} {'─'*4} {'─'*4} {'─'*11}")

        total_art = total_cl = total_pt = total_app = 0

        for k in success:
            r = results[k]
            print(f"   {k:<30s} {r.get('doc_type','?'):<12s} "
                  f"{r.get('articles',0):>4d} {r.get('clauses',0):>4d} "
                  f"{r.get('points',0):>4d} {r.get('appendices',0):>4d} "
                  f"{r.get('effective_from','?'):<12s}")
            total_art += r.get("articles", 0)
            total_cl  += r.get("clauses", 0)
            total_pt  += r.get("points", 0)
            total_app += r.get("appendices", 0)

        print(f"   {'─'*29} {'─'*11} {'─'*4} {'─'*4} {'─'*4} {'─'*4}")
        print(f"   {'TOTAL':<30s} {'':12s} {total_art:>4d} {total_cl:>4d} "
              f"{total_pt:>4d} {total_app:>4d}")

    # ── PARTIAL / SKIP / FAIL ──────────────────────────────
    for label, icon, items in [
        ("PARTIAL", "⚠️ ", partial),
        ("SKIPPED", "⏭️ ", skipped),
        ("FAILED",  "❌", failed),
    ]:
        if items:
            print(f"\n{icon} {label} ({len(items)}):")
            for k in items:
                reason = results[k].get("reason", "Unknown")[:60]
                print(f"   {k:<30s} | {reason}")

    # ── Parsed files on disk ───────────────────────────────
    parsed_files = sorted(config.PARSED_DIR.glob("*.json"))
    if parsed_files:
        print(f"\n📁 JSON files in {config.PARSED_DIR}:")
        for f in parsed_files:
            size = f.stat().st_size / 1024
            print(f"   {f.name:<42s} {size:>7.1f} KB")

    # ── Next steps ─────────────────────────────────────────
    print(f"\n{'=' * 70}")
    if len(success) == total:
        print("🎉 ALL DOCUMENTS PARSED SUCCESSFULLY!")
        print("\nNext steps:")
        print("   1. Validate: python validate_parsed_docs.py")
        print("   2. Chunk:    python chunk_documents.py")
    elif success:
        print(f"✅ {len(success)}/{total} documents ready")
        if failed:
            print(f"\n⚠️  {len(failed)} failed — common causes:")
            print("   • Fully scanned PDF without OCR language pack (vie)")
            print("   • Unusual document structure")
            print("   • Install: apt-get install tesseract-ocr-vie")
    else:
        print("❌ No documents parsed. Check PDFs in data/raw/")
    print("=" * 70 + "\n")


# ═══════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    results = parse_all_documents()

    # Save summary
    summary_path = config.LOG_DIR / "parsing_summary_v7.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=json_serializer)
    print(f"📄 Summary: {summary_path}")