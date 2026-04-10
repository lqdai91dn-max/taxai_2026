# TaxAI — Kế hoạch dự án tổng thể

> **Tài liệu này là nguồn tham chiếu duy nhất** cho toàn bộ kiến trúc, quyết định thiết kế,
> và lộ trình triển khai của dự án TaxAI. Cập nhật lần cuối: 2026-04-02.

---

## 1. Mục tiêu dự án

**Xây dựng chatbot tư vấn thuế Việt Nam** phục vụ 2 nhóm người dùng chính:

| Nhóm | Đối tượng | Loại câu hỏi điển hình |
|---|---|---|
| **TNCN** | Cá nhân có thu nhập từ lương, đầu tư, bất động sản | "Lương 30 triệu/tháng, 1 con, đóng thuế bao nhiêu?" |
| **HKD** | Hộ kinh doanh / cá nhân kinh doanh | "Tôi bán hàng doanh thu 1.2 tỷ/năm, thuế phải nộp?" |

### Tiêu chí chất lượng (3 trụ cột)

```
[T1] Tính chính xác      — con số thuế đúng, không hallucinate
[T2] Nguồn pháp lý       — trích dẫn đúng văn bản (Luật, Nghị định, Thông tư)
[T4] Thực tế người dùng  — trả lời được câu hỏi thực tế, không chỉ lookup formal
```

### Baseline và tiến độ benchmark

| Pipeline | Avg (tier) | Pass | T2 | T3 | T4 | Ghi chú |
|---|---|---|---|---|---|---|
| **R25 (planner)** | **0.938** | **100%** | 0.900 | — | 0.975 | **PRODUCTION — target** |
| Pipeline v2 P4 | 0.658 | 50% | 0.700 | — | 0.650 | FAIL — đã dừng |
| v4 R26 (khởi đầu) | 0.241 | ~10% | 0.252 | 0.295 | 0.175 | Before any fixes |
| v4 R27 | 0.587 | — | 0.330 | 0.954 | 0.478 | Fix1-3 |
| v4 R28 | 0.599 | **44/225 (19.6%)** | 0.354 | 0.949 | 0.493 | Fix1-7, cap=8, no annotations |
| v4 R29 | 0.642 | **70/225 (31.1%)** | **0.502** | 0.949 | **0.500** | Fix1-8 + 6 annotations + cap=4 ✅ |
| **v4 R49** | **0.617** | **120/225 (53.3%)** | **0.557** | **0.949** | **0.676** | 225-question full benchmark ✅ |
| v4 R50 (target) | ≥0.670 | ≥65% | ≥0.640 | 0.949 | ≥0.720 | Sau Sprint A+B+C |
| v4 R51 (projected) | ≥0.730 | ≥75% | ≥0.710 | 0.949 | ≥0.760 | Sau Sprint D (Contextual) |

**Switch criterion:** v4 ≥ R25 trên tất cả 3 tiêu chí: T2 ≥ 0.900, T4 ≥ 0.975, T1 ≥ 1.000

**Hard deadline: 01/07/2026** — Luật 109/2025/QH15 hiệu lực. Thuế TNCN thay đổi hoàn toàn. Cache phải invalidate, mọi câu trả lời TNCN phải reflect law mới.

---

### R49 — Phân tích theo topic (2026-04-02)

| Topic | N | Pass% | Avg | Trạng thái | Root cause |
|---|---|---|---|---|---|
| Thuế hộ kinh doanh | 41 | 68.3% | 0.732 | ✅ OK | — |
| Thuế thu nhập cá nhân | 23 | 73.9% | 0.732 | ✅ OK | — |
| Xử phạt vi phạm | 15 | 66.7% | 0.678 | ✅ Đạt | — |
| Ủy quyền quyết toán TNCN | 6 | 66.7% | 0.667 | ✅ Đạt | — |
| Hiệu lực pháp luật | 4 | 100% | 0.875 | ✅ Tốt | — |
| Thuế thương mại điện tử | 38 | 47.4% | 0.544 | ⚠️ Yếu | T4 yếu — routing thiếu 108; nhiều sub-pattern |
| Nghĩa vụ kê khai | 20 | 45.0% | 0.592 | ⚠️ Yếu | T2 yếu — routing 126 thay vì 68+18, thiếu co-retrieval |
| Kế toán HKD | 26 | 46.2% | 0.567 | ⚠️ Yếu | T4 yếu — thiếu chi tiết mẫu sổ (152_2025_TTBTC) |
| Thủ tục hành chính | 14 | 21.4% | 0.357 | ❌ Kém | T2=0 — thiếu 108_2025_QH15 [đã fix P0] |
| Miễn giảm thuế | 3 | 0.0% | 0.417 | ❌ Kém | 310 false positive [đã fix P0] + annotation |
| Quyết toán thuế TNCN | 7 | 57.1% | 0.643 | ⚠️ Trung | — |
| Giảm trừ gia cảnh | 8 | 37.5% | 0.604 | ⚠️ Yếu | T4 yếu — thiếu detail |
| Hoàn thuế TNCN | 5 | 40.0% | 0.592 | ⚠️ Yếu | — |

---

### Lộ trình Post-R49 — Thiết kế lại (2026-04-02)

#### Trạng thái hiện tại (những gì đã xong)

```
✅ Phase 0 — Pre-R49 Cleanup (01/04/2026)
   - Dead tools disabled (get_article/get_guidance/get_impl_chain)
   - MAX_ITERATIONS 3→4
   - QACache versioning + flush 183 entries cũ
   - RPD Counter UTC+7

✅ P0 Global Fixes (02/04/2026)
   - 108_2025_QH15 đăng ký vào DOCUMENT_REGISTRY + routing rule
   - 310_2025_NDCP gate: 3-layer block (exclude_doc_ids + negative routing + penalty keyword check)
     → HybridSearch.search() có exclude_doc_ids
     → search_legal_docs() có _GENERAL_SEARCH_EXCLUDE + _has_penalty_keywords()
     → AGENT_SYSTEM_PROMPT có 310 usage rule

✅ P1 Partial (02/04/2026)
   - 18_2026_TTBTC co-retrieval: routing rule BẮT BUỘC 2 bước (68 + 18)
   - KHÔNG dùng 126_2020_NDCP cho câu hỏi kê khai HKD
   - Q220/Q221 annotation fix (expected_docs đã cập nhật)
```

---

#### Sprint A — P1 Completion + Quick Wins
**Thời gian:** ~2-3 ngày (2026-04-03 → 04-07)
**Dự báo sau Sprint A:** Pass ~58-62%, T2 ~0.585, T4 ~0.700

| Task | Mô tả | Effort | Priority |
|---|---|---|---|
| A1 | Annotation Q81/Q82 — xác định 108_2025_QH15 có thuộc expected_docs không | 30 min | 🔴 |
| A2 | Annotation enrich Q133/Q144/Q178/Q181 — thêm key_facts từ 18_2026_TTBTC (10 ngày, 01/CNKD...) để T4 pass | 2h | 🔴 |
| A3 | **Synonym Dictionary** — query expansion cho GTGT/TNCN/HKD/BĐS acronyms tại query time | 2h | 🟠 |
| A4 | Mini-test R50 P1 topics (20 câu kê khai) để validate | 30 min | 🔴 |

**Lý do Synonym Dictionary là Sprint A (không phải D):**
Acronym mismatch (GTGT vs "giá trị gia tăng") là query-time fix, zero re-index cost, dễ rollback.
Tách biệt hoàn toàn với Contextual Retrieval → không cần chờ P4.

---

#### Sprint B — P2 Kế toán HKD
**Thời gian:** ~3-4 ngày (2026-04-08 → 04-14)
**Dự báo sau Sprint B:** Pass ~63-67%, T2 ~0.615, T4 ~0.725

| Task | Mô tả | Effort |
|---|---|---|
| B1 | Sub-pattern analysis 26 câu — phân loại root cause (routing / annotation / key_facts) | 2h |
| B2 | Routing rules cho 152_2025_TTBTC (mẫu sổ, chế độ kế toán) vào system prompt | 2h |
| B3 | Annotation enrichment: thêm key_facts cụ thể cho T4-failing questions | 3h |
| B4 | Mini-test R50 P2 topics để validate | 30 min |

---

#### Sprint C — P3 Thuế TMĐT
**Thời gian:** ~5-7 ngày (2026-04-15 → 04-28)
**Dự báo sau Sprint C:** Pass ~68-73%, T2 ~0.655, T4 ~0.750

| Task | Mô tả | Effort |
|---|---|---|
| C1 | Sub-pattern analysis 38 câu — phân loại theo sub-type (sàn TMĐT, cá nhân online, nền tảng nước ngoài) | 3h |
| C2 | 109/GTGT vocabulary: Synonym Dict (Sprint A) xử lý acronym, routing thêm 109 khi có từ "miễn thuế GTGT" | 2h |
| C3 | TMĐT sub-pattern routing — targeted rules theo từng sub-type | 4h |
| C4 | Annotation update — expected_docs + key_facts cho 38 câu | 4h |
| C5 | Mini-test R50 P3 topics | 30 min |

---

#### R50 Full Benchmark
**Thời gian:** Cuối tuần 5 (~2026-04-29 → 05-05)

| Metric | R49 Baseline | R50 Target |
|---|---|---|
| Pass% | 53.3% | ≥ 65% |
| T2 | 0.557 | ≥ 0.640 |
| T4 | 0.676 | ≥ 0.720 |
| Avg | 0.617 | ≥ 0.670 |

---

#### Sprint D — P4 Contextual Retrieval (Experimental)
**Thời gian:** ~2-3 ngày (2026-05-06 → 05-12)
**Dự báo sau Sprint D:** T2 +0.04–0.08, Pass +5-8pp

**Quyết định thiết kế (đã thống nhất):**

```
D1 — Template Breadcrumb cho BM25 (không LLM, không re-embed)
   Format: "[Tên văn bản | Chương | Điều | Khoản]\n[nội dung chunk]"
   Scope thử nghiệm: 18_2026_TTBTC trước (nhỏ, gây nhiều T2 failures nhất)
   Implement: thêm field bm25_text vào metadata, HybridSearch ưu tiên nếu có

D2 — Evaluate impact
   Mini-test P1 topics (kê khai) sau khi re-index 18_2026_TTBTC
   Nếu T2 P1 tăng ≥ +0.03 → extend sang 109_2025_QH15

D3 — Extend nếu D2 positive
   109_2025_QH15 breadcrumb → giải quyết "thuế suất GTGT theo Luật mới"
   Cân nhắc re-embed vector nếu breadcrumb-only chưa đủ

D4 — KHÔNG làm: LLM-generated context summary
   Lý do: template breadcrumb đủ 80% hiệu quả, zero cost, zero hallucination risk
```

**Điều kiện tiến hành D:** R50 phải chạy xong để có baseline sạch trước khi đo impact D.

---

#### Sprint E — Production Readiness
**Thời gian:** 2026-05-13 → 06-30 (trước hard deadline 01/07/2026)

| Task | Priority | Lý do |
|---|---|---|
| E1: Multi-turn conversation — `history: list[dict]` vào `TaxAIAgent.answer()` | 🔴 P0 | Production blocker — chatbot không nhớ context |
| E2: Cache TTL cho 01/07/2026 — invalidate TNCN cache khi law version thay đổi | 🔴 P0 | Hard deadline — law effective date |
| E3: Streaming responses — Gemini stream + `st.write_stream` | 🟠 P1 | UX blocking 3-8s |
| E4: Dead code cleanup — `answer_generator.py` (384 lines), `src/graph/` (4 files), 7 debug files | 🟡 P2 | Maintainability |

---

#### Dự báo benchmark trajectory tổng thể

| Milestone | Thời gian | Pass% | T2 | T4 | Avg | Gap to prod (T2) |
|---|---|---|---|---|---|---|
| R49 Baseline | 02/04/2026 | 53.3% | 0.557 | 0.676 | 0.617 | -0.343 |
| After Sprint A | ~07/04/2026 | 58-62% | 0.585 | 0.700 | 0.640 | -0.315 |
| After Sprint B | ~14/04/2026 | 63-67% | 0.615 | 0.725 | 0.665 | -0.285 |
| After Sprint C | ~28/04/2026 | 68-73% | 0.655 | 0.750 | 0.695 | -0.245 |
| **R50 Full** | ~05/05/2026 | **≥65%** | **≥0.640** | **≥0.720** | **≥0.670** | ~-0.260 |
| After Sprint D | ~12/05/2026 | 73-78% | 0.710 | 0.760 | 0.730 | -0.190 |
| **Production target** | R25 baseline | 100% | 0.900 | 0.975 | 0.938 | 0 |

**Nhận xét gap còn lại:** Sau toàn bộ Sprint A-D, T2 dự báo ~0.710 — còn cách production target 0.190pp. Phần gap này cần QueryIntent Builder (P5.1) + NodeMetadata Reranker (P5.3) để bridge semantic gap triệt để hơn. Đây là Phase tiếp theo sau R51.

---

#### Nguyên tắc ưu tiên trong từng Sprint

```
1. Chạy mini-test TRƯỚC khi implement → có baseline
2. Fix annotation TRƯỚC khi fix routing → tránh optimize sai signal
3. Routing rules TRƯỚC Contextual Retrieval → giữ debuggability (control > emergence)
4. Synonym Dict có thể làm bất kỳ lúc nào (không có dependency)
5. Contextual Retrieval CHỈ sau R50 (cần baseline sạch để đo impact)
6. Production features (multi-turn, cache TTL) PHẢI xong trước 01/07/2026
```

---

## 2. Vấn đề gốc rễ cần giải quyết

### Semantic Gap — Nguyên nhân chính của mọi lỗi

```
User:    "tiệm vàng của tôi doanh thu 60 tỷ, thuế bao nhiêu?"
Corpus:  "hộ kinh doanh vàng bạc đá quý — phân phối hàng hóa — tỷ lệ 1%"
LLM:     ??? (không map được)
```

**Bản chất:** LLM không phải "query understanding engine đáng tin cậy". Phải treat LLM như "reasoning engine — nhưng CHỈ khi input đã được chuẩn hóa". Nếu input không chuẩn → output không tin được.

### 3 Breaking Points của kiến trúc cũ (v1–v3)

| #  | Vấn đề | Hậu quả |
|----|--------|---------|
| B1 | LLM tự hiểu query → embed → retrieve (end-to-end uncontrolled) | Retrieve sai doc, sai chunk |
| B2 | LLM vừa đọc luật vừa tính toán trong 1 prompt | Hallucinate số, sai công thức |
| B3 | Không có validation Python — chỉ dùng LLM check LLM | Anti-pattern: lỗi lan truyền |

---

## 3. Kiến trúc v4 — Thiết kế đầy đủ

### 3.1 Nguyên tắc thiết kế (Architecture Principles)

```
[AP1]  Chuẩn hóa trước, LLM sau
       → QueryIntent Builder chuẩn hóa query trước khi đưa vào retrieval

[AP2]  Phân tách vai trò: Reason ≠ Compute ≠ Synthesize
       → LLM đọc luật (Legal Reasoner) → Python tính (Calculator) → LLM format (Synthesizer)

[AP3]  Python là ground truth — LLM không được override số
       → State Immutability Lock sau step Calculator

[AP4]  Validate ở Python, không validate bằng LLM
       → Coverage check, VND match, citation binding = pure Python

[AP5]  RAG là nguồn authority cho rates — không hardcode trong Python
       → bracket_rate, business_category đến từ LLM đọc RAG

[AP6]  Degrade gracefully thay vì crash
       → 4-level rollback + circuit breaker
```

### 3.2 Full Pipeline Flow

```
[INPUT]  Query + Session State
    │
    ▼
[P5]  QueryIntent Builder
    │  ├─ Rule Parser (deterministic, ~0ms)
    │  │    • Regex patterns cho số tiền, ngành nghề phổ biến
    │  │    • Keyword mapping: "tiệm vàng" → HKD, "lương" → TNCN salary
    │  │    • Output có confidence score
    │  └─ LLM Extractor (optional, ~200ms, chỉ khi rule uncertain)
    │       • Dùng 5W2H structured prompt
    │       • Merge với rule output + conflict resolution
    │  Output: QueryIntent{who, activity_group, tax_domain, financials, time, intent, flags}
    │
    ▼
[P6]  Dynamic Prompt Assembly
    │  ├─ Load specialist system prompt theo tax_domain
    │  │    • HKD specialist: focus PP doanh thu / PP lợi nhuận
    │  │    • PIT specialist: focus lũy tiến, giảm trừ gia cảnh
    │  ├─ Load tools theo intent.requires_*
    │  └─ Model routing: Flash (simple query) / Pro (complex multi-step)
    │
    ▼
[RETRIEVAL]  Two-Stage
    │  ├─ Stage 1: Vector search Top-K=20 (no hard filter — tránh miss)
    │  └─ Stage 2: Reranker
    │       • Metadata match bonus (doc_id, activity_group, tax_domain)
    │       • Semantic score × metadata bonus → weighted reorder
    │       • Không drop chunks — chỉ reorder
    │
    ▼
[6.1]  LLM Legal Reasoner
    │  Input: query + top chunks + QueryIntent
    │  Task: đọc RAG → extract params có nguồn
    │  Output JSON (schema cứng):
    │    { template_type,
    │      params_validated: {param: {value, source: chunk_id}},
    │      assumptions[],
    │      clarification_needed, clarification_question,
    │      scenarios[] (max 2) }
    │  KHÔNG được tính toán — chỉ extract + validate params
    │
    ▼
[VALIDATION LAYER]  Python (deterministic)
    │  ├─ Template consistency check (VALID_TEMPLATE_COMBINATIONS)
    │  ├─ Coverage check (COVERAGE_RULES[template])
    │  ├─ Citation binding validate (source in retrieved_chunks)
    │  └─ IF clarification_needed → short-circuit → trả về câu hỏi
    │
    ├─ FAIL → Rollback Level 1 → 2 → 3 → 4
    │
    ▼
[6.2]  Python Safe Calculator (Template Registry)
    │  Input: template_type + params (flat dict)
    │  ├─ run_template(template_type, params)
    │  │    → validate input (required fields + sanity caps)
    │  │    → execute calculator function (pure Python)
    │  │    → normalize output (VND floor rounding per law)
    │  └─ CalcOutput → set_calc_output() → STATE LOCKED (finalized=True)
    │       LLM KHÔNG thể thay đổi tax_amount sau bước này
    │
    ▼
[6.3]  LLM Synthesizer (thin prompt — read-only)
    │  Input: locked CalcOutput + question + citations
    │  Task: format answer đẹp, dễ hiểu, có citations
    │  Rules: KHÔNG recompute, KHÔNG override số, bắt buộc include assumptions
    │
    ▼
[FINAL VALIDATOR]  Python
    │  ├─ VND normalization match (tax_amount phải có trong text)
    │  ├─ Assumption mention check (nếu risk ≥ medium)
    │  └─ Citation present check
    │  IF fail → retry 6.3 (max 2×) → Circuit Breaker
    │
    ▼
[AUDIT]  log_pipeline_run() → JSONL
    │  session_id, template, tax_amount, degrade_level, retry_count,
    │  assumptions, doc_ids, latency_ms, audit_trail
    │
    ▼
[OUTPUT]  answer + citations + tax_amount + audit_ref
```

### 3.3 Rollback Strategy

```
Level 1: Retry same template (max 2×)
         → Khi calculator raise ValueError (params lỗi)

Level 2: Fallback simpler template
         → HKD_profit → HKD_percentage (khi expenses unknown)
         → PIT_progressive → PIT_full (khi taxable income chưa có)

Level 3: Reset chunks + re-retrieve từ đầu       [Full Production]
         → Khi inconsistency_score > threshold

Level 4: Degrade gracefully
         → Answer + strong disclaimer + snippets từ RAG
         → "Liên hệ tư vấn viên để được hỗ trợ"

Circuit Breaker: consecutive_failures > 3 → block request → alert ops
```

---

## 4. Schema v1.0 (FROZEN 2026-03-26)

### 4.1 QueryIntent — Cấu trúc hóa query người dùng

**Mục đích:** Bridge semantic gap giữa ngôn ngữ đời thường và corpus pháp lý.
Mỗi field có `value` + `confidence` + `source` (rule | llm | merged).

```json
{
  "who": {"value": ["HKD"], "confidence": 0.9, "source": "rule"},
  "activity_group": ["goods_distribution"],
  "tax_domain": ["HKD"],
  "financials": {
    "revenue":         60000000000,
    "income_value":    null,
    "dependent_count": null
  },
  "time": {"year": 2026},
  "intent": {
    "primary":              "calculate",
    "secondary":            null,
    "requires_calculation": true,
    "requires_conditions":  true
  },
  "flags": {
    "is_first_time":     null,
    "is_sole_property":  null,
    "is_online_platform": false
  }
}
```

### 4.2 NodeMetadata — Annotation cho document nodes

**Mục đích:** Mirror QueryIntent ở phía corpus để enable precise reranking.
Được tạo offline (LLM annotation pass), ghi vào ChromaDB metadata.

```json
{
  "applies_to": {"who": ["HKD"], "source": "explicit"},
  "activity_group": ["goods_distribution"],
  "tax_domain": ["HKD"],
  "content_type": "tax_rate",
  "legal": {"effective_from": "2026-03-05", "effective_to": null},
  "confidence": 0.95
}
```

### 4.3 Canonical ENUMs (FROZEN)

**activity_group:**
```
HKD (TT40 / NĐ68):
  goods_distribution        — phân phối, cung cấp hàng hóa
  services_without_materials — dịch vụ, xây dựng không bao thầu NVL
  manufacturing_transport   — sản xuất, vận tải, xây dựng bao thầu NVL, ăn uống
  asset_rental              — cho thuê tài sản / bất động sản
  e_commerce_platform       — sàn TMĐT (đặc thù riêng)

TNCN (Luật 109/2025/QH15):
  salary_wages              — tiền lương, tiền công
  real_estate_transfer      — chuyển nhượng BĐS
  capital_investment        — đầu tư vốn (cổ tức, lãi)
  capital_transfer          — chuyển nhượng vốn / chứng khoán
  lottery_prizes            — xổ số, trúng thưởng
  royalties_franchising     — bản quyền, nhượng quyền
  inheritance_gifts         — thừa kế, quà tặng

Fallback: other_activities, UNSPECIFIED
```

**WHO:** `individual` | `HKD` | `employer` | `employee` | `enterprise` | `UNSPECIFIED`
**TAX_DOMAIN:** `PIT` | `HKD` | `VAT` | `TMDT` | `PENALTY` | `UNSPECIFIED`
**CONTENT_TYPE:** `tax_rate` | `threshold` | `condition_rule` | `procedure` | `definition`

### 4.4 COVERAGE_RULES (MVP — static)

```python
COVERAGE_RULES = {
  "PIT_full":        ["gross_income"],
  "PIT_progressive": ["annual_taxable_income"],
  "HKD_percentage":  ["annual_revenue", "business_category"],
  "HKD_profit":      ["annual_revenue", "annual_expenses", "business_category"],
  "deduction_calc":  [],
}
```

### 4.5 VALID_TEMPLATE_COMBINATIONS

```python
{
  "PIT_full":        {"tax_domain": ["PIT"], "who": ["individual","employee"]},
  "PIT_progressive": {"tax_domain": ["PIT"], "who": ["individual","employee"]},
  "PIT_flat_20":     {"tax_domain": ["PIT"], "who": ["individual"],
                      "requires": {"is_resident": False}},
  "HKD_percentage":  {"tax_domain": ["HKD"], "who": ["HKD"]},
  "HKD_profit":      {"tax_domain": ["HKD"], "who": ["HKD"],
                      "requires": {"revenue_gt": 3_000_000_000}},
}
```

---

## 5. Lộ trình triển khai theo Phase

---

### Phase MVP-Core — "Build nền móng" ✅ HOÀN THÀNH (2026-03-26)

**Mục tiêu:**
Xây dựng các thành phần Python thuần (deterministic) của pipeline v4.
Không phụ thuộc LLM — có thể unit test độc lập.

**Kỹ thuật:**

| Component | File | Vai trò |
|---|---|---|
| Template Registry | `src/agent/template_registry.py` | Named Python functions với validation + rounding |
| PipelineState + Lock | `pipeline_v4/state.py` | Immutable state sau Calculator step |
| Validation Layer | `pipeline_v4/validation.py` | COVERAGE_RULES + citation binding |
| Final Validator | `pipeline_v4/final_validator.py` | VND regex match + assumption mention |
| Audit Trail | `pipeline_v4/audit.py` | JSONL append-only per session |
| Orchestrator skeleton | `pipeline_v4/orchestrator.py` | Wiring tất cả components |

**Template Registry — chi tiết:**
- `PIT_full`: gross_income + dependents → tự tính deduction → lũy tiến → tax
- `PIT_progressive`: annual_taxable_income → lũy tiến (khi đã biết thu nhập tính thuế)
- `HKD_percentage`: revenue + business_category → GTGT + TNCN PP doanh thu
- `HKD_profit`: revenue + expenses + category → GTGT + TNCN PP lợi nhuận
- `deduction_calc`: dependents + months → giảm trừ gia cảnh standalone
- Rounding: PIT floor_1000 (Thông tư 111), HKD floor_100 (NĐ 68)

**Kết quả đạt được:**
- ✅ Template Registry: all 5 templates, validation, normalized output
- ✅ State Lock: ImmutableStateError khi cố ghi lại sau finalize()
- ✅ Rollback Level 1-2: retry + fallback simpler template
- ✅ Final Validator: VND normalization match (multi-format: "18,000,000", "18 triệu", "18.000.000")
- ✅ Audit: JSONL append, không crash pipeline khi log fail

---

### Phase P5 — "Semantic Alignment" ✅ HOÀN THÀNH (2026-03-27)

**Mục tiêu:**
Giải quyết root cause của mọi lỗi T2/T4: semantic gap giữa user language và legal corpus.
Xây dựng hệ thống 2 phía: chuẩn hóa query (QueryIntent) + annotate corpus (NodeMetadata).

**Vấn đề cụ thể P5 giải quyết:**
```
User:    "tiệm vàng doanh thu 60 tỷ"
         ↓ không chuẩn hóa
Embed:   "tiệm vàng 60 tỷ"
Corpus:  "hộ kinh doanh vàng bạc đá quý — phân phối hàng hóa — 1%"
Match:   FAIL (semantic gap)

User:    "tiệm vàng doanh thu 60 tỷ"
         ↓ QueryIntent Builder
Intent:  {who: HKD, activity_group: goods_distribution, revenue: 60B}
         ↓ NodeMetadata reranker
Corpus:  {who: HKD, activity_group: goods_distribution, content_type: tax_rate}
Match:   SUCCESS (metadata bridge)
```

#### P5.1 — QueryIntent Builder

**Kỹ thuật: Hybrid (Rule + LLM + Merge)**

```python
# Bước 1: Rule Parser (deterministic, ~0ms, no API cost)
# Regex + keyword lookup trên query text
rules = [
  # WHO detection
  (r"hộ kinh doanh|hkd|kinh doanh cá thể|tiệm|cửa hàng",
   → who=HKD, confidence=0.9),
  (r"lương|tiền công|thu nhập cá nhân|tncn",
   → who=individual, activity=salary_wages, confidence=0.95),

  # FINANCIALS extraction
  (r"(\d+[\.,]?\d*)\s*(tỷ|triệu|nghìn)",
   → extract amount, unit normalize → VND),

  # ACTIVITY GROUP
  (r"tiệm vàng|vàng bạc|kim hoàn",
   → activity_group=goods_distribution, confidence=0.85),
  (r"nhà hàng|quán ăn|café",
   → activity_group=manufacturing_transport, confidence=0.9),
]

# Bước 2: LLM Extractor (chỉ khi confidence < threshold)
# Dùng 5W2H structured prompt:
# WHO: ai là người nộp thuế?
# WHAT: thu nhập / doanh thu từ hoạt động gì?
# WHICH LAW: nhóm thuế nào (PIT / HKD / VAT)?
# HOW MUCH: con số cụ thể?
# WHEN: năm tính thuế?
# HOW (intent): hỏi để tính hay để hiểu điều kiện?
# WHY (context): lần đầu? đặc thù?

# Bước 3: Merge + Conflict Resolution
# rule.confidence > llm.confidence → dùng rule (rule deterministic hơn)
# conflict → flag, dùng giá trị có confidence cao hơn
```

**Input/Output:**
- Input: raw query string (ví dụ: "tôi kinh doanh mỹ phẩm online, doanh thu 800 triệu/năm, thuế bao nhiêu?")
- Output: `QueryIntent` JSON với mọi field có `confidence` score

**Test: 15 câu query đa dạng** (HKD, TNCN, edge cases)

#### P5.2 — NodeMetadata Annotation (offline)

**Kỹ thuật:**
- LLM annotation pass trên mọi node đã parse (offline, chạy 1 lần)
- System prompt với:
  - Zero-hallucination instruction
  - Strict ENUM enforcement (chỉ canonical values)
  - 3 few-shot examples (HKD rate, TNCN lottery, ambiguous condition)
  - Validation loop: LLM output → regex validate → retry ≤3 lần → fallback confidence=0.0
- Ghi kết quả vào ChromaDB metadata (persistent)

**3 few-shot examples:**
```
Ex1 (HKD rate):     "hộ kinh doanh phân phối hàng hóa... tỷ lệ 1%"
→ {who: HKD, activity: goods_distribution, content_type: tax_rate, confidence: 0.95}

Ex2 (TNCN lottery): "trúng thưởng xổ số... thuế suất 10%..."
→ {who: individual, activity: lottery_prizes, content_type: tax_rate, confidence: 0.90}

Ex3 (condition):    "trường hợp doanh thu không xác định được..."
→ {who: HKD, activity: UNSPECIFIED, content_type: condition_rule, confidence: 0.70}
```

#### P5.3 — Two-Stage Retrieval với NodeMetadata Reranker

**Kỹ thuật:**
```python
# Stage 1: Vector search (hiện tại — giữ nguyên)
hits = vector_store.search(query_text, n_results=20)

# Stage 2: Reranker (mới)
def rerank(hits, query_intent):
    for hit in hits:
        meta = hit.metadata  # NodeMetadata từ ChromaDB
        bonus = 0.0
        # Metadata match bonus
        if meta.tax_domain intersects query_intent.tax_domain: bonus += 0.3
        if meta.activity_group intersects query_intent.activity_group: bonus += 0.2
        if meta.who intersects query_intent.who: bonus += 0.1
        # Temporal relevance
        if meta.legal.effective_from <= today: bonus += 0.1
        # Content type bonus cho calculation queries
        if query_intent.intent.requires_calculation and meta.content_type == "tax_rate":
            bonus += 0.2
        hit.final_score = hit.semantic_score * (1 + bonus)
    return sorted(hits, key=lambda h: h.final_score, reverse=True)
```

**Kết quả mong muốn P5:**
- QueryIntent coverage ≥ 90% trên test set 15 queries
- NodeMetadata annotation: confidence > 0.7 cho ≥ 85% nodes
- Reranker: target doc trong top-5 cho ≥ 90% queries

---

### Phase P6 — "Dynamic Prompt Assembly" ✅ HOÀN THÀNH (2026-03-27)

**Mục tiêu:**
Thay vì dùng 1 monolithic prompt cho mọi query, tự động lắp ghép prompt phù hợp theo QueryIntent.
Giảm token cost, tăng accuracy cho từng domain.

**Vấn đề P6 giải quyết:**
```
Monolithic prompt hiện tại:
→ Phải cover HKD + TNCN + điều kiện + tính toán trong 1 prompt
→ Prompt to, LLM "phân tâm", accuracy thấp hơn
→ Tốn token cho context không liên quan

Dynamic prompt:
→ HKD query → load HKD specialist prompt (ngắn, focused)
→ TNCN query → load PIT specialist prompt
→ Simple query → Flash model (rẻ hơn)
→ Complex multi-step → Pro model (chính xác hơn)
```

**Kỹ thuật:**

```python
# Specialist Prompts (lưu file riêng, dễ update)
SPECIALIST_PROMPTS = {
    "HKD": load("prompts/hkd_specialist.txt"),
    # Focus: PP doanh thu vs PP lợi nhuận, ngưỡng 500M/3B,
    #         business_category mapping, GTGT + TNCN breakdown

    "PIT": load("prompts/pit_specialist.txt"),
    # Focus: lũy tiến 5 bậc, giảm trừ gia cảnh, cư trú/không cư trú,
    #         Luật 109/2025/QH15 (hiệu lực 01/07/2026)

    "GENERAL": load("prompts/general.txt"),
    # Fallback khi domain không rõ
}

# Tool loading theo intent
def load_tools(query_intent):
    tools = ["search_legal_docs"]  # luôn có
    if query_intent.intent.requires_calculation:
        tools.append("calculator")
    if query_intent.tax_domain == "HKD":
        tools.append("get_guidance")  # Sổ tay HKD
    return tools

# Model routing
def route_model(query_intent):
    if query_intent.intent.requires_calculation and len(query_intent.scenarios) > 1:
        return "gemini-2.5-pro"   # complex: multi-scenario, cần tính nhiều
    return "gemini-2.5-flash"     # simple: single calculation
```

**Kết quả mong muốn P6:**
- Token reduction: giảm ~30-40% so với monolithic prompt
- Routing accuracy: ≥ 95% queries chọn đúng specialist prompt
- Model routing: complex queries → Pro model khi cần

---

### Phase v4-E2E — "Benchmark Loop đến Pass" 🔄 ĐANG TIẾN HÀNH

**Mục tiêu:** Đạt T2 ≥ 0.900 và T4 ≥ 0.975 qua vòng lặp Fix → Benchmark → Analyze.

#### Các fixes đã áp dụng (R26 → R28)

| Fix | Nội dung | Tác động |
|---|---|---|
| Fix1 | doc_id normalization trong eval_adapter | Đếm đúng T2 citations |
| Fix2 | Explain path → luôn vào RAG | T3 0.295 → 0.954 |
| Fix3 | Clarification → RAG fallback | Routing đúng hơn |
| Fix4 | Cap=4 unique docs (từ cap=8) | T2 precision x2 |
| Fix5-6 | Query augmentation domain terms | Retrieval HKD/TNCN tốt hơn |
| Fix7 | TNCN: check "tncn" abbreviation | Prevent Q205 regression |
| Fix8 | "thuế khoán" → "khai thuế HKD" bridge | Vocabulary gap 2013→2026 |

#### Annotations đã hoàn thành (ChromaDB)

| Doc | Chunks | Hiệu lực | Ghi chú |
|---|---|---|---|
| 68_2026_NDCP | 159 | 05/03/2026 | ✅ HKD core — re-annotated |
| 18_2026_TTBTC | 46 | 2026 | ✅ HKD transition rules |
| 152_2025_TTBTC | 29 | 2025 | ✅ Hướng dẫn NĐ68 |
| 109_2025_QH15 | 203 | 01/07/2026 | ✅ Luật TNCN mới |
| 111_2013_TTBTC | 441 | 2013 | ✅ Luật TNCN cũ (context) |
| 92_2015_TTBTC | 233 | 2015 | ✅ TT cũ (context) |

#### Roadmap tiếp theo (ước tính)

```
R29 ✅ DONE — T2=0.502, T4=0.500, pass=70/225 (31.1%)
    +0.148 T2 từ R28. Vượt dự kiến.
    │
    ▼
R30–R31 — Fix 4 topics 0%/thấp + T4 synthesis
    Mục tiêu: T2 ≈ 0.62–0.70, T4 ≈ 0.60
    Việc làm:
    • "Ủy quyền quyết toán TNCN" 0/6 — investigate retrieval
    • "Miễn giảm thuế" 0/3 — investigate retrieval
    • "Giảm trừ gia cảnh" 1/8 — cải thiện T4 (số liệu cụ thể)
    • "Quyết toán thuế TNCN" 1/7 — multi-doc query issues
    • T4: few-shot prompt với số liệu cụ thể trong RAG synthesis
    • Fix retrieval misses: Q10, Q42, Q46
    │
    ▼
R32–R33 — Annotate 108_2025_QH15 (680 chunks — lớn nhất)
    Mục tiêu: T2 ≈ 0.75–0.85
    Việc làm:
    • python -m src.retrieval.node_annotator --doc-id 108_2025_QH15 --write-chroma
    • Annotate 125_2020_NDCP, 1296_CTNVT, So_Tay_HKD nếu cần
    │
    ▼
R34+ — Fine-tune + final benchmark
    Mục tiêu: T2 ≥ 0.900, T4 ≥ 0.975
    Điều kiện switch production → v4
```

**Ước tính timeline:** ~3–4 tuần (R29 → pass threshold)

**Switch criterion:**
```
IF v4.T1 >= 1.000
AND v4.T2 >= 0.900
AND v4.T4 >= 0.975
THEN switch app.py → v4  (1 dòng thay đổi)
```

---

### Phase v4-Switch — "Wire + Deploy" (SAU KHI PASS BENCHMARK)

**Mục tiêu:** Đưa v4 lên production, deploy online cho nhóm beta users (~3–5 người).

**Chiến lược deployment: Streamlit Community Cloud (miễn phí)**
- Link dạng `https://taxai.streamlit.app`
- Đủ cho 3-5 concurrent users
- Sleep sau 7 ngày không dùng (wake ~30s khi có user mới)

**Việc cần làm:**

1. **Wire v4 vào `app.py`** (~2 giờ):
   ```python
   # Thay thế:
   from src.generation.answer_generator import AnswerGenerator
   generator = AnswerGenerator()

   # Bằng:
   from src.agent.pipeline_v4.orchestrator import PipelineV4
   pipeline = PipelineV4()
   ```

2. **Cập nhật `_render_sources()`** trong app.py để hiển thị citations format mới của v4:
   - v4 trả `doc_number` + `title` + `note` (thay vì `document_number` + `breadcrumb`)

3. **Tắt Neo4j dependency** (chỉ dùng cho sidebar doc list, đã có `try/except`):
   - Hardcode danh sách docs thay vì query Neo4j → sidebar vẫn hiển thị

4. **Dọn `requirements.txt`**:
   - Bỏ: `neo4j`, `PyPDF2` (duplicate), `google-generativeai` (deprecated)
   - Giữ: `streamlit`, `chromadb`, `sentence-transformers`, `google-genai`, ...

5. **Commit ChromaDB data** vào repo (62MB — dưới limit GitHub):
   ```bash
   git add data/chroma/
   ```

6. **Deploy lên Streamlit Community Cloud**:
   - connect GitHub repo → set secret `GEMINI_API_KEY`
   - Done — có link public

**Ước tính thời gian:** ~2–3 ngày sau khi pass benchmark

---

### Phase v4-UI — "UI Hoàn thiện" (SONG SONG VỚI BETA)

Cải tiến `app.py` dựa trên feedback từ beta users:

| Feature | Mô tả | Độ ưu tiên |
|---|---|---|
| Calculator breakdown | Hiển thị: doanh thu × tỷ lệ = thuế (khi dùng template) | Cao |
| Assumption box | Hiển thị rõ `assumptions[]` từ v4 | Cao |
| Session memory | Multi-turn: nhớ context giữa các câu | Trung bình |
| Disclaimer tự động | Cảnh báo khi answer dùng luật chưa hiệu lực (01/07/2026) | Trung bình |
| Mobile layout | Streamlit không mobile-friendly → cần nếu user dùng phone | Thấp (sau feedback) |

---

### Phase v4-Full — "Scale Production" (SAU KHI CÓ USER DATA)

> Chỉ xây sau khi có data thực tế từ beta users (≥ 100 conversations).

| Component | Mô tả | Khi nào |
|---|---|---|
| Multi-turn Session State | Versioned state, temporal authority | Sau 100 real conversations |
| Delta Retrieval | User đính chính → fetch bổ sung, không re-route | Sau multi-turn |
| Dynamic Dependency Graph | Branching conditions thay COVERAGE_RULES static | Sau validation data |
| Circuit Breaker | consecutive_failures > 3 → alert | Cùng lúc scale |
| Scenario Branching | Tối đa 2 scenarios khi confidence thấp | Sau user feedback |
| FastAPI + React | Nếu cần mobile app hoặc nhiều concurrent users | Sau traction |
| Per-stage Eval Metrics | Đo accuracy từng stage riêng | Sau E2E stable |

---

## 6. Quyết định thiết kế quan trọng (Design Decisions)

### D1: Tại sao không hardcode tax rates trong Python?

**Vấn đề:** Luật thay đổi liên tục (NĐ68 thay NĐ40, Luật 109 thay Luật TNCN 2007).
**Quyết định:** Rates trong `calculator_tools.py` là config-driven constants với citation. Khi luật thay đổi → chỉ cần update 1 file.
**Anti-pattern:** Đặt rates trong QueryIntent schema hay hardcode trong orchestrator.

### D2: Tại sao không dùng LLM validate LLM?

**Vấn đề:** LLM có thể hallucinate trong 6.1 → dùng LLM khác để check → lỗi lan truyền.
**Quyết định:** Final Validator là pure Python (regex, số học). Chỉ 2 checks: VND present + assumption mention.
**Anti-pattern:** "LLM Judge" để verify answer của LLM khác.

### D3: Tại sao tách Legal Reasoner (6.1) khỏi Synthesizer (6.3)?

**Vấn đề:** Monolithic prompt vừa đọc luật vừa tính vừa viết answer → cả 3 có thể sai.
**Quyết định:** 3 bước rõ ràng:
- 6.1: LLM **đọc** RAG → extract params (không tính)
- 6.2: Python **tính** (không đọc, không viết)
- 6.3: LLM **viết** → dùng số từ 6.2 (không tính lại)
**Kiểm chứng:** State Lock prevent LLM từ override tax_amount.

### D4: Tại sao COVERAGE_RULES là config, không phải trong NodeMetadata?

**Vấn đề:** Required fields khác nhau theo intent, không phải theo node.
Cùng 1 node "HKD tax rate 1%" có thể serve "calculate" (cần revenue) và "explain" (không cần).
**Quyết định:** COVERAGE_RULES[template] = runtime config. NodeMetadata chỉ chứa content classification.
**Anti-pattern:** required_conditions field trong NodeMetadata (coupling sai tầng).

### D5: Tại sao QueryIntent Builder là hybrid Rule + LLM, không phải LLM-only?

**Vấn đề:** LLM-only: chậm (~500ms), tốn cost, không deterministic.
Rule-only: miss edge cases, brittle với ngôn ngữ đời thường.
**Quyết định:** Rule parser trước (~0ms, no cost). Chỉ gọi LLM khi confidence < threshold (~200ms, tiết kiệm).
**Design:** Merge với conflict resolution: rule wins khi tie vì deterministic.

---

## 7. Anti-patterns — KHÔNG làm lại

| Pattern | Lý do không làm | Thay bằng |
|---|---|---|
| Hardcode tax rates trong Python code | Mất RAG authority khi luật thay đổi | Config constants trong calculator_tools.py |
| LLM validate LLM (Final Validator = LLM) | Lỗi lan truyền, không deterministic | Python regex + số học |
| Pre-compute tax trước legal reasoning | Đảo thứ tự: không biết eligibility → không biết phải tính gì | 6.1 reason first → 6.2 compute |
| required_conditions trong NodeMetadata | Coupling sai: same node, different intent = different requirements | COVERAGE_RULES config |
| C3 global temporal penalty | Không thể áp global rule — context phụ thuộc query | NodeMetadata.legal.effective_from per node |
| P2.2 authority hierarchy penalty | QA cần evidence correctness, không phải authority ranking | Reranker metadata bonus thay penalty |
| B4 query-time amendment resolution | Cần index-time metadata, không phải query-time | NodeMetadata.legal fields |
| Multi-turn state trước khi có user data | Over-engineering — chưa biết cần gì | Chờ production data, dùng COVERAGE_RULES static |
| LLM-only QueryIntent | Chậm + tốn cost + không deterministic | Hybrid: Rule first, LLM khi uncertain |
| cap=8 cho citations | Precision quá thấp: 1 expected / 8 cited → T2=0.25 | cap=4: T2=0.50 cho cùng trường hợp |
| Thêm "thu nhập cá nhân" khi query có "tncn" | Query augmentation phá retrieval khi từ viết tắt đã có | Check cả viết tắt trước khi append |
| `&` cuối command khi `run_in_background=True` | Fork 2 Python process song song → quota 429 + kết quả sai | Chỉ dùng `run_in_background=True`, không có `&` |

---

## 8. Files quan trọng — Index

```
src/
├─ agent/
│   ├─ template_registry.py      ← Template Registry (PIT/HKD calculators) ✅
│   ├─ generator.py              ← GeminiLLM wrapper (Stage 3 Synthesizer) ✅
│   ├─ pipeline_v4/
│   │   ├─ orchestrator.py       ← Main pipeline v4 (routing, RAG, calc) ✅
│   │   ├─ state.py              ← PipelineState + Immutability Lock ✅
│   │   ├─ validation.py         ← COVERAGE_RULES + Validation Layer ✅
│   │   ├─ final_validator.py    ← VND match + assumption check ✅
│   │   ├─ audit.py              ← JSONL audit trail ✅
│   │   ├─ query_intent.py       ← QueryIntent Builder (P5.1) ✅
│   │   ├─ prompt_assembler.py   ← Dynamic Prompt Assembly (P6) ✅
│   │   └─ eval_adapter.py       ← V4Adapter cho eval_runner ✅
│   ├─ planner.py                ← R25 PRODUCTION (không touch)
│   └─ pipeline.py               ← Pipeline v2 (deprecated)
│
├─ retrieval/
│   ├─ hybrid_search.py          ← BM25 + Vector search ✅
│   ├─ reranker.py               ← P5.3 NodeMetadata Reranker ✅
│   ├─ node_annotator.py         ← Offline annotation → ChromaDB ✅
│   ├─ vector_store.py           ← ChromaDB wrapper ✅
│   └─ embedder.py               ← Vietnamese-SBERT embeddings ✅
│
├─ tools/
│   └─ retrieval_tools.py        ← search_legal_docs, get_article, get_guidance ✅
│
tests/
├─ eval_runner.py                ← Benchmark runner (T1/T2/T3/T4 scoring) ✅
└─ test_parser_regression.py     ← Parser regression tests (CLAUDE.md)

data/
├─ eval/questions.json           ← 225 test questions (40 cũ + 185 mới)
├─ eval/results/                 ← Benchmark results per run
├─ chroma/                       ← ChromaDB persistent (4381 chunks indexed)
└─ parsed/                       ← Parsed legal documents (JSON)

app.py                           ← Streamlit UI (hiện dùng pipeline cũ, sẽ switch v4)
```

---

## 9. Corpus văn bản pháp lý

**Tổng:** 4381 chunks trong ChromaDB. Cột "Annotated" = đã có NodeMetadata (P5.2).

| doc_id | Văn bản | Hiệu lực | Chunks | Annotated |
|---|---|---|---|---|
| `109_2025_QH15` | Luật Thuế TNCN mới — biểu thuế 5 bậc, giảm trừ 15.5M/6.2M | 01/07/2026 | 203 | ✅ |
| `68_2026_NDCP` | NĐ 68 — Thuế HKD: PP doanh thu + PP lợi nhuận | 05/03/2026 | 159 | ✅ |
| `18_2026_TTBTC` | TT 18 BTC — Hướng dẫn HKD chuyển đổi 2026 | 2026 | 46 | ✅ |
| `20_2026_NDCP` | NĐ 20 — Quản lý thuế 2026 | 2026 | 141 | ❌ |
| `152_2025_TTBTC` | TT 152 BTC — Hướng dẫn NĐ68 | 2025 | 29 | ✅ |
| `117_2025_NDCP` | NĐ 117 — Quản lý thuế HKD | 2025 | 92 | ❌ |
| `373_2025_NDCP` | NĐ 373 — Xử phạt vi phạm hành chính thuế | 2025 | 67 | ❌ |
| `310_2025_NDCP` | NĐ 310 — Điều chỉnh thu nhập tính thuế | 2025 | 130 | ❌ |
| `198_2025_QH15` | Luật Quản lý Thuế 2025 | 2025 | 97 | ❌ |
| `149_2025_QH15` | Luật bổ sung 149 | 2025 | 7 | ❌ |
| `110_2025_UBTVQH15` | Nghị quyết UBTVQH15 110 | 2025 | 4 | ❌ |
| `108_2025_QH15` | Luật Thuế TNCN 108 — bổ sung | 2025 | ~680 | ❌ ← ưu tiên cao |
| `125_2020_NDCP` | NĐ 125 — Xử phạt hành chính thuế | 2020 | ~571 | ❌ |
| `126_2020_NDCP` | NĐ 126 — Quản lý thuế | 2020 | — | ❌ |
| `92_2015_TTBTC` | TT 92 — Thuế TNCN cũ (2015) | 2015 | 233 | ✅ |
| `111_2013_TTBTC` | TT 111 — Thuế TNCN cũ (2013) | 2013 | 441 | ✅ |
| `1296_CTNVT` | Công văn 1296 — Hướng dẫn quyết toán TNCN | 2024 | 33 | ❌ |
| `So_Tay_HKD` | Sổ tay HKD — 26 bảng biểu thực tế | 2024 | 47 | ❌ |

---

## 10. Remaining Issues (R25 Production)

Những câu hỏi R25 chưa đạt điểm tối đa:

| Q | Score | Issue |
|---|---|---|
| Q10 | 0.75 | T2 partial: chỉ cite 1/2 docs cần thiết |
| Q11 | 0.75 | T2 partial: chỉ cite 1/2 docs cần thiết |
| Q13 | 0.75 | T4 partial: trả lời thiếu khía cạnh thực tế |
| Q17 | 0.75 | T4 partial: trả lời thiếu khía cạnh thực tế |

> Các câu này sẽ được giải quyết tự nhiên bởi v4 với NodeMetadata reranker (Q10/Q11 — multi-doc retrieval)
> và specialist prompt (Q13/Q17 — practical context).
