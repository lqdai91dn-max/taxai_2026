"""
Node Builder - Cấu trúc dữ liệu cốt lõi cho văn bản pháp luật

Thiết kế dựa trên research:
- Cấu trúc cây đệ quy (recursive tree)
- Hỗ trợ lead_in_text và trailing_text
- Tự động tạo breadcrumb cho RAG
- Phát hiện và lưu references

Author: TaxAI Team
Version: 3.1 - Regex Parsing + Document Type Support
"""

from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import date
import re


@dataclass
class LegalReference:
    """
    Tham chiếu nội bộ/ngoại bộ
    
    VD: "theo quy định tại Khoản 2 Điều 7" → Reference
    """
    text_match: str  # Chuỗi gốc: "Khoản 2 Điều 7"
    target_id: str   # ID mục tiêu: "doc_109_dieu_7_khoan_2"
    
    def to_dict(self) -> Dict[str, str]:
        return {
            "text_match": self.text_match,
            "target_id": self.target_id
        }


@dataclass
class LegalNode:
    """
    Node đệ quy đại diện cho một phần tử trong văn bản pháp luật
    
    Hierarchy:
        Phần > Chương > Mục > Điều > Khoản > Điểm > Tiết
    
    Attributes:
        node_id: ID duy nhất (VD: "doc_109_dieu_5_khoan_2_diem_a")
        node_type: Loại node ("Phần", "Chương", "Điều", "Khoản", "Điểm", "Tiết")
        node_index: Chỉ số (VD: "5", "2", "a", "1")
        title: Tiêu đề (chỉ có ở Phần/Chương/Điều)
        content: Nội dung chính
        lead_in_text: Văn bản dẫn nhập (trước các node con)
        trailing_text: Văn bản kết (sau các node con)
        breadcrumb: Đường dẫn ngữ cảnh cho RAG
        references: Danh sách tham chiếu
        children: Danh sách node con (đệ quy)
    """
    
    node_id: str
    node_type: str  # "Phần" | "Chương" | "Mục" | "Điều" | "Khoản" | "Điểm" | "Tiết"
    node_index: str
    title: Optional[str] = None
    content: str = ""
    lead_in_text: str = ""  # ✅ NEW: Văn bản dẫn nhập
    trailing_text: str = ""  # ✅ NEW: Văn bản kết
    breadcrumb: str = ""
    references: List[LegalReference] = field(default_factory=list)
    children: List['LegalNode'] = field(default_factory=list)
    
    # Metadata bổ sung
    formulas: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    
    def add_child(self, child: 'LegalNode') -> None:
        """Thêm node con"""
        self.children.append(child)
    
    def add_reference(self, ref: LegalReference) -> None:
        """Thêm tham chiếu"""
        self.references.append(ref)
    
    def set_breadcrumb(self, parent_breadcrumb: str) -> None:
        """
        Tạo breadcrumb dựa trên breadcrumb của parent
        
        VD: 
            Parent: "Luật 109/2025/QH15 > Chương I"
            Current: Điều 5
            Result: "Luật 109/2025/QH15 > Chương I > Điều 5"
        """
        if self.title:
            # Node có tiêu đề (Phần, Chương, Điều)
            label = f"{self.node_type} {self.node_index}: {self.title}"
        else:
            # Node không có tiêu đề (Khoản, Điểm, Tiết)
            label = f"{self.node_type} {self.node_index}"
        
        if parent_breadcrumb:
            self.breadcrumb = f"{parent_breadcrumb} > {label}"
        else:
            self.breadcrumb = label
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to JSON dict theo chuẩn schema
        
        Returns:
            Dict có cấu trúc:
            {
                "node_id": "doc_109_dieu_5_khoan_2",
                "node_type": "Khoản",
                "node_index": "2",
                "title": null,
                "content": "Miễn thuế...",
                "lead_in_text": "",
                "trailing_text": "",
                "breadcrumb": "Luật 109/2025/QH15 > Chương I > Điều 5 > Khoản 2",
                "references": [...],
                "children": [...]
            }
        """
        result = {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "node_index": self.node_index,
            "title": self.title,
            "content": self.content,
            "breadcrumb": self.breadcrumb,
            "references": [ref.to_dict() for ref in self.references],
            "children": [child.to_dict() for child in self.children]
        }
        
        # ✅ Chỉ thêm lead_in_text/trailing_text nếu có
        if self.lead_in_text:
            result["lead_in_text"] = self.lead_in_text
        
        if self.trailing_text:
            result["trailing_text"] = self.trailing_text
        
        # ✅ Chỉ thêm formulas/tables nếu có
        if self.formulas:
            result["formulas"] = self.formulas
        
        if self.tables:
            result["tables"] = self.tables
        
        return result


@dataclass
class LegalDocument:
    """
    Văn bản pháp luật hoàn chỉnh
    
    Attributes:
        document_id: ID văn bản (VD: "109_2025_QH15")
        document_type: Loại văn bản ("Luật", "Nghị định", "Thông tư", etc)
        document_number: Số hiệu (VD: "109/2025/QH15")
        title: Tiêu đề
        issue_date: Ngày ban hành
        effective_date: Ngày hiệu lực
        structure: Cây cấu trúc (root nodes)
    """
    
    document_id: str
    document_type: str
    document_number: str
    title: str
    issue_date: date
    effective_date: date
    structure: List[LegalNode] = field(default_factory=list)
    
    def add_root_node(self, node: LegalNode) -> None:
        """Thêm node gốc (Phần/Chương/Điều)"""
        self.structure.append(node)
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to final JSON output
        
        Returns:
            {
                "metadata": {...},
                "data": [...]
            }
        """
        return {
            "metadata": {
                "document_id": self.document_id,
                "document_type": self.document_type,
                "document_number": self.document_number,
                "title": self.title,
                "issue_date": self.issue_date.isoformat(),
                "effective_date": self.effective_date.isoformat()
            },
            "data": [node.to_dict() for node in self.structure]
        }


class NodeBuilder:
    """
    Factory class để tạo các LegalNode
    
    Sử dụng:
        builder = NodeBuilder(
            document_id="109_2025_QH15",
            document_number="109/2025/QH15",
            document_type="Luật"
        )
        dieu_5 = builder.create_node(
            node_type="Điều",
            node_index="5",
            title="Các trường hợp miễn thuế, giảm thuế khác",
            parent_breadcrumb="Luật 109/2025/QH15 > Chương I"
        )
    """
    
    def __init__(
        self, 
        document_id: str, 
        document_number: str,
        document_type: str = "Luật"  # ✅ FIX LỖI 4: Thêm document_type
    ):
        self.document_id = document_id
        self.document_number = document_number
        self.document_type = document_type  # ✅ FIX LỖI 4
    
    def create_node(
        self,
        node_type: str,
        node_index: str,
        title: Optional[str] = None,
        content: str = "",
        parent_breadcrumb: str = ""
    ) -> LegalNode:
        """
        Tạo một LegalNode mới
        
        Args:
            node_type: "Phần" | "Chương" | "Mục" | "Điều" | "Khoản" | "Điểm" | "Tiết"
            node_index: Chỉ số (VD: "5", "2", "a")
            title: Tiêu đề (optional)
            content: Nội dung
            parent_breadcrumb: Breadcrumb của parent
        
        Returns:
            LegalNode mới với node_id tự động
        """
        # Generate node_id
        node_id = self._generate_node_id(
            node_type, 
            node_index, 
            parent_breadcrumb
        )
        
        # Create node
        node = LegalNode(
            node_id=node_id,
            node_type=node_type,
            node_index=node_index,
            title=title,
            content=content
        )
        
        # Set breadcrumb
        if parent_breadcrumb:
            node.set_breadcrumb(parent_breadcrumb)
        else:
            # ✅ FIX LỖI 4: Use document_type instead of hardcoded "Luật"
            node.set_breadcrumb(f"{self.document_type} {self.document_number}")
        
        return node
    
    def _generate_node_id(
        self, 
        node_type: str, 
        node_index: str,
        parent_breadcrumb: str
    ) -> str:
        """
        Tạo node_id duy nhất - Production Grade
        
        Logic:
            - Lấy các node_index từ breadcrumb bằng REGEX (an toàn)
            - Thêm node_index hiện tại
            - Format: doc_{doc_id}_dieu_5_khoan_2_diem_a
            
        ✅ FIX LỖI 2: Dùng regex thay vì split() để parse breadcrumb
        
        VD:
            parent_breadcrumb = "Luật 109/2025/QH15 > Điều 5 > Khoản 2"
            node_type = "Điểm"
            node_index = "a"
            → "doc_109_dieu_5_khoan_2_diem_a"
            
            parent_breadcrumb = "Nghị định 117 > Phụ lục 1 > Điều 1"
            node_type = "Khoản"  
            node_index = "1"
            → "doc_117_phu_luc_1_dieu_1_khoan_1"
        """
        # Extract indices from breadcrumb
        parts = [f"doc_{self.document_id}"]
        
        # ✅ Detect if in appendix/phụ lục
        is_appendix = False
        appendix_num = None
        
        if parent_breadcrumb:
            appendix_match = re.search(
                r'Phụ\s+lục\s+(\d+|[IVXLCDM]+)',
                parent_breadcrumb,
                re.IGNORECASE
            )
            
            if appendix_match:
                is_appendix = True
                appendix_num = appendix_match.group(1)
                parts.append(f"phu_luc_{appendix_num}")
        
        # Parse breadcrumb with REGEX để lấy hierarchy path
        if parent_breadcrumb:
            breadcrumb_parts = parent_breadcrumb.split(" > ")

            for part in breadcrumb_parts:
                # Skip phụ lục (already handled)
                if "Phụ lục" in part or "PHỤ LỤC" in part:
                    continue

                # Phần (VD: "Phần I", "Phần I: Quy định chung")
                phan_match = re.search(r'Phần\s+([IVXLCDM]+)', part, re.IGNORECASE)
                if phan_match:
                    parts.append(f"phan_{phan_match.group(1)}")
                    continue

                # Chương (VD: "Chương I", "Chương II: Thuế TNCN")
                chuong_match = re.search(r'Chương\s+([IVXLCDM]+)', part, re.IGNORECASE)
                if chuong_match:
                    parts.append(f"chuong_{chuong_match.group(1)}")
                    continue

                # Mục (VD: "Mục 1", "Mục 2: Thu nhập chịu thuế")
                muc_match = re.search(r'Mục\s+(\d+)', part, re.IGNORECASE)
                if muc_match:
                    parts.append(f"muc_{muc_match.group(1)}")
                    continue

                # Điều
                dieu_match = re.search(r'Điều\s+(\d+[a-z]?)', part, re.IGNORECASE)
                if dieu_match:
                    parts.append(f"dieu_{dieu_match.group(1)}")
                    continue

                # Khoản
                khoan_match = re.search(r'Khoản\s+(\d+[a-z]?)', part, re.IGNORECASE)
                if khoan_match:
                    parts.append(f"khoan_{khoan_match.group(1)}")
                    continue

                # Điểm
                diem_match = re.search(r'Điểm\s+([a-zđ]+)', part, re.IGNORECASE)
                if diem_match:
                    parts.append(f"diem_{diem_match.group(1)}")
                    continue

                # Tiết
                tiet_match = re.search(r'Tiết\s+(\d+[a-z]?)', part, re.IGNORECASE)
                if tiet_match:
                    parts.append(f"tiet_{tiet_match.group(1)}")
                    continue

        # Add current node
        type_map = {
            "Phần": "phan",
            "Chương": "chuong",
            "Mục": "muc",
            "Phụ lục": "phu_luc",
            "Điều": "dieu",
            "Khoản": "khoan",
            "Điểm": "diem",
            "Tiết": "tiet"
        }

        if node_type in type_map:
            parts.append(f"{type_map[node_type]}_{node_index}")
        
        return "_".join(parts)


# ============================================
# UNIT TEST - Kiểm tra logic
# ============================================

if __name__ == "__main__":
    # Test NodeBuilder
    builder = NodeBuilder(
        document_id="109_2025_QH15",
        document_number="109/2025/QH15",
        document_type="Luật"  # ✅ Added document_type
    )
    
    # Tạo Điều 5
    dieu_5 = builder.create_node(
        node_type="Điều",
        node_index="5",
        title="Các trường hợp miễn thuế, giảm thuế khác",
        parent_breadcrumb="Luật 109/2025/QH15 > Chương I"
    )
    
    print(f"✅ Created Điều 5:")
    print(f"   node_id: {dieu_5.node_id}")
    print(f"   breadcrumb: {dieu_5.breadcrumb}")
    
    # Tạo Khoản 2
    khoan_2 = builder.create_node(
        node_type="Khoản",
        node_index="2",
        content="Miễn thuế thu nhập cá nhân trong thời hạn 05 năm...",
        parent_breadcrumb=dieu_5.breadcrumb
    )
    
    dieu_5.add_child(khoan_2)
    
    print(f"\n✅ Created Khoản 2:")
    print(f"   node_id: {khoan_2.node_id}")
    print(f"   breadcrumb: {khoan_2.breadcrumb}")
    
    # Tạo Điểm a
    diem_a = builder.create_node(
        node_type="Điểm",
        node_index="a",
        content="Thu nhập từ dự án hoạt động công nghiệp công nghệ số...",
        parent_breadcrumb=khoan_2.breadcrumb
    )
    
    khoan_2.add_child(diem_a)
    
    print(f"\n✅ Created Điểm a:")
    print(f"   node_id: {diem_a.node_id}")
    print(f"   breadcrumb: {diem_a.breadcrumb}")
    
    # Test to_dict
    import json
    print("\n✅ JSON Output (Điều 5 with children):")
    print(json.dumps(dieu_5.to_dict(), indent=2, ensure_ascii=False))