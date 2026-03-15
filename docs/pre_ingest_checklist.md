# Pre-Ingest Checklist — Trước khi đưa data vào Graph DB

Checklist này là **living document** — update mỗi khi:
- Phát hiện bug mới trong parsed data
- Thêm document mới vào hệ thống
- Thay đổi parser làm thay đổi output format

**Ingest Gate:** Chỉ ingest khi Group 0 + Group 1 + Group 2 = 100% DONE, Group 3 đã quyết định xong.

---

## Group 0 — Parser Validation (tự động, chạy trước ingest)

> Chạy: `python src/parsing/parser_validator.py`

| # | Check | Impact | Status |
|---|---|---|---|
| 0.1 | Duplicate node_index trong cùng parent | Graph traversal sai | ✅ DONE (0 errors — amendment content reclassified as warning) |
| 0.2 | Missing node_id trên bất kỳ node nào | Graph ingest fail | ✅ DONE (0 errors) |
| 0.3 | Breadcrumb path không khớp với node hierarchy | Citation sai | ✅ DONE (0 errors) |
| 0.4 | reference target_id format không hợp lệ | Dangling edges | ✅ DONE (0 errors) |
| 0.5 | Empty content + empty children + no lead_in_text trên Điều node | RAG chunk rỗng | ✅ DONE (0 errors) |
| 0.6 | Multi-reference chưa split (Điều 8, Điều 11, Điều 12 → 3 edges) | Missing graph edges | ✅ DONE (1 warning — 109 Điều 29, acceptable) |

---

## Group 1 — Data Quality

| # | Vấn đề | Docs bị ảnh hưởng | Impact | Status |
|---|---|---|---|---|
| 1.1 | Breadcrumb dùng sai document_number | Tất cả | Citation sai trong chatbot | ✅ DONE (re-parse từ docx fix tất cả) |
| 1.2 | effective_date verify đủ 13 docs + validity status | Tất cả | Validity filter sai | ✅ DONE — 109/2025 status=pending (hiệu lực 2026-07-01), còn lại active |
| 1.3 | Broken words: `ng hiệp`, `ng hiên` trong source Word | 109, 149, 152, 198, 20, 310 | Text search kém | 🔵 ACCEPT (source file issue) |

---

## Group 2 — Graph Schema Readiness

| # | Vấn đề | Chi tiết | Impact | Status |
|---|---|---|---|---|
| 2.1 | Cross-document relationships chưa map | 310→125/2020, 149→LuậtGTGT, 373→NĐ126/2020, 152→TT88/2021 | Thiếu AMENDS/SUPERSEDES edges | ✅ DONE (data/graph/cross_doc_relationships.json — 7 relationships, 4 external stubs) |
| 2.2 | Validity status chưa xác định | Doc nào còn hiệu lực, đã bị thay thế | Chatbot dùng luật cũ | ✅ DONE (status field added to all metadata — 109 = pending, rest = active) |
| 2.3 | Internal references có `external_*` target_id | Docs chưa có trong hệ thống | Dangling edges | 🔵 ACCEPT (tạo stub nodes) |

---

## Group 3 — Schema Design (quyết định trước khi ingest)

| # | Quyết định | Decision | Status |
|---|---|---|---|
| 3.1 | Node type cho Type B docs (1296, So_Tay) | `GuidanceChunk` riêng (không reuse Khoản) + `source_type` property + `HAS_CHUNK` relationship | ✅ DONE |
| 3.2 | Relationship EXPLAINED_BY: Type A ↔ Type B | `EXPLAINED_BY {confidence: Float, method: "auto"\|"manual"}` — auto threshold ≥ 0.82 | ✅ DONE |
| 3.3 | Legal hierarchy weighting | `hierarchy_rank` ở Document level (Int) + `valid_from`/`valid_to` cho temporal validity | ✅ DONE |

---

## Ingest Gate

```
Chỉ ingest khi:
  ✅ Group 0: 100% DONE  (parser_validator.py pass sạch)
  ✅ Group 1: 100% DONE  (data quality verified)
  ✅ Group 2: 100% DONE  (relationships mapped, validity confirmed)
  ✅ Group 3: LOCKED      (schema design finalized 2026-03-13)
```

---

## Bug Tracker

| Bug ID | Ngày | Doc | Mô tả | Severity | Status |
|---|---|---|---|---|---|
| BUG-001 | 2026-03-13 | 310_2025_NDCP | Duplicate diem_b trong khoan_3 | Medium | ✅ Fixed (patch) |
| BUG-002 | 2026-03-13 | 152_2025_TTBTC | issue_date sai (2015 thay vì 2025) | Low | ✅ Fixed |
| BUG-003 | 2026-03-13 | 198_2025_QH15 | document_number sai (57/2014 thay vì 198/2025) | High | ✅ Fixed |
| BUG-004 | 2026-03-13 | 20_2026_NDCP | document_number sai (198/2025/QH15) | High | ✅ Fixed |
| BUG-005 | 2026-03-13 | 310_2025_NDCP | document_number sai (125/2020) | High | ✅ Fixed |
| BUG-006 | 2026-03-13 | 198_2025_QH15 | document_type sai ("Luật" thay vì "Nghị quyết") | High | ✅ Fixed |
| BUG-007 | 2026-03-13 | 310_2025_NDCP | 37 duplicate Khoản/Điểm — amendment doc flatten nested content. Validator updated: severity=warning [amendment inline content — known] | Medium | ✅ Fixed (validator reclassified) |
| BUG-008 | 2026-03-13 | 373_2025_NDCP | phu_luc_1 (Arabic) duplicate 3 lần, trùng với phu_luc_I (Roman) | Medium | ✅ Fixed (patch: remove_node ×3) |
| BUG-009 | 2026-03-13 | 18_2026_TTBTC | Duplicate Khoản 3 Điều 6 — Gemini mislabel khoản chuyển tiếp | Medium | ✅ Fixed (patch: set node_index=5) |

---

## Cách update

**Khi phát hiện bug mới:**
1. Add vào Bug Tracker với Bug ID tiếp theo, status `⏳ TODO`
2. Đánh giá Severity:
   - **High** → add vào Group 1 hoặc 2, block ingest
   - **Medium** → fix trước ingest nếu dễ
   - **Low** → accept hoặc backlog
3. Sau khi fix → đổi status `✅ Fixed`

**Status legend:**
- ⏳ TODO — chưa làm
- 🔄 IN PROGRESS — đang làm
- ✅ DONE / Fixed — hoàn thành
- 🔵 ACCEPT — chấp nhận có lý do rõ ràng
