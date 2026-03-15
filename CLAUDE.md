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
| `add_table` | Thêm structured table vào top-level `tables` | ✅ (no-op nếu cùng page_number + headers) |

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

Source: D=docx, P=pdf+pdfplumber, G=pdf+Gemini 2.5 Pro

**Type A — Văn bản pháp luật (có Điều/Khoản):**

| Document | Source | root_count | total_nodes | max_depth | tables |
|---|---|---|---|---|---|
| 109_2025_QH15 | D | 4 Chương | 203 | 4 | 2 |
| 117_2025_NDCP | D | 5 (Chương+Phụ lục) | 92 | 5 | 10 |
| 152_2025_TTBTC | D | 3 Chương | 29 | 4 | 9 |
| 20_2026_NDCP | D | 9 (Chương+Phụ lục) | 141 | 4 | 4 |
| 373_2025_NDCP | D | 15 (Điều+Phụ lục) | 67 | 4 | 16 |
| 310_2025_NDCP | D | 4 Điều | 130 | 3 | 0 |
| 110_2025_UBTVQH15 | D | 2 Điều | 4 | 2 | 0 |
| 149_2025_QH15 | D | 2 Điều | 7 | 3 | 0 |
| 198_2025_QH15 | D | 7 Chương | 97 | 4 | 0 |
| 68_2026_NDCP | G | 5 Chương | 159 | 4 | 14 |
| 18_2026_TTBTC | G (scan) | 6 Điều | 29 | 3 | 0 |

**Type B — Văn bản hướng dẫn (không có Điều/Khoản, dùng cho chatbot context):**

| Document | Source | Ghi chú |
|---|---|---|
| 1296_CTNVT | G | Công văn hướng dẫn quyết toán thuế TNCN |
| So_Tay_HKD | G | Sổ tay HKD — 26 tables, guidance content |

---

## Patches hiện tại

| Patch file | Doc | Bug | Ngày |
|---|---|---|---|
| `373_2025_NDCP.patch.json` | 373_2025_NDCP | Bug A: false Phụ lục II từ page-break mid-sentence; Bug B: phu_luc_1 Arabic × 3 duplicates | 2026-03-11 |
| `109_2025_QH15.patch.json` | 109_2025_QH15 | Bug A: thuế suất 59%→5% (Điều 22). Bug B: pdfplumber drop spaces. Bug C: dính chữ. Bug D: số bị split. Bug E: sai dấu Điêu/thuê. Bug F: page artifact. Bug G: add_table biểu thuế lũy tiến | 2026-03-12 |
| `310_2025_NDCP.patch.json` | 310_2025_NDCP | Bug: validator reclassify amendment node duplicates thành WARNING | 2026-03-13 |
| `18_2026_TTBTC.patch.json` | 18_2026_TTBTC | Bug: duplicate Khoản 3 — set node_index="5" | 2026-03-13 |

---

## KHÔNG BAO GIỜ làm

- Sửa trực tiếp file JSON trong `data/parsed/` (mất khi re-parse)
- Comment out regression test để bypass
- Giảm metric trong GOLDEN mà không có lý do rõ ràng
- Merge parser change khi REPARSE test chưa pass
