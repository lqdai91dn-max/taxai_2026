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
                return f"external_{_slugify(self.target_document)}"
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
        # Only capture law name, not full sentence
        self.external_laws = re.compile(
            r'(?:theo\s+quy\s+định\s+của\s+)?(Luật\s+[A-ZẮẰẲẴẠĂÂẤẦẨẪẬÓÒỎÕỌÔỐỒỔỖỘƠỚỜỞỠỢÉÈẺẼẸÊẾỀỂỄỆÚÙỦŨỤƯỨỪỬỮỰÍÌỈĨỊÝỲỶỸỴĐ][a-zắằẳẵạăâấầẩẫậóòỏõọôốồổỗộơớờởỡợéèẻẽẹêếềểễệúùủũụưứừửữựíìỉĩịýỳỷỹỵđ\s]{1,50})(?=\s|;|,|\.|$)',
            re.UNICODE
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
        references = []
        
        # Find internal references
        references.extend(self._find_internal_references(text))
        
        # Find self-references
        references.extend(self._find_self_references(text, current_dieu))
        
        # Find external references
        references.extend(self._find_external_references(text))
        
        # Sort by position
        references.sort(key=lambda r: r.start_pos)
        
        # Remove duplicates (same position)
        unique_refs = []
        seen_positions = set()
        
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
            law_name = match.group(1).strip()
            
            refs.append(ReferenceMatch(
                text_match=match.group(0),
                ref_type="external",
                target_document=law_name,
                start_pos=match.start(),
                end_pos=match.end()
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