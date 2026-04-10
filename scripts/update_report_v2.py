"""Update BAO_CAO_DU_AN_v2.md: add Mermaid diagrams + full 225 questions Appendix B."""
import json
from collections import defaultdict

# Read current v2 markdown
with open('BAO_CAO_DU_AN_v2.md', encoding='utf-8') as f:
    content = f.read()

# --- 1. Add Mermaid diagrams after section 2.2 ---
MERMAID_SECTION = r"""
### 2.4 Sơ đồ luồng dữ liệu (Mermaid)

```mermaid
flowchart TD
    A([fa:fa-user Người dùng]) --> B[Nhập câu hỏi]
    B --> C{Pre-route\n_pre_route\(\)}
    C -->|OOD / ngoài phạm vi| D([❌ Từ chối\nNgoài phạm vi tư vấn])
    C -->|Liên quan thuế| E{Cache Lookup\ncosine ≥ 0.92?}
    E -->|HIT| F([⚡ Cache Hit\n< 200ms])
    E -->|MISS| G[TaxAIAgent\nAgentic Loop]
    G --> H[Gemini Flash\nFunction Calling]
    H -->|search_legal_docs| I[Hybrid Search\nBM25 + Vector + RRF]
    H -->|calculate_tax_hkd| J[Calculator\nPython deterministic]
    H -->|lookup_deadline| K[Lookup Tables]
    I --> L[Recency Boost\n+ Expired Filter]
    L --> H
    J --> H
    K --> H
    H -->|DONE đủ thông tin| M[generator.py\nFormat + Citation]
    M --> N{citations = empty?}
    N -->|Yes| O[Citation Fallback\ntop-2 chunks]
    N -->|No| P[Cache Store\nFull answer]
    O --> P
    P --> Q([✅ Câu trả lời\n+ Trích dẫn pháp luật])
```

```mermaid
flowchart LR
    subgraph PARSE [Parsing Pipeline - Offline]
        P1[Stage 1\nExtract\ndocx/pdf/gemini] --> P2[Stage 2\nNormalize\nOCR fix]
        P2 --> P3[Stage 3\nParse\nState Machine]
        P3 --> P4[Stage 4\nPatch\ndoc-specific]
        P4 --> P5[Stage 5\nValidate]
        P5 --> P6[(data/parsed/\ndoc_id.json)]
    end
    subgraph EMBED [Embedding - Offline]
        P6 --> E1[embedder.py\nvietnamese-sbert]
        E1 --> E2[(ChromaDB\nVector Index)]
    end
    subgraph BM25 [BM25 - Runtime]
        P6 --> B1[bm25_index.py\nIn-memory]
    end
```

```mermaid
flowchart TD
    subgraph EVAL [Hệ thống Đánh giá 4 Tầng]
        QQ([Câu hỏi + Câu trả lời]) --> T1
        T1{T1: Tính toán\nnếu có yêu cầu} -->|Sai số < 1 percent| T2
        T1 -->|Sai số liệu| FAIL1([❌ FAIL T1])
        T2{T2: Trích dẫn\ncitation score} -->|score ≥ threshold| T3
        T2 -->|Thiếu / sai văn bản| FAIL2([❌ FAIL T2])
        T3{T3: Tool Selection\nĐúng loại tool} -->|search/calc/lookup đúng| T4
        T3 -->|Dùng sai tool| FAIL3([❌ FAIL T3])
        T4{T4: Key Facts\n≥ 80 percent facts} -->|Đủ nội dung| PASS
        T4 -->|Thiếu key facts| FAIL4([❌ FAIL T4])
        PASS([✅ PASS\nscore ≥ 0.70])
    end
```

"""

# Insert after section 2.3 block
ANCHOR = "    Hiển thị cho người dùng\n```\n"
if ANCHOR in content:
    content = content.replace(ANCHOR, ANCHOR + MERMAID_SECTION, 1)
    print("✅ Mermaid diagrams added")
else:
    print("⚠️  Anchor not found for Mermaid insertion")

# --- 2. Build full Appendix B ---
d = json.load(open('data/eval/questions.json', encoding='utf-8'))
topics = defaultdict(list)
for q in d:
    topics[q.get('topic', '')].append(q)

icon = {'easy': '🟢', 'medium': '🟡', 'hard': '🔴'}
lines = []
lines.append("## Phụ lục B: Bộ câu hỏi benchmark 225 câu\n")
lines.append("**Ký hiệu độ khó:** 🟢 Easy | 🟡 Medium | 🔴 Hard  ")
lines.append("**Ký hiệu loại:** 🧮 Cần tính toán\n")
lines.append("---\n")

for i, t in enumerate(sorted(topics.keys()), 1):
    qs = sorted(topics[t], key=lambda x: x['id'])
    lines.append(f"### B.{i} {t} ({len(qs)} câu)\n")
    lines.append("| Câu | Độ khó | Câu hỏi | Văn bản liên quan |")
    lines.append("|---|---|---|---|")
    for q in qs:
        diff = icon.get(q['difficulty'], '🟡')
        calc = ' 🧮' if q.get('needs_calculation') else ''
        qtext = q['question'][:130].replace('|', '╎')
        if len(q['question']) > 130:
            qtext += '...'
        docs = ', '.join(q.get('expected_docs', [])[:3])
        lines.append(f"| Q{q['id']}{calc} | {diff} | {qtext} | {docs} |")
    lines.append("")

appendix_b_new = "\n".join(lines)

# Replace old appendix B section
appendix_start = content.find("## Phụ lục B:")
if appendix_start >= 0:
    content = content[:appendix_start] + appendix_b_new
    content += "\n\n---\n\n*Báo cáo được biên soạn từ source code, git history, và benchmark results của dự án TaxAI.*  \n"
    content += "*Ngày cập nhật: 10/04/2026 | Version: 2.0*\n"
    print("✅ Appendix B replaced with full 225 questions")
else:
    print("⚠️  Appendix B anchor not found")

with open('BAO_CAO_DU_AN_v2.md', 'w', encoding='utf-8') as f:
    f.write(content)

print(f"✅ Done. Total lines: {len(content.splitlines())}")
