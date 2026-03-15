"""
Patch Applier — Layer 2 của hệ thống 3-layer regression protection.

Mục đích:
    Áp dụng các correction đặc thù của từng document SAU khi parser chạy xong.
    Patch files tồn tại vĩnh viễn trong data/patches/ và được áp dụng lại
    mỗi lần re-parse → fix không bị mất khi cập nhật parser code.

Cách dùng:
    - Document-specific bug → tạo data/patches/{doc_id}.patch.json
    - General parser bug    → sửa parser code + chạy regression test
    - KHÔNG sửa trực tiếp JSON file (mất khi re-parse)

Patch file format (data/patches/{doc_id}.patch.json):
    {
        "version": 1,
        "doc_id": "373_2025_NDCP",
        "description": "Mô tả bug và cách fix",
        "ops": [
            {
                "op": "set_field",
                "node_id": "doc_..._khoan_7",
                "field": "content",
                "value": "Nội dung đúng..."
            },
            {
                "op": "remove_node",
                "node_id": "doc_..._phu_luc_II"
            },
            {
                "op": "add_reference",
                "node_id": "doc_..._khoan_7",
                "reference": {
                    "text_match": "Thông tư số 80/2021/TT-BTC",
                    "target_id": "external_thong_tu_80_2021_tt_btc"
                }
            }
        ]
    }

Supported operations (tất cả đều idempotent — an toàn khi apply nhiều lần):
    set_field      — gán giá trị cho một field của node
    remove_node    — xóa node khỏi cây (no-op nếu không tìm thấy)
    add_reference  — thêm reference vào node (no-op nếu đã tồn tại)
    add_node       — thêm node vào children của parent (no-op nếu đã là direct child)

Author: TaxAI Team
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

PATCHES_DIR = Path(__file__).parent.parent.parent / "data" / "patches"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

try:
    from ..utils.logger import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class PatchApplier:
    """
    Áp dụng patch files lên parsed document dict.

    Tất cả operations đều idempotent:
        - set_field: luôn ghi đè bằng value đúng
        - remove_node: no-op nếu node không tồn tại
        - add_reference: no-op nếu reference đã tồn tại
    """

    def apply(self, result: dict, doc_id: str, pdf_path: Optional[Path] = None) -> dict:
        """
        Tìm và áp dụng patch file cho doc_id.

        Args:
            result:   Dict kết quả parse (có key "data", "tables", ...)
            doc_id:   VD: "373_2025_NDCP"
            pdf_path: Path tới file PDF gốc — dùng để verify pdf_sha256 nếu patch có khai báo.

        Returns:
            result đã được patch (in-place + return)
        """
        patch_path = PATCHES_DIR / f"{doc_id}.patch.json"
        if not patch_path.exists():
            return result

        with open(patch_path, encoding="utf-8") as f:
            patch = json.load(f)

        # ── SHA256 verification ───────────────────────────────────────────────
        expected_hash = patch.get("pdf_sha256")
        if expected_hash:
            if pdf_path is None or not pdf_path.exists():
                logger.warning(
                    f"[{doc_id}] ⚠️  Patch khai báo pdf_sha256 nhưng không có pdf_path "
                    f"để verify — áp dụng patch mà không kiểm tra hash."
                )
            else:
                actual_hash = _sha256_of(pdf_path)
                if actual_hash != expected_hash:
                    logger.warning(
                        f"[{doc_id}] ⚠️  PDF SHA256 MISMATCH — patch được tạo cho PDF khác.\n"
                        f"  Expected: {expected_hash}\n"
                        f"  Actual:   {actual_hash}\n"
                        f"  Patch vẫn được áp dụng nhưng cần kiểm tra lại tính đúng đắn."
                    )
                else:
                    logger.info(f"[{doc_id}] ✅ PDF SHA256 verified")

        ops = patch.get("ops", [])
        applied = 0
        skipped = 0

        for op in ops:
            if self._apply_op(result, op):
                applied += 1
            else:
                skipped += 1

        logger.info(
            f"🩹 Patch {doc_id}: {applied} ops applied, {skipped} skipped (already correct)"
        )
        return result

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _apply_op(self, result: dict, op: dict) -> bool:
        """Dispatch op đến handler tương ứng. Returns True nếu có thay đổi."""
        op_type = op.get("op")
        nodes = result["data"]

        if op_type == "set_field":
            return self._op_set_field(nodes, op)
        elif op_type == "remove_node":
            return self._op_remove_node(nodes, op)
        elif op_type == "add_reference":
            return self._op_add_reference(nodes, op)
        elif op_type == "add_node":
            return self._op_add_node(nodes, op)
        elif op_type == "add_table":
            return self._op_add_table(result, op)
        else:
            logger.warning(f"⚠️  PatchApplier: unknown op '{op_type}' — skipped")
            return False

    # ── Operations ────────────────────────────────────────────────────────────

    def _op_set_field(self, nodes: list, op: dict) -> bool:
        """Gán op['value'] vào op['field'] của node có id op['node_id']."""
        node = self._find_node(nodes, op["node_id"])
        if node is None:
            return False
        if node.get(op["field"]) == op["value"]:
            return False  # Already correct, no change needed
        node[op["field"]] = op["value"]
        return True

    def _op_remove_node(self, nodes: list, op: dict) -> bool:
        """Xóa node có id op['node_id'] khỏi cây. No-op nếu không tìm thấy."""
        return self._remove_from_tree(nodes, op["node_id"])

    def _op_add_reference(self, nodes: list, op: dict) -> bool:
        """
        Thêm reference vào node. No-op nếu reference (cùng text_match) đã tồn tại.
        """
        node = self._find_node(nodes, op["node_id"])
        if node is None:
            return False
        refs = node.setdefault("references", [])
        new_ref = op["reference"]
        if any(r.get("text_match") == new_ref["text_match"] for r in refs):
            return False  # Already present
        refs.append(new_ref)
        return True

    def _op_add_node(self, nodes: list, op: dict) -> bool:
        """
        Thêm node vào children của parent_id.
        Idempotent: no-op nếu node_id đã là direct child của parent.

        Op format:
            {
                "op": "add_node",
                "parent_id": "doc_..._dieu_10",
                "after": "doc_..._khoan_7",   # optional — insert after this sibling
                "node": {
                    "node_id": "...",
                    "node_type": "khoản",
                    "content": "...",
                    "children": []
                }
            }

        Use case: trong regression scenario, false node steal valid children.
        add_node đặt node đúng vị trí trước khi remove_node xóa false node.
        """
        parent = self._find_node(nodes, op["parent_id"])
        if parent is None:
            logger.warning(f"⚠️  add_node: parent '{op['parent_id']}' not found — skipped")
            return False

        new_node = op["node"]
        new_node_id = new_node["node_id"]
        children = parent.setdefault("children", [])

        # Idempotent: no-op nếu đã là direct child của parent này
        if any(c.get("node_id") == new_node_id for c in children):
            return False

        # Tìm vị trí chèn: sau node `after` nếu có, ngược lại append cuối
        after_id = op.get("after")
        if after_id:
            insert_idx = next(
                (i + 1 for i, c in enumerate(children) if c.get("node_id") == after_id),
                len(children),
            )
        else:
            insert_idx = len(children)

        children.insert(insert_idx, new_node)
        return True

    def _op_add_table(self, result: dict, op: dict) -> bool:
        """
        Thêm structured table vào top-level result['tables'].
        Idempotent: no-op nếu table với cùng page_number và headers đã tồn tại.

        Op format:
            {
                "op": "add_table",
                "table": {
                    "headers": [...],
                    "rows": [[...]],
                    "row_count": N,
                    "col_count": N,
                    "extraction_strategy": "patch",
                    "page_number": N
                }
            }
        """
        tables = result.setdefault("tables", [])
        new_table = op["table"]
        new_page = new_table.get("page_number")
        new_headers = new_table.get("headers", [])

        # Idempotent: no-op nếu đã có table cùng page_number và headers
        for existing in tables:
            if (existing.get("page_number") == new_page
                    and existing.get("headers") == new_headers):
                return False

        # Gán table_index = tiếp theo trong danh sách
        new_table = dict(new_table)
        new_table["table_index"] = len(tables)
        tables.append(new_table)

        # Cập nhật tables_found trong metadata nếu có
        if "pdf_metadata" in result:
            result["pdf_metadata"]["tables_found"] = len(tables)

        return True

    # ── Tree helpers ──────────────────────────────────────────────────────────

    def _find_node(self, nodes: list, node_id: str) -> Optional[dict]:
        """Tìm node theo id trong cây đệ quy. Returns None nếu không tìm thấy."""
        for node in nodes:
            if node.get("node_id") == node_id:
                return node
            found = self._find_node(node.get("children", []), node_id)
            if found is not None:
                return found
        return None

    def _remove_from_tree(self, nodes: list, node_id: str) -> bool:
        """
        Xóa node có id khỏi danh sách nodes (hoặc children đệ quy).
        Returns True nếu tìm thấy và xóa được.
        """
        for i, node in enumerate(nodes):
            if node.get("node_id") == node_id:
                nodes.pop(i)
                return True
            if self._remove_from_tree(node.get("children", []), node_id):
                return True
        return False
