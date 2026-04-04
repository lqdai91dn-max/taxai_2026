"""
ParsePipeline — Bước 1: Explicit 5-stage parsing pipeline.

Nguyên tắc thiết kế:
    Mỗi bug được fix ở đúng 1 stage. Khi sửa stage X không thể
    vô tình break stage Y vì các stage độc lập nhau.

    Bug OCR / PDF layout     → Stage 1 (Extractor)
    Bug whitespace / encoding → Stage 2 (Normalizer)
    Bug detect Điều/Khoản    → Stage 3 (StructureParser)
    Bug chỉ 1 document       → Stage 4 (PatchManager)
    Invariant violation      → Stage 5 (Validator) — warn, không raise

Thứ tự bắt buộc:
    Extract → Normalize → Parse → Patch → Validate
    (Text PHẢI sạch trước khi detect structure)

Cảnh báo Normalizer:
    CHỈ fix whitespace/encoding/OCR artifacts.
    KHÔNG merge lines tự do — tránh "Khoản 1. a)" khi merge Khoản với Điểm.
"""

from pathlib import Path
from datetime import date, datetime
from typing import Optional

try:
    from .pdfplumber_helper import SmartPDFHelper, clean_legal_text
    from .docx_helper import extract_text_and_tables_from_docx, extract_text_and_tables_from_doc
    from .gemini_helper import GeminiPDFExtractor
    from .state_machine import StateMachineParser
    from .patch_applier import PatchApplier
    from ..utils.logger import logger
except ImportError:
    from src.parsing.pdfplumber_helper import SmartPDFHelper, clean_legal_text
    from src.parsing.docx_helper import extract_text_and_tables_from_docx, extract_text_and_tables_from_doc
    from src.parsing.gemini_helper import GeminiPDFExtractor
    from src.parsing.state_machine import StateMachineParser
    from src.parsing.patch_applier import PatchApplier
    import logging
    logger = logging.getLogger(__name__)

logger = logger.bind(module="pipeline") if hasattr(logger, "bind") else logger


# ─────────────────────────────────────────────────────────────────────────────
# Table normalization (thuộc Stage 2)
# ─────────────────────────────────────────────────────────────────────────────

def _merge_split_tables(tables: list) -> list:
    """
    Merge bảng bị split qua page break: cùng col_count + trang liên tiếp.

    Khi pdfplumber gặp một bảng chạy qua nhiều trang, nó tạo ra N bảng riêng.
    Bảng tiếp theo có "headers" thực ra là data row đầu tiên tràn sang trang mới.
    """
    if not tables:
        return tables

    merged = [dict(tables[0])]
    merged[-1]["rows"] = list(tables[0].get("rows", []))

    for table in tables[1:]:
        last = merged[-1]
        same_cols = table["col_count"] == last["col_count"]
        consecutive = table.get("page_number", 0) == last.get("page_number", 0) + 1

        if same_cols and consecutive:
            cont_hdr = table.get("headers", [])
            if any(h for h in cont_hdr):
                last["rows"].append(cont_hdr)
            last["rows"].extend(table.get("rows", []))
            last["row_count"] = len(last["rows"])
            last["page_number"] = table.get("page_number", last.get("page_number"))
        else:
            entry = dict(table)
            entry["rows"] = list(table.get("rows", []))
            merged.append(entry)

    for i, t in enumerate(merged):
        t["table_index"] = i

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Metadata extraction (Stage 2.5 — cần clean text)
# ─────────────────────────────────────────────────────────────────────────────

import re


def _stem_to_doc_number(stem: str) -> str:
    """Chuyển filename stem → số hiệu văn bản chính thức.

    Ví dụ:
        20_2026_NDCP     → 20/2026/NĐ-CP
        152_2025_TTBTC   → 152/2025/TT-BTC
        198_2025_QH15    → 198/2025/QH15
        110_2025_UBTVQH15 → 110/2025/UBTVQH15
    """
    suffix_map = {
        "NDCP": "NĐ-CP",
        "TTBTC": "TT-BTC",
        "QH15": "QH15",
        "QH14": "QH14",
        "QH13": "QH13",
        "UBTVQH15": "UBTVQH15",
        "UBTVQH14": "UBTVQH14",
    }
    parts = stem.split("_")
    if len(parts) >= 3:
        number = parts[0]
        year = parts[1]
        suffix = "_".join(parts[2:])
        return f"{number}/{year}/{suffix_map.get(suffix, suffix)}"
    return stem.replace("_", "/")


def extract_metadata(text: str, filename: str) -> dict:
    """
    Extract document metadata từ clean text + filename.
    Đặt sau Stage 2 (Normalize) vì cần text đã sạch để regex chính xác.
    """
    stem = Path(filename).stem
    document_id = stem

    # ── document_type — từ dòng đầu tiên (trước Căn cứ) ────────────────────
    can_cu_pos = re.search(r"\bCăn\s+cứ\b", text)
    header_end = can_cu_pos.start() if can_cu_pos else 600
    # Chỉ scan header thực sự (trước Căn cứ), không scan references
    header_text = text[: min(header_end, 400)].upper()

    type_patterns = [
        (r"^\s*THÔNG\s+TƯ\b", "Thông tư"),
        (r"^\s*NGHỊ\s+ĐỊNH\b", "Nghị định"),
        (r"^\s*QUYẾT\s+ĐỊNH\b", "Quyết định"),
        (r"^\s*NGHỊ\s+QUYẾT\b", "Nghị quyết"),
        (r"^\s*LUẬT\b", "Luật"),
    ]

    doc_type = "Luật"
    for pattern, dtype in type_patterns:
        if re.search(pattern, header_text, re.MULTILINE):
            doc_type = dtype
            break

    # ── document_number — từ filename (reliable), chỉ override nếu có "Số:" ──
    # Không dùng regex scan body text vì sẽ bắt nhầm số văn bản được tham chiếu
    doc_number = _stem_to_doc_number(stem)
    so_match = re.search(r"Số\s*:\s*(\d+/\d+/[A-Z][A-Z0-9\-]+)", text[:300])
    if so_match:
        doc_number = so_match.group(1)

    # title
    title = f"{doc_type} {doc_number}"
    doc_type_kw = {
        "Thông tư": r"THÔNG\s+TƯ",
        "Nghị định": r"NGHỊ\s+ĐỊNH",
        "Quyết định": r"QUYẾT\s+ĐỊNH",
        "Nghị quyết": r"NGHỊ\s+QUYẾT",
        "Luật": r"LUẬT",
    }
    kw_pattern = doc_type_kw.get(doc_type, r"LUẬT")
    title_after_kw = re.search(
        kw_pattern + r"\s*\n\s*((?:.+\n?)+?)(?=\s*(?:_{3,}|Căn\s+cứ)|\Z)",
        text[:3000],
        re.IGNORECASE,
    )
    if title_after_kw:
        candidate = re.sub(r"\s*\n\s*", " ", title_after_kw.group(1)).strip()
        can_cu_idx = candidate.find("Căn cứ")
        if can_cu_idx > 0:
            candidate = candidate[:can_cu_idx].strip()
        if len(candidate) >= 5 and not re.match(r"^[\d/\-\s]+$", candidate):
            title = candidate

    if title == f"{doc_type} {doc_number}":
        title_match = re.search(
            r"(?:Về|Hướng\s+dẫn|Quy\s+định)\s+(.{10,200}?)(?:\n|$)", text[:3000]
        )
        if title_match:
            title = title_match.group(0).strip()

    # dates
    issue_date = date.today()
    effective_date = date.today()

    # issue_date — chỉ chấp nhận date có year khớp với filename year (±1)
    # Tránh bắt nhầm ngày của văn bản được tham chiếu trong title/Căn cứ
    _stem_parts = stem.split("_")
    _filename_year = int(_stem_parts[1]) if len(_stem_parts) >= 2 and _stem_parts[1].isdigit() else 0
    _date_pattern = r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})"
    _search_zones = [
        text[:header_end],   # header trước Căn cứ
        text[-800:],         # cuối văn bản (khu vực chữ ký)
        text,                # toàn bộ (last resort)
    ]
    for zone in _search_zones:
        for date_match in re.finditer(_date_pattern, zone, re.IGNORECASE):
            try:
                d = date(
                    int(date_match.group(3)),
                    int(date_match.group(2)),
                    int(date_match.group(1)),
                )
                # Chỉ chấp nhận nếu year khớp filename year ±1
                if _filename_year == 0 or abs(d.year - _filename_year) <= 1:
                    issue_date = d
                    break
            except ValueError:
                pass
        else:
            continue
        break

    eff_patterns = [
        (
            r"hiệu\s+lực.{0,80}ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})",
            lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1))),
        ),
        (
            r"hiệu\s+lực.{0,80}ngày\s+(\d{1,2})/(\d{1,2})/(\d{4})",
            lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1))),
        ),
        (
            r"kể\s+từ\s+ngày\s+(\d{1,2})/(\d{1,2})/(\d{4})",
            lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1))),
        ),
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


# ─────────────────────────────────────────────────────────────────────────────
# Version
# ─────────────────────────────────────────────────────────────────────────────

PARSER_VERSION = "7.0"
"""
Tăng version khi có thay đổi logic parsing (không phải chỉ fix bug nhỏ):
  Major (X.0): thay đổi kiến trúc pipeline, output format
  Minor (7.X): thêm tính năng mới (node type, field mới)
"""


# ─────────────────────────────────────────────────────────────────────────────
# Guidance section splitter (Stage 3.5 fallback)
# ─────────────────────────────────────────────────────────────────────────────

_SECTION_PATTERN = re.compile(
    r"^(?:"
    r"[A-Z]\."                       # A. B. C.
    r"|[IVX]+\."                     # I. II. III. IV.
    r"|\d+\."                        # 1. 2. 3.
    r"|\d+\.\d+\.?"                  # 1.1. 1.2.
    r")\s+\S",
)


def _extract_guidance_sections(text: str, doc_id: str, metadata: dict) -> list:
    """
    Fallback cho guidance docs (công văn, sổ tay) không có cấu trúc Điều/Khoản.
    Chia text thành các "Phần" nodes theo section headings (A., I., 1., 1.1.)
    để embedder có thể index nội dung văn bản.

    Node schema tối giản: node_id, node_type, title, content, depth, children=[].
    """
    lines = text.splitlines()
    sections: list[dict] = []
    current_title = "Nội dung"
    current_lines: list[str] = []

    def _flush(title: str, body_lines: list[str], idx: int) -> None:
        content = " ".join(body_lines).strip()
        if len(content) < 30:
            return
        sections.append({
            "node_id":    f"doc_{doc_id}_phan_{idx}",
            "node_type":  "Phần",
            "title":      title,
            "content":    content,
            "depth":      1,
            "children":   [],
        })

    section_idx = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if _SECTION_PATTERN.match(line):
            _flush(current_title, current_lines, section_idx)
            section_idx += 1
            current_title = line
            current_lines = []
        else:
            current_lines.append(line)

    _flush(current_title, current_lines, section_idx)
    logger.info(f"📋 Guidance sectioner: {len(sections)} sections from {doc_id}")
    return sections


# ─────────────────────────────────────────────────────────────────────────────
# ParsePipeline
# ─────────────────────────────────────────────────────────────────────────────

class ParsePipeline:
    """
    Orchestrates 5 independent parsing stages.

    Dùng ParsePipeline.run(pdf_path) → dict kết quả parse.
    PDFParser là thin wrapper xử lý I/O (save JSON) bên trên pipeline này.
    """

    def __init__(self, use_gemini: bool = False, gemini_model: str = "gemini-2.5-pro"):
        self._extractor = SmartPDFHelper()
        self._patcher = PatchApplier()
        self._gemini: GeminiPDFExtractor | None = None
        if use_gemini:
            self._gemini = GeminiPDFExtractor(model=gemini_model)

    def run(self, pdf_path: Path) -> dict:
        """
        Full pipeline: PDF → structured JSON dict.

        Args:
            pdf_path: Path to PDF file

        Returns:
            dict with keys: metadata, data, tables, pdf_metadata, parser_version
        """
        doc_id = pdf_path.stem

        # ── Stage 1: Extract ────────────────────────────────────────────────
        raw_text, raw_tables, total_pages, pdf_meta = self._stage_extract(pdf_path)
        is_docx = pdf_path.suffix.lower() in (".docx", ".doc")

        # ── Stage 2: Normalize ──────────────────────────────────────────────
        clean_text, clean_tables = self._stage_normalize(raw_text, raw_tables, is_docx=is_docx)

        # ── Stage 2.5: Extract metadata (cần clean text) ────────────────────
        metadata = extract_metadata(clean_text, pdf_path.name)

        # ── Stage 3: Parse structure ────────────────────────────────────────
        result = self._stage_parse(clean_text, clean_tables, pdf_meta, metadata)

        # ── Stage 3.5: Fallback text sectioner for guidance docs ─────────────
        # Khi state machine không detect được Điều/Khoản (guidance/công văn),
        # split text theo section headings (A., B., I., II., 1., 2., 1.1.) thành
        # flat "Phần" nodes để embedder có thể index nội dung văn bản.
        if not result["data"] and clean_text.strip():
            result["data"] = _extract_guidance_sections(clean_text, doc_id, metadata)

        # ── Stage 4: Apply patches ───────────────────────────────────────────
        result = self._stage_patch(result, doc_id, pdf_path)

        # ── Stage 5: Validate ────────────────────────────────────────────────
        self._stage_validate(result, doc_id)

        result["parser_version"] = PARSER_VERSION

        logger.info(
            f"✅ Pipeline done: {doc_id} | "
            f"v{PARSER_VERSION} | "
            f"{len(result['data'])} roots | "
            f"{len(result.get('tables', []))} tables"
        )
        return result

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def _stage_extract(self, input_path: Path):
        """
        Input (PDF hoặc DOCX) → raw text + raw tables.

        - .docx → DocxHelper (python-docx, không OCR)
        - .pdf + gemini=True → GeminiPDFExtractor (Gemini 2.5 Pro, text) + pdfplumber (tables)
        - .pdf + gemini=False → SmartPDFHelper (pdfplumber + Tesseract fallback)

        clean_output=False: KHÔNG clean ở đây.
        Normalization là trách nhiệm của Stage 2.
        Fix bug ở đây nếu: text bị mất, OCR sai, bảng không detect được.
        """
        logger.info("📄 Stage 1: Extract")
        if input_path.suffix.lower() == ".docx":
            return extract_text_and_tables_from_docx(input_path)
        if input_path.suffix.lower() == ".doc":
            return extract_text_and_tables_from_doc(input_path)
        if self._gemini is not None:
            return self._gemini.extract_text_and_tables(
                input_path,
                extract_tables=True,
                clean_output=False,
            )
        return self._extractor.extract_text_and_tables(
            input_path,
            extract_tables=True,
            clean_output=False,
        )

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    def _stage_normalize(self, raw_text: str, raw_tables: list, is_docx: bool = False):
        """
        Normalize text + merge fragmented tables.

        Text transformations được phép:
          ✅ NFC unicode normalization
          ✅ Fix "NĐ-\\nCP" → "NĐ-CP" (hyphenated line break)
          ✅ Remove page numbers, decorative separators
          ✅ Normalize consecutive whitespace
          ✅ fix_merged_words (CHỈ cho PDF — DOCX text đã đúng, không cần split)

        KHÔNG được:
          ❌ Merge lines bừa (vỡ structure: "Khoản 1. a)" sai)
          ❌ Lowercase (vỡ detect "Điều", "Phụ lục")
          ❌ Remove punctuation (vỡ content)

        Fix bug ở đây nếu: encoding sai, dính chữ, khoảng trắng thừa.
        """
        logger.info("🧹 Stage 2: Normalize")
        clean_text = clean_legal_text(raw_text, fix_merged=not is_docx)
        clean_tables = _merge_split_tables(raw_tables)
        return clean_text, clean_tables

    # ── Stage 3 ───────────────────────────────────────────────────────────────

    def _stage_parse(
        self,
        text: str,
        tables: list,
        pdf_meta: dict,
        metadata: dict,
    ) -> dict:
        """
        Clean text → structured node tree.

        State machine parser: detect Chương/Điều/Khoản/Điểm/Tiết,
        resolve internal references, build breadcrumbs.

        Fix bug ở đây nếu: detect sai structure, hierarchy sai,
        nội dung bị gán nhầm node.
        """
        logger.info("🏗️  Stage 3: Parse structure")
        parser = StateMachineParser(
            document_id=metadata["document_id"],
            document_number=metadata["document_number"],
            document_type=metadata["document_type"],
        )
        document = parser.parse_text(text)

        # Override với metadata đã extract
        document.document_type = metadata["document_type"]
        document.document_number = metadata["document_number"]
        document.title = metadata["title"]
        document.issue_date = metadata["issue_date"]
        document.effective_date = metadata["effective_date"]

        result = document.to_dict()
        result["pdf_metadata"] = {**pdf_meta, "tables_found": len(tables)}
        result["tables"] = tables
        return result

    # ── Stage 4 ───────────────────────────────────────────────────────────────

    def _stage_patch(self, result: dict, doc_id: str, pdf_path: Path) -> dict:
        """
        Apply document-specific corrections từ data/patches/{doc_id}.patch.json.

        Fix bug ở đây nếu: bug chỉ xảy ra ở 1 document do đặc thù PDF.
        Tạo patch file thay vì sửa parser code để không ảnh hưởng doc khác.
        pdf_path được truyền vào để PatchApplier verify pdf_sha256.
        """
        logger.info("🩹 Stage 4: Apply patches")
        return self._patcher.apply(result, doc_id, pdf_path=pdf_path)

    # ── Stage 5 ───────────────────────────────────────────────────────────────

    def _stage_validate(self, result: dict, doc_id: str) -> None:
        """
        Kiểm tra invariants cơ bản. Warn thay vì raise để không block pipeline.

        Invariants hiện tại:
        - Không có Phụ lục node với "kèm theo" trong title (Bug A indicator)
        - Mọi node phải có node_id

        Thêm invariant mới ở đây khi phát hiện pattern bug lặp lại.
        """
        logger.info("✅ Stage 5: Validate")
        nodes = result.get("data", [])
        self._check_all_nodes(nodes, doc_id)

    def _check_all_nodes(self, nodes: list, doc_id: str) -> None:
        for n in nodes:
            # Invariant: Phụ lục không phải mid-sentence reference
            if n.get("node_type") == "Phụ lục":
                title = n.get("title") or ""
                if "kèm theo" in title.lower():
                    logger.warning(
                        f"[{doc_id}] ⚠️  INVARIANT: Phụ lục '{n['node_id']}' "
                        f"có 'kèm theo' trong title → possible Bug A"
                    )

            # Invariant: node_id phải tồn tại
            if not n.get("node_id"):
                logger.warning(
                    f"[{doc_id}] ⚠️  INVARIANT: node thiếu node_id "
                    f"(type={n.get('node_type')}, index={n.get('node_index')})"
                )

            self._check_all_nodes(n.get("children", []), doc_id)
