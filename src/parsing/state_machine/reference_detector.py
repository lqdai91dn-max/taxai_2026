"""
Reference Detector - Phát hiện tham chiếu pháp luật

Loại tham chiếu:
--------------
1. INTERNAL (Nội bộ cùng văn bản):
   - "theo quy định tại Khoản 2 Điều 7"
   - "quy định tại Điều này"
   - "theo quy định tại điểm a khoản 3 Điều 10"

2. EXTERNAL (Tham chiếu văn bản khác):
   - "theo quy định của Luật Chứng khoán"
   - "theo quy định của pháp luật về thuế"
   - "Nghị định 123/2024/NĐ-CP"

3. SELF-REFERENCE (Tự tham chiếu):
   - "Điều này"
   - "khoản này"
   - "Luật này"

Author: TaxAI Team
Version: 3.0
"""

import re
import unicodedata
from typing import List, Tuple, Optional
from dataclasses import dataclass


# Trailing phrases thường gặp sau tên Luật — cần loại bỏ khi normalize
_LAW_TRAILING_RE = re.compile(
    r'\s+và\s+(?:các\s+)?(?:văn\s+bản|quy\s+định)(?:\s+\S+)*',
    re.IGNORECASE | re.UNICODE,
)

# Clause-stop patterns dùng để phát hiện kết thúc tên luật.
# Viết ở dạng KHÔNG DẤU (no-accent) để khớp cả khi text bị OCR lỗi.
# VD: "Luật Thủ đô, nghị quyêt..." → strip_accents → "Luat Thu do, nghi quyet..."
#     pattern ",\s+nghi\s+quyet" → match → cắt tại dấu phẩy
_LAW_CLAUSE_STOP_RE_NOACCENT = re.compile(
    r'(?:'
    # Prepositions / conjunctions that safely signal end-of-law-name context.
    # Note: 'khi' / 'de' / 'ma' removed — too short, false-positives on words like
    # 'khi' inside "vũ khí" (stripped → "vu khi"), "để lại" (stripped → "de lai").
    r'\s+(?:trong|theo|ve\s+viec|doi\s+voi|nham\s+muc|duoc\s+ap)\b'
    r'|,\s+(?:mua|ban|ky|cap|thi\s+hanh|nham|hoac\s+la'            # verbs after comma
    r'|nghi\s+quyet|van\s+ban|quyet\s+dinh|giai\s+phap'            # doc-type nouns after comma
    r'|quy\s+dinh|huong\s+dan)\b'                                   # other clause starters
    r')',
    re.IGNORECASE,   # ASCII-only after stripping accents
)


def _strip_accents(text: str) -> str:
    """Bỏ dấu tiếng Việt để matching accent-insensitive.

    Biến 'nghị quyêt' và 'nghị quyết' đều thành 'nghi quyet'
    → dùng cho _LAW_CLAUSE_STOP_RE_NOACCENT để tránh lỗi OCR drop tone.
    """
    nfd = unicodedata.normalize('NFD', text)
    base = ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')
    return base.replace('đ', 'd').replace('Đ', 'D')


def _clean_law_name(name: str) -> str:
    """Strip trailing phrases như 'và các văn bản hướng dẫn' và clause starters từ tên luật.

    Dùng accent-insensitive matching để không bị ảnh hưởng bởi lỗi OCR
    (VD: 'quyêt' thay vì 'quyết' vẫn được detect và cắt đúng chỗ).
    """
    # 1. Strip "và các văn bản hướng dẫn..."
    name = _LAW_TRAILING_RE.sub('', name)
    # 2. Detect clause-stop trên bản stripped-accent, cắt trên bản gốc
    name_noaccent = _strip_accents(name)
    m = _LAW_CLAUSE_STOP_RE_NOACCENT.search(name_noaccent)
    if m:
        name = name[:m.start()]
    return name.rstrip(' ,').strip()


def _slugify(text: str) -> str:
    """
    Chuyển tên văn bản pháp luật thành ASCII slug an toàn cho ID.

    Quy tắc:
    - Collapse whitespace/newlines
    - "pháp luật về X" → "luat_X"
    - Bỏ dấu tiếng Việt (NFD + strip Mn), xử lý đ/Đ riêng
    - Chỉ giữ [a-z0-9_]

    VD:
        "pháp luật về công nghệ\\ncao" → "luat_cong_nghe_cao"
        "Luật thủ đô"                  → "luat_thu_do"
        "Nghị định 123/2024/NĐ-CP"    → "nghi_dinh_123_2024_nd_cp"
    """
    # 1. Collapse whitespace/newlines
    text = re.sub(r'\s+', ' ', text).strip()

    # 2. "pháp luật về X" → "luật X"
    text = re.sub(r'pháp\s+luật\s+về\s+', 'luật ', text, flags=re.IGNORECASE)

    # 3. Xử lý đ/Đ trước khi NFD (NFD không decompose đ)
    text = text.replace('đ', 'd').replace('Đ', 'D')

    # 4. NFD + bỏ combining marks
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')

    # 5. Lowercase, giữ [a-z0-9], thay còn lại bằng _
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '_', text)

    # 6. Gộp ___, strip đầu/cuối
    text = re.sub(r'_+', '_', text).strip('_')

    return text or "unknown"


@dataclass
class ReferenceMatch:
    """
    Một tham chiếu được phát hiện
    
    Attributes:
        text_match: Chuỗi gốc được tìm thấy
        ref_type: "internal" | "external" | "self"
        target_dieu: Số Điều được tham chiếu (nếu có)
        target_khoan: Số Khoản được tham chiếu (nếu có)
        target_diem: Chữ cái Điểm được tham chiếu (nếu có)
        target_document: Tên văn bản ngoài (nếu external)
        start_pos: Vị trí bắt đầu trong text
        end_pos: Vị trí kết thúc trong text
    """
    text_match: str
    ref_type: str  # "internal" | "external" | "self"
    target_dieu: Optional[str] = None
    target_khoan: Optional[str] = None
    target_diem: Optional[str] = None
    target_document: Optional[str] = None
    start_pos: int = 0
    end_pos: int = 0
    
    def generate_target_id(
        self,
        document_id: str,
        current_dieu: Optional[str] = None,
        dieu_index: Optional[dict] = None
    ) -> str:
        """
        Tạo target_id cho reference.

        Args:
            document_id: ID văn bản (VD: "117_2025_NDCP")
            current_dieu: Số Điều hiện tại (cho self-reference)
            dieu_index: Mapping {dieu_number → full_node_id} để resolve
                        đúng path kể cả Chương
                        VD: {"4": "doc_117_2025_NDCP_chuong_II_dieu_4"}

        Returns:
            target_id string (VD: "doc_117_2025_NDCP_chuong_II_dieu_4_khoan_2")
        """
        if self.ref_type == "external":
            if self.target_document:
                base = f"external_{_slugify(self.target_document)}"
                # Bug 1+2 fix: nếu external ref có target_dieu (từ context merge),
                # thêm vào ID để phân biệt "NĐ108 Điều 14" với chỉ "NĐ108".
                if self.target_dieu:
                    base += f"_dieu_{self.target_dieu}"
                    base += self._build_sub_path()
                return base
            return "external_unknown"

        if self.ref_type == "self":
            if current_dieu:
                base = self._resolve_dieu_base(
                    document_id, current_dieu, dieu_index
                )
                extra = self._build_sub_path()
                return base + extra
            return f"doc_{document_id}_self"

        # Internal reference
        if self.target_dieu:
            base = self._resolve_dieu_base(
                document_id, self.target_dieu, dieu_index
            )
            extra = self._build_sub_path()
            return base + extra

        return f"doc_{document_id}"

    def _resolve_dieu_base(
        self,
        document_id: str,
        dieu_number: str,
        dieu_index: Optional[dict]
    ) -> str:
        """Trả về full node_id của Điều, kể cả Chương trong path.

        Nếu dieu_index được cung cấp (pass 2 của finalize_parsing) nhưng Điều
        không tìm thấy → Điều đó không thuộc văn bản này → external reference.
        """
        if dieu_index and dieu_number in dieu_index:
            return dieu_index[dieu_number]
        if dieu_index:
            # dieu_index có nhưng Điều X không tồn tại trong doc → external
            return f"external_dieu_{dieu_number}"
        # dieu_index chưa được build → fallback internal (sẽ được re-resolve sau)
        return f"doc_{document_id}_dieu_{dieu_number}"

    def _build_sub_path(self) -> str:
        """Build phần path sau Điều (Khoản, Điểm)."""
        parts = []
        if self.target_khoan:
            parts.append(f"khoan_{self.target_khoan}")
        if self.target_diem:
            parts.append(f"diem_{self.target_diem}")
        return ("_" + "_".join(parts)) if parts else ""


class ReferenceDetector:
    """
    Phát hiện và trích xuất tham chiếu pháp luật
    
    Sử dụng:
        detector = ReferenceDetector(document_id="109_2025_QH15")
        refs = detector.find_references(text, current_dieu="5")
    """
    
    def __init__(self, document_id: str):
        self.document_id = document_id
        
        # Compile regex patterns
        self._compile_patterns()
    
    def _compile_patterns(self):
        """Compile all regex patterns for performance"""
        
        # Pattern 1: Internal reference (Khoản X Điều Y)
        # VD: "theo quy định tại Khoản 2 Điều 7"
        self.internal_full = re.compile(
            r'(?:theo\s+quy\s+định\s+)?(?:tại\s+)?'
            r'(?:điểm\s+([a-zđ])\s+)?'
            r'(?:khoản\s+(\d+)\s+)?'
            r'Điều\s+(\d+)',
            re.IGNORECASE
        )
        
        # Pattern 2: Self-reference (Điều này, khoản này)
        self.self_ref = re.compile(
            r'(Điều|Khoản|điểm)\s+này',
            re.IGNORECASE
        )
        
        # Pattern 3: External legal documents
        # Bug 3 fix: cho phép dấu phẩy trong tên luật (VD: "Luật Khoa học, công nghệ...")
        # và tăng giới hạn độ dài lên 120 ký tự.
        # _clean_law_name() sẽ strip trailing phrases như "và các văn bản hướng dẫn".
        # Bug 3 fix: dùng \w (Unicode word chars) thay vì hardcode Vietnamese chars.
        # \w với re.UNICODE matches tất cả chữ tiếng Việt bao gồm à, á, ã...
        # Cho phép `,` bên trong tên luật (VD: "Luật Khoa học, công nghệ và đổi mới sáng tạo")
        # _clean_law_name() xử lý trailing phrases như "và các văn bản hướng dẫn".
        # First char: uppercase A-Z hoặc Đ để loại "Luật này", "Luật trên"...
        # Continuation: \w (Unicode — covers all Vietnamese accented chars) + \s + comma.
        # Stop at "và Luật X" để tránh merge 2 luật khác nhau trong cùng câu.
        # _clean_law_name() xử lý trailing "và các văn bản hướng dẫn...".
        self.external_laws = re.compile(
            r'(?:theo\s+quy\s+định\s+của\s+)?'
            r'(Luật\s+[A-ZĐ](?:(?!\s+và\s+Luật\s+[A-ZĐ])[\w\s,]){2,120})'
            r'(?=\s|;|\.|$)',
            re.UNICODE,
        )
        
        self.external_decrees = re.compile(
            r'(Nghị\s+định|Thông\s+tư|Quyết\s+định|Nghị\s+quyết)\s+(?:số\s+)?(\d+/\d+/[\w\-]+)',
            re.IGNORECASE | re.UNICODE
        )
        
        # Pattern 4: Generic law references — disabled (too noisy, captures
        # sentence fragments like "pháp luật về thuế hay không")
        self.external_generic = None
    
    def find_references(
        self, 
        text: str, 
        current_dieu: Optional[str] = None
    ) -> List[ReferenceMatch]:
        """
        Tìm tất cả tham chiếu trong text
        
        Args:
            text: Nội dung cần tìm
            current_dieu: Điều hiện tại (để xử lý self-reference)
        
        Returns:
            List of ReferenceMatch objects
        
        Examples:
            >>> detector = ReferenceDetector("109_2025_QH15")
            >>> text = "theo quy định tại Khoản 2 Điều 7 của Luật này"
            >>> refs = detector.find_references(text, current_dieu="5")
            >>> len(refs)
            1
            >>> refs[0].target_dieu
            '7'
            >>> refs[0].target_khoan
            '2'
        """
        internal_refs = self._find_internal_references(text)
        self_refs = self._find_self_references(text, current_dieu)
        external_refs = self._find_external_references(text)

        # Bug 1+2 fix: upgrade "Điều X" → external khi ngay sau đó là tên văn bản ngoài.
        # VD: "Điều 14 Nghị định 108" → external_nghi_dinh_108_nd_cp_dieu_14
        #     "Điều 28, Điều 29 Nghị định 108" → cả 2 external với NĐ108
        internal_refs, external_refs = self._resolve_external_context(
            internal_refs, external_refs, text
        )

        references = internal_refs + self_refs + external_refs
        references.sort(key=lambda r: r.start_pos)

        # Remove duplicates (same position)
        unique_refs = []
        seen_positions: set = set()
        for ref in references:
            if ref.start_pos not in seen_positions:
                unique_refs.append(ref)
                seen_positions.add(ref.start_pos)

        return unique_refs
    
    def _find_internal_references(self, text: str) -> List[ReferenceMatch]:
        """Tìm tham chiếu nội bộ (Khoản X Điều Y)"""
        refs = []
        
        for match in self.internal_full.finditer(text):
            diem = match.group(1)  # Điểm (optional)
            khoan = match.group(2)  # Khoản (optional)
            dieu = match.group(3)   # Điều (required)
            
            refs.append(ReferenceMatch(
                text_match=match.group(0),
                ref_type="internal",
                target_dieu=dieu,
                target_khoan=khoan,
                target_diem=diem,
                start_pos=match.start(),
                end_pos=match.end()
            ))
        
        return refs
    
    def _resolve_external_context(
        self,
        internal_refs: List['ReferenceMatch'],
        external_refs: List['ReferenceMatch'],
        text: str,
        window: int = 120,
    ) -> Tuple[List['ReferenceMatch'], List['ReferenceMatch']]:
        """
        Khi "Điều X" xuất hiện ngay trước tên văn bản ngoài (trong vòng `window` ký tự,
        không có '.' hay 'này' ngăn cách), upgrade thành external ref với doc context đó.

        Nguyên tắc:
          - Lấy external ref GẦN NHẤT sau internal ref (break ở ref đầu tiên hợp lệ).
          - Một external ref có thể được dùng bởi nhiều internal ref (VD: "Điều 28, Điều 29 NĐ108").
          - External ref đã được merge sẽ KHÔNG xuất hiện như standalone reference nữa.
          - Không upgrade nếu gap_text chứa '.' (sentence boundary) hoặc 'này' (self-ref signal).

        VD:
            "Điều 14 Nghị định 108/2024"          → external_nghi_dinh_108_..._dieu_14
            "Điều 28, Điều 29 Nghị định 108/2024" → cả 2 external với NĐ108
            "Điều 5 của Luật này"                 → không upgrade ('này' trong gap)
            "Điều 5. Sau đó Nghị định 108"        → không upgrade ('.' trong gap)
        """
        if not external_refs:
            return internal_refs, external_refs

        # Sort external refs by start_pos để tìm kiếm tuần tự
        sorted_ext = sorted(enumerate(external_refs), key=lambda x: x[1].start_pos)
        consumed_ext: set = set()
        upgraded: List[ReferenceMatch] = []

        for iref in internal_refs:
            matched_ext_idx = None

            for orig_idx, eref in sorted_ext:
                if eref.start_pos < iref.end_pos:
                    continue
                gap = eref.start_pos - iref.end_pos
                if gap > window:
                    break
                gap_text = text[iref.end_pos:eref.start_pos]
                # Không upgrade nếu có sentence boundary hoặc self-ref signal
                if '.' in gap_text or '\n' in gap_text or 'này' in gap_text:
                    continue
                matched_ext_idx = orig_idx
                break  # Lấy external ref gần nhất hợp lệ

            if matched_ext_idx is not None:
                eref = external_refs[matched_ext_idx]
                consumed_ext.add(matched_ext_idx)
                upgraded.append(ReferenceMatch(
                    text_match=iref.text_match,
                    ref_type="external",
                    target_dieu=iref.target_dieu,
                    target_khoan=iref.target_khoan,
                    target_diem=iref.target_diem,
                    target_document=eref.target_document,
                    start_pos=iref.start_pos,
                    end_pos=iref.end_pos,
                ))
            else:
                upgraded.append(iref)

        remaining_external = [
            e for i, e in enumerate(external_refs) if i not in consumed_ext
        ]
        return upgraded, remaining_external

    def _find_self_references(
        self, 
        text: str, 
        current_dieu: Optional[str]
    ) -> List[ReferenceMatch]:
        """Tìm tự tham chiếu (Điều này, khoản này)"""
        refs = []
        
        for match in self.self_ref.finditer(text):
            level = match.group(1).lower()  # "điều" | "khoản" | "điểm"
            
            # Determine what it refers to
            target_khoan = None
            target_diem = None
            
            # "khoản này" trong context của Điều → cần biết khoản nào
            # Nhưng thường "khoản này" chỉ khoản đang được định nghĩa
            # Nên để None, sẽ được resolve bởi parser
            
            refs.append(ReferenceMatch(
                text_match=match.group(0),
                ref_type="self",
                target_dieu=current_dieu,
                start_pos=match.start(),
                end_pos=match.end()
            ))
        
        return refs
    
    def _find_external_references(self, text: str) -> List[ReferenceMatch]:
        """Tìm tham chiếu văn bản ngoài"""
        refs = []
        
        # Find law references
        for match in self.external_laws.finditer(text):
            # Bug 3 fix: strip trailing "và các văn bản hướng dẫn..." từ tên luật
            law_name = _clean_law_name(match.group(1).strip())
            if not law_name:
                continue

            refs.append(ReferenceMatch(
                text_match=match.group(0),
                ref_type="external",
                target_document=law_name,
                start_pos=match.start(),
                end_pos=match.end(),
            ))
        
        # Find decree/circular references
        for match in self.external_decrees.finditer(text):
            doc_type = match.group(1)
            doc_number = match.group(2)
            doc_name = f"{doc_type} {doc_number}"
            
            refs.append(ReferenceMatch(
                text_match=match.group(0),
                ref_type="external",
                target_document=doc_name,
                start_pos=match.start(),
                end_pos=match.end()
            ))
        
        # Generic law references disabled (too noisy)
        
        return refs


# ============================================
# UNIT TEST
# ============================================

if __name__ == "__main__":
    detector = ReferenceDetector(document_id="109_2025_QH15")
    
    print("=" * 70)
    print("TEST: Reference Detection")
    print("=" * 70)
    
    test_cases = [
        # Test 1: Internal reference
        (
            "Thu nhập được xác định theo quy định tại Khoản 2 Điều 7 của Luật này.",
            "5",
            "Internal reference (Khoản 2 Điều 7)"
        ),
        
        # Test 2: Self-reference
        (
            "Chính phủ quy định chi tiết Điều này.",
            "5",
            "Self-reference (Điều này)"
        ),
        
        # Test 3: External law reference
        (
            "theo quy định của Luật Chứng khoán",
            "5",
            "External reference (Luật Chứng khoán)"
        ),
        
        # Test 4: Generic law reference
        (
            "theo quy định của pháp luật về công nghệ cao",
            "5",
            "Generic law reference"
        ),
        
        # Test 5: Decree reference
        (
            "theo Nghị định 123/2024/NĐ-CP",
            "5",
            "Decree reference"
        ),
        
        # Test 6: Complex - multiple references
        (
            "Miễn thuế theo quy định tại điểm a khoản 2 Điều 10 và Luật Đầu tư.",
            "5",
            "Multiple references"
        ),
    ]
    
    for i, (text, current_dieu, description) in enumerate(test_cases, 1):
        print(f"\n{'─' * 70}")
        print(f"TEST {i}: {description}")
        print(f"{'─' * 70}")
        print(f"Text: {text}")
        print(f"Current Điều: {current_dieu}")
        
        refs = detector.find_references(text, current_dieu=current_dieu)
        
        print(f"\nFound {len(refs)} reference(s):")
        
        for ref in refs:
            print(f"\n  📎 Reference:")
            print(f"     Text: '{ref.text_match}'")
            print(f"     Type: {ref.ref_type}")
            
            if ref.ref_type == "internal":
                print(f"     Target: Điều {ref.target_dieu}", end="")
                if ref.target_khoan:
                    print(f", Khoản {ref.target_khoan}", end="")
                if ref.target_diem:
                    print(f", Điểm {ref.target_diem}", end="")
                print()
            elif ref.ref_type == "external":
                print(f"     Document: {ref.target_document}")
            elif ref.ref_type == "self":
                print(f"     Self-reference to Điều {ref.target_dieu}")
            
            target_id = ref.generate_target_id(
                document_id="109_2025_QH15",
                current_dieu=current_dieu
            )
            print(f"     Target ID: {target_id}")
    
    print("\n" + "=" * 70)
    print("✅ All tests completed!")
    print("=" * 70)