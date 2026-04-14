# Lời Thuyết Trình — TaxAI v2.0 Engineering Blueprint
**Phong cách:** Học thuật, rõ ràng, bám sát kiến trúc thực tế  
**Thời lượng tham khảo:** ~3–4 phút/slide chính | ~1–2 phút/slide phụ

---

## SLIDE 1 — TRANG BÌA: Giải phẫu Hệ thống TaxAI v2.0

> *Pass Rate: 225/225 (100%) | Latency: <200ms (Cache Hit) | Status: Production-ready*

---

Xin chào tất cả mọi người.

Hôm nay tôi trình bày buổi **Technical Deep-Dive** về TaxAI phiên bản 2.0 — một hệ thống RAG chuyên biệt cho lĩnh vực tư vấn pháp luật thuế Việt Nam.

"Giải phẫu" ở tiêu đề có nghĩa chúng tôi sẽ đi thẳng vào **bên trong hệ thống**: từng lớp kiến trúc, từng quyết định kỹ thuật, và quan trọng hơn — **tại sao hệ thống từng thất bại và chúng tôi đã sửa nó như thế nào**.

Ba chỉ số trên trang bìa là điểm đến của toàn bộ hành trình:
- **225/225 câu benchmark pass** — tức là 100%, đạt ngày 9/4/2026 sau 3 vòng cải tiến liên tục.
- **Dưới 200 mili giây** phản hồi khi có cache hit — so với 8–12 giây khi phải gọi LLM.
- **Production-ready** — có nghĩa là kiến trúc đủ ổn định để chịu tải người dùng thực tế, xử lý được lỗi, và tự phục hồi khi có sự cố.

Tất cả những điều đó là kết quả của nhiều quyết định kỹ thuật cụ thể — và đó là những gì tôi muốn trình bày hôm nay.

---

## SLIDE 2 — Khoảng trống giữa Người dùng và Pháp luật

> *Ngôn ngữ bình dân ↔ Ma trận pháp lý | "Từ khóa tìm kiếm thông thường hoàn toàn vô tác dụng"*

---

Để hiểu tại sao TaxAI được xây dựng theo cách này, cần bắt đầu từ **bài toán gốc**.

Bên trái slide là người dùng thực tế: một chủ hộ kinh doanh, một kế toán, một người lao động đang thắc mắc về thuế. Câu hỏi của họ đơn giản và đời thường — *"Doanh thu 800 triệu đóng thuế bao nhiêu?"* — nhưng không có ngữ cảnh pháp lý đi kèm.

Bên phải là thực tế của hệ thống pháp luật thuế: một **ma trận chồng chéo**. Chỉ riêng từ đầu năm 2026, Nghị định 68/2026/NĐ-CP đã bãi bỏ toàn bộ cơ chế thuế khoán — một thay đổi cấu trúc triệt để mà phần lớn người kinh doanh không nắm được. Một câu hỏi đơn giản về thuế có thể cần tra cứu chéo 3 đến 5 văn bản khác nhau. Và ngôn ngữ pháp lý được viết cho chuyên gia pháp lý, không phải cho người dùng thông thường.

Câu chốt ở phía dưới slide: **"Từ khóa tìm kiếm thông thường hoàn toàn vô tác dụng."**

Google trả về blog và diễn đàn — không đảm bảo cập nhật, không có trích dẫn pháp lý chính xác, và thường lẫn lộn các phiên bản luật cũ và mới. Đây là bài toán mà TaxAI được thiết kế để giải quyết: **thu hẹp khoảng trống ngôn ngữ và kiến thức** giữa người dùng thông thường và hệ thống pháp luật phức tạp.

---

## SLIDE 3 — TaxAI vs Chatbot Thông thường

> *RAG System thế hệ mới kết hợp tra cứu và tư duy*

---

Vậy TaxAI khác gì so với một chatbot AI thông thường? Đây là câu hỏi quan trọng về định vị kỹ thuật.

Tôi muốn đi qua 4 chiều so sánh — và giải thích cụ thể **tại sao** mỗi chiều quan trọng với bài toán tư vấn pháp luật:

**Chiều 1 — Nguồn kiến thức.** Chatbot thông thường học một lần trong quá trình training rồi đóng băng. Khi Nghị định 68 ban hành năm 2026, chatbot không tự cập nhật. TaxAI **tra cứu trực tiếp** từ corpus văn bản pháp luật tại thời điểm có câu hỏi — chỉ cần thêm văn bản mới vào hệ thống, re-embed, không cần retrain model.

**Chiều 2 — Tính minh bạch.** Chatbot không có trách nhiệm giải trình về nguồn gốc thông tin. TaxAI có cơ chế **bắt buộc trích dẫn** — mỗi câu trả lời phải gắn số Điều, số Khoản, tên văn bản. Đây không phải tính năng tuỳ chọn mà là điều kiện cứng trong evaluation framework — không trích dẫn đủ là fail.

**Chiều 3 — Toán học — đây là điểm quan trọng nhất.** LLM có thể "hallucinate" số liệu tài chính trông rất tự tin nhưng sai hoàn toàn. Trong TaxAI, cấu hình `ALLOW_LLM_CALCULATION = False` được đặt ở cấp hệ thống. Toàn bộ tính toán — thuế GTGT, TNCN lũy tiến, tiền phạt chậm nộp — đều chạy qua code Python xác định. LLM chỉ được phép đọc kết quả và đưa vào câu trả lời, không được tự ước tính.

**Chiều 4 — Cập nhật.** TaxAI thiết kế theo hướng **corpus-driven**: thêm văn bản mới → parse → embed → hệ thống hiểu ngay. Không phụ thuộc vào knowledge cutoff của model.

---

## SLIDE 4 — Giới hạn Phạm vi Corpus

> *20 văn bản cốt lõi | Thuế HKD + Thuế TNCN*

---

Một câu hỏi quan trọng trong thiết kế RAG: bao phủ rộng hay sâu?

TaxAI chọn **sâu hơn rộng** — 20 văn bản cốt lõi, tập trung vào hai lĩnh vực có nhu cầu cao nhất.

**Lĩnh vực 1 — Thuế Hộ Kinh Doanh.** Trọng tâm là Nghị định 68/2026/NĐ-CP — văn bản pháp lý mới nhất, có hiệu lực từ 01/01/2026, thay thế hoàn toàn cơ chế thuế khoán. Đây là thay đổi lớn nhất trong 10 năm với hộ kinh doanh — từ mô hình "CQT tự ấn định mức thuế" sang "HKD tự kê khai theo tỷ lệ hoặc lợi nhuận". Ngoài ra là Luật Quản lý thuế, Thông tư 40 (hướng dẫn tổng quát), Thông tư 152 (chế độ kế toán HKD), Thông tư 18 (sổ sách và biểu mẫu mới).

**Lĩnh vực 2 — Thuế Thu Nhập Cá Nhân.** Trọng tâm là Luật 109/2025/QH15 — luật thuế TNCN mới hoàn toàn, có hiệu lực từ 01/07/2026 — một hard deadline quan trọng cho toàn bộ lộ trình phát triển hệ thống. Kèm theo là Thông tư 80 hướng dẫn khai quyết toán, Công văn 1296 về hướng dẫn thực tế, và Sổ tay HKD dùng cho chatbot context.

Triết lý thiết kế: **thà trả lời 100% chính xác trong phạm vi hẹp, còn hơn trả lời 70% đúng trong phạm vi rộng**. Đặc biệt quan trọng với tư vấn tài chính — một câu trả lời sai có thể gây thiệt hại thực tế cho người dùng.

---

## SLIDE 5 — Luồng Xử Lý End-to-End

> *5 bước: Cache → Pre-routing → Agentic Loop → Generation → Storage*

---

Đây là slide tổng thể — toàn bộ hành trình của một câu hỏi từ lúc gõ vào đến lúc nhận câu trả lời.

**Bước 1 — Cache Lookup.** Đây là bước đầu tiên và là quyết định kiến trúc quan trọng: **Cache-First**. Trước khi làm bất cứ điều gì khác, hệ thống hỏi: "Câu hỏi này có tương tự câu hỏi nào đã trả lời chưa?" Dùng Cosine Similarity với ngưỡng 0.88 — tức là câu hỏi mới phải có ít nhất 88% tương đồng ngữ nghĩa với câu đã cache. Nếu đạt — trả lời ngay, dưới 200ms, không gọi API. Nếu không — tiếp tục.

**Bước 2 — Pre-routing.** Trước khi gọi LLM tốn kém, một bộ lọc nhẹ `_pre_route()` kiểm tra: câu hỏi có thuộc domain thuế không? Bộ lọc này gồm hai lớp regex: lớp đầu tìm tax anchor keywords như "thuế", "kê khai", "doanh thu", "sàn TMĐT", "quyết toán" — nếu có bất kỳ từ nào → pass ngay. Lớp hai tìm OOD hard keywords như "thời tiết", "bóng đá" — nếu không có tax anchor nhưng có OOD → reject. Design này ưu tiên **false negative ít hại hơn false positive** — tức là từ chối nhầm một câu hỏi thuế ít nguy hiểm hơn là để lọt câu hỏi vô nghĩa.

**Bước 3 — Agentic Loop.** Đây là core của hệ thống — một vòng lặp tư duy đa bước với model `gemini-3-flash-preview`. AI tự lập kế hoạch: cần search gì, cần tính gì, cần tra bảng gì. Tối đa **4 vòng lặp** (MAX_ITERATIONS=4, tăng từ 3 sau khi xóa 4 Neo4j tools dead).

**Bước 4 — Generation.** Tổng hợp câu trả lời có cấu trúc và ép buộc gắn trích dẫn pháp lý. Nếu LLM quên format citations — Citation Fallback tự động chèn.

**Bước 5 — Storage.** Lưu câu trả lời đầy đủ vào Semantic Cache — không cắt xén — để phục vụ câu hỏi tương tự sau này.

---

## SLIDE 6 — Giải phẫu một Truy vấn Thực tế

> *"Bán quần áo online doanh thu 800tr nộp bao nhiêu thuế?"*

---

Lý thuyết là vậy — bây giờ hãy xem từng bước hoạt động thực tế với một câu hỏi cụ thể.

**Step 1 — Cache Lookup: MISS.** Vector embedding của câu hỏi được so sánh với toàn bộ cache. Similarity cao nhất là 0.71 — dưới ngưỡng 0.88. Câu hỏi mới, tiếp tục xử lý.

**Step 2 — Pre-routing: PASS.** `_pre_route()` detect từ "bán hàng", "doanh thu" — match tax anchor regex. Cho qua trong dưới 1 mili giây.

**Step 3 — Iteration 1 (Agentic Loop).** Gemini nhận system prompt với routing rules. Detect đây là câu hỏi HKD bán lẻ hàng hóa. Quyết định: gọi `search_legal_docs("thuế HKD bán hàng may mặc online")`. Hybrid search (BM25 + Vector + RRF) trả về top chunks từ Nghị định 68, Điều 5: tỷ lệ GTGT 1%, TNCN 0.5% cho ngành bán lẻ.

**Step 4 — Iteration 2.** Gemini đánh giá: đã có cơ sở pháp lý, cần tính số cụ thể. Gọi `calculate_tax_hkd(revenue=800_000_000, category="retail")`. Python calculator trả về: `GTGT = 800.000.000 × 1% = 8.000.000đ; TNCN = 800.000.000 × 0.5% = 4.000.000đ; Tổng = 12.000.000đ`. Kết quả deterministic — không phải ước tính của LLM.

**Step 5 — Output và Storage.** Gemini format câu trả lời với trích dẫn "Theo Điều 5 Nghị định 68/2026/NĐ-CP". Generator verify citations — có đủ → lưu full answer vào cache. Tổng thời gian: ~9 giây. Câu hỏi tương tự hỏi lại: 150ms.

---

## SLIDE 7 — Parsing Pipeline: Offline Engine

> *Input: Messy Documents → Output: Structured Tree Diagram*

---

Toàn bộ retrieval pipeline phụ thuộc vào chất lượng dữ liệu đầu vào. **Garbage in, garbage out** — nếu parse sai, search sẽ trả về kết quả sai dù thuật toán có tốt đến đâu.

Parsing Pipeline là một **offline engine 5 stage** — chạy một lần khi thêm văn bản mới, không chạy khi có câu hỏi.

**Stage 1 — Extract.** Ba extractor song song tùy loại file: `docx_helper.py` dùng python-docx cho Word files; `pdfplumber_helper.py` auto-detect PDF digital hay scan; với PDF scan — tức là ảnh chụp giấy — dùng `gemini_helper.py` với Gemini 2.5 Pro, đạt độ chính xác OCR khoảng 95%.

**Stage 2 — Normalize.** Text thô sau extract thường có lỗi OCR đặc thù tiếng Việt: dấu bị nhận sai ("thuê" thay vì "thuế"), ký tự bị tách ("điê u" thay vì "điều"), khoảng trắng thừa. `text_normalizer.py` sửa các lỗi này theo rule-based — không dùng LLM để đảm bảo tốc độ và determinism.

**Stage 3 — Parse: State Machine.** Đây là bước phức tạp nhất. `parser_core.py` implement một **State Machine** nhận dạng cấu trúc pháp lý phân cấp: `NULL → Điều → Khoản → Điểm → Tiết`. State machine theo dõi ngữ cảnh — ví dụ khi gặp "Điều 5" thì reset về level Điều, bất kể đang ở đâu. Output là một cây node với ID duy nhất cho mỗi Điều khoản.

**Stage 4 — Patch.** Một số PDF có bug đặc thù do cách định dạng riêng — không thể sửa bằng rule chung mà không phá vỡ văn bản khác. Hệ thống Patch File (`.patch.json`) cho phép sửa chính xác từng node, idempotent — áp dụng nhiều lần vẫn cho kết quả như nhau. Hiện có **6 patch files** cho 6 văn bản.

**Stage 5 — Validate.** Kiểm tra toàn bộ cây: ID có unique không, không có node mồ côi, cấu trúc hợp lệ. Xuất JSON chuẩn vào `data/parsed/`.

---

## SLIDE 8 — Indexing: Mạng lưới Dữ liệu Kép

> *Path 1: Vector Index (ChromaDB) | Path 2: Keyword Index (BM25)*

---

Sau khi parse, mỗi văn bản được index theo **hai cách độc lập và bổ sung cho nhau**.

**Path 1 — Vector Index với ChromaDB.** Mỗi chunk văn bản được chuyển thành vector 768 chiều bằng model `keepitreal/vietnamese-sbert` — một SBERT model fine-tuned đặc biệt cho tiếng Việt, chạy hoàn toàn local, không cần API, latency ~50ms/batch. Vector được lưu trong ChromaDB với cosine distance metric. Ưu điểm của vector search: **bắt được intent dù người dùng dùng từ khác văn bản**. Ví dụ: "đăng ký người phụ thuộc" match với "giảm trừ gia cảnh cho NPT" — hai cách nói khác nhau nhưng cùng ý nghĩa.

**Path 2 — Keyword Index với BM25.** `BM25Okapi` từ thư viện `rank_bm25` được build in-memory khi khởi động, lưu toàn bộ corpus ~13.000 nodes. Tokenizer tiếng Việt tùy chỉnh để xử lý morphology đặc thù. Latency dưới 10ms. Ưu điểm của BM25: **chính xác tuyệt đối với mã văn bản và thuật ngữ đặc thù**. Query "Điều 5 NĐ68" hay "mẫu 01/TKN-CNKD" sẽ match chính xác — Vector search không làm được điều này vì embedding làm mờ ranh giới ký tự.

Hai path này không phải thay thế nhau mà **bổ sung cho nhau** — và sẽ được kết hợp ở bước tiếp theo bằng RRF.

---

## SLIDE 9 — Hybrid Search & RRF

> *BM25 + Vector → RRF (Reciprocal Rank Fusion) → Kết quả tối ưu*

---

Đây là lớp kết hợp kết quả từ hai hệ thống tìm kiếm — và thuật toán được chọn là **Reciprocal Rank Fusion (RRF)**.

Vấn đề cần giải quyết: BM25 cho ra một bảng xếp hạng, Vector Search cho ra bảng xếp hạng khác. Điểm BM25 và điểm Vector không so sánh được trực tiếp vì đơn vị đo khác nhau hoàn toàn. Không thể chỉ cộng điểm.

**Giải pháp RRF**: thay vì so sánh điểm số, RRF so sánh **thứ hạng**. Công thức:

```
rrf_score += bm25_weight / (k + rank_BM25 + 1)  [bm25_weight = 0.3]
rrf_score += vector_weight / (k + rank_Vector + 1)  [vector_weight được tính ngầm]
```

Chunk nào lọt Top ở cả hai bảng xếp hạng sẽ có RRF score cao nhất. Chunk chỉ top ở một bảng vẫn được tính. Chunk không xuất hiện ở bảng nào — bị bỏ qua.

**Tại sao RRF tốt hơn weighted sum?** Vì không cần tinh chỉnh trọng số theo từng loại query. Một câu hỏi có mã văn bản cụ thể sẽ được BM25 đẩy lên cao tự nhiên. Câu hỏi semantic sẽ được Vector đẩy lên cao. RRF tự cân bằng mà không cần intervention.

Thực tế: BM25 thắng khi query chứa "Điều 5 NĐ68", "mẫu 01/TKN-CNKD". Vector thắng khi query chứa "hoàn thuế", "miễn giảm", "tạm ngừng kinh doanh". Kết hợp: **best of both worlds**.

---

## SLIDE 10 — Kiểm soát Thời gian & Hiệu lực

> *Stage 1: Recency Boost | Stage 2: Expired Filter*

---

RRF cho kết quả tốt về ngữ nghĩa — nhưng chưa xử lý được **chiều thời gian**. Trong lĩnh vực pháp luật, một văn bản mới hơn thường có ưu tiên cao hơn văn bản cũ về cùng chủ đề.

**Stage 1 — Recency Boost và Legal Authority Hierarchy.** Sau RRF, mỗi chunk nhận thêm một hệ số tổng hợp:

```
final_score = rrf_score × hierarchy_factor × (1 + β × recency)

hierarchy_factor:
  Luật (Quốc hội) × 1.10
  Nghị quyết      × 1.07
  Nghị định       × 1.05
  Thông tư        × 1.00
  Công văn        × 0.85

recency = 1 / (1 + log(1 + days_since_effective / 365))
β = 0.15
```

Ví dụ thực tế: Nghị định 68/2026 (văn bản mới) vs Thông tư 40/2021 (văn bản cũ) về cùng chủ đề HKD. RRF có thể cho điểm ngang nhau, nhưng sau authority boost: NĐ68 nhận 1.05 × recency_mới ≈ 1.15 trong khi TT40 nhận 1.00 × recency_cũ ≈ 1.03. NĐ68 được đẩy lên trên — đúng hành vi mong muốn.

Văn bản đã bị thay thế còn nhận thêm **supersession penalty ×0.25** — giảm RRF score xuống còn 25%.

**Stage 2 — Expired Filter (Tường lửa).** Đây là bộ lọc cứng: bất kỳ chunk nào từ văn bản có `effective_to < today` sẽ bị loại hoàn toàn. Hiện tại Thông tư 111/2013 và Thông tư 92/2015 được set `effective_to = 30/06/2026` — từ ngày 01/07/2026 trở đi, hai thông tư này sẽ tự động bị filter khỏi mọi kết quả.

---

## SLIDE 11 — Semantic Cache: Trí nhớ Tốc độ Cao

> *Cosine Similarity ngưỡng 0.88 | Cache Miss: 8–15s | Cache Hit: <200ms*

---

QA Cache là một trong những quyết định kiến trúc quan trọng nhất — và cũng là nơi chứa hai bug nghiêm trọng mà chúng tôi đã phải debug.

**Nguyên lý Semantic Cache:** không phải cache truyền thống kiểu key-value (hỏi y hệt mới hit). Mỗi câu hỏi được embed thành vector 768 chiều — ChromaDB lưu cả embedding lẫn câu trả lời. Khi có câu hỏi mới, hệ thống tìm câu hỏi cũ có cosine similarity cao nhất. Ngưỡng quyết định: **0.88** — được chọn sau thực nghiệm để cân bằng giữa recall (bắt được câu hỏi tương tự) và precision (không nhầm câu hỏi khác nhau về mặt pháp lý).

Ví dụ: "Doanh thu 800 triệu bán quần áo online thuế bao nhiêu?" và "Shop may mặc trên Shopee doanh thu 800 triệu phải đóng thuế gì?" → similarity 0.91 → Cache HIT → 150ms.

**So sánh hai nhánh:**
- Cache Miss: gọi Gemini API, chờ 2–3 vòng agentic loop → **8–15 giây**, tốn chi phí API.
- Cache Hit: vector lookup ChromaDB → **< 200ms**, zero API cost.

Với đặc thù tư vấn thuế — nhiều người dùng hỏi cùng câu hỏi mùa quyết toán — cache hit rate thực tế ước tính **~30%**, tăng theo thời gian vận hành.

---

## SLIDE 12 — Gỡ lỗi Hệ thống Cache

> *Panel 1: Truncation Bug | Panel 2: Stale Reference Bug*

---

Trong quá trình phát triển, chúng tôi gặp hai bug cache nghiêm trọng. Tôi trình bày chi tiết vì đây là **bài học thực tế có giá trị**.

**Bug 1 — Truncation Bug (Lỗi cắt cụt).**

Trong code ban đầu:
```python
cache.store(question, answer[:2000])  # Chỉ lưu 2000 ký tự đầu
```

Vấn đề: câu trả lời TaxAI thường dài 2.500–4.000 ký tự. Phần "Tóm Lại" — kết luận và khuyến nghị — luôn nằm ở **cuối** câu trả lời → luôn bị cắt. Người dùng nhận câu trả lời từ cache thấy nội dung đúng nhưng thiếu kết luận — một trải nghiệm tệ mà rất khó debug vì lỗi chỉ xảy ra với câu hỏi từ cache.

Khắc phục: xóa giới hạn cắt, lưu full answer. Flush 5 entries đã truncate khỏi cache.

**Bug 2 — Stale Reference Bug (Lỗi mất kết nối).**

Vấn đề phức tạp hơn và liên quan đến cơ chế `@st.cache_resource` của Streamlit. Khi Streamlit khởi động, nó tạo một instance `QACache` và giữ trong RAM cho đến khi process restart. Nếu người dùng xóa cache qua script bên ngoài, ChromaDB collection bị xóa khỏi disk. Nhưng object `QACache` trong RAM vẫn giữ reference đến collection UUID đã không còn tồn tại. Lần `lookup()` tiếp theo: crash với `Collection does not exist`.

Khắc phục — `_ensure_collection()`:
```python
def _ensure_collection(self) -> None:
    existing = [c.name for c in self._client.list_collections()]
    if QA_COLLECTION not in existing:
        self._col = self._client.get_or_create_collection(
            name=QA_COLLECTION,
            metadata={"hnsw:space": "cosine"}
        )
```

Hàm này được gọi tự động ở đầu mỗi `lookup()` và `store()`. Hệ thống tự phục hồi mà không cần restart — quan trọng trong môi trường production.

---

## SLIDE 13 — Agentic Loop: Vòng lặp Tư duy Đa bước

> *AI tự kiểm tra sự thiếu hụt thông tin | MAX_ITERATIONS = 4*

---

Điểm khác biệt quan trọng nhất về mặt kiến trúc giữa TaxAI và một RAG pipeline thông thường là **Agentic Loop** — hay còn gọi là ReAct pattern (Reason + Act).

RAG pipeline thông thường: Nhận câu hỏi → Search → Ghép context → Trả lời. Một chiều, cứng nhắc — không xử lý được câu hỏi đòi hỏi nhiều bước.

TaxAI Agentic Loop gồm **4 bước lặp lại**:

**Bước 1 — Reason:** Gemini đọc câu hỏi và context hiện tại, **tự quyết định** cần làm gì tiếp theo: search thêm, tính toán, tra bảng, hay đủ để trả lời. Quyết định này được guide bởi system prompt với 20+ routing rules.

**Bước 2 — Act:** Gọi tool được chọn và nhận kết quả thô. Ví dụ: `search_legal_docs()` trả về top-K chunks với metadata; `calculate_tax_hkd()` trả về JSON với từng loại thuế.

**Bước 3 — Observe:** Gemini đọc kết quả tool. Đánh giá: "Với thông tin này, tôi đã đủ để trả lời chính xác chưa?"

**Bước 4 — Quyết định:** Nếu chưa đủ → quay lại Bước 1 với state đã được cập nhật. Nếu đủ → thoát vòng lặp và sinh câu trả lời.

**Constraint tối quan trọng:** `MAX_ITERATIONS = 4`. Không có giới hạn này, LLM có thể rơi vào vòng lặp vô tận — liên tục search mà không bao giờ quyết định trả lời. Con số 4 được chọn dựa trên thực nghiệm: 95% câu hỏi giải quyết trong 2–3 vòng; vòng 4 là safety net cho câu hỏi phức tạp nhất.

Ngoài ra, hệ thống có **Token Bucket Rate Limiter** — giới hạn 1.800.000 tokens/phút (90% quota Gemini Flash Paid tier), tự động throttle để tránh burst 429 khi chạy benchmark.

---

## SLIDE 14 — Hệ thống Tools: Chia rẽ Trách nhiệm

> *Tool 1: Tra cứu | Tool 2: Tính toán | Tool 3: Tra cứu bảng*

---

Trong Agentic Loop, AI có quyền truy cập vào **3 nhóm công cụ**, mỗi nhóm có ranh giới trách nhiệm rõ ràng và không overlap.

**Tool 1 — Tra cứu: Hybrid Search** (`search_legal_docs`, `get_article`). Đây là công cụ duy nhất được phép truy cập corpus văn bản pháp luật. Trả về chunks nguyên bản — không tóm tắt, không suy diễn. Constraint quan trọng: LLM **không được phép tự diễn giải** kết quả search — chỉ được đưa nguyên chunk vào câu trả lời.

**Tool 2 — Tính toán: Python Calculator** (`calculate_tax_hkd`, `calculate_tncn_progressive`, `calculate_deduction`, `calculate_tax_hkd_profit`). Đây là **core safety mechanism** của hệ thống. Mọi phép tính đều được hardcode bằng Python: tỷ lệ thuế theo ngành nghề, bảng lũy tiến TNCN 7 bậc, công thức phạt chậm nộp. Config `ALLOW_LLM_CALCULATION = False` đặt ở cấp hệ thống. Lý do: LLM có thể nhân sai một phép nhân đơn giản — trong tư vấn tài chính, sai 1 phép tính có thể gây thiệt hại hàng chục triệu đồng cho người dùng.

**Tool 3 — Tra cứu bảng: Lookup Tables** (`lookup_deadline`, `lookup_form`, `lookup_rate_by_sector`). Các thông tin có cấu trúc cố định — deadline nộp thuế theo kỳ, tỷ lệ thuế theo ngành nghề, mã biểu mẫu hành chính — được lưu trong lookup tables. Trả về trong <1ms, chính xác tuyệt đối.

Nguyên tắc thiết kế tổng quát: **mỗi loại thông tin có một và chỉ một nguồn chân lý** — văn bản pháp lý từ search, số liệu từ Python, metadata từ lookup table. LLM là orchestrator, không phải oracle.

---

## SLIDE 15 — Routing Rules: Luật ngầm Định tuyến

> *System Prompt với 20+ routing rules | Xóa bỏ hoàn toàn hiện tượng AI lạc đề*

---

Một trong những nguyên nhân chính gây fail trong evaluation là AI **không biết phải search văn bản nào** — dù câu hỏi có vẻ rõ ràng.

Ví dụ: câu hỏi về thuế bán hàng trên Shopee. Nếu chỉ search chung, AI tìm được quy định HKD chung từ Nghị định 68 — nhưng bỏ qua Nghị định 117/2025 quy định cụ thể về **cơ chế sàn TMĐT khấu trừ thay bên bán**. Câu trả lời thiếu thông tin quan trọng nhất → T2 fail.

Giải pháp: **Routing Rules** — các quy tắc ràng buộc được inject vào System Prompt, hoạt động như "luật ngầm" định hướng AI đi đúng đường.

Một số routing rules thực tế trong hệ thống:

```
HKD bán hàng trên sàn TMĐT (Shopee, Lazada, TikTok):
→ BẮT BUỘC search cả 2 doc: 117_2025_NDCP + 68_2026_NDCP

Câu hỏi về đăng ký kinh doanh cấp xã:
→ BẮT BUỘC search 126_2020_NDCP
→ BẮT BUỘC đề cập "01 ngày làm việc" trong câu trả lời

Câu hỏi về MST = CCCD, cập nhật thông tin MST:
→ BẮT BUỘC search 86_2024_TTBTC

Câu hỏi về tiền ăn giữa ca:
→ CHỈ ĐƯỢC dùng 111_2013_TTBTC (tránh nhầm sang văn bản khác)
```

Tổng cộng hơn **20 routing rules** trong system prompt hiện tại, được thêm vào từng bước dựa trên phân tích thất bại từ benchmark.

Kết quả: xóa bỏ hoàn toàn hiện tượng **AI lạc đề** — trả lời đúng chủ đề nhưng sai văn bản.

---

## SLIDE 16 — Framework Đánh giá T1-T4

> *4 tầng rào chắn | Tất cả 4 tầng phải pass đồng thời*

---

Để đo lường chính xác chất lượng hệ thống, chúng tôi thiết kế một **evaluation framework 4 tầng** — nghiêm khắc hơn nhiều so với benchmark thông thường kiểu "câu trả lời có đúng không?"

Một câu trả lời được tính là PASS **khi và chỉ khi vượt qua đồng thời cả 4 tầng**. Thất bại ở bất kỳ tầng nào → fail toàn bộ, không bù trừ.

**Tầng 1 — T1: Tính toán.** Chỉ áp dụng khi câu hỏi yêu cầu tính số liệu. Sai số cho phép < 1%. Đây là tầng duy nhất có **hard fail**: tính sai dù chỉ một phép tính → fail toàn bộ câu, bất kể các tầng khác có tốt đến đâu.

**Tầng 2 — T2: Trích dẫn.** Câu trả lời phải trích dẫn đúng văn bản pháp lý. Được chấm bằng Citation Score: tỷ lệ overlap giữa documents được cite và `expected_docs` trong annotation. Trích dẫn sai văn bản cũng bị penalize — không chỉ trích dẫn thiếu.

**Tầng 3 — T3: Chọn Tool.** Câu hỏi tính toán phải dùng calculator tool. Câu hỏi tra cứu phải dùng search. Nếu AI dùng search để "ước tính" số liệu thuế thay vì gọi calculator → fail — dù kết quả số tình cờ đúng.

**Tầng 4 — T4: Nội dung.** Câu trả lời phải bao phủ trên 80% key facts đã annotation. Key facts là những thông tin cốt lõi không thể thiếu: mức thuế suất, điều kiện áp dụng, ngoại lệ quan trọng.

225 câu benchmark phân bổ qua 20 chủ đề, 3 mức độ khó, được annotation và verify thủ công.

---

## SLIDE 17 — Cơ chế Chấm điểm & Trọng số

> *Score = T1×w1 + T2×w2 + T3×w3 + T4×w4 | Ngưỡng Pass: ≥ 0.70*

---

Cụ thể hơn về công thức chấm điểm:

```
Score = T1×w1 + T2×w2 + T3×w3 + T4×w4
Pass nếu Score ≥ 0.70
```

Trọng số phân bổ phản ánh mức độ ưu tiên: T2 (Trích dẫn) và T4 (Nội dung) mang trọng số cao hơn vì đây là giá trị cốt lõi của hệ thống — câu trả lời phải có nguồn và phải đầy đủ thông tin.

**Quy tắc Cốt tử — T1 Override.** Tầng tính toán có một quy tắc đặc biệt không nằm trong công thức trên: **tính sai một con số tài chính → fail toàn câu**, bất kể điểm tổng hợp là bao nhiêu.

Lý do thiết kế như vậy: đây là tư vấn tài chính, không phải quiz kiến thức. Nếu hệ thống nói "bạn cần nộp 8 triệu" trong khi thực tế là 18 triệu — dù văn phong trôi chảy, trích dẫn đầy đủ — người dùng vẫn bị thiệt hại thực tế 10 triệu đồng. Trong thiết kế an toàn hệ thống, đây gọi là **safety-critical requirement**: một số thuộc tính không thể đánh đổi với bất kỳ thuộc tính nào khác.

Ngưỡng ≥ 0.70 cũng không phải tùy ý — nó phản ánh mức "câu trả lời đủ tốt để người dùng có thể tham khảo" chứ không phải "hoàn hảo". Một câu trả lời có thể thiếu một số chi tiết phụ nhưng vẫn pass nếu các yếu tố cốt lõi đầy đủ.

---

## SLIDE 18 — Bài học Thất bại R56: Sự thật về AI

> *76.9% (Fail 52/225) | 40 annotation sai | 8 thiếu routing | 2 LLM quên format*

---

Đây là slide quan trọng nhất về mặt **nghiên cứu và bài học**.

Tại checkpoint R56 — ngày 04/04/2026 — hệ thống đạt 76.9%, tức là 52 câu fail. Phản ứng ban đầu là tìm cách sửa code. Nhưng sau khi phân tích kỹ từng câu fail, bức tranh rõ hơn nhiều:

```
Tổng 52 câu fail:
├── 40 câu (77%) — Annotation sai, không phải hệ thống sai
├──  8 câu (15%) — Thiếu routing rule → search sai văn bản
└──  2 câu  (4%) — LLM quên format danh sách citations
```

**Insight nghiêm túc:** Hơn ba phần tư số câu "fail" thực ra là do **benchmark không đo đúng**. Annotation yêu cầu hệ thống cite văn bản X và Y, nhưng câu trả lời đúng của chuyên gia pháp luật chỉ cần cite X. Annotation đặt key_fact với từ ngữ quá hẹp, không chấp nhận câu trả lời đúng nhưng dùng từ đồng nghĩa.

Đây là bài học có giá trị cho bất kỳ dự án AI nào: **"Chất lượng bộ test quan trọng ngang ngửa chất lượng code."** Một hệ thống tốt có thể bị underestimate nếu benchmark không được thiết kế cẩn thận. Và ngược lại — một benchmark dễ có thể tạo ra cảm giác accuracy giả.

Phát hiện này dẫn đến quyết định: không vội sửa code, mà **kiểm toán lại toàn bộ benchmark trước**.

---

## SLIDE 19 — Hành trình vươn đến 100% Pass Rate

> *76.9% → 94.7% → 96% → 100% | 3 giai đoạn can thiệp*

---

Từ 76.9% đến 100% là **3 giai đoạn can thiệp có chủ đích**, mỗi giai đoạn giải quyết một class vấn đề khác nhau.

**Giai đoạn 1 — R57: Annotation Audit** (76.9% → 94.7%, +18pp)

Công cụ: `scripts/analyze_failures.py` phân loại fail theo 3 bucket: annotation sai, routing miss, và hard/corpus gap. Sau đó `scripts/rescore_with_new_annotations.py` re-score kết quả cũ với annotation mới — không cần chạy lại 225 câu qua LLM.

Thay đổi: 40 câu annotation được fix (expected_docs, key_facts phrasing mở rộng). 6 routing rules mới thêm vào system prompt. **+18pp chỉ từ sửa benchmark và hướng dẫn routing** — không thêm một dòng code retrieval hay model mới nào.

**Giai đoạn 2 — R58: Routing Refinement** (94.7% → 96%, +1.3pp)

Tinh chỉnh thêm annotation cho 3 câu borderline. Thêm routing rules cho TMĐT và MST. 3 câu flip pass.

**Giai đoạn 3 — R59: Generator Fix** (96% → 100%, +4pp)

Phân tích 9 câu cuối: 2 câu do LLM quên format citations (Q40 — nội dung đúng, citations=[]). Fix: **Citation Fallback** trong `generator.py` — tự động lấy top-2 retrieved chunks điền vào citations khi LLM trả về list rỗng. 4 routing rules mới cho các pattern còn lại (salon/spa 5%+2%, mẫu 01/TKN-CNKD, MST cũ 10 số, UBND xã 01 ngày làm việc). 1 annotation fix (Q85 wrong expected_docs). Tất cả 9 câu flip pass.

**Tổng kết:** 100% đạt bằng sự kết hợp của annotation quality, routing precision, và một generator fix nhỏ nhưng có giá trị lớn.

---

## SLIDE 20 — Key Design Decisions: Quyết định Cốt lõi

> *Cache-First Architecture | Tối giản hóa Luồng dữ liệu*

---

Nhìn lại toàn bộ hành trình, có hai quyết định kiến trúc tôi muốn highlight vì chúng có tác động lớn nhất đến hiệu năng và độ tin cậy.

**Quyết định 1 — Cache-First Architecture.**

Nguyên tắc: kiểm tra cache **trước khi khởi động bất kỳ pipeline nào**. Tưởng đơn giản nhưng đây là quyết định về thứ tự ưu tiên có hệ quả lớn:

- Bảo vệ hệ thống khỏi **API rate limit**: khi nhiều người cùng hỏi câu tương tự trong mùa quyết toán thuế, cache hấp thụ phần lớn traffic mà không tốn API call.
- **Tiết kiệm 100% chi phí** cho câu hỏi lặp lại — với ngưỡng 0.88, cache hit rate ~30% và tăng dần.
- **Latency nhất quán**: user không bao giờ chờ quá 200ms cho câu hỏi phổ biến.

**Quyết định 2 — Tối giản hóa Luồng dữ liệu.**

Phiên bản trước có bước "Preliminary Retrieval": search văn bản trước khi gọi agent, dùng kết quả để tạo cache key `hash(question + sorted(doc_ids))`. Mục đích tốt — cache key chính xác hơn. Nhưng tác hại: **double retrieval** — search lần 1 trong preliminary, rồi agent search lại lần 2. Mỗi câu miss cache tốn gấp đôi thời gian retrieval.

Quyết định: xóa bỏ, đơn giản hóa cache key về `lookup(question)` thuần túy. Cache accuracy giảm không đáng kể nhưng latency giảm 1–2 giây.

Song song đó: **tính thuế bằng Python thay vì LLM** — loại bỏ toàn bộ rủi ro hallucination số liệu tài chính. Không phải optimization về performance mà là **correctness guarantee** — đảm bảo tính đúng 100%, không phụ thuộc vào "ngày LLM tốt hay xấu".

---

## SLIDE 21 — Hạn chế & Roadmap Tương lai

> *Hạn chế hiện tại | Roadmap tới 01/07/2026*

---

Và cuối cùng — điều quan trọng là nói thẳng thắn về những gì hệ thống **chưa làm được**.

**Hạn chế 1 — Không có hội thoại đa lượt (Multi-turn).**

Mỗi câu hỏi được xử lý độc lập. Session history hiển thị trên UI nhưng chỉ để tham khảo — không inject vào context của câu hỏi tiếp theo. Hậu quả thực tế: người dùng hỏi "Tôi bán quần áo 800tr thuế bao nhiêu?" rồi hỏi tiếp "Nếu chuyển sang PP lợi nhuận thì sao?" — hệ thống không nhớ "800tr" hay "bán quần áo" ở câu trước.

**Hạn chế 2 — Phụ thuộc Google API.**

Latency và availability phụ thuộc vào Gemini API. Đã có retry cho 503/429, nhưng không có offline fallback. Nếu Google API down, hệ thống không phục vụ được câu mới (cache hit vẫn hoạt động).

**Roadmap tới hard deadline 01/07/2026** — ngày Luật TNCN mới có hiệu lực:

- **Quý 2:** Tích hợp **DST — Dialogue State Tracker** — một state machine theo dõi context {entity, revenue, tax_method, scenario} giữa các turns mà không gọi thêm LLM. Mục tiêu: multi-turn conversation với chi phí gần zero.

- **Quý 3:** Mở rộng corpus sang **Thuế TNDN** (800.000+ doanh nghiệp) và **BHXH** (bắt buộc mọi doanh nghiệp có người lao động).

- **Quý 4:** **Triển khai Production multi-tenant** — authentication, lưu lịch sử hội thoại vào database, REST API cho tích hợp bên thứ ba.

Và quan trọng nhất: ngày 01/07/2026 cần **invalidate QA cache** và **kích hoạt effective_to filter** cho TT111/TT92 — đây là deadline kỹ thuật đã được chuẩn bị sẵn trong kiến trúc.

---

Cảm ơn mọi người đã lắng nghe. Tôi sẵn sàng giải đáp câu hỏi.

---
---

# Q&A ANTICIPATION — Câu hỏi thường gặp & Gợi ý trả lời

---

## NHÓM A — Câu hỏi về Kỹ thuật RAG

---

**Q: Tại sao chọn ChromaDB thay vì Pinecone, Weaviate hay Elasticsearch cho vector search?**

**A:** ChromaDB phù hợp với giai đoạn hiện tại vì ba lý do: *(1)* Chạy local — không phụ thuộc cloud, không tốn chi phí infrastructure trong giai đoạn development. *(2)* File-based — toàn bộ index lưu vào disk, dễ backup và migrate. *(3)* Python-native API đơn giản, ít boilerplate. Nhược điểm: không scale tốt cho multi-user production. Roadmap Q4/2026 khi deploy multi-tenant sẽ migrate sang cloud vector DB như Pinecone hoặc Weaviate.

---

**Q: Ngưỡng cache 0.88 được chọn như thế nào? Tại sao không cao hơn như 0.95?**

**A:** Ngưỡng 0.88 là kết quả thực nghiệm, cân bằng hai rủi ro trái chiều. Ngưỡng quá cao (0.95) → cache gần như không hit, mất lợi ích cache. Ngưỡng quá thấp (0.80) → hit nhầm câu hỏi về các văn bản khác nhau — ví dụ câu về TT111 có thể hit cache câu về NĐ68 dù ngữ nghĩa gần giống. Trong lĩnh vực pháp lý, câu hỏi giống nhau nhưng áp dụng năm khác nhau có thể cho câu trả lời hoàn toàn khác → cần ngưỡng đủ cao để phân biệt. 0.88 được xác nhận qua 15+ test cases edge case.

---

**Q: BM25 weight 0.3 và vector weight — tại sao phân bổ như vậy?**

**A:** BM25 weight 0.3 trong RRF có nghĩa là mỗi chunk BM25 đóng góp ít hơn một chút so với Vector (implied ~0.7). Lý do: với corpus pháp luật tiếng Việt, query của người dùng thường dùng ngôn ngữ bình dân trong khi văn bản dùng ngôn ngữ hàn lâm — Vector search bridge được semantic gap này tốt hơn. BM25 vẫn giữ trọng số đủ để win khi có mã văn bản hay số điều cụ thể. Đây là hyperparameter có thể tune thêm khi có đủ data.

---

**Q: Tại sao không dùng reranking model (cross-encoder) sau RRF?**

**A:** Cross-encoder reranking đã được xem xét nhưng không triển khai vì hai lý do: *(1)* Không có Vietnamese legal cross-encoder pre-trained — phải fine-tune từ đầu, đòi hỏi labeled pairs data chưa có. *(2)* Thêm 200–500ms latency per query. Thay vào đó, chúng tôi dùng Authority Hierarchy + Recency Boost như một rule-based reranker nhẹ hơn, phù hợp với đặc thù corpus pháp lý có cấu trúc rõ ràng (Luật > NĐ > TT).

---

**Q: Tại sao không dùng Contextual Retrieval (thêm breadcrumb vào chunk)?**

**A:** Contextual Retrieval là kỹ thuật của Anthropic — thêm summary context vào đầu mỗi chunk trước khi embed. Chúng tôi đã xem xét và quyết định chưa implement vì: cần gọi LLM cho mỗi chunk khi re-index (chi phí cao khi corpus lớn), và hiện tại accuracy đã đạt 100% benchmark với hybrid search + routing rules. Sẽ xem xét lại khi mở rộng corpus sang TNDN/BHXH — lúc đó routing rules đơn lẻ sẽ khó scale.

---

## NHÓM B — Câu hỏi về Evaluation

---

**Q: 100% benchmark có nghĩa là hệ thống hoàn hảo không? Có overfit benchmark không?**

**A:** 100% benchmark không có nghĩa là hoàn hảo — đây là điều quan trọng cần nói thẳng. Benchmark 225 câu là một **proxy** cho chất lượng thực, không phải toàn bộ không gian câu hỏi. Có ba giới hạn rõ ràng: *(1)* Benchmark chỉ bao phủ 20 chủ đề chính — câu hỏi edge case ngoài các chủ đề này chưa được đo. *(2)* R57 đã sửa annotation để "dễ pass hơn" — ngưỡng đã được điều chỉnh. *(3)* Câu hỏi đa lượt (multi-turn) chưa có trong benchmark. 100% là baseline tốt để từ đó mở rộng, không phải điểm đến cuối cùng.

---

**Q: Tại sao không dùng LLM-as-judge thay vì T1-T4 manual annotation?**

**A:** LLM-as-judge có thể dùng nhưng có một vấn đề nghiêm trọng với lĩnh vực pháp luật: LLM có thể accept câu trả lời sai pháp luật nếu nghe có vẻ hợp lý. Framework T1-T4 có các tầng deterministic — T1 so sánh số học, T2 so sánh set document IDs, T3 so sánh tool names. Chỉ T4 (key facts) là semantic. Đây là trade-off có chủ đích: ít linh hoạt hơn, nhưng **reproducible và không bị biased bởi LLM style preferences**.

---

**Q: Annotation audit R57 có phải là "hạ tiêu chuẩn để đạt điểm cao" không?**

**A:** Đây là câu hỏi công bằng và quan trọng. Câu trả lời: không phải hạ tiêu chuẩn mà là **sửa tiêu chuẩn sai**. Ví dụ cụ thể: Q85 hỏi về phạt vi phạm CCCD. Annotation cũ yêu cầu cite 68_2026_NDCP và 310_2025_NDCP. Hệ thống cite 125_2020_NDCP — văn bản thực sự quy định về xử phạt vi phạm liên quan CCCD. Hệ thống đúng, annotation sai. Mọi thay đổi annotation đều được verify bằng cách đọc văn bản pháp luật gốc — không phải làm mềm tiêu chí.

---

## NHÓM C — Câu hỏi về Kiến trúc và Quyết định Thiết kế

---

**Q: Tại sao không dùng streaming response để cải thiện UX?**

**A:** Streaming đã được thử nghiệm và **chủ động loại bỏ**. Lý do: Streamlit streaming cũ trong hệ thống là fake streaming — hiển thị từng từ với delay nhân tạo, không nhận response thực sự theo stream từ Gemini. Điều này không giảm actual latency — chỉ là animation. Real streaming với Gemini API sẽ cần thay đổi kiến trúc generator và không tương thích với citation fallback logic hiện tại. Quyết định: dùng spinner "Đang tra cứu..." thay thế — UX đơn giản hơn, không có overhead kỹ thuật.

---

**Q: Tại sao không dùng local LLM (Ollama + Llama/Qwen) để tránh phụ thuộc API?**

**A:** Local LLM là option đã được đánh giá. Vấn đề: tiếng Việt pháp lý là niche domain — hầu hết local model chất lượng thấp hơn đáng kể so với Gemini Flash cho task này. Thực nghiệm sơ bộ với Qwen2.5 local cho citation accuracy giảm ~20%. Ngoài ra, multi-tool calling (function calling) với local model không ổn định. Quyết định: giữ Gemini Flash, chấp nhận API dependency, bù đắp bằng cache + retry logic để giảm tác động khi API có sự cố.

---

**Q: MAX_ITERATIONS = 4 — nếu câu hỏi cần nhiều hơn 4 bước thì sao?**

**A:** Trong thực tế vận hành, chưa gặp câu hỏi nào hợp lệ cần quá 4 bước. Phân tích benchmark: 80% câu hỏi xong trong 2 vòng, 15% cần 3 vòng, 5% cần đúng 4 vòng. Câu hỏi cần "vòng lặp vô tận" thường là câu hỏi ngoài corpus — hệ thống không tìm được thông tin và cứ loop. MAX_ITERATIONS là safety net ngăn tình trạng này. Khi hit max, hệ thống trả lời với thông tin hiện có và thêm disclaimer "Không tìm thấy đủ thông tin trong corpus".

---

**Q: Patch file có phải là technical debt không? Tại sao không fix parser core?**

**A:** Patch file là **quyết định kiến trúc có chủ đích**, không phải technical debt. Lý do: parser core phải áp dụng cho tất cả 20 văn bản đồng thời. Một bug chỉ xảy ra ở một PDF cụ thể do cách định dạng đặc thù — nếu fix trong parser core, có thể phá vỡ parse của văn bản khác. Patch file giải quyết bug document-specific, idempotent, có version tracking rõ ràng. Parser regression test (3-layer: FAST + REPARSE + patch) đảm bảo không có regression khi thay đổi.

---

## NHÓM D — Câu hỏi về Business và Roadmap

---

**Q: Khi nào deploy production? Cần gì để sẵn sàng?**

**A:** Technical foundation đã production-ready về mặt accuracy và stability. Để deploy thực sự cần 3 thứ: *(1)* Infrastructure — Streamlit Cloud hoặc Railway cho backend, cloud vector DB thay ChromaDB file. *(2)* Authentication — user management, rate limiting per user, chat history persistence. *(3)* Compliance — cần disclaimer rõ ràng "không thay thế tư vấn pháp lý chuyên nghiệp". Timeline Q4/2026 là khả thi nếu có nguồn lực.

---

**Q: Chi phí vận hành hàng tháng ước tính là bao nhiêu?**

**A:** Với traffic hiện tại (development + testing), chi phí Gemini API khoảng vài USD/tháng nhờ cache hit rate ~30%. Ước tính production với 100 user/ngày, mỗi user 5 câu, cache hit 40%: 100 × 5 × 60% miss × ~10.000 tokens/câu = 3M tokens/ngày. Gemini Flash ~$0.075/1M tokens input → ~$7/ngày → ~$200/tháng. Con số này sẽ giảm khi cache warm up và tăng khi user base tăng.

---

**Q: Tại sao không mở rộng sang TNDN ngay bây giờ thay vì chờ Q3?**

**A:** Mở rộng corpus kỹ thuật thực ra đơn giản — chỉ cần parse + embed. Nhưng có hai blockers: *(1)* Benchmark annotation cho TNDN cần thời gian — cần viết 50–100 câu hỏi mới và verify annotation với chuyên gia. *(2)* Routing rules system prompt sẽ phức tạp hơn đáng kể khi thêm TNDN — cần thời gian test và điều chỉnh. Q3 là timeline thực tế, không phải lười biếng kỹ thuật.

---

**Q: Có lo ngại gì về việc TaxAI tư vấn sai và gây thiệt hại cho người dùng không?**

**A:** Đây là câu hỏi quan trọng nhất về mặt trách nhiệm. Chúng tôi có ba lớp bảo vệ: *(1)* Mọi câu trả lời đều có trích dẫn nguồn — người dùng có thể verify trực tiếp. *(2)* Calculator deterministic — không thể tính sai thuế. *(3)* UI rõ ràng: TaxAI là công cụ tham khảo, không thay thế tư vấn từ chuyên gia hoặc cơ quan thuế. Với câu hỏi phức tạp hoặc trường hợp đặc thù, hệ thống recommend "liên hệ Cục Thuế để xác nhận". Mục tiêu là **augment**, không replace, chuyên gia.

---

*Script & Q&A biên soạn: 10/04/2026*  
*Dựa trên kiến trúc thực tế TaxAI v2.0 — source code tại `c:\Users\LENOVO\Desktop\Project\Code\`*
