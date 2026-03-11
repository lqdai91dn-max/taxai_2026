"""
Indentation Checker - Phát hiện lead_in_text và trailing_text

Nguyên lý hoạt động:
-----------------
Văn bản pháp luật VN có cấu trúc thụt lề rõ ràng:

Điều 7. Thuế thu nhập cá nhân đối với thu nhập từ kinh doanh
1. Cá nhân cư trú có hoạt động sản xuất...                    ← Khoản (lề chuẩn)
2. Thuế thu nhập cá nhân đối với thu nhập từ kinh doanh...    ← Khoản (lề chuẩn)
   a) Thu nhập tính thuế được xác định...                     ← Điểm (lề sâu hơn)
   b) Cá nhân kinh doanh có doanh thu...                       ← Điểm (lề sâu hơn)
Thu nhập từ cho thuê bất động sản...                          ← Trailing text (lề = Khoản)

Logic phát hiện:
1. Nếu text có lề BẰNG "Khoản" mà đang ở trong "Điểm" → Trailing text của Khoản
2. Nếu text có lề SÂU HƠN "Khoản" → Nối tiếp nội dung Điểm hiện tại
3. Nếu text xuất hiện TRƯỚC Khoản đầu tiên → Lead-in text của Điều

Author: TaxAI Team
Version: 3.0
"""

import re
from typing import Optional, Tuple
from enum import Enum


class IndentLevel(Enum):
    """Các mức thụt lề chuẩn trong văn bản pháp luật VN"""
    DIEU = 0      # Không thụt lề
    KHOAN = 0     # Không thụt lề (cùng level với Điều)
    DIEM = 3      # Thụt 3 spaces
    TIET = 6      # Thụt 6 spaces (nếu có)


class IndentationChecker:
    """
    Phân tích thụt lề để phát hiện:
    - Lead-in text (văn bản dẫn nhập)
    - Trailing text (văn bản kết)
    - Content continuation (nội dung nối tiếp)
    """
    
    def __init__(self):
        # Regex patterns để nhận diện các cấp độ
        self.patterns = {
            # Không dùng IGNORECASE: tránh nhầm "phần vốn" (v), "phần mềm" (m)
            # Chỉ khớp "Phần" hoặc "PHẦN" + Roman numeral VIẾT HOA
            'phan': re.compile(r'^(?:Phần|PHẦN)\s+([IVXLCDM]+)\s*\n?\s*(.*)$'),
            'chuong': re.compile(r'^Chương\s+([IVXLCDM]+)\s*\n?\s*(.*)$', re.IGNORECASE),
            'muc': re.compile(r'^Mục\s+(\d+)\s*\n?\s*(.*)$', re.IGNORECASE),
            'phu_luc': re.compile(r'^Phụ\s+lục(?:\s+(\d+|[IVXLCDM]+))?\s*\n?\s*(.*)$', re.IGNORECASE),
            'dieu': re.compile(r'^Điều\s+(\d+[a-zA-Z]*)\.\s+(.*)$'),
            'khoan': re.compile(r'^(\d+)\.\s+(.*)$'),
            'diem': re.compile(r'^([a-zđ])\)\s+(.*)$'),
            'tiet': re.compile(r'^([a-zđ]\.\d+)\)\s+(.*)$'),  # VD: a.1), b.2)
        }
    
    def get_indentation(self, line: str) -> int:
        """
        Đo số lượng khoảng trắng đầu dòng
        
        Args:
            line: Dòng text cần đo
        
        Returns:
            Số spaces thụt lề (0 = không thụt)
        
        Examples:
            "1. Người nộp thuế..." → 0
            "   a) Thu nhập..." → 3
            "      a.1) Chi tiết..." → 6
        """
        # Đếm spaces đầu dòng
        stripped = line.lstrip(' ')
        return len(line) - len(stripped)
    
    def detect_line_type(self, line: str) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Phát hiện loại dòng (Điều/Khoản/Điểm/Tiết hoặc plain text)
        
        Args:
            line: Dòng text cần phân tích
        
        Returns:
            (line_type, index, content)
            
            line_type: "dieu" | "khoan" | "diem" | "tiet" | "text"
            index: Chỉ số (VD: "5", "2", "a") hoặc None
            content: Nội dung sau chỉ số hoặc toàn bộ line
        
        Examples:
            "Điều 5. Miễn thuế..." → ("dieu", "5", "Miễn thuế...")
            "1. Người nộp thuế..." → ("khoan", "1", "Người nộp thuế...")
            "   a) Thu nhập..." → ("diem", "a", "Thu nhập...")
            "Thu nhập từ cho thuê..." → ("text", None, "Thu nhập từ cho thuê...")
        """
        line = line.strip()
        
        # Check Phần
        match = self.patterns['phan'].match(line)
        if match:
            return ("phan", match.group(1), match.group(2) if match.group(2) else "")
        
        # Check Chương
        match = self.patterns['chuong'].match(line)
        if match:
            return ("chuong", match.group(1), match.group(2) if match.group(2) else "")
        
        # Check Mục
        match = self.patterns['muc'].match(line)
        if match:
            return ("muc", match.group(1), match.group(2) if match.group(2) else "")

        # Check Phụ lục (số thứ tự optional — VD: "Phụ lục" hoặc "Phụ lục I")
        match = self.patterns['phu_luc'].match(line)
        if match:
            idx = match.group(1) if match.group(1) else '1'
            content = match.group(2) if match.group(2) else ""
            # Reject mid-sentence references: "Phụ lục II kèm theo Thông tư..."
            # A real Phụ lục header never starts with "kèm theo"
            if 'kèm theo' in content.lower():
                return ("text", None, line)
            return ("phu_luc", idx, content)

        # Check Điều
        match = self.patterns['dieu'].match(line)
        if match:
            return ("dieu", match.group(1), match.group(2))
        
        # Check Khoản
        match = self.patterns['khoan'].match(line)
        if match:
            # Cần validate: không phải số tiền, năm, etc
            if self._is_valid_khoan_number(line):
                return ("khoan", match.group(1), match.group(2))
        
        # Check Điểm
        match = self.patterns['diem'].match(line)
        if match:
            return ("diem", match.group(1), match.group(2))
        
        # Check Tiết (nếu có)
        match = self.patterns['tiet'].match(line)
        if match:
            return ("tiet", match.group(1), match.group(2))
        
        # Plain text
        return ("text", None, line)
    
    def _is_valid_khoan_number(self, line: str) -> bool:
        """
        Kiểm tra xem "1." có phải là Khoản hay chỉ là số tiền/năm/bảng biểu

        False positives cần tránh:
        - "100 triệu đồng"
        - "2025 năm"
        - "5% thuế suất"
        - "1. Ngành nghề .........."  ← table row trong biểu mẫu
        - "2. Tổng cộng (1)"          ← table row
        - "3. ................"        ← placeholder row

        Returns:
            True nếu là Khoản hợp lệ
        """
        # Regex false positives — số/đơn vị
        false_patterns = [
            r'^\d+\s+(triệu|tỷ|nghìn|%|năm|tháng|ngày)',
            r'^\d{4}\s',       # Years (2025, 2026)
            r'^\d+\.\d+\s',    # Decimals (1.5, 2.3)
            r'^\d+\s*VNĐ',
            r'^\d+/\d+',       # Fractions/dates
            # Bảng biểu: dòng Ngành nghề (template form)
            r'^\d+\.\s+Ngành\s+nghề',
            # Bảng biểu: nội dung là placeholder dots
            r'^\d+\.\s+\S.*\.{4,}',
            # Bảng biểu: Tổng cộng (N)
            r'^\d+\.\s+Tổng\s+cộng',
        ]

        for pattern in false_patterns:
            if re.match(pattern, line, re.IGNORECASE):
                return False

        # Phải có nội dung đủ dài sau số (>= 4 chars để chấp nhận "Khai thuế" = 9 chars)
        match = re.match(r'^\d+\.\s*(.+)$', line)
        if match and len(match.group(1)) < 4:
            return False

        return True
    
    def classify_text_block(
        self,
        text_line: str,
        current_context: dict
    ) -> str:
        """
        Phân loại một dòng text không đánh số
        
        Args:
            text_line: Dòng text cần phân loại
            current_context: Context hiện tại của state machine
                {
                    'current_level': 'dieu' | 'khoan' | 'diem' | 'tiet',
                    'node_indent': int,  # Lề của node hiện tại
                    'has_children': bool
                }
        
        Returns:
            "lead_in" | "trailing" | "continuation"
        
        Logic:
            1. So sánh indent của text với indent của node hiện tại:
               - indent > node_indent → continuation (nội dung nối tiếp)
               - indent == node_indent và chưa có children → lead_in
               - indent == node_indent và đã có children → trailing
               - indent < node_indent → trailing (kết thúc node, về parent)
        
        Examples:
            Context: {current_level: 'khoan', node_indent: 0, has_children: False}
            Text: "Miễn thuế trong trường hợp..." (indent = 0)
            → "lead_in" (văn bản dẫn nhập của Khoản)
            
            Context: {current_level: 'khoan', node_indent: 0, has_children: True}
            Text: "Thu nhập từ cho thuê..." (indent = 0)
            → "trailing" (văn bản kết của Khoản)
            
            Context: {current_level: 'diem', node_indent: 3}
            Text: "   được miễn thuế..." (indent = 3)
            → "continuation" (nội dung nối tiếp của Điểm, cùng indent)
        """
        # Đo thụt lề của dòng text
        text_indent = self.get_indentation(text_line)
        node_indent = current_context.get('node_indent', 0)
        has_children = current_context.get('has_children', False)
        current_level = current_context.get('current_level', '')
        
        # Trường hợp 1: Text thụt SÂU HƠN node → Continuation
        # VD: Điểm có indent=3, text có indent=6 → text là phần của Điểm
        if text_indent > node_indent:
            return "continuation"
        
        # Trường hợp 2: Text cùng indent với node
        if text_indent == node_indent:
            # ✅ FIX: Điểm/Tiết không có lead_in_text riêng
            # Nếu text cùng indent với Điểm/Tiết → luôn là continuation
            if current_level in ['diem', 'tiet']:
                return "continuation"
            
            # Các level khác (Điều, Khoản):
            # - Nếu chưa có children → Lead-in
            # - Nếu đã có children → Trailing
            if not has_children:
                return "lead_in"
            else:
                return "trailing"
        
        # Trường hợp 3: Text indent NHỎ HƠN node → Trailing (về parent level)
        return "trailing"
    
    def should_close_node(
        self,
        next_line: str,
        current_level: str
    ) -> bool:
        """
        Quyết định có nên đóng node hiện tại không
        
        Args:
            next_line: Dòng tiếp theo
            current_level: Level hiện tại ('dieu', 'khoan', 'diem')
        
        Returns:
            True nếu cần đóng node
        
        Logic:
            - Nếu đang ở Khoản, gặp Khoản mới hoặc Điều mới → Đóng
            - Nếu đang ở Điểm, gặp Điểm mới hoặc Khoản mới → Đóng
        """
        line_type, _, _ = self.detect_line_type(next_line)
        
        close_rules = {
            'dieu': ['dieu'],  # Điều chỉ đóng khi gặp Điều mới
            'khoan': ['khoan', 'dieu'],  # Khoản đóng khi gặp Khoản/Điều mới
            'diem': ['diem', 'khoan', 'dieu'],  # Điểm đóng khi gặp Điểm/Khoản/Điều
            'tiet': ['tiet', 'diem', 'khoan', 'dieu']
        }
        
        should_close = line_type in close_rules.get(current_level, [])
        
        return should_close


# ============================================
# UNIT TEST
# ============================================

if __name__ == "__main__":
    checker = IndentationChecker()
    
    print("=" * 70)
    print("TEST 1: Detect Line Type")
    print("=" * 70)
    
    test_lines = [
        "Điều 5. Các trường hợp miễn thuế, giảm thuế khác",
        "1. Người nộp thuế gặp khó khăn...",
        "2. Miễn thuế thu nhập cá nhân trong thời hạn 05 năm...",
        "   a) Thu nhập từ dự án hoạt động công nghiệp...",
        "   b) Thu nhập từ dự án nghiên cứu...",
        "Thu nhập từ cho thuê bất động sản không áp dụng...",
        "100 triệu đồng",  # False positive
        "2025 năm"  # False positive
    ]
    
    for line in test_lines:
        line_type, index, content = checker.detect_line_type(line)
        indent = checker.get_indentation(line)
        print(f"\n📝 Line: {line[:50]}...")
        print(f"   Type: {line_type}")
        print(f"   Index: {index}")
        print(f"   Indent: {indent} spaces")
    
    print("\n" + "=" * 70)
    print("TEST 2: Classify Text Block")
    print("=" * 70)
    
    # Scenario 1: Lead-in text
    context_1 = {
        'current_level': 'khoan',
        'node_indent': 0,
        'has_children': False
    }
    text_1 = "Miễn thuế trong các trường hợp sau:"
    result_1 = checker.classify_text_block(text_1, context_1)
    print(f"\n✅ Scenario 1: Text xuất hiện TRƯỚC các điểm")
    print(f"   Text: {text_1}")
    print(f"   Context: {context_1}")
    print(f"   Result: {result_1} (expected: lead_in)")
    
    # Scenario 2: Trailing text
    context_2 = {
        'current_level': 'khoan',
        'node_indent': 0,
        'has_children': True
    }
    text_2 = "Thu nhập từ cho thuê bất động sản không áp dụng..."
    result_2 = checker.classify_text_block(text_2, context_2)
    print(f"\n✅ Scenario 2: Text xuất hiện SAU các điểm, lề BẰNG Khoản")
    print(f"   Text: {text_2}")
    print(f"   Context: {context_2}")
    print(f"   Result: {result_2} (expected: trailing)")
    
    # Scenario 3: Continuation
    context_3 = {
        'current_level': 'diem',
        'node_indent': 3,  # Điểm có indent 3
        'has_children': False
    }
    text_3 = "   được miễn thuế theo quy định tại Luật này."  # Cùng indent với Điểm
    result_3 = checker.classify_text_block(text_3, context_3)
    print(f"\n✅ Scenario 3: Text nối tiếp Điểm, CÙNG indent với Điểm")
    print(f"   Text: {text_3}")
    print(f"   Context: {context_3}")
    print(f"   Result: {result_3} (expected: continuation)")
    
    # Scenario 4: Continuation with deeper indent
    context_4 = {
        'current_level': 'diem',
        'node_indent': 3,
        'has_children': False
    }
    text_4 = "      với các điều kiện sau đây."  # Indent sâu hơn Điểm
    result_4 = checker.classify_text_block(text_4, context_4)
    print(f"\n✅ Scenario 4: Text nối tiếp Điểm, indent SÂU HƠN")
    print(f"   Text: {text_4}")
    print(f"   Context: {context_4}")
    print(f"   Result: {result_4} (expected: continuation)")
    
    print("\n" + "=" * 70)
    print("✅ All tests completed!")
    print("=" * 70)