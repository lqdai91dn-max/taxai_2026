# Graph Schema — Hệ thống pháp luật Việt Nam

> **Status:** LOCKED (Group 3 decisions finalized 2026-03-13)

---

## Node Types

### Type A — Văn bản pháp luật (có cấu trúc Điều/Khoản)

```
(:Document)
  id             String   "109_2025_QH15"
  doc_type       String   "Luật" | "Nghị định" | "Thông tư" | "Nghị quyết" | "Công văn"
  doc_number     String   "109/2025/QH15"
  title          String
  issue_date     Date
  effective_date Date
  valid_from     Date     -- bắt đầu có hiệu lực (= effective_date)
  valid_to       Date?    -- null = vẫn còn hiệu lực; set khi bị supersede
  status         String   "active" | "pending" | "superseded" | "amended"
  hierarchy_rank Int      1=Hiến pháp, 2=Luật/NQ, 3=NĐ (pháp lệnh), 4=NĐ, 5=TT, 6=QĐ, 7=CV

(:Chapter)      -- Chương
(:Section)      -- Mục
(:Article)      -- Điều
(:Clause)       -- Khoản
(:Point)        -- Điểm
(:SubPoint)     -- Tiết

  id            String   node_id từ parsed JSON
  doc_id        String   FK → Document.id
  index         String   "I", "1", "a", "đ"
  title         String?
  content       String?
  lead_in_text  String?
  breadcrumb    String
```

### Type B — Văn bản hướng dẫn (GuidanceChunk)

> **Decision 3.1:** GuidanceChunk là node type riêng (không reuse Khoản).
> Lý do: Type B không có legal authority, mixed content (text+table), không có node_index hierarchy.
> `source_type` distinguishes guidance category cho retrieval filtering.

```
(:GuidanceDocument)
  id            String   "1296_CTNVT" | "So_Tay_HKD"
  doc_id        String   (= id, alias)
  source_org    String   "Cục Thuế" | "Bộ Tài chính"
  title         String
  doc_type      String   "Công văn" | "Sổ tay"
  hierarchy_rank Int     7  -- Công văn/hướng dẫn, non-binding

(:GuidanceChunk)
  id            String   "{doc_id}_chunk_{n}"
  doc_id        String   FK → GuidanceDocument.id
  source_type   String   "Công văn" | "Sổ tay" | "FAQ"  -- category filter
  content       String
  topic_tags    String[] ["HKD", "thuế GTGT", "khai thuế"]
  chunk_index   Int
  page_number   Int?
```

---

## Relationship Types

### Hierarchy (Type A)
```
(:Document)       -[:HAS_CHAPTER]->    (:Chapter)
(:Chapter)        -[:HAS_SECTION]->    (:Section)
(:Chapter)        -[:HAS_ARTICLE]->    (:Article)
(:Section)        -[:HAS_ARTICLE]->    (:Article)
(:Article)        -[:HAS_CLAUSE]->     (:Clause)
(:Clause)         -[:HAS_POINT]->      (:Point)
(:Point)          -[:HAS_SUBPOINT]->   (:SubPoint)
```

### Hierarchy (Type B)
```
(:GuidanceDocument) -[:HAS_CHUNK]-> (:GuidanceChunk)
```

### Cross-references (nội bộ)
```
(:Clause|:Point)  -[:REFERENCES {text_match: String}]-> (:Article|:Clause|:Point)
```

### Cross-document
```
(:Document)  -[:AMENDS     {effective_date: Date}]->    (:Document)
(:Document)  -[:SUPERSEDES {effective_date: Date}]->    (:Document)
(:Document)  -[:IMPLEMENTS]->                           (:Document)
  -- VD: NĐ 68/2026 IMPLEMENTS Luật 109/2025
  -- VD: TT 152/2025 IMPLEMENTS Luật 109/2025
  -- VD: NQ 110/2025 AMENDS Luật 109/2025
```

### Type A ↔ Type B

> **Decision 3.2:** EXPLAINED_BY có `confidence` + `method`.
> Auto-detect dùng semantic similarity (threshold ≥ 0.82 để add edge).
> Manual mapping luôn add (confidence=1.0, method="manual").
> Ở retrieval time: boost GuidanceChunk khi confidence ≥ 0.82 và source_type phù hợp context.

```
(:Article|:Clause) -[:EXPLAINED_BY {
  confidence: Float,   -- 0.0–1.0, semantic similarity score
  method:     String   -- "auto" | "manual"
}]-> (:GuidanceChunk)
```

### Tables
```
(:Document|:GuidanceDocument) -[:HAS_TABLE]-> (:Table)
(:Article)                     -[:HAS_TABLE]-> (:Table)
  -- Tables hiện tại là top-level array trong parsed JSON
```

---

## Legal Hierarchy Ranking

> **Decision 3.3:** `hierarchy_rank` lưu ở Document level (không phải runtime).
> Dùng để weighting khi trả lời conflict queries ("NĐ nói X, TT nói Y → follow NĐ").
> `valid_from`/`valid_to` cho temporal validity queries.

```python
HIERARCHY_RANK = {
    "Hiến pháp":    1,
    "Luật":         2,
    "Nghị quyết":   2,   # NQ của QH/UBTVQH
    "Pháp lệnh":    3,
    "Nghị định":    4,
    "Thông tư":     5,
    "Quyết định":   6,
    "Công văn":     7,
    "Sổ tay":       7,   # non-binding guidance
}
```

---

## Documents hiện tại

```
Luật 109/2025/QH15   (rank=2, status=pending, valid_from=2026-07-01)
  ├─[:IMPLEMENTS]─< NĐ 68/2026/NĐ-CP   (rank=4, valid_from=2026-03-05)
  ├─[:IMPLEMENTS]─< TT 152/2025/TT-BTC  (rank=5, valid_from=2026-01-01)
  └─[:AMENDS]─< NQ 110/2025/UBTVQH15   (rank=2, valid_from=2026-01-01)

NĐ 310/2025/NĐ-CP    (rank=4, status=active, valid_from=2026-01-16)
  └─[:AMENDS]─> NĐ 125/2020/NĐ-CP  (external stub)

Luật 149/2025/QH15   (rank=2, status=active, valid_from=2026-01-01)
  └─[:AMENDS]─> Luật Thuế GTGT  (external stub)

NĐ 373/2025/NĐ-CP    (rank=4, status=active, valid_from=2026-02-14)
  └─[:AMENDS]─> NĐ 126/2020/NĐ-CP  (external stub)

TT 152/2025/TT-BTC   (rank=5, status=active, valid_from=2026-01-01)
  └─[:SUPERSEDES]─> TT 88/2021/TT-BTC  (external stub, valid_to=2026-01-01)

NĐ 20/2026/NĐ-CP     (rank=4, status=active, valid_from=2026-01-15)
NĐ 117/2025/NĐ-CP    (rank=4, status=active)
NQ 198/2025/QH15     (rank=2, status=active, valid_from=2025-05-17)
TT 18/2026/TT-BTC    (rank=5, status=active, valid_from=2026-03-05)

-- Type B (non-binding):
CV 1296/CTNVT         (rank=7, GuidanceDocument)
Sổ tay HKD            (rank=7, GuidanceDocument)
```

---

## Query patterns quan trọng cho chatbot

**1. Direct lookup:**
```cypher
MATCH (a:Article {id: "doc_109_2025_QH15_chuong_II_dieu_22"})
RETURN a
```

**2. Full context của một Điều:**
```cypher
MATCH (a:Article {id: $article_id})
OPTIONAL MATCH (a)-[:HAS_CLAUSE]->(k:Clause)-[:HAS_POINT]->(d:Point)
OPTIONAL MATCH (a)<-[:HAS_ARTICLE]-(ch:Chapter)
RETURN a, k, d, ch
```

**3. Cross-references từ một node:**
```cypher
MATCH (n {id: $node_id})-[:REFERENCES]->(target)
RETURN target
```

**4. Tìm implementation chain:**
```cypher
MATCH (law:Document {doc_number: "109/2025/QH15"})
      <-[:IMPLEMENTS*1..3]-(impl:Document)
RETURN impl ORDER BY impl.hierarchy_rank
```

**5. Validity check — chỉ lấy doc còn hiệu lực tại thời điểm T:**
```cypher
MATCH (doc:Document)
WHERE doc.valid_from <= date($query_date)
  AND (doc.valid_to IS NULL OR doc.valid_to > date($query_date))
RETURN doc
```

**6. Conflict resolution — ưu tiên theo hierarchy_rank:**
```cypher
MATCH (a:Article)-[:HAS_CLAUSE]->(k:Clause)
MATCH (k)<-[:HAS_CLAUSE]-(a2)<--(doc:Document)
WHERE doc.status IN ["active", "pending"]
RETURN k, doc.hierarchy_rank ORDER BY doc.hierarchy_rank ASC
```

**7. Plain-language explanation (với confidence filter):**
```cypher
MATCH (a:Article {id: $article_id})-[r:EXPLAINED_BY]->(chunk:GuidanceChunk)
WHERE r.confidence >= 0.82
RETURN chunk.content, r.confidence, r.method
ORDER BY r.confidence DESC
```
