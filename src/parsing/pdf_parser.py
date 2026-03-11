"""
TaxAI 2026 - PDF Parser
Thin I/O wrapper trên ParsePipeline.

Trách nhiệm duy nhất của PDFParser:
  - Nhận pdf_path, validate tồn tại
  - Delegate toàn bộ parsing logic cho ParsePipeline
  - Save JSON output nếu được yêu cầu

Mọi parsing logic nằm trong src/parsing/pipeline.py.

Author: TaxAI Team
Version: 6.0 - Pipeline architecture
"""

import json
from pathlib import Path
from datetime import date, datetime
from typing import Optional

from .pipeline import ParsePipeline

# Logger
try:
    from ..utils.logger import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


# =========================================================
# BACKWARD COMPAT — public API giữ nguyên
# =========================================================

# extract_metadata và merge_split_tables được re-export từ pipeline
# để các script ngoài import từ pdf_parser vẫn hoạt động
from .pipeline import extract_metadata, _merge_split_tables as merge_split_tables


def json_serializer(obj):
    """Handle date/datetime objects khi json.dump"""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


# =========================================================
# MAIN PARSER CLASS — thin I/O wrapper
# =========================================================

class PDFParser:
    """
    PDF → JSON: thin wrapper xử lý I/O, delegate parsing cho ParsePipeline.

    Usage:
        parser = PDFParser()
        result = parser.parse("data/raw/109_2025_QH15.pdf")
    """

    def __init__(self):
        self._pipeline = ParsePipeline()

    def parse(
        self,
        pdf_path: str | Path,
        output_path: Optional[str | Path] = None,
        save_json: bool = True,
    ) -> dict:
        """
        Parse PDF → structured JSON dict.

        Args:
            pdf_path:    Đường dẫn tới file PDF
            output_path: Nơi lưu JSON (optional, default: data/parsed/{stem}.json)
            save_json:   Có lưu file không

        Returns:
            dict với keys: metadata, data, tables, pdf_metadata
        """
        pdf_path = Path(pdf_path)

        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        logger.info(f"📄 Parsing: {pdf_path.name}")

        # ── Delegate toàn bộ parsing logic cho pipeline ───────────────────
        result = self._pipeline.run(pdf_path)

        if not result.get("data"):
            logger.error(f"❌ No nodes parsed from {pdf_path.name}")

        # ── Save JSON ──────────────────────────────────────────────────────
        if save_json:
            if output_path is None:
                output_path = pdf_path.parent.parent / "parsed" / f"{pdf_path.stem}.json"

            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=json_serializer)

            logger.info(f"💾 Saved: {output_path}")

        return result


# =========================================================
# BACKWARD COMPAT — parse_all_documents.py dùng ParserV7
# =========================================================

ParserV7 = PDFParser


# =========================================================
# QUICK TEST
# =========================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m src.parsing.pdf_parser <path_to_pdf>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    parser = PDFParser()
    result = parser.parse(pdf_path)

    print(f"\n✅ Done!")
    print(f"   document_type: {result['metadata']['document_type']}")
    print(f"   document_number: {result['metadata']['document_number']}")
    print(f"   root nodes: {len(result['data'])}")
