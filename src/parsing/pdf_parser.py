"""
TaxAI 2026 - PDF Parser (pdfplumber version)
Orchestrator: SmartPDFHelper + StateMachineParser

Author: Le Quang Dai
Version: 5.0 - pdfplumber migration
"""

import json
import re
from pathlib import Path
from datetime import date, datetime
from typing import Optional

from .pdfplumber_helper import SmartPDFHelper
from .state_machine import StateMachineParser

# Logger
try:
    from ..utils.logger import logger
except:
    import logging
    logger = logging.getLogger(__name__)


# =========================================================
# JSON SERIALIZER (cho date objects)
# =========================================================

def json_serializer(obj):
    """Handle date/datetime objects khi json.dump"""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


# =========================================================
# METADATA EXTRACTOR
# =========================================================

def extract_metadata(text: str, filename: str) -> dict:
    """
    Extract document metadata từ text và filename

    Args:
        text: Full document text
        filename: PDF filename (VD: "109_2025_QH15.pdf")

    Returns:
        {
            document_id, document_type, document_number,
            title, issue_date, effective_date
        }
    """

    # --- document_id từ filename ---
    stem = Path(filename).stem  # "109_2025_QH15"
    document_id = stem

    # --- document_type từ text ---
    # Chỉ tìm trong phần header (trước "Căn cứ") để tránh nhầm với references
    can_cu_pos = re.search(r'\bCăn\s+cứ\b', text)
    header_end = can_cu_pos.start() if can_cu_pos else 600
    header_text = text[:min(header_end, 600)].upper()

    type_patterns = [
        (r'THÔNG\s+TƯ', "Thông tư"),
        (r'QUYẾT\s+ĐỊNH', "Quyết định"),
        (r'NGHỊ\s+ĐỊNH', "Nghị định"),
        (r'NGHỊ\s+QUYẾT', "Nghị quyết"),
    ]

    doc_type = "Luật"  # default

    for pattern, dtype in type_patterns:
        if re.search(pattern, header_text):
            # Nếu match "Nghị quyết" nhưng số hiệu có /QH → vẫn là Luật
            if dtype == "Nghị quyết" and re.search(r'/QH\d+', text[:3000]):
                continue
            doc_type = dtype
            break

    # Fallback: nếu header quá ngắn (< 50 chars), search rộng hơn nhưng
    # chỉ tìm keyword đứng độc lập trên dòng (tránh nhầm "Căn cứ Nghị định")
    if doc_type == "Luật" and len(header_text.strip()) < 50:
        for pattern, dtype in type_patterns:
            if re.search(r'^\s*' + pattern + r'\s*$', text[:2000],
                         re.IGNORECASE | re.MULTILINE):
                if dtype == "Nghị quyết" and re.search(r'/QH\d+', text[:3000]):
                    continue
                doc_type = dtype
                break

    # --- document_number từ text ---
    doc_number = stem.replace("_", "/")  # fallback

    # Ưu tiên 1: tìm "Số: XXX/YYYY/TYPE" trong header (chính xác nhất)
    so_match = re.search(
        r'Số\s*:\s*(\d+/\d+/[A-Z][A-Z0-9\-]+)',
        text[:800]
    )
    if so_match:
        doc_number = so_match.group(1)
    else:
        # Fallback: tìm theo pattern nhưng ưu tiên match gần đầu văn bản nhất
        number_patterns = [
            r'(\d+/\d+/TT-[A-Z]+)',    # Thông tư: 152/2025/TT-BTC
            r'(\d+/\d+/NĐ-CP)',        # Nghị định: 117/2025/NĐ-CP
            r'(\d+/\d+/UBTVQH\d+)',    # Ủy ban thường vụ
            r'(\d+/\d+/QH\d+)',        # Luật: 109/2025/QH15
        ]
        for pattern in number_patterns:
            match = re.search(pattern, text[:1500])
            if match:
                doc_number = match.group(1)
                break

    # --- title ---
    title = f"{doc_type} {doc_number}"  # fallback

    # Ưu tiên 1: dòng ngay sau keyword loại văn bản (chứa tiêu đề đề mục)
    # VD: "THÔNG TƯ\nHướng dẫn chế độ kế toán..." → "Hướng dẫn chế độ kế toán..."
    # VD: "NGHỊ ĐỊNH\nQuy định về quản lý..." → "Quy định về quản lý..."
    doc_type_kw = {
        "Thông tư": r"THÔNG\s+TƯ",
        "Nghị định": r"NGHỊ\s+ĐỊNH",
        "Quyết định": r"QUYẾT\s+ĐỊNH",
        "Nghị quyết": r"NGHỊ\s+QUYẾT",
        "Luật": r"LUẬT",
    }
    kw_pattern = doc_type_kw.get(doc_type, r"LUẬT")
    title_after_kw = re.search(
        kw_pattern + r'\s*\n\s*(.+(?:\n.+)?)',
        text[:3000], re.IGNORECASE
    )
    if title_after_kw:
        candidate = re.sub(r'\s*\n\s*', ' ', title_after_kw.group(1)).strip()
        # Cắt bỏ phần "Căn cứ..." trở đi (không thuộc tiêu đề)
        can_cu_idx = candidate.find('Căn cứ')
        if can_cu_idx > 0:
            candidate = candidate[:can_cu_idx].strip()
        # Loại bỏ dòng chỉ là số/ký tự đặc biệt (không phải tiêu đề thật)
        if len(candidate) >= 5 and not re.match(r'^[\d/\-\s]+$', candidate):
            title = candidate

    # Ưu tiên 2 (fallback): tìm "Về..." hoặc "Hướng dẫn..." trong header
    if title == f"{doc_type} {doc_number}":
        title_match = re.search(
            r'(?:Về|Hướng\s+dẫn|Quy\s+định)\s+(.{10,200}?)(?:\n|$)',
            text[:3000]
        )
        if title_match:
            title = title_match.group(0).strip()

    # --- dates ---
    issue_date = date.today()
    effective_date = date.today()

    # Ngày ban hành — trong 3000 ký tự đầu (phần header)
    date_match = re.search(
        r'ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})',
        text[:3000], re.IGNORECASE
    )
    if date_match:
        try:
            issue_date = date(
                int(date_match.group(3)),
                int(date_match.group(2)),
                int(date_match.group(1))
            )
        except ValueError:
            pass

    # Ngày hiệu lực — tìm trong toàn bộ text
    # VD: "có hiệu lực thi hành kể từ ngày 01 tháng 7 năm 2025"
    # VD: "có hiệu lực thi hành kể từ ngày 01/01/2026"  ← format dd/mm/yyyy
    eff_patterns = [
        # Format dài: "ngày 01 tháng 01 năm 2026"
        (r'hiệu\s+lực.{0,80}ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})',
         lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
        # Format ngắn: "ngày 01/01/2026" hoặc "ngày 01/7/2025"
        (r'hiệu\s+lực.{0,80}ngày\s+(\d{1,2})/(\d{1,2})/(\d{4})',
         lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
        # Format chỉ số: "từ ngày 01/01/2026" (không có "hiệu lực" ngay trước)
        (r'kể\s+từ\s+ngày\s+(\d{1,2})/(\d{1,2})/(\d{4})',
         lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
    ]
    for eff_pattern, eff_builder in eff_patterns:
        eff_match = re.search(eff_pattern, text, re.IGNORECASE)
        if eff_match:
            try:
                effective_date = eff_builder(eff_match)
                break
            except ValueError:
                continue

    return {
        "document_id": document_id,
        "document_type": doc_type,
        "document_number": doc_number,
        "title": title,
        "issue_date": issue_date,
        "effective_date": effective_date,
    }


# =========================================================
# MAIN PARSER CLASS
# =========================================================

class PDFParser:
    """
    Full pipeline: PDF → JSON

    Usage:
        parser = PDFParser()
        result = parser.parse("data/raw/109_2025_QH15.pdf")
        # result là dict, save thành JSON
    """

    def __init__(self):
        self.pdf_helper = SmartPDFHelper()

    def parse(
        self,
        pdf_path: str | Path,
        output_path: Optional[str | Path] = None,
        save_json: bool = True
    ) -> dict:
        """
        Parse PDF → structured JSON

        Args:
            pdf_path: Đường dẫn tới file PDF
            output_path: Nơi lưu JSON (optional)
            save_json: Có lưu file không

        Returns:
            dict theo chuẩn schema:
            {
                "metadata": {...},
                "data": [nodes...]
            }
        """
        pdf_path = Path(pdf_path)

        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        logger.info(f"📄 Parsing: {pdf_path.name}")

        # ── STEP 1: Extract text ──────────────────────────
        text, tables, total_pages, pdf_meta = \
            self.pdf_helper.extract_text_and_tables(
                pdf_path,
                extract_tables=True,
                clean_output=True
            )

        if not text.strip():
            logger.error(f"❌ No text extracted from {pdf_path.name}")
            return {"error": "No text extracted", "metadata": {}, "data": []}

        logger.info(f"✅ Extracted {len(text)} chars from {total_pages} pages")

        # ── STEP 2: Extract metadata ──────────────────────
        meta = extract_metadata(text, pdf_path.name)
        logger.info(
            f"📋 Detected: {meta['document_type']} "
            f"{meta['document_number']}"
        )

        # ── STEP 3: Parse structure ───────────────────────
        parser = StateMachineParser(
            document_id=meta["document_id"],
            document_number=meta["document_number"],
            document_type=meta["document_type"]
        )

        document = parser.parse_text(text)

        # Override metadata với extracted info
        document.document_type = meta["document_type"]
        document.document_number = meta["document_number"]
        document.title = meta["title"]
        document.issue_date = meta["issue_date"]
        document.effective_date = meta["effective_date"]

        # ── STEP 4: Convert to dict ───────────────────────
        result = document.to_dict()

        # Thêm pdf_metadata vào result
        result["pdf_metadata"] = pdf_meta
        result["tables"] = tables

        node_count = len(result.get("data", []))
        logger.info(f"✅ Parsed {node_count} root nodes")

        # ── STEP 5: Save JSON ─────────────────────────────
        if save_json:
            if output_path is None:
                # Default: data/parsed/{stem}.json
                output_path = pdf_path.parent.parent / "parsed" / \
                              f"{pdf_path.stem}.json"

            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False,
                         indent=2, default=json_serializer)

            logger.info(f"💾 Saved: {output_path}")

        return result


# =========================================================
# BACKWARD COMPAT — parse_all_documents.py dùng ParserV7
# =========================================================

# Alias để không phải sửa parse_all_documents.py
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