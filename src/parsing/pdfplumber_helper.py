"""
TaxAI 2026 - Smart PDF Helper
Handles BOTH digital PDFs and scanned PDFs intelligently

Auto-detection:
- Checks if PDF has text layer
- Digital PDF → pdfplumber direct extraction
- Scanned PDF → Tesseract OCR + pdfplumber structure

Features:
- Unified API for both types
- Smart quality assessment
- Table extraction (best effort)
- Minimal text cleaning

Author: TaxAI Team
Version: 4.0 - Smart Hybrid
Date: 2026-02-27
"""

import pdfplumber
from pathlib import Path
from typing import Tuple, List, Dict, Any, Optional
import re
from src.parsing.text_normalizer import fix_merged_words

# OCR support (optional - for pure scanned PDFs)
try:
    import pytesseract
    from PIL import Image
    import io
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

# Logger
try:
    from ..utils.logger import logger
except:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)


# =========================================================
# TEXT CLEANING - MINIMAL (for good quality PDFs)
# =========================================================

def clean_legal_text(text: str, fix_merged: bool = True) -> str:
    """
    Minimal cleaning for Vietnamese legal PDFs

    Works for both:
    - Digital PDFs with perfect text
    - OCR PDFs with good quality

    fix_merged=False: bỏ qua fix_merged_words — dùng cho DOCX/DOC source
    (text đã đúng, không có OCR space-drop artifact).
    """
    import unicodedata

    # Unicode NFC normalization — gộp ký tự decomposed từ embedded fonts
    # VD: 'e' + combining circumflex → 'ê' (một code point)
    text = unicodedata.normalize('NFC', text)

    # Fix merged words (OCR space-drop): "cánhân" → "cá nhân"
    # Root cause: some embedded fonts have zero advance-width for space glyph
    # → pdfplumber can't infer inter-word boundaries → syllables merge.
    # CHỈ chạy cho PDF — DOCX text đã đúng, fix_merged_words gây false positive.
    if fix_merged:
        text = fix_merged_words(text)

    # Normalize hyphenated breaks trong legal references
    # VD: "NĐ-\nCP" → "NĐ-CP", "TT- BKHĐT" → "TT-BKHĐT" (cả line-break và space)
    # \s+ để xử lý cả \n, space thừa do PDF layout (pdfplumber đôi khi insert space)
    text = re.sub(r'([A-ZĐ])-\s+([A-ZĐ])', r'\1-\2', text)

    # Remove page numbers
    text = re.sub(r'\n\s*[-\']*\s*\d{1,3}\s*\n', '\n', text)
    text = re.sub(r'^\s*\d{1,3}\s*$', '', text, flags=re.MULTILINE)
    
    # Remove page separators
    text = re.sub(r'\n\s*[-_]{3,}\s*\n', '\n', text)
    
    # Remove signature blocks
    text = re.sub(
        r'(?:CHỦ\s+TỊCH|Chủ\s+tịch)\s+(?:QUỐC\s+HỘI|Quốc\s+hội).*',
        '', text, flags=re.IGNORECASE | re.DOTALL
    )
    
    # Normalize spacing
    text = re.sub(r'[ ]{3,}', '  ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'\s+([,;:.])', r'\1', text)
    
    return text.strip()


# =========================================================
# TABLE EXTRACTION - MULTI-STRATEGY
# =========================================================

def extract_tables_smart(page: Any) -> List[Dict[str, Any]]:
    """
    Smart table extraction - tries multiple strategies
    
    Strategies:
    1. Lines-based (for PDFs with table borders)
    2. Text-based (for borderless tables)
    3. Words-based (fallback for scan quality)
    """
    
    strategies = [
        # Strategy 1: Lines (best for digital PDFs with borders)
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "snap_tolerance": 3,
        },
        # Strategy 2: Text (for borderless tables)
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "x_tolerance": 3,
            "y_tolerance": 3,
        },
        # Strategy 3: Explicit (manual detection)
        {
            "vertical_strategy": "explicit",
            "horizontal_strategy": "explicit",
        },
    ]
    
    tables_data = []
    
    for settings in strategies:
        try:
            tables = page.extract_tables(table_settings=settings)
            if tables and len(tables) > 0:
                # Found tables with this strategy!
                for idx, table in enumerate(tables):
                    if not table or len(table) < 2:
                        continue
                    
                    headers = [str(h).replace('\n', ' ').strip() if h else "" for h in table[0]]
                    rows = [[str(c).replace('\n', ' ').strip() if c else "" for c in row] for row in table[1:]]
                    
                    # Filter empty rows
                    rows = [r for r in rows if any(c for c in r)]
                    
                    if rows:
                        tables_data.append({
                            "table_index": idx,
                            "headers": headers,
                            "rows": rows,
                            "row_count": len(rows),
                            "col_count": len(headers),
                            "extraction_strategy": settings.get("vertical_strategy", "unknown")
                        })
                
                if tables_data:
                    # Success with this strategy, no need to try others
                    break
        except:
            continue
    
    return tables_data


# =========================================================
# PDF TYPE DETECTION
# =========================================================

def detect_pdf_type(pdf_path: Path, threshold: int = 100) -> str:
    """
    Detect if PDF is digital or scanned
    
    Returns:
        'digital' - Has good text layer
        'scanned_ocr' - Scanned but has OCR layer
        'scanned_pure' - Scanned, no text layer (needs OCR)
    """
    
    with pdfplumber.open(pdf_path) as pdf:
        # Check first 3 pages
        total_text_len = 0
        pages_to_check = min(3, len(pdf.pages))
        
        for i in range(pages_to_check):
            text = pdf.pages[i].extract_text()
            if text:
                total_text_len += len(text.strip())
        
        avg_text_per_page = total_text_len / pages_to_check
        
        if avg_text_per_page > 500:
            # Has substantial text - digital or scanned with OCR
            return 'digital'
        elif avg_text_per_page > 50:
            # Some text but sparse - likely scanned with poor OCR
            return 'scanned_ocr'
        else:
            # Almost no text - pure scan, needs OCR
            return 'scanned_pure'


# =========================================================
# MAIN HELPER CLASS
# =========================================================

class SmartPDFHelper:
    """
    Smart PDF Helper - Handles both digital and scanned PDFs
    
    Auto-detects PDF type and uses appropriate extraction method:
    - Digital PDFs: pdfplumber (fast, accurate)
    - Scanned PDFs with OCR layer: pdfplumber + cleaning
    - Pure scanned PDFs: Tesseract OCR (if available)
    """
    
    def __init__(self):
        """Initialize Smart PDF Helper"""
        self.has_tesseract = TESSERACT_AVAILABLE
        logger.info("✅ Smart PDF Helper initialized")
        if not self.has_tesseract:
            logger.warning("⚠️  Tesseract not available - pure scans will fail")
    
    def extract_text_and_tables(
        self,
        pdf_path: Path,
        extract_tables: bool = True,
        clean_output: bool = True,
        force_ocr: bool = False
    ) -> Tuple[str, List[Dict[str, Any]], int, Dict[str, Any]]:
        """
        Smart extraction - handles both PDF types
        
        Args:
            pdf_path: Path to PDF
            extract_tables: Try to extract tables
            clean_output: Apply minimal cleaning
            force_ocr: Force OCR even if text layer exists
        
        Returns:
            (text, tables, total_pages, metadata)
        """
        
        logger.info(f"🔍 Analyzing: {pdf_path.name}")
        
        # Detect PDF type
        if not force_ocr:
            pdf_type = detect_pdf_type(pdf_path)
        else:
            pdf_type = 'scanned_pure'
        
        logger.info(f"📄 PDF Type: {pdf_type}")
        
        # Extract based on type
        if pdf_type == 'scanned_pure' and not self.has_tesseract:
            raise RuntimeError(
                "Pure scanned PDF detected but Tesseract not available. "
                "Install: pip install pytesseract"
            )
        
        # Use pdfplumber for all types (it handles OCR-embedded PDFs well)
        text, tables, total_pages = self._extract_with_pdfplumber(
            pdf_path, extract_tables
        )
        
        # Clean if requested
        if clean_output:
            logger.info("🧹 Cleaning text...")
            text = clean_legal_text(text)
        
        # Metadata
        metadata = {
            'pdf_type': pdf_type,
            'total_pages': total_pages,
            'text_length': len(text),
            'tables_found': len(tables),
            'extraction_method': 'pdfplumber'
        }
        
        logger.info(
            f"✅ Extracted {len(text)} chars, "
            f"{len(tables)} tables from {total_pages} pages"
        )
        
        return text, tables, total_pages, metadata
    
    def _extract_with_pdfplumber(
        self,
        pdf_path: Path,
        extract_tables: bool
    ) -> Tuple[str, List[Dict[str, Any]], int]:
        """Extract using pdfplumber, tự động OCR trang scan"""

        text_parts = []
        all_tables = []

        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)

            for page_num, page in enumerate(pdf.pages, 1):
                try:
                    # Extract tables trước để lấy bbox loại trừ
                    page_tables = []
                    if extract_tables:
                        page_tables = extract_tables_smart(page)
                        for table in page_tables:
                            table['page_number'] = page_num
                        all_tables.extend(page_tables)

                    # Extract text với table bbox bị loại trừ
                    # → tránh table cell text lẫn vào nội dung văn bản
                    page_text = self._extract_text_no_tables(page)

                    # Nếu trang không có text → OCR (trang scan)
                    if not page_text or len(page_text.strip()) < 50:
                        logger.info(f"  📸 Trang {page_num}: không có text → OCR...")
                        page_text = self._ocr_page(page, page_num)

                    if page_text:
                        text_parts.append(page_text)

                    # Progress
                    if page_num % 5 == 0 or page_num == total_pages:
                        logger.info(f"  Progress: {page_num}/{total_pages}")

                except Exception as e:
                    logger.error(f"❌ Error page {page_num}: {e}")

        full_text = "\n".join(text_parts)
        # Gán lại table_index tuần tự toàn document (per-page idx luôn bắt đầu từ 0)
        for global_idx, table in enumerate(all_tables):
            table['table_index'] = global_idx
        return full_text, all_tables, total_pages

    def _extract_text_no_tables(self, page) -> str:
        """
        Extract text từ page, loại trừ vùng bảng biểu (table bounding boxes).

        Mục đích: ngăn text trong cell bảng lẫn vào luồng văn bản chính,
        tránh parser nhầm table rows thành Khoản/Điểm.

        Cách hoạt động:
            1. Tìm tất cả table bbox trên trang
            2. Dùng page.filter() để loại character objects nằm trong bbox
            3. Extract text từ filtered page

        Fallback về page.extract_text() nếu find_tables() thất bại.
        """
        try:
            found_tables = page.find_tables()
            if not found_tables:
                # Không có bảng → trả về text bình thường
                return page.extract_text() or ""

            # Lấy bounding boxes của tất cả bảng
            table_bboxes = [t.bbox for t in found_tables]  # (x0, top, x1, bottom)

            def not_in_any_table(obj):
                # Chỉ lọc ký tự (char), giữ lại mọi obj khác
                if obj.get("object_type") != "char":
                    return True
                x0_o = obj.get("x0", 0)
                x1_o = obj.get("x1", 0)
                top_o = obj.get("top", 0)
                bot_o = obj.get("bottom", 0)
                for (bx0, btop, bx1, bbot) in table_bboxes:
                    # Một chút margin (2pt) để bắt ký tự sát border
                    if (x0_o >= bx0 - 2 and x1_o <= bx1 + 2 and
                            top_o >= btop - 2 and bot_o <= bbot + 2):
                        return False  # trong bảng → loại bỏ
                return True  # ngoài bảng → giữ lại

            filtered_page = page.filter(not_in_any_table)
            return filtered_page.extract_text() or ""

        except Exception as e:
            logger.debug(f"  _extract_text_no_tables fallback: {e}")
            return page.extract_text() or ""

    def _ocr_page(self, page, page_num: int) -> str:
        """OCR một trang scan bằng Tesseract"""

        if not self.has_tesseract:
            logger.warning(f"  ⚠️ Tesseract không có - bỏ qua trang {page_num}")
            return ""

        try:
            import pytesseract
            from PIL import Image

            # Thiết lập đường dẫn Tesseract trên Windows
            pytesseract.pytesseract.tesseract_cmd = \
                r'C:\Program Files\Tesseract-OCR\tesseract.exe'

            # Convert trang PDF → ảnh (300 DPI cho chất lượng tốt)
            img = page.to_image(resolution=300).original

            # OCR với tiếng Việt
            config = '--oem 3 --psm 6'
            text = pytesseract.image_to_string(img, lang='vie', config=config)

            # Post-process: sửa lỗi OCR phổ biến tiếng Việt
            text = self._fix_ocr_errors(text)

            char_count = len(text.strip())
            logger.info(f"  ✅ OCR trang {page_num}: {char_count} ký tự")

            # Trong _fix_ocr_errors(), ngay trước return text
            import re
            found = re.findall(r'Chương\s+\S+', text)
            logger.info(f"🔍 DEBUG Chương patterns: {found}")

            return text

        except Exception as e:
            logger.error(f"  ❌ OCR lỗi trang {page_num}: {e}")
            return ""


    def _fix_ocr_errors(self, text: str) -> str:
        """Sửa lỗi OCR phổ biến cho văn bản pháp luật tiếng Việt"""
        import re

        # Tách "Phụ lục" header ra khỏi OCR noise của vùng chữ ký/con dấu.
        # VD: "/§y 2. M 2À Phụ lục" → "\nPhụ lục"
        # (Xảy ra khi chữ ký và tiêu đề phụ lục nằm cùng vùng trên trang scan)
        text = re.sub(
            r'^[/\\§$#@!*{}\[\]|~^<>]{1,}[^\n]{0,60}?(Phụ\s+lục)',
            r'\n\1',
            text,
            flags=re.MULTILINE
        )

        fixes = [
            # Số bị nhận nhầm
            (r'(?<!\d)0(?=\s*,\s*\d+%)', '0'),   # giữ nguyên 0,x%
            (r'\bl\b(?=\s*\d)', '1'),              # l → 1 trước số
            (r'(?<=\d)O(?=\d)', '0'),              # O → 0 giữa số

            # Từ khóa pháp lý hay bị sai
            (r'Ðiều', 'Điều'),
            (r'Khoán', 'Khoản'),
            (r'Diéu', 'Điều'),
            (r'thuê\s+suât', 'thuế suất'),
            (r'thuê\s+suất', 'thuế suất'),
            (r'Chinh\s+phủ', 'Chính phủ'),
            (r'câ\s+nhân', 'cá nhân'),
            (r'thu\s+nhâp', 'thu nhập'),

            # Dấu câu
            (r'\s+([,;:.])', r'\1'),
            (r'\n{3,}', '\n\n'),
            
            # Sửa lỗi số La Mã bị OCR nhầm
            (r'Chương\s+IH\b', 'Chương III'),
            (r'Chương\s+HI\b', 'Chương III'),   # OCR nhầm III → HI
            (r'Chương\s+TV\b', 'Chương IV'),
            (r'Chương\s+VI1\b', 'Chương VII'),
            (r'Chương\s+VI11\b', 'Chương VIII'),
            (r'Chương\s+1X\b', 'Chương IX'),
            (r'Chương\s+1\b', 'Chương I'),    # "Chương 1" → "Chương I"
            (r'Mục\s+IH\b', 'Mục III'),
            (r'Mục\s+HI\b', 'Mục III'),         # OCR nhầm III → HI
            (r'Mục\s+TV\b', 'Mục IV'),
        ]

        for pattern, replacement in fixes:
            text = re.sub(pattern, replacement, text)

        return text


# =========================================================
# GLOBAL INSTANCE
# =========================================================

smart_pdf_helper = SmartPDFHelper()


# =========================================================
# EXPORTS
# =========================================================

__all__ = [
    "SmartPDFHelper",
    "smart_pdf_helper",
    "clean_legal_text",
    "extract_tables_smart",
    "detect_pdf_type",
]