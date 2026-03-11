# CLAUDE.md — Parser Change Policy

## 3-Layer Regression Protection

Mọi thay đổi liên quan đến parser phải tuân thủ hệ thống 3 lớp bảo vệ dưới đây.
**Mục tiêu:** fix bug này không phá bug kia.

---

## Layer 1 — Regression Tests (phát hiện sớm)

### Chạy trước và sau MỌI thay đổi parser:

```bash
# FAST (không cần PDF, ~2 giây): kiểm tra JSON files hiện tại
pytest tests/test_parser_regression.py -v

# REPARSE (cần PDF, ~2-5 phút): re-parse từ PDF và kiểm tra output
pytest tests/test_parser_regression.py -v -m reparse
```

### Khi nào chạy gì:

| Tình huống | Test cần chạy |
|---|---|
| Sửa `indentation_checker.py` | Cả FAST và REPARSE |
| Sửa `parser_core.py` | Cả FAST và REPARSE |
| Sửa `pdf_parser.py` | FAST |
| Sửa `pdfplumber_helper.py` | FAST + REPARSE cho doc liên quan |
| Thêm patch file mới | Chỉ FAST |
| Sửa trực tiếp JSON | KHÔNG LÀM (xem Layer 2) |

### Khi REPARSE test fail:

1. **Xác định loại regression:**
   - Metric giảm (total_nodes, max_depth) → regression thật → revert
   - Metric tăng → có thể là improvement → update GOLDEN nếu verify ok
2. **Nếu là edge case của 1 document** → dùng patch file (Layer 2)
3. **Không bao giờ** comment out test để cho qua

---

## Layer 2 — Patch Files (document-specific fixes)

### Khi nào dùng patch:

- Bug chỉ xảy ra ở 1 document do đặc điểm riêng của PDF đó
- Không thể fix bằng general parser rule mà không ảnh hưởng doc khác

### Cách tạo patch:

```bash
# Tạo file: data/patches/{doc_id}.patch.json
```

```json
{
  "version": 1,
  "doc_id": "373_2025_NDCP",
  "description": "Mô tả bug rõ ràng: nguyên nhân + triệu chứng",
  "ops": [
    {
      "op": "set_field",
      "node_id": "doc_373_2025_NDCP_phu_luc_I_khoan_7",
      "field": "content",
      "value": "Nội dung đúng..."
    },
    {
      "op": "remove_node",
      "node_id": "doc_373_2025_NDCP_phu_luc_II"
    },
    {
      "op": "add_reference",
      "node_id": "doc_...",
      "reference": { "text_match": "...", "target_id": "..." }
    }
  ]
}
```

### Supported operations:

| Op | Mô tả | Idempotent |
|---|---|---|
| `set_field` | Gán giá trị cho field của node | ✅ |
| `remove_node` | Xóa node khỏi cây | ✅ (no-op nếu không có) |
| `add_reference` | Thêm reference | ✅ (no-op nếu đã có) |

### Sau khi tạo patch:

1. Chạy FAST test để verify patch hoạt động đúng
2. Thêm spot_check vào GOLDEN trong `test_parser_regression.py`
3. Chạy lại FAST test để xác nhận

---

## Layer 3 — Quy tắc phân loại fix

```
Bug mới phát hiện
       │
       ▼
Chỉ xảy ra ở 1 document? ──Yes──► Tạo patch file (Layer 2)
       │                           + Thêm spot_check vào GOLDEN
       No
       │
       ▼
Rule chung áp dụng cho mọi doc
       │
       ▼
Sửa parser code (indentation_checker / parser_core)
       │
       ▼
Chạy FAST + REPARSE test
       │
       ├─ Pass ──► OK, update GOLDEN nếu metric thay đổi
       └─ Fail ──► Investigate, không merge nếu có regression
```

---

## Documents đã parse thành công (baseline)

| Document | root_count | total_nodes | max_depth | tables |
|---|---|---|---|---|
| 109_2025_QH15 | 4 Chương | 203 | 4 | 0 |
| 117_2025_NDCP | 4 Chương | 91 | 5 | 10 |
| 152_2025_TTBTC | 3 Chương | 29 | 4 | 11 |
| 20_2026_NDCP | 9 (Chương+Phụ lục) | 141 | 4 | 2 |
| 373_2025_NDCP | 13 Điều | 65 | 4 | 15 |
| 310_2025_NDCP | 4 Điều | 130 | 3 | 0 |

---

## Patches hiện tại

| Patch file | Doc | Bug | Ngày |
|---|---|---|---|
| `373_2025_NDCP.patch.json` | 373_2025_NDCP | Bug A: false Phụ lục II từ page-break mid-sentence | 2026-03-11 |

---

## KHÔNG BAO GIỜ làm

- Sửa trực tiếp file JSON trong `data/parsed/` (mất khi re-parse)
- Comment out regression test để bypass
- Giảm metric trong GOLDEN mà không có lý do rõ ràng
- Merge parser change khi REPARSE test chưa pass
