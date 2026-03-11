"""
Parser Regression Test Suite — Layer 1 của hệ thống 3-layer protection.

Mục đích:
    Đảm bảo mọi thay đổi parser code không làm giảm chất lượng
    của các document đã được parse và verified.

Hai modes:

    FAST (default) — load JSON hiện tại, kiểm tra golden metrics.
    Chạy sau mỗi lần sửa code để phát hiện hồi quy ngay lập tức:

        pytest tests/test_parser_regression.py -v

    REPARSE — thực sự re-parse từ PDF, xác nhận parser tạo ra đúng output.
    Chạy BẮT BUỘC trước và sau khi thay đổi parser code:

        pytest tests/test_parser_regression.py -v -m reparse

Quy tắc cập nhật GOLDEN:
    - Chỉ update GOLDEN khi một improvement được verify là tốt hơn.
    - Không giảm bất kỳ metric nào (root_count, total_nodes, ...).
    - Kèm comment giải thích lý do thay đổi.
"""

import json
import pytest
from pathlib import Path

PARSED_DIR = Path(__file__).parent.parent / "data" / "parsed"
RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
GOLDEN_DIR = Path(__file__).parent / "golden"


# ── Golden metrics ────────────────────────────────────────────────────────────
# Chụp từ JSON files đã được verify ngày 2026-03-11.
# KHÔNG thay đổi nếu không có lý do rõ ràng + improvement.

GOLDEN: dict = {
    # Luật Thuế thu nhập doanh nghiệp (sửa đổi)
    "109_2025_QH15": {
        "root_count": 4,
        "root_types": {"Chương"},
        "total_nodes": 203,
        "max_depth": 4,
        "tables_count": 0,
        "no_false_nodes": [],
        "spot_checks": [
            {
                "node_id": "doc_109_2025_QH15_chuong_I",
                "exists": True,
                "has_children": True,
                "min_children": 3,
            },
        ],
    },

    # Nghị định quản lý thuế TMĐT
    "117_2025_NDCP": {
        "root_count": 4,
        "root_types": {"Chương"},
        "total_nodes": 91,
        "max_depth": 5,
        "tables_count": 10,
        "no_false_nodes": [],
        "spot_checks": [
            # Điều 5 Khoản 2 có lead_in_text dẫn nhập các điểm con
            {
                "node_id": "doc_117_2025_NDCP_chuong_II_dieu_5_khoan_2",
                "exists": True,
                "has_field": "lead_in_text",
            },
            # Tiết a.1 phải có content đúng
            {
                "node_id": "doc_117_2025_NDCP_chuong_II_dieu_5_khoan_2_diem_a_tiet_a.1",
                "exists": True,
                "field": "content",
                "value": "Hàng hóa: 1%",
            },
        ],
    },

    # Thông tư 152/2025/TT-BTC
    # tables_count: 11 (raw) → 7 sau merge_split_tables (improvement: 4 bảng split được gộp)
    "152_2025_TTBTC": {
        "root_count": 3,
        "root_types": {"Chương"},
        "total_nodes": 29,
        "max_depth": 4,
        "tables_count": 7,
        "no_false_nodes": [],
        "spot_checks": [],
    },

    # Nghị định 20/2026/NĐ-CP (có Phụ lục hợp lệ I, II, III)
    "20_2026_NDCP": {
        "root_count": 9,
        "root_types": {"Chương", "Phụ lục"},
        "total_nodes": 141,
        "max_depth": 4,
        "tables_count": 2,
        "no_false_nodes": [],
        "spot_checks": [
            # Ba Phụ lục hợp lệ — title phải khớp chính xác
            {
                "node_id": "doc_20_2026_NDCP_phu_luc_I",
                "exists": True,
                "field": "title",
                "value": "Phụ lục I",
            },
            {
                "node_id": "doc_20_2026_NDCP_phu_luc_II",
                "exists": True,
                "field": "title",
                "value": "Phụ lục II",
            },
            {
                "node_id": "doc_20_2026_NDCP_phu_luc_III",
                "exists": True,
                "field": "title",
                "value": "Phụ lục III",
            },
        ],
    },

    # Nghị định 373/2025/NĐ-CP (sửa đổi Phụ lục — có Bug A đã được fix)
    "373_2025_NDCP": {
        "root_count": 13,
        "root_types": {"Điều"},
        "total_nodes": 65,
        "max_depth": 4,
        "tables_count": 15,
        # Node này là false positive từ Bug A — phải KHÔNG tồn tại
        "no_false_nodes": ["doc_373_2025_NDCP_phu_luc_II"],
        "spot_checks": [
            # Khoản 7 phải có content đầy đủ (được fix bởi patch)
            {
                "node_id": "doc_373_2025_NDCP_phu_luc_I_khoan_7",
                "exists": True,
                "content_contains": "năm 2021 của Bộ Tài chính",
            },
        ],
    },

    # Nghị định 310/2025/NĐ-CP
    "310_2025_NDCP": {
        "root_count": 4,
        "root_types": {"Điều"},
        "total_nodes": 130,
        "max_depth": 3,
        "tables_count": 0,
        "no_false_nodes": [],
        "spot_checks": [],
    },
}


# ── Invariants (áp dụng cho tất cả documents) ────────────────────────────────

GLOBAL_INVARIANTS = [
    # Không có Phụ lục node nào có title chứa "kèm theo"
    # (chỉ báo Bug A: mid-sentence false detection)
    {
        "name": "no_phu_luc_with_kem_theo_title",
        "description": "Phụ lục node không được có 'kèm theo' trong title",
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def count_all_nodes(nodes: list) -> int:
    total = len(nodes)
    for n in nodes:
        total += count_all_nodes(n.get("children", []))
    return total


def get_max_depth(nodes: list, depth: int = 0) -> int:
    if not nodes:
        return depth
    return max(get_max_depth(n.get("children", []), depth + 1) for n in nodes)


def find_node(nodes: list, node_id: str) -> dict | None:
    for n in nodes:
        if n.get("node_id") == node_id:
            return n
        found = find_node(n.get("children", []), node_id)
        if found is not None:
            return found
    return None


def collect_nodes_by_type(nodes: list, node_type: str) -> list:
    """Thu thập tất cả nodes có node_type nhất định (đệ quy)."""
    result = []
    for n in nodes:
        if n.get("node_type") == node_type:
            result.append(n)
        result.extend(collect_nodes_by_type(n.get("children", []), node_type))
    return result


def load_parsed(doc_id: str) -> dict:
    path = PARSED_DIR / f"{doc_id}.json"
    assert path.exists(), f"Thiếu file parsed: {path}"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Core validation ───────────────────────────────────────────────────────────

def validate_document(doc_id: str, data: dict) -> None:
    """
    Chạy tất cả golden assertions cho một document.
    Raise AssertionError với message rõ ràng nếu có regression.
    """
    g = GOLDEN[doc_id]
    nodes = data["data"]

    # ── Structural metrics ────────────────────────────────────────────────
    actual_root_count = len(nodes)
    assert actual_root_count == g["root_count"], (
        f"[{doc_id}] root_count: expected {g['root_count']}, got {actual_root_count}"
    )

    actual_types = set(n["node_type"] for n in nodes)
    assert actual_types == g["root_types"], (
        f"[{doc_id}] root_types: expected {g['root_types']}, got {actual_types}"
    )

    actual_total = count_all_nodes(nodes)
    assert actual_total == g["total_nodes"], (
        f"[{doc_id}] total_nodes: expected {g['total_nodes']}, got {actual_total}"
    )

    actual_depth = get_max_depth(nodes)
    assert actual_depth == g["max_depth"], (
        f"[{doc_id}] max_depth: expected {g['max_depth']}, got {actual_depth}"
    )

    actual_tables = len(data.get("tables", []))
    assert actual_tables == g["tables_count"], (
        f"[{doc_id}] tables_count: expected {g['tables_count']}, got {actual_tables}"
    )

    # ── False node check ──────────────────────────────────────────────────
    for bad_id in g.get("no_false_nodes", []):
        node = find_node(nodes, bad_id)
        assert node is None, (
            f"[{doc_id}] false node '{bad_id}' không được tồn tại nhưng vẫn có mặt"
        )

    # ── Spot checks ───────────────────────────────────────────────────────
    for chk in g.get("spot_checks", []):
        nid = chk["node_id"]
        node = find_node(nodes, nid)

        should_exist = chk.get("exists", True)
        if should_exist:
            assert node is not None, (
                f"[{doc_id}] node '{nid}' không tìm thấy"
            )
        else:
            assert node is None, (
                f"[{doc_id}] node '{nid}' không được tồn tại"
            )

        if node is None:
            continue

        if "has_children" in chk:
            has_ch = len(node.get("children", [])) > 0
            assert has_ch == chk["has_children"], (
                f"[{doc_id}] node '{nid}' has_children: expected {chk['has_children']}, "
                f"got {has_ch}"
            )

        if "min_children" in chk:
            n_ch = len(node.get("children", []))
            assert n_ch >= chk["min_children"], (
                f"[{doc_id}] node '{nid}' children: expected >={chk['min_children']}, "
                f"got {n_ch}"
            )

        if "has_field" in chk:
            assert chk["has_field"] in node, (
                f"[{doc_id}] node '{nid}' thiếu field '{chk['has_field']}'"
            )

        if "field" in chk and "value" in chk:
            actual = node.get(chk["field"])
            assert actual == chk["value"], (
                f"[{doc_id}] node '{nid}'.{chk['field']}: "
                f"expected {repr(chk['value'])}, got {repr(actual)}"
            )

        if "content_contains" in chk:
            content = node.get("content") or ""
            assert chk["content_contains"] in content, (
                f"[{doc_id}] node '{nid}' content không chứa "
                f"'{chk['content_contains']}'. "
                f"Actual content: {repr(content[:100])}"
            )

    # ── Global invariants ─────────────────────────────────────────────────
    phu_luc_nodes = collect_nodes_by_type(nodes, "Phụ lục")
    for n in phu_luc_nodes:
        title = n.get("title") or ""
        assert "kèm theo" not in title.lower(), (
            f"[{doc_id}] Phụ lục node '{n['node_id']}' có 'kèm theo' trong title "
            f"({repr(title)}) — đây là false positive từ mid-sentence detection"
        )


# ── FAST tests (load từ JSON, không re-parse) ─────────────────────────────────

@pytest.mark.parametrize("doc_id", list(GOLDEN.keys()))
def test_golden_metrics(doc_id: str) -> None:
    """
    [FAST] Load JSON hiện tại và kiểm tra golden metrics.
    Không cần PDF. Phát hiện sớm nếu JSON bị sửa sai hoặc bị ghi đè.

    Chạy: pytest tests/test_parser_regression.py -v
    """
    data = load_parsed(doc_id)
    validate_document(doc_id, data)


# ── REPARSE tests (thực sự re-parse từ PDF) ────────────────────────────────────

@pytest.mark.reparse
@pytest.mark.parametrize("doc_id", list(GOLDEN.keys()))
def test_reparse_golden_metrics(doc_id: str) -> None:
    """
    [REPARSE] Re-parse từ PDF và kiểm tra output khớp golden metrics.

    Chạy BẮT BUỘC trước và sau khi thay đổi parser code:
        pytest tests/test_parser_regression.py -v -m reparse

    Nếu có regression (test fail sau khi sửa parser):
        1. Kiểm tra xem có cần update GOLDEN không (nếu là improvement)
        2. Nếu là regression thật sự → revert parser change
        3. Xem xét dùng patch file thay vì parser change
    """
    pdf_path = RAW_DIR / f"{doc_id}.pdf"
    if not pdf_path.exists():
        pytest.skip(f"PDF không có: {pdf_path.name}")

    # Import here to avoid circular imports at module level
    from src.parsing.pdf_parser import PDFParser

    parser = PDFParser()
    data = parser.parse(pdf_path, save_json=False)
    validate_document(doc_id, data)


# ── SNAPSHOT tests (exact JSON diff so với golden) ─────────────────────────────

def _load_snapshot(doc_id: str) -> dict | None:
    path = GOLDEN_DIR / f"{doc_id}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_snapshot(doc_id: str, data: dict) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{doc_id}.json"
    snapshot = {
        "metadata": data["metadata"],
        "data": data["data"],
        "tables": data.get("tables", []),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)



# Fields in metadata that fall back to date.today() when not found in PDF.
# Excluding from snapshot diff to avoid false failures on different run dates.
_VOLATILE_METADATA_FIELDS = {"issue_date", "effective_date"}


def _diff_snapshot(expected: dict, actual_data: dict) -> list[str]:
    """
    So sánh snapshot với actual output.
    Trả về list các diff messages (rỗng = match hoàn toàn).
    Chỉ so sánh: metadata (trừ date fallback fields), data, tables — không so sánh pdf_metadata.
    """
    actual = {
        "metadata": actual_data["metadata"],
        "data": actual_data["data"],
        "tables": actual_data.get("tables", []),
    }

    # Build detailed diff
    diffs = []

    # metadata — skip volatile date fields (they fallback to date.today())
    for key in expected.get("metadata", {}):
        if key in _VOLATILE_METADATA_FIELDS:
            continue
        ev = expected["metadata"].get(key)
        av = actual["metadata"].get(key)
        if ev != av:
            diffs.append(f"  metadata.{key}: expected {repr(ev)}, got {repr(av)}")

    # structural counts (fast indicator)
    exp_nodes = count_all_nodes(expected["data"])
    act_nodes = count_all_nodes(actual["data"])
    if exp_nodes != act_nodes:
        diffs.append(f"  total_nodes: expected {exp_nodes}, got {act_nodes}")

    exp_tables = len(expected.get("tables", []))
    act_tables = len(actual.get("tables", []))
    if exp_tables != act_tables:
        diffs.append(f"  tables_count: expected {exp_tables}, got {act_tables}")

    # Deep content check: serialize data+tables (excluding volatile metadata)
    if not diffs:
        exp_content = json.dumps(
            {"data": expected["data"], "tables": expected.get("tables", [])},
            ensure_ascii=False, sort_keys=True,
        )
        act_content = json.dumps(
            {"data": actual["data"], "tables": actual.get("tables", [])},
            ensure_ascii=False, sort_keys=True,
        )
        if exp_content != act_content:
            diffs.append(
                "  Nội dung data/tables khác nhau (cùng số node/table). "
                "Chạy với --update-snapshots nếu đây là improvement."
            )

    return diffs


@pytest.mark.snapshot
@pytest.mark.reparse
@pytest.mark.parametrize("doc_id", list(GOLDEN.keys()))
def test_snapshot_reparse(doc_id: str, update_snapshots: bool) -> None:
    """
    [SNAPSHOT] Re-parse từ PDF và so sánh TOÀN BỘ JSON output với golden snapshot.

    Chặt hơn test_reparse_golden_metrics: phát hiện bất kỳ thay đổi content nào,
    không chỉ thay đổi số lượng node.

    Chạy:
        pytest tests/test_parser_regression.py -v -m snapshot

    Khi có improvement (output mới tốt hơn), update snapshots:
        pytest tests/test_parser_regression.py -v -m snapshot --update-snapshots
    """
    pdf_path = RAW_DIR / f"{doc_id}.pdf"
    if not pdf_path.exists():
        pytest.skip(f"PDF không có: {pdf_path.name}")

    from src.parsing.pdf_parser import PDFParser

    parser = PDFParser()
    actual = parser.parse(pdf_path, save_json=False)

    if update_snapshots:
        _save_snapshot(doc_id, actual)
        pytest.skip(f"Snapshot updated: {doc_id}.json")
        return

    snapshot = _load_snapshot(doc_id)
    if snapshot is None:
        _save_snapshot(doc_id, actual)
        pytest.skip(f"Snapshot mới được tạo: {doc_id}.json — chạy lại để verify")
        return

    diffs = _diff_snapshot(snapshot, actual)
    assert not diffs, (
        f"[{doc_id}] Snapshot mismatch — parser output đã thay đổi:\n"
        + "\n".join(diffs)
        + "\n\n  Nếu đây là improvement: chạy với --update-snapshots để cập nhật golden."
    )
