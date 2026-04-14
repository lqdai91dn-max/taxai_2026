# TaxAI v2.0 — Phản Biện

> *Tổng hợp các câu hỏi phản biện từ giáo viên hướng dẫn, kèm phân tích trung thực về giới hạn và định hướng đúng của dự án.*
>
> *Cập nhật: 14/04/2026*

---

## PB1. So sánh với Gemini Gems — tại sao cần build custom thay vì dùng sẵn?

**Câu hỏi:** Gemini có chức năng Gems — chỉ cần đưa data lên là có thể sử dụng agent. Tại sao lại phải build hệ thống phức tạp?

**Phân tích:**

Gems là *good enough* cho người dùng cá nhân hỏi câu đơn giản. TaxAI giải quyết bài toán khác — tư vấn đủ độ tin cậy để có thể trích dẫn pháp lý, không hallucinate số tiền thuế.

| Khía cạnh | Gemini Gems | TaxAI |
|---|---|---|
| Parsing cấu trúc pháp luật | Flat text, không hierarchy | Điều/Khoản/Điểm với ID từng node |
| Citation chính xác | "theo tài liệu bạn cung cấp" | Điều X Khoản Y NĐ 68/2026/NĐ-CP |
| Tính toán số liệu | LLM tự tính → có thể sai | Deterministic Python tool |
| Authority hierarchy | Mọi chunk ngang nhau | Luật×1.10 > NĐ×1.05 > TT×1.00 > CV×0.85 |
| Văn bản hết hiệu lực | Không lọc | Supersession penalty ×0.25 |
| Corpus VN 2025–2026 | Upload thủ công, không structured | Parse sẵn 20 văn bản + patch chính xác |

**Điểm yếu thật sự của TaxAI so với Gems:** Gems được Google maintain, multimodal, cập nhật tự động. TaxAI phải re-parse thủ công khi có văn bản mới.

---

## PB2. Tiêu chuẩn so sánh — 100% pass rate có thực sự tốt hơn LLM không?

**Câu hỏi:** Tiêu chuẩn so sánh kết quả với các LLM khác là tốt hơn bao nhiêu %? Có thực sự tốt hơn không?

**Phân tích:**

"Tốt hơn raw LLM bao nhiêu %" là **câu hỏi sai framing**. Raw GPT-4 hay Gemini là *nền tảng* — không phải đối thủ cạnh tranh. Câu đúng là:

> **"Hệ thống có giải quyết được bài toán mà raw LLM không giải quyết được không?"**

Với TaxAI, câu trả lời rõ ràng là có:
- Raw Gemini không có Luật 109/2025, NĐ 68/2026 trong training data
- Raw Gemini tự tính thuế → hallucinate số liệu
- Raw Gemini không cite được "Điều 5 Khoản 2 NĐ 68/2026/NĐ-CP"

**Điều AI Engineer thực sự đo:**

| Loại so sánh | Nội dung |
|---|---|
| Ablation study | RAG vs no-RAG, hybrid vs vector-only, with/without supersession filter |
| Baseline thực tế | So với phương án thay thế (tự đọc luật, hỏi kế toán) — không phải so với GPT-4 |
| Domain benchmark | Bloomberg GPT chỉ so với GPT-4 *trên financial tasks* — thua general tasks, thắng domain |

**Điểm yếu cần thừa nhận:**
- 100% là trên internal benchmark do team tự viết — có thể vô tình "dạy theo test"
- Chưa có baseline A/B test với Gemini raw / GPT-4o trên cùng bộ câu hỏi
- Annotation audit (R57) phát hiện 6 routing rules sai — chưa audit toàn bộ 225 câu

---

## PB3. Các tools tính toán — còn thủ công ở đâu? Có tự động hóa được không?

**Câu hỏi:** Nếu luật mới có công thức tính toán cần tool mới thì hệ thống có tự viết tool đó được không?

**Phân tích:**

Về kỹ thuật, LLM hoàn toàn có thể đọc NĐ mới và sinh Python code cho tool mới. Nhưng **không ai làm vậy trong production**, vì:

1. **Liability** — sai 1% thuế suất có thể gây thiệt hại tiền tỷ. Code do LLM sinh ra mà không có human review thì không ai chịu trách nhiệm pháp lý.
2. **Edge cases** — Luật thuế VN đầy điều khoản ngoại lệ. LLM dễ bỏ sót điều khoản chuyển tiếp, phụ lục, footnote.
3. **Verification vòng tròn** — muốn verify tool mới đúng phải có test cases, phải có expert biết đáp án đúng.

**Tầng tự động hóa thực tế:**

| Tầng | Mức độ tự động | Khi có luật mới |
|---|---|---|
| Document ingestion (parse + embed) | Hoàn toàn tự động | Chạy script |
| Validity dates / supersession | Bán tự động | Script + human review |
| Tax rate tables (hardcoded dict) | Thủ công | Engineer đọc → sửa code → review |
| Tool logic mới | Thủ công | Engineer đọc luật → viết code → test |

**Thực tế ngành:** Stripe Tax, TurboTax, ABBYY — đều có dedicated tax engineers đọc luật và viết/review calculation logic. LLM chỉ được dùng để *tóm tắt thay đổi cho engineer đọc nhanh hơn*, không tự viết production code.

---

## PB4. Ý nghĩa thực tế — dự án có giá trị khi so sánh với raw LLM không?

**Câu hỏi:** Dự án này có ý nghĩa thực tế không khi so sánh với các LLM khác?

**Phân tích:**

Có — nhưng giá trị không nằm ở "đánh bại GPT-4 trên benchmark tổng quát", mà nằm ở:

- Văn bản pháp luật VN 2025–2026 không có trong training data của bất kỳ LLM nào
- Tư vấn thuế cần citation pháp lý cụ thể — không thể paraphrase, phải trích đúng điều khoản
- Tính toán thuế cần deterministic — hallucinate 1 con số = sai nghĩa vụ thuế hàng triệu đồng
- Production-ready: rate limiting, caching, cost control — không thể có với Gems hay raw API

**Giới hạn cần thừa nhận:**
- Corpus hiện tại ~20 văn bản — còn thiếu nhiều NĐ, TT khác
- Chưa test với người dùng thực — chỉ mới benchmark nội bộ
- Chưa deploy production — chưa biết failure mode ngoài thực tế
- Maintenance cost cao khi luật thay đổi hàng năm

**Kết luận:** Ý nghĩa thực tế có, nhưng cần thêm (1) ablation study đo từng component, (2) user study với người dùng thực, (3) corpus đầy đủ hơn — thì mới chứng minh định lượng được "tốt hơn ở đâu".

---

## PB5. OCR — làm sao đạt 99% độ chính xác?

**Câu hỏi:** Hiện tại làm sao để xử lý hoàn toàn độ chính xác của OCR lên 99%?

**Phân tích:**

**Thực trạng: không có framework hay kỹ thuật nào đạt 99% ổn định trên mọi loại tài liệu.**

| Tool | PDF digital-born | Scanned / chất lượng kém |
|---|---|---|
| pdfplumber | ~99–100% (không cần OCR) | Không áp dụng |
| Tesseract | ~85–93% | ~70–80% |
| AWS Textract | ~95–98% | ~88–93% |
| Google Document AI | ~97–99% | ~90–95% |
| Gemini 2.5 Pro Vision | ~95–97% | ~88–94% |
| ABBYY FineReader | ~98–99% | ~92–96% |

**Tại sao 99% là mục tiêu sai framing:**

"Accuracy" cần xác định đo ở tầng nào:
- **Character-level**: 95% nghe nhiều, nhưng sai "Điêu" → "Điều" chỉ cần patch 1 dòng
- **Semantic-level**: Sai "5%" → "59%" là thảm họa dù character error rate thấp
- Tiếng Việt có dấu thanh là thách thức đặc thù — OCR engine train trên tiếng Anh đặc biệt yếu

**Cách industry tiếp cận thực tế:**

| Kỹ thuật | Mô tả | Trade-off |
|---|---|---|
| Ensemble voting | Chạy 3 engine, lấy majority vote | Tăng ~2–3%, cost tăng 3x |
| Domain post-processing | Normalize rules cho lỗi hệ thống | Cần manual analysis lỗi |
| Patch files | Fix lỗi cụ thể ảnh hưởng semantic | TaxAI đang dùng ✓ |
| Confidence-based human review | Flag ~5–10% sections cần review | Cần human-in-the-loop |
| Fine-tuning | Train model trên domain data | Expensive, cần labeled data |

**TaxAI đang làm đúng hướng:** Pipeline 5 tầng + patch files là industry standard cho production document processing. Thay vì đuổi theo 99% character accuracy tổng quát, TaxAI đảm bảo 100% accuracy trên các con số và điều khoản có ảnh hưởng trực tiếp đến câu trả lời — đó mới là metric có ý nghĩa.

---

*Phản biện biên soạn: 14/04/2026*
*Dựa trên câu hỏi thực tế từ giáo viên hướng dẫn — TaxAI v2.0*
