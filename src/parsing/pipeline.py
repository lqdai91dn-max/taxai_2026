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
    from .state_machine import StateMachineParser
    from .patch_applier import PatchApplier
    from ..utils.logger import logger
except ImportError:
    from src.parsing.pdfplumber_helper import SmartPDFHelper, clean_legal_text
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


def extract_metadata(text: str, filename: str) -> dict:
    """
    Extract document metadata từ clean text + filename.
    Đặt sau Stage 2 (Normalize) vì cần text đã sạch để regex chính xác.
    """
    stem = Path(filename).stem
    document_id = stem

    # document_type — tìm trong header (trước "Căn cứ")
    can_cu_pos = re.search(r"\bCăn\s+cứ\b", text)
    header_end = can_cu_pos.start() if can_cu_pos else 600
    header_text = text[: min(header_end, 600)].upper()

    type_patterns = [
        (r"THÔNG\s+TƯ", "Thông tư"),
        (r"QUYẾT\s+ĐỊNH", "Quyết định"),
        (r"NGHỊ\s+ĐỊNH", "Nghị định"),
        (r"NGHỊ\s+QUYẾT", "Nghị quyết"),
    ]

    doc_type = "Luật"
    for pattern, dtype in type_patterns:
        if re.search(pattern, header_text):
            if dtype == "Nghị quyết" and re.search(r"/QH\d+", text[:3000]):
                continue
            doc_type = dtype
            break

    if doc_type == "Luật" and len(header_text.strip()) < 50:
        for pattern, dtype in type_patterns:
            if re.search(r"^\s*" + pattern + r"\s*$", text[:2000], re.IGNORECASE | re.MULTILINE):
                if dtype == "Nghị quyết" and re.search(r"/QH\d+", text[:3000]):
                    continue
                doc_type = dtype
                break

    # document_number
    doc_number = stem.replace("_", "/")
    so_match = re.search(r"Số\s*:\s*(\d+/\d+/[A-Z][A-Z0-9\-]+)", text[:800])
    if so_match:
        doc_number = so_match.group(1)
    else:
        number_patterns = [
            r"(\d+/\d+/TT-[A-Z]+)",
            r"(\d+/\d+/NĐ-CP)",
            r"(\d+/\d+/UBTVQH\d+)",
            r"(\d+/\d+/QH\d+)",
        ]
        for pattern in number_patterns:
            match = re.search(pattern, text[:1500])
            if match:
                doc_number = match.group(1)
                break

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

    date_match = re.search(
        r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})",
        text[:3000],
        re.IGNORECASE,
    )
    if date_match:
        try:
            issue_date = date(
                int(date_match.group(3)),
                int(date_match.group(2)),
                int(date_match.group(1)),
            )
        except ValueError:
            pass

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
# ParsePipeline
# ─────────────────────────────────────────────────────────────────────────────

class ParsePipeline:
    """
    Orchestrates 5 independent parsing stages.

    Dùng ParsePipeline.run(pdf_path) → dict kết quả parse.
    PDFParser là thin wrapper xử lý I/O (save JSON) bên trên pipeline này.
    """

    def __init__(self):
        self._extractor = SmartPDFHelper()
        self._patcher = PatchApplier()

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

        # ── Stage 2: Normalize ──────────────────────────────────────────────
        clean_text, clean_tables = self._stage_normalize(raw_text, raw_tables)

        # ── Stage 2.5: Extract metadata (cần clean text) ────────────────────
        metadata = extract_metadata(clean_text, pdf_path.name)

        # ── Stage 3: Parse structure ────────────────────────────────────────
        result = self._stage_parse(clean_text, clean_tables, pdf_meta, metadata)

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

    def _stage_extract(self, pdf_path: Path):
        """
        PDF → raw text + raw tables.

        clean_output=False: KHÔNG clean ở đây.
        Normalization là trách nhiệm của Stage 2.
        Fix bug ở đây nếu: text bị mất, OCR sai, bảng không detect được.
        """
        logger.info("📄 Stage 1: Extract")
        return self._extractor.extract_text_and_tables(
            pdf_path,
            extract_tables=True,
            clean_output=False,
        )

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    def _stage_normalize(self, raw_text: str, raw_tables: list):
        """
        Normalize text + merge fragmented tables.

        Text transformations được phép:
          ✅ NFC unicode normalization
          ✅ Fix "NĐ-\\nCP" → "NĐ-CP" (hyphenated line break)
          ✅ Remove page numbers, decorative separators
          ✅ Normalize consecutive whitespace

        KHÔNG được:
          ❌ Merge lines bừa (vỡ structure: "Khoản 1. a)" sai)
          ❌ Lowercase (vỡ detect "Điều", "Phụ lục")
          ❌ Remove punctuation (vỡ content)

        Fix bug ở đây nếu: encoding sai, dính chữ, khoảng trắng thừa.
        """
        logger.info("🧹 Stage 2: Normalize")
        clean_text = clean_legal_text(raw_text)
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
