"""
State Machine Parser Core - Trái tim của parsing engine

Kiến trúc State Machine:
----------------------
1. Đọc từng dòng text
2. Detect loại dòng (Điều/Khoản/Điểm/Text)
3. Quản lý stack hierarchy
4. Phân loại text (lead_in/trailing/continuation)
5. Xây dựng cây nodes đệ quy
6. Detect references tự động
7. Export JSON chuẩn

State transitions:
-----------------
NULL → Điều → Khoản → Điểm → Tiết
  ↓      ↓       ↓       ↓
 Text   Text    Text   Text
  ↓      ↓       ↓       ↓
lead_in trailing continuation

Author: TaxAI Team  
Version: 3.0 - Complete Rewrite Based on Legal Document Research
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
import json
import re

from .node_builder import NodeBuilder, LegalNode, LegalDocument, LegalReference
from .indentation_checker import IndentationChecker
from .reference_detector import ReferenceDetector, ReferenceMatch

from src.utils.logger import logger

logger = logger.bind(module="state_machine_parser")


class ParserState:
    """
    Current parsing state - Stack-based hierarchy management
    
    Stack structure:
        [Phần I, Chương I, Điều 5, Khoản 2, Điểm a]
        
    Example transitions:
        [] → [Phần I]
        [Phần I] → [Phần I, Chương I]
        [Phần I, Chương I] → [Phần I, Chương I, Điều 5]
        [Phần I, Chương I, Điều 5] → [Phần I, Chương I, Điều 5, Khoản 1]
    """
    
    def __init__(self):
        self.stack: List[LegalNode] = []
        self.current_phan: Optional[LegalNode] = None
        self.current_chuong: Optional[LegalNode] = None
        self.current_muc: Optional[LegalNode] = None
        self.current_phu_luc: Optional[LegalNode] = None
        self.current_dieu: Optional[LegalNode] = None
        self.current_khoan: Optional[LegalNode] = None
        self.current_diem: Optional[LegalNode] = None
        self.current_tiet: Optional[LegalNode] = None
    
    def get_current_node(self) -> Optional[LegalNode]:
        """Get the deepest node in stack"""
        return self.stack[-1] if self.stack else None
    
    def get_current_level(self) -> str:
        """Get current level name"""
        if self.current_tiet:
            return 'tiet'
        elif self.current_diem:
            return 'diem'
        elif self.current_khoan:
            return 'khoan'
        elif self.current_dieu:
            return 'dieu'
        elif self.current_muc:
            return 'muc'
        elif self.current_chuong:
            return 'chuong'
        elif self.current_phan:
            return 'phan'
        elif self.current_phu_luc:
            return 'phu_luc'
        else:
            return 'none'

    def get_node_indent(self) -> int:
        """Get indent of current node"""
        level = self.get_current_level()
        indent_map = {
            'phan': 0,
            'chuong': 0,
            'muc': 0,
            'phu_luc': 0,
            'dieu': 0,
            'khoan': 0,
            'diem': 3,
            'tiet': 6
        }
        return indent_map.get(level, 0)
    
    def has_children(self) -> bool:
        """Check if current node has children"""
        current = self.get_current_node()
        return bool(current and current.children)
    
    def push(self, node: LegalNode, level: str):
        """
        Push a new node onto stack
        
        Args:
            node: Node to push
            level: 'phan' | 'chuong' | 'muc' | 'dieu' | 'khoan' | 'diem' | 'tiet'
        """
        # Update level pointers
        if level == 'phu_luc':
            # Phụ lục là root-level, reset toàn bộ state
            self.current_phu_luc = node
            self.current_phan = None
            self.current_chuong = None
            self.current_muc = None
            self.current_dieu = None
            self.current_khoan = None
            self.current_diem = None
            self.current_tiet = None
            self.stack = [node]

        elif level == 'phan':
            self.current_phan = node
            self.current_phu_luc = None
            self.current_chuong = None
            self.current_muc = None
            self.current_dieu = None
            self.current_khoan = None
            self.current_diem = None
            self.current_tiet = None
            self.stack = [node]
        
        elif level == 'chuong':
            self.current_chuong = node
            self.current_muc = None
            self.current_dieu = None
            self.current_khoan = None
            self.current_diem = None
            self.current_tiet = None
            # Keep Phần (if exists)
            if self.current_phan:
                self.stack = [self.current_phan, node]
            else:
                self.stack = [node]
        
        elif level == 'muc':
            self.current_muc = node
            self.current_dieu = None
            self.current_khoan = None
            self.current_diem = None
            self.current_tiet = None
            # Keep Phần + Chương
            base = []
            if self.current_phan:
                base.append(self.current_phan)
            if self.current_chuong:
                base.append(self.current_chuong)
            self.stack = base + [node]
        
        elif level == 'dieu':
            self.current_dieu = node
            self.current_khoan = None
            self.current_diem = None
            self.current_tiet = None
            # Keep Phần + Chương + Mục
            base = []
            if self.current_phan:
                base.append(self.current_phan)
            if self.current_chuong:
                base.append(self.current_chuong)
            if self.current_muc:
                base.append(self.current_muc)
            self.stack = base + [node]
        
        elif level == 'khoan':
            self.current_khoan = node
            self.current_diem = None
            self.current_tiet = None
            # Keep all above + Điều
            base = []
            if self.current_phan:
                base.append(self.current_phan)
            if self.current_chuong:
                base.append(self.current_chuong)
            if self.current_muc:
                base.append(self.current_muc)
            if self.current_dieu:
                base.append(self.current_dieu)
            self.stack = base + [node]
        
        elif level == 'diem':
            self.current_diem = node
            self.current_tiet = None
            # Keep all above + Khoản
            base = []
            if self.current_phan:
                base.append(self.current_phan)
            if self.current_chuong:
                base.append(self.current_chuong)
            if self.current_muc:
                base.append(self.current_muc)
            if self.current_dieu:
                base.append(self.current_dieu)
            if self.current_khoan:
                base.append(self.current_khoan)
            self.stack = base + [node]
        
        elif level == 'tiet':
            self.current_tiet = node
            # Keep all above
            base = []
            if self.current_phan:
                base.append(self.current_phan)
            if self.current_chuong:
                base.append(self.current_chuong)
            if self.current_muc:
                base.append(self.current_muc)
            if self.current_dieu:
                base.append(self.current_dieu)
            if self.current_khoan:
                base.append(self.current_khoan)
            if self.current_diem:
                base.append(self.current_diem)
            self.stack = base + [node]
    
    def get_parent_node(self) -> Optional[LegalNode]:
        """Get parent node of current node"""
        return self.stack[-2] if len(self.stack) >= 2 else None


class StateMachineParser:
    """
    State Machine Parser - Main parsing engine
    
    Usage:
        parser = StateMachineParser(
            document_id="109_2025_QH15",
            document_number="109/2025/QH15",
            document_type="Luật"
        )
        
        document = parser.parse_text(full_text)
        
        # Save to JSON
        with open('output.json', 'w', encoding='utf-8') as f:
            json.dump(document.to_dict(), f, ensure_ascii=False, indent=2)
    """
    
    def __init__(
        self, 
        document_id: str, 
        document_number: str,
        document_type: str = "Luật"  # ✅ Added document_type
    ):
        """
        Initialize parser
        
        Args:
            document_id: ID văn bản (e.g., "109_2025_QH15")
            document_number: Số hiệu (e.g., "109/2025/QH15")
            document_type: Loại văn bản (e.g., "Luật", "Nghị định", "Thông tư")
        """
        self.document_id = document_id
        self.document_number = document_number
        self.document_type = document_type  # ✅ Store document_type
        
        # Initialize components
        # ✅ Pass document_type to NodeBuilder
        self.node_builder = NodeBuilder(document_id, document_number, document_type)
        self.indent_checker = IndentationChecker()
        self.ref_detector = ReferenceDetector(document_id)
        
        # Parser state
        self.state = ParserState()

        # Root nodes (Điều nodes will be added here)
        self.root_nodes: List[LegalNode] = []

        # Table context flag — True khi đang trong vùng biểu mẫu/bảng
        # Dùng như secondary defense sau khi pdfplumber đã loại table bbox.
        # Xử lý borderless tables mà find_tables() có thể bỏ sót.
        self.inside_table: bool = False

        # Stop-parsing flag — True khi gặp phần hành chính cuối văn bản
        # (Nơi nhận:, chữ ký) → không parse thêm nội dung vào nodes.
        self.parsing_complete: bool = False
    
    def parse_text(self, text: str) -> LegalDocument:
        """
        Main parsing method - Parse full document text
        
        Args:
            text: Full document text (cleaned, OCR if needed)
        
        Returns:
            LegalDocument with complete hierarchy
        
        Process:
            1. Split text into lines
            2. For each line:
               a. Detect line type (Điều/Khoản/Điểm/Text)
               b. Handle based on type
               c. Update state
            3. Build final document
        """
        lines = text.split('\n')
        
        for i, line in enumerate(lines):
            # Skip empty lines
            if not line.strip():
                continue

            # Stop parsing when hitting administrative closing section
            if re.match(r'Nơi\s+nhận\s*:', line.strip(), re.IGNORECASE):
                self.parsing_complete = True
                break

            if self.parsing_complete:
                break

            # Detect line type
            line_type, index, node_content = self.indent_checker.detect_line_type(line)

            # ── Table context tracking ────────────────────────────────────
            # Khi gặp header biểu mẫu → vào table mode (secondary defense)
            if self._is_form_template_line(line):
                self.inside_table = True
            # Khi gặp Điều/Chương mới → thoát table mode
            if line_type in ('dieu', 'chuong', 'muc', 'phan'):
                self.inside_table = False
            # Trong table mode: ép Khoản/Điểm/Tiết về 'text' tránh nhầm
            if self.inside_table and line_type in ('khoan', 'diem', 'tiet'):
                line_type = 'text'
            # Trong table mode: bỏ qua text thường (form fields, table rows)
            if self.inside_table and line_type == 'text':
                continue
            # ─────────────────────────────────────────────────────────────

            # Handle based on type
            if line_type == 'phu_luc':
                self._handle_phu_luc(index, node_content)

            elif line_type == 'phan':
                self._handle_phan(index, node_content)

            elif line_type == 'chuong':
                self._handle_chuong(index, node_content)

            elif line_type == 'muc':
                self._handle_muc(index, node_content)

            elif line_type == 'dieu':
                self._handle_dieu(index, node_content)

            elif line_type == 'khoan':
                self._handle_khoan(index, node_content)

            elif line_type == 'diem':
                self._handle_diem(index, node_content)

            elif line_type == 'tiet':
                self._handle_tiet(index, node_content)

            elif line_type == 'text':
                self._handle_text(line)
        
        # Finalize: Close any open nodes
        self._finalize_parsing()
        
        # Build document
        from datetime import date

        document = LegalDocument(
            document_id=self.document_id,
            document_type=self.document_type,  # ✅ FIX ①
            document_number=self.document_number,
            title=f"{self.document_type} {self.document_number}",  # ✅ FIX ②
            issue_date=date.today(),
            effective_date=date.today(),
            structure=self.root_nodes
        )
        
        return document
    
    # ========================================
    # NODE HANDLERS
    # ========================================
    
    def _handle_phan(self, index: str, content: str):
        """
        Handle Phần line
        
        Logic:
            1. Create Phần node
            2. Push to state
            3. Add to root_nodes
        """
# ✅ MỚI
        phan_node = self.node_builder.create_node(
            node_type="Phần",
            node_index=index,
            title=content,
            parent_breadcrumb=f"{self.document_type} {self.document_number}"  # ✅
        )
        
        self.state.push(phan_node, 'phan')
        self.root_nodes.append(phan_node)
    
    def _handle_phu_luc(self, index: str, content: str):
        """
        Handle Phụ lục line — tạo root node riêng biệt.

        Phụ lục KHÔNG phải nội dung của Điều/Khoản nào.
        Reset toàn bộ state để ngăn nội dung phụ lục bị
        gán vào node cuối cùng đang mở (VD: Điều 13).
        """
        phu_luc_node = self.node_builder.create_node(
            node_type="Phụ lục",
            node_index=index,
            title=content or f"Phụ lục {index}",
            parent_breadcrumb=f"{self.document_type} {self.document_number}"
        )

        self.state.push(phu_luc_node, 'phu_luc')
        self.root_nodes.append(phu_luc_node)
        logger.info(f"📎 Phụ lục {index} → root node")

    def _handle_chuong(self, index: str, content: str):
        """
        Handle Chương line
        
        Logic:
            1. Create Chương node
            2. Add to Phần (if exists) or root
            3. Push to state
        """

        parent_breadcrumb = f"{self.document_type} {self.document_number}"  # ✅
        if self.state.current_phan:
            parent_breadcrumb = self.state.current_phan.breadcrumb
        
        chuong_node = self.node_builder.create_node(
            node_type="Chương",
            node_index=index,
            title=content,
            parent_breadcrumb=parent_breadcrumb
        )
        
        # Add to parent (Phần or root)
        if self.state.current_phan:
            self.state.current_phan.add_child(chuong_node)
        else:
            self.root_nodes.append(chuong_node)
        
        self.state.push(chuong_node, 'chuong')
    
    def _handle_muc(self, index: str, content: str):
        """
        Handle Mục line
        
        Logic:
            1. Create Mục node
            2. Add to Chương
            3. Push to state
        """
        if not self.state.current_chuong:
            return
        
        muc_node = self.node_builder.create_node(
            node_type="Mục",
            node_index=index,
            title=content,
            parent_breadcrumb=self.state.current_chuong.breadcrumb
        )
        
        self.state.current_chuong.add_child(muc_node)
        self.state.push(muc_node, 'muc')
    
    def _handle_dieu(self, index: str, content: str):
        """
        Handle Điều line
        
        Logic:
            1. Create Điều node
            2. Add to parent (Mục > Chương > Phần > root)
            3. Push to state
        """
        # Determine parent
        # ✅ MỚI
        parent = None
        parent_breadcrumb = f"{self.document_type} {self.document_number}"  # ✅
        
        if self.state.current_muc:
            parent = self.state.current_muc
            parent_breadcrumb = parent.breadcrumb
        elif self.state.current_chuong:
            parent = self.state.current_chuong
            parent_breadcrumb = parent.breadcrumb
        elif self.state.current_phan:
            parent = self.state.current_phan
            parent_breadcrumb = parent.breadcrumb
        
        # Create Điều node
        dieu_node = self.node_builder.create_node(
            node_type="Điều",
            node_index=index,
            title=content,
            parent_breadcrumb=parent_breadcrumb
        )
        
        # Add to parent or root
        if parent:
            parent.add_child(dieu_node)
        else:
            self.root_nodes.append(dieu_node)
        
        # Push to state
        self.state.push(dieu_node, 'dieu')
    
    def _handle_khoan(self, index: str, content: str):
        """
        Handle Khoản line
        
        Logic:
            1. Create Khoản node
            2. Add to current Điều
            3. Push to state
        """
        # ✅ MỚI
        if not self.state.current_dieu:
            logger.warning(
                f"⚠️  Khoản {index} xuất hiện nhưng không có Điều nào đang mở — bỏ qua. "
                f"Content: '{content[:50]}...'"
            )
            return
        
        # Create Khoản node
        khoan_node = self.node_builder.create_node(
            node_type="Khoản",
            node_index=index,
            content=content,
            parent_breadcrumb=self.state.current_dieu.breadcrumb
        )

        # Đảm bảo node_id duy nhất (VD: table row tạo Khoản trùng index)
        self._ensure_unique_node_id(khoan_node, self.state.current_dieu.children)

        # Add to Điều
        self.state.current_dieu.add_child(khoan_node)

        # Push to state
        self.state.push(khoan_node, 'khoan')
    
    def _handle_diem(self, index: str, content: str):
        """
        Handle Điểm line

        Logic:
            1. Create Điểm node
            2. Add to current Khoản
            3. Push to state
        """
        if not self.state.current_khoan:
            logger.warning(
                f"⚠️  Điểm {index} xuất hiện nhưng không có Khoản nào đang mở — bỏ qua. "
                f"Content: '{content[:50]}...'"
            )
            return

        # Create Điểm node
        diem_node = self.node_builder.create_node(
            node_type="Điểm",
            node_index=index,
            content=content,
            parent_breadcrumb=self.state.current_khoan.breadcrumb
        )

        # Đảm bảo node_id duy nhất (VD: Khoản có 2 nhóm Điểm a, b)
        self._ensure_unique_node_id(diem_node, self.state.current_khoan.children)

        # Add to Khoản
        self.state.current_khoan.add_child(diem_node)

        # Push to state
        self.state.push(diem_node, 'diem')

    def _handle_tiet(self, index: str, content: str):
        """
        Handle Tiết line (if exists)

        Logic:
            1. Create Tiết node
            2. Add to current Điểm
            3. Push to state
        """
        if not self.state.current_diem:
            # Tiết without Điểm - skip
            return

        # Create Tiết node
        tiet_node = self.node_builder.create_node(
            node_type="Tiết",
            node_index=index,
            content=content,
            parent_breadcrumb=self.state.current_diem.breadcrumb
        )

        # Đảm bảo node_id duy nhất
        self._ensure_unique_node_id(tiet_node, self.state.current_diem.children)

        # Add to Điểm
        self.state.current_diem.add_child(tiet_node)

        # Push to state
        self.state.push(tiet_node, 'tiet')

    def _ensure_unique_node_id(self, node: 'LegalNode', siblings: list) -> None:
        """
        Đảm bảo node_id không trùng với các siblings hiện có.

        Trường hợp xảy ra: Khoản có 2 nhóm Điểm a, b độc lập
        (VD: nhóm tỷ lệ thuế và nhóm khai thuế đều bắt đầu từ a)).

        Giải pháp: thêm hậu tố _2, _3... vào node_id (giữ nguyên
        node_index để breadcrumb vẫn hiển thị đúng).
        """
        existing_ids = {s.node_id for s in siblings}
        if node.node_id not in existing_ids:
            return

        # Đếm bao nhiêu sibling có cùng node_index
        count = sum(1 for s in siblings if s.node_index == node.node_index) + 1
        node.node_id = f"{node.node_id}_{count}"
        logger.debug(
            f"🔁 Duplicate node_index '{node.node_index}' — "
            f"đã đổi node_id thành '{node.node_id}'"
        )
    
    # ── Content noise patterns (case-sensitive: uppercase = form template) ──
    # Dùng không có re.IGNORECASE để tránh false positive với lowercase text.
    # "HỘ, CÁ NHÂN KINH..." (all caps) = form template header
    # "hộ kinh doanh, cá nhân kinh doanh" (lowercase) = legal text
    _FORM_NOISE_RE = re.compile(
        r'HỘ,\s*CÁ\s+NHÂN\s+KINH'   # merged-column form header (all caps)
        r'|SỔ\s+DOANH\s+THU\s+BÁN\s+HÀNG'  # standalone form title (all caps)
    )
    # Phần hành chính cuối văn bản (không phải nội dung luật)
    _ADMIN_SECTION_RE = re.compile(r'Nơi\s+nhận\s*:', re.IGNORECASE)

    def _clean_content(self, text: str) -> str:
        """
        Post-process nội dung node:
        1. Normalize PDF line-break: \\n → space
        2. Truncate tại form template block (biểu mẫu S1a-HKD, ...)
        3. Truncate tại phần hành chính "Nơi nhận:"
        """
        if not text:
            return text

        # Bước 3: Truncate tại "Nơi nhận:" (phần hành chính)
        m = self._ADMIN_SECTION_RE.search(text)
        if m:
            text = text[:m.start()].rstrip(' \n\t,./.')

        # Bước 2: Truncate tại form template block (case-sensitive uppercase)
        m = self._FORM_NOISE_RE.search(text)
        if m:
            text = text[:m.start()].rstrip(' \n\t,.:')

        # Bước 1: Normalize PDF line-breaks → space (giữ lại newline thật nếu có)
        text = re.sub(r'\n', ' ', text)
        text = re.sub(r'  +', ' ', text)  # collapse multiple spaces

        return text.strip()

    # ── Table form template detection ─────────────────────────────────────
    _FORM_TEMPLATE_RE = re.compile(
        r"""
        HỘ,?\s*CÁ\s*NHÂN\s*KINH\s*DOANH\s*:  # biểu mẫu form header
        | Mẫu\s+số\s+S\d+[a-z]-HKD            # mẫu số S1a-HKD, S2b-HKD, ...
        | SỔ\s+(?:DOANH\s+THU|CHI\s+TIẾT      # tiêu đề sổ kế toán
                  |THEO\s+DÕI|NHẬT\s+KÝ)
        | Địa\s+điểm\s+kinh\s+doanh\s*:       # header form
        | Kỳ\s+kê\s+khai\s*:                  # header form
        | Đơn\s+vị\s+tính\s*:                 # header form
        """,
        re.IGNORECASE | re.VERBOSE
    )

    def _is_form_template_line(self, line: str) -> bool:
        """
        Kiểm tra dòng có phải là header biểu mẫu/bảng biểu không.

        Khi True → đặt self.inside_table = True để ngăn các dòng số
        trong bảng biểu bị nhầm thành Khoản/Điểm.

        Patterns được nhận diện:
        - "HỘ, CÁ NHÂN KINH DOANH:........"
        - "Mẫu số S1a-HKD", "Mẫu số S2b-HKD"
        - "SỔ DOANH THU BÁN HÀNG HÓA, DỊCH VỤ"
        - "Địa điểm kinh doanh:...", "Kỳ kê khai:...", "Đơn vị tính:"
        """
        return bool(self._FORM_TEMPLATE_RE.match(line.strip()))

    def _handle_text(self, line: str):
        """
        Handle text line (not numbered)
        
        Logic:
            1. Classify text (lead_in/trailing/continuation)
            2. Append to appropriate field
        
        Classification:
            - lead_in: Text before children (Khoản has text, then Điểm a, b, c)
            - trailing: Text after children (Điểm a, b, c, then text)
            - continuation: Text continues current node content
        """
        current_node = self.state.get_current_node()
        
        if not current_node:
            # No current node - skip
            return
        
        # Build context for classification
        context = {
            'current_level': self.state.get_current_level(),
            'node_indent': self.state.get_node_indent(),
            'has_children': self.state.has_children()
        }
        
        # Classify text
        text_type = self.indent_checker.classify_text_block(line, context)
        
        # Append to appropriate field
        if text_type == 'lead_in':
            # Lead-in text (before children)
            if current_node.lead_in_text:
                current_node.lead_in_text += "\n" + line.strip()
            else:
                current_node.lead_in_text = line.strip()
        
        elif text_type == 'trailing':
            # Trailing text (after children)
            if current_node.trailing_text:
                current_node.trailing_text += "\n" + line.strip()
            else:
                current_node.trailing_text = line.strip()
        
        elif text_type == 'continuation':
            # Continuation (extend content)
            if current_node.content:
                current_node.content += "\n" + line.strip()
            else:
                current_node.content = line.strip()
    
    def _finalize_parsing(self):
        """
        Finalize parsing - Post-processing

        Tasks:
            0. Fix Điều title continuations (PDF line-wrap → lead_in nhầm)
            1. Merge content + lead_in + trailing cho leaf nodes (không có children)
            2. Build dieu_index (two-pass) để resolve reference target_id đúng
            3. Detect references in all nodes với dieu_index
        """
        # Pass 0: fix Điều title bị cắt ngang do PDF line-wrap
        # Khi Điều title quá dài → PDF wrap → dòng tiếp theo vào lead_in_text
        # Heuristic: lead_in của Điều KHÔNG kết thúc bằng ':' → là title continuation
        # (Lead-in thực sự luôn kết thúc bằng ':' như "Nghị định này quy định:")
        self._fix_dieu_title_continuations(self.root_nodes)

        # Pass 1: merge text fields cho leaf nodes
        # Leaf node = không có children → content, lead_in, trailing đều là
        # phần của cùng 1 câu/đoạn, cần nối liền để embedding không bị cụt câu
        self._merge_leaf_content(self.root_nodes)

        # Pass 2: build dieu_index {dieu_number → full_node_id}
        dieu_index: dict = {}
        self._collect_dieu_index(self.root_nodes, dieu_index)

        # Pass 3: detect references, dùng dieu_index để resolve đúng path
        self._detect_all_references(self.root_nodes, dieu_index)

    def _fix_dieu_title_continuations(self, nodes: List[LegalNode]) -> None:
        """
        Sửa lỗi title Điều bị cắt ngang do PDF line-wrap.

        Vấn đề: Khi title Điều dài, PDF xuống dòng → dòng tiếp theo của title
        bị parser gán vào lead_in_text (vì đây là text đầu tiên trước Khoản con).

        Heuristic phân biệt:
        - Lead-in thực sự (intro trước danh sách Khoản): luôn kết thúc bằng ':'
          VD: "Nghị định này quy định:"
        - Title continuation (PDF line-wrap): KHÔNG kết thúc bằng ':'
          VD: "phải khấu trừ", "thương mại điện tử thuộc đối tượng..."

        Fix: nếu Điều.lead_in_text không kết thúc bằng ':', nối vào title
        và cập nhật breadcrumb của Điều + toàn bộ descendants.
        """
        for node in nodes:
            # Chỉ áp dụng fix cho Điều CÓ children (Khoản/Điểm).
            # Điều không có children (VD: Điều điều khoản cuối như "Hiệu lực thi hành")
            # có lead_in_text là body content thực sự, không phải title continuation.
            if node.node_type == "Điều" and node.lead_in_text and node.children:
                lead_in = node.lead_in_text.strip()
                if lead_in and not lead_in.endswith(':'):
                    # Build old label để thay thế trong breadcrumb descendants
                    old_label = f"Điều {node.node_index}: {node.title}" \
                        if node.title else f"Điều {node.node_index}"

                    # Normalize \n in lead_in (PDF line-wrap tạo ra nhiều dòng
                    # trong lead_in_text, tất cả đều là continuation của title)
                    lead_in_clean = re.sub(r'\s*\n\s*', ' ', lead_in).strip()

                    # Append lead_in vào title
                    if node.title:
                        node.title = (node.title.strip() + " " + lead_in_clean).strip()
                    else:
                        node.title = lead_in_clean
                    node.lead_in_text = ""

                    # Cập nhật breadcrumb của chính Điều này
                    new_label = f"Điều {node.node_index}: {node.title}"
                    if old_label in node.breadcrumb:
                        node.breadcrumb = node.breadcrumb.replace(
                            old_label, new_label, 1
                        )

                    # Cập nhật breadcrumb của tất cả descendants
                    if node.children:
                        self._update_descendant_breadcrumbs(
                            node.children, old_label, new_label
                        )

                    logger.debug(
                        f"📝 Fixed Điều {node.node_index} title: "
                        f"appended '{lead_in[:40]}...'"
                    )

            # Đệ quy vào children (Khoản/Điểm không có lead_in title nhưng
            # cần xử lý các Điều lồng nhau nếu có — defensive)
            if node.children:
                self._fix_dieu_title_continuations(node.children)

    def _update_descendant_breadcrumbs(
        self,
        nodes: List[LegalNode],
        old_label: str,
        new_label: str
    ) -> None:
        """
        Cập nhật đệ quy breadcrumb của tất cả descendants khi label cha thay đổi.

        Args:
            nodes: Danh sách nodes cần cập nhật
            old_label: Label cũ (VD: "Điều 5: Thời điểm...ngắn")
            new_label: Label mới (VD: "Điều 5: Thời điểm...đầy đủ")
        """
        for node in nodes:
            if old_label in node.breadcrumb:
                node.breadcrumb = node.breadcrumb.replace(old_label, new_label, 1)
            if node.children:
                self._update_descendant_breadcrumbs(node.children, old_label, new_label)

    def _merge_leaf_content(self, nodes: List[LegalNode]) -> None:
        """
        Với mỗi leaf node (không có children), merge:
            content + lead_in_text + trailing_text → content

        Mục đích: embedding nhận được câu/đoạn đầy đủ thay vì câu cụt.

        Với parent node (có children), giữ nguyên lead_in_text vì đó là
        văn bản dẫn nhập trước danh sách con — có giá trị ngữ cảnh riêng.
        """
        for node in nodes:
            if node.children:
                # Với parent node: nếu content bị cắt giữa chừng (không kết thúc bằng
                # dấu câu) và có lead_in_text → nối content vào lead_in_text để tạo câu
                # hoàn chỉnh. Xảy ra khi PDF wrap dòng giữa chừng trong Khoản/Điểm.
                if node.content and node.lead_in_text:
                    stripped = node.content.strip()
                    if stripped and stripped[-1] not in '.?!:;':
                        node.lead_in_text = stripped + " " + node.lead_in_text.strip()
                        node.content = ""
                        logger.debug(
                            f"🔗 Merged partial content into lead_in for "
                            f"{node.node_id} ({len(stripped)} chars)"
                        )
                # Recurse — xử lý children trước
                self._merge_leaf_content(node.children)
            else:
                # Leaf node: gộp tất cả text parts
                parts = []
                if node.content:
                    parts.append(node.content.strip())
                if node.lead_in_text:
                    parts.append(node.lead_in_text.strip())
                if node.trailing_text:
                    parts.append(node.trailing_text.strip())

                if len(parts) > 1:
                    # Nối bằng space — đây là cùng 1 đoạn văn bị cắt qua dòng
                    node.content = " ".join(parts)
                    node.lead_in_text = ""
                    node.trailing_text = ""
                    logger.debug(
                        f"🔗 Merged leaf content for {node.node_id}: "
                        f"{len(parts)} parts → {len(node.content)} chars"
                    )

            # Post-processing on all nodes: normalize + clean content
            if node.content:
                node.content = self._clean_content(node.content)
            if node.lead_in_text:
                node.lead_in_text = self._clean_content(node.lead_in_text)

    def _collect_dieu_index(self, nodes: List[LegalNode], index: dict) -> None:
        """
        Duyệt đệ quy toàn bộ cây, thu thập tất cả Điều nodes.

        Kết quả: index["4"] = "doc_117_2025_NDCP_chuong_II_dieu_4"
        """
        for node in nodes:
            if node.node_type == "Điều":
                index[node.node_index] = node.node_id
            if node.children:
                self._collect_dieu_index(node.children, index)

    def _detect_all_references(
        self,
        nodes: List[LegalNode],
        dieu_index: dict = None,
        current_dieu_number: str = None,
        current_khoan_id: str = None,
    ):
        """
        Recursively detect references in all nodes.

        Args:
            nodes: List of nodes to process
            dieu_index: {dieu_number → full_node_id} để generate đúng target_id
            current_dieu_number: số Điều đang xử lý (truyền xuống children)
            current_khoan_id: full node_id của Khoản đang xử lý (cho "khoản này")
        """
        for node in nodes:
            # Cập nhật context khi vào node mới
            dieu_num = current_dieu_number
            khoan_id = current_khoan_id
            if node.node_type == "Điều":
                dieu_num = node.node_index
                khoan_id = None  # reset khi vào Điều mới
            elif node.node_type == "Khoản":
                khoan_id = node.node_id

            # Dùng space thay vì \n để tránh reference pattern span across newline
            text_to_search = " ".join(filter(None, [
                node.content,
                node.lead_in_text,
                node.trailing_text
            ]))

            if text_to_search:
                refs = self.ref_detector.find_references(
                    text_to_search,
                    current_dieu=dieu_num
                )

                seen_refs: set = set()
                for ref_match in refs:
                    # Bug 4: bỏ qua self-reference ("Điều này", "khoản này", "điểm này")
                    # — chúng không bổ sung thông tin cho reference graph
                    if ref_match.ref_type == "self":
                        continue

                    target_id = ref_match.generate_target_id(
                        document_id=self.document_id,
                        current_dieu=dieu_num,
                        dieu_index=dieu_index
                    )

                    # Bug 3: dedup (text_match, target_id) trùng trong cùng node
                    ref_key = (ref_match.text_match, target_id)
                    if ref_key in seen_refs:
                        continue
                    seen_refs.add(ref_key)

                    node.add_reference(LegalReference(
                        text_match=ref_match.text_match,
                        target_id=target_id
                    ))

            if node.children:
                self._detect_all_references(
                    node.children, dieu_index,
                    current_dieu_number=dieu_num,
                    current_khoan_id=khoan_id,
                )

    def _resolve_self_target(
        self,
        text_match: str,
        node: "LegalNode",
        dieu_number: str,
        khoan_id: str,
        dieu_index: dict,
    ) -> str:
        """
        Resolve self-reference ("Điều này", "khoản này", "điểm này")
        sang node_id chính xác dựa trên context hiện tại.

        text_match  → target
        "Điều này"  → node_id của Điều đang xử lý
        "khoản này" → node_id của Khoản đang xử lý
        "điểm này"  → node_id của node Điểm hiện tại
        """
        txt = text_match.lower()
        if 'điều' in txt:
            if dieu_number and dieu_index and dieu_number in dieu_index:
                return dieu_index[dieu_number]
            if dieu_number:
                return f"doc_{self.document_id}_dieu_{dieu_number}"
        elif 'khoản' in txt:
            if khoan_id:
                return khoan_id
            if dieu_number and dieu_index and dieu_number in dieu_index:
                return dieu_index[dieu_number]
        elif 'điểm' in txt:
            if node.node_type == "Điểm":
                return node.node_id
        return f"doc_{self.document_id}_self"


# ============================================
# CONVENIENCE FUNCTION
# ============================================

def parse_legal_document(
    text: str,
    document_id: str,
    document_number: str,
    output_path: Optional[Path] = None
) -> LegalDocument:
    """
    Parse a legal document text into structured JSON
    
    Args:
        text: Full document text (cleaned)
        document_id: Document ID (e.g., "109_2025_QH15")
        document_number: Document number (e.g., "109/2025/QH15")
        output_path: Optional path to save JSON output
    
    Returns:
        LegalDocument object
    
    Example:
        >>> text = open('law.txt', 'r', encoding='utf-8').read()
        >>> doc = parse_legal_document(
        ...     text=text,
        ...     document_id="109_2025_QH15",
        ...     document_number="109/2025/QH15",
        ...     output_path=Path("output/109_2025_QH15.json")
        ... )
        >>> print(f"Parsed {len(doc.structure)} articles")
    """
    # Create parser
    parser = StateMachineParser(
        document_id=document_id,
        document_number=document_number
    )
    
    # Parse
    document = parser.parse_text(text)
    
    # Save if output path provided
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(document.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"✅ Saved to: {output_path}")
    
    return document


# ============================================
# UNIT TEST
# ============================================

if __name__ == "__main__":
    # Test with sample text
    sample_text = """
Điều 5. Các trường hợp miễn thuế, giảm thuế khác

1. Người nộp thuế gặp khó khăn do thiên tai, dịch bệnh, hỏa hoạn, tai nạn, 
bệnh hiểm nghèo ảnh hưởng đến khả năng nộp thuế thì được giảm thuế tương 
ứng với mức độ thiệt hại nhưng không vượt quá số thuế phải nộp.

2. Miễn thuế thu nhập cá nhân trong thời hạn 05 năm đối với thu nhập từ 
tiền lương, tiền công của cá nhân là nhân lực công nghiệp công nghệ số chất 
lượng cao thuộc các trường hợp sau:

   a) Thu nhập từ dự án hoạt động công nghiệp công nghệ số trong khu công 
   nghệ số tập trung;
   
   b) Thu nhập từ dự án nghiên cứu và phát triển, sản xuất sản phẩm công 
   nghệ số trọng điểm, chip bán dẫn, hệ thống trí tuệ nhân tạo;
   
   c) Thu nhập từ các hoạt động đào tạo nhân lực công nghiệp công nghệ số.

3. Miễn thuế thu nhập cá nhân trong thời hạn 05 năm đối với thu nhập từ 
tiền lương, tiền công của cá nhân là nhân lực công nghệ cao thực hiện hoạt 
động nghiên cứu và phát triển công nghệ cao hoặc công nghệ chiến lược thuộc 
Danh mục công nghệ cao được ưu tiên đầu tư phát triển hoặc Danh mục công nghệ 
chiến lược theo quy định của pháp luật về công nghệ cao.

4. Chính phủ quy định chi tiết Điều này.
"""
    
    print("=" * 70)
    print("TEST: State Machine Parser")
    print("=" * 70)
    
    # Parse
    doc = parse_legal_document(
        text=sample_text,
        document_id="109_2025_QH15",
        document_number="109/2025/QH15"
    )
    
    # Print results
    print(f"\n✅ Parsed document:")
    print(f"   Articles: {len(doc.structure)}")
    
    for article in doc.structure:
        print(f"\n📄 {article.node_type} {article.node_index}: {article.title}")
        print(f"   Breadcrumb: {article.breadcrumb}")
        print(f"   Clauses: {len(article.children)}")
        
        for khoan in article.children:
            print(f"\n   📌 Khoản {khoan.node_index}:")
            print(f"      Content: {khoan.content[:80]}...")
            print(f"      Points: {len(khoan.children)}")
            
            if khoan.lead_in_text:
                print(f"      Lead-in: {khoan.lead_in_text[:50]}...")
            
            if khoan.trailing_text:
                print(f"      Trailing: {khoan.trailing_text[:50]}...")
            
            for diem in khoan.children:
                print(f"      • Điểm {diem.node_index}: {diem.content[:60]}...")
    
    # Test JSON export
    print("\n" + "=" * 70)
    print("JSON Export (first article):")
    print("=" * 70)
    
    if doc.structure:
        first_article = doc.structure[0]
        print(json.dumps(first_article.to_dict(), ensure_ascii=False, indent=2))
    
    print("\n" + "=" * 70)
    print("✅ Test completed!")
    print("=" * 70)