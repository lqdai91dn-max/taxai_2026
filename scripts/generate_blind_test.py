"""
scripts/generate_blind_test.py

Tạo blind test dataset (60 câu) dùng Gemini 2.5 Flash.
Quy trình:
  1. Generate câu hỏi theo 4 nhóm (Straightforward/Cross-domain/Temporal/Edge)
  2. Annotate expected_docs + key_facts ngay trong generation
  3. Output: data/eval/blind_test_draft.json (cần human review trước khi freeze)

QUAN TRỌNG: Chạy script này TRƯỚC khi bắt đầu Phase 1.
Sau khi review xong → rename thành blind_test_frozen.json và KHÔNG CHỈNH SỬA NỮA.
"""

import json
import os
import time
import logging
from pathlib import Path
from dotenv import load_dotenv
from google import genai

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
OUTPUT_PATH = Path("data/eval/blind_test_draft.json")

# ─── Corpus context (cho LLM biết doc nào tồn tại) ────────────────────────────
CORPUS_DOCS = {
    "109_2025_QH15":      "Luật Thuế TNCN 2025 (hiệu lực 01/01/2026) — thay thế Luật TNCN 2007+2012",
    "108_2025_QH15":      "Luật Quản lý thuế 2025 — quy định thủ tục kê khai, nộp thuế",
    "68_2026_NDCP":       "Nghị định 68/2026 — quy định chi tiết HKD: phương pháp tính thuế, ngưỡng",
    "18_2026_TTBTC":      "Thông tư 18/2026 — mẫu biểu, hồ sơ kê khai thuế HKD",
    "117_2025_NDCP":      "Nghị định 117/2025 — quản lý thuế TMĐT: sàn khấu trừ thay người bán",
    "126_2020_NDCP":      "Nghị định 126/2020 — quy trình quyết toán thuế TNCN",
    "152_2025_TTBTC":     "Thông tư 152/2025 — sổ sách kế toán HKD (S1a, S2a, S2b...)",
    "310_2025_NDCP":      "Nghị định 310/2025 — sửa đổi mức phạt vi phạm hành chính thuế",
    "125_2020_NDCP":      "Nghị định 125/2020 — xử phạt vi phạm hành chính thuế (bản gốc)",
    "373_2025_NDCP":      "Nghị định 373/2025 — ủy quyền quyết toán thuế TNCN",
    "86_2024_TTBTC":      "Thông tư 86/2024 — thủ tục đăng ký MST, hoàn thuế",
    "1296_CTNVT":         "Công văn 1296 — hướng dẫn quyết toán thuế TNCN 2025",
    "So_Tay_HKD":         "Sổ tay HKD — hướng dẫn thực hành kế toán cho hộ kinh doanh",
    "111_2013_TTBTC":     "Thông tư 111/2013 — hướng dẫn Luật TNCN cũ (một phần còn hiệu lực)",
    "92_2015_TTBTC":      "Thông tư 92/2015 — sửa đổi TT111/2013 (một phần còn hiệu lực)",
    "20_2026_NDCP":       "Nghị định 20/2026 — miễn giảm thuế, các trường hợp đặc biệt",
    "198_2025_QH15":      "Luật 198/2025 — sửa đổi một số luật thuế",
    "149_2025_QH15":      "Luật 149/2025 — hiệu lực pháp luật, điều khoản chuyển tiếp",
}

# ─── Nhóm câu hỏi cần generate ────────────────────────────────────────────────
GENERATION_PLAN = [
    {
        "group": "straightforward",
        "label": "Câu hỏi đơn giản, 1 topic, 1 luật",
        "count": 15,
        "user_types": ["individual", "employee", "accountant"],
        "topics": [
            "Thuế suất HKD theo ngành nghề",
            "Ngưỡng miễn thuế HKD",
            "Giảm trừ gia cảnh bản thân 2026",
            "Hoàn thuế TNCN đã khấu trừ thừa",
            "Mức phạt nộp chậm tờ khai thuế",
            "Thuế cho thuê nhà/tài sản",
            "Đăng ký MST lần đầu",
            "Sổ doanh thu S2b-HKD",
            "Thuế TMĐT sàn Shopee khấu trừ",
            "Khấu trừ thuế TNCN 10% hợp đồng ngắn hạn",
        ],
        "style": "Câu hỏi rõ ràng, đơn giản. Mix formal và informal. Ví dụ 'Thuế suất X là bao nhiêu?' hoặc 'Tôi bán X thì đóng thuế bao nhiêu phần trăm?'",
    },
    {
        "group": "cross_domain",
        "label": "Câu hỏi giao thoa nhiều topic/luật",
        "count": 20,
        "user_types": ["household_business", "online_seller"],
        "topics": [
            "Vừa làm văn phòng vừa bán hàng TikTok/Shopee",
            "HKD khai thuế bị phạt và muốn giảm trừ chi phí",
            "Quyết toán TNCN khi có nhiều nguồn thu nhập",
            "HKD TMĐT bị sàn khấu trừ sẵn còn phải khai thêm không",
            "Cho thuê nhà + làm thêm freelance trong năm",
            "Chuyển từ thuế khoán sang phương pháp mới",
            "Người lao động + có thu nhập đầu tư chứng khoán",
        ],
        "style": "Câu hỏi phức tạp, có nhiều điều kiện, dùng ngôn ngữ thực tế. Có thể dùng từ lóng: '25 củ', 'app trừ thuế', 'kê khai gộp'. Câu hỏi nên ambiguous nhẹ để test scope routing.",
    },
    {
        "group": "temporal",
        "label": "Câu hỏi liên quan đến năm 2024/2025 vs 2026 (luật cũ vs mới)",
        "count": 15,
        "user_types": ["accountant", "employer", "individual"],
        "topics": [
            "Quyết toán năm 2024 áp dụng mức giảm trừ nào (11tr hay 15.5tr)",
            "Năm 2025 làm HKD khoán, sang 2026 chuyển tiếp thế nào",
            "Hoàn thuế năm 2023/2024 thủ tục theo luật cũ hay mới",
            "Hợp đồng thuê nhà ký trước 2026 nộp thuế theo luật nào",
            "Điều khoản chuyển tiếp khi chuyển từ khoán sang mới",
            "Người lao động quyết toán 2025 dùng biểu thuế cũ hay mới",
        ],
        "style": "Câu hỏi có năm cụ thể (2023, 2024, 2025, 2026). Phải test được transitional logic. Câu trả lời đúng cần cite luật cũ (111_2013 hoặc 92_2015) cho nghiệp vụ cũ.",
    },
    {
        "group": "edge_attack",
        "label": "Câu hỏi edge case, thiết kế để lộ điểm yếu hệ thống",
        "count": 10,
        "user_types": ["individual", "household_business", "accountant"],
        "topics": [
            "Trường hợp miễn thuế đặc biệt (ODA, tổ chức phi lợi nhuận)",
            "Xử lý sai biệt giữa dữ liệu eTax và thực tế",
            "Câu hỏi về điều khoản bị bãi bỏ bởi luật mới",
            "Multi-step: tính thuế + có phạt + có hoàn",
            "Bất khả kháng, miễn giảm trong trường hợp thiên tai",
        ],
        "style": "Câu hỏi cố tình mơ hồ, thiếu thông tin, hoặc hỏi về edge case pháp lý. Mục tiêu: khiến hệ thống lộ ra điểm yếu. Ví dụ: câu hỏi có thể cần nhiều bước suy luận, hoặc cần luật vừa cũ vừa mới.",
    },
]

# ─── Prompt template ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Bạn là chuyên gia tạo dataset đánh giá cho hệ thống AI tư vấn thuế Việt Nam.

Nhiệm vụ: Tạo câu hỏi thuế cho bộ BLIND TEST — dùng để đánh giá khả năng tổng quát hóa của hệ thống, KHÔNG dùng để training.

Corpus tài liệu hiện có trong hệ thống:
{corpus_docs}

Nguyên tắc tạo câu hỏi:
1. Câu hỏi phải "tấn công" hệ thống — tìm ra điểm yếu, không phải xác nhận điểm mạnh
2. Phải khác về wording so với bộ dev set 225 câu hiện tại
3. annotation phải CHÍNH XÁC dựa trên văn bản pháp luật thực tế
4. key_facts chỉ điền khi đã verify từ luật — nếu không chắc thì để null, ĐỪNG bịa
5. expected_docs phải là doc_id đúng từ corpus list trên

Output format JSON array. Mỗi câu hỏi có đủ fields:
- id: string (BT_[GROUP]_[NUM], ví dụ BT_STRAIGHT_001)
- group: string
- topic: string
- user_type: string (individual/employee/accountant/household_business/online_seller/employer)
- difficulty: string (easy/medium/hard)
- question: string (câu hỏi thực tế, natural language)
- expected_docs: array of doc_ids (chỉ từ corpus list)
- key_facts: object {{tax_rates: [], thresholds: [], form_codes: [], deadlines: []}} — null nếu không áp dụng
- annotation_note: string (ghi chú về điều khoản nào trong doc nào trả lời câu này)
"""

def build_generation_prompt(group_config: dict, batch_size: int = 5) -> str:
    corpus_text = "\n".join(
        f"  - {doc_id}: {desc}"
        for doc_id, desc in CORPUS_DOCS.items()
    )

    return f"""Tạo {batch_size} câu hỏi cho nhóm: {group_config['label']}

Style yêu cầu: {group_config['style']}

User types phù hợp: {', '.join(group_config['user_types'])}

Topics gợi ý (không bắt buộc theo đúng, có thể mix):
{chr(10).join(f'- {t}' for t in group_config['topics'])}

Lưu ý quan trọng:
- Phải có ít nhất 1 câu hỏi NOISY/REALISTIC (dùng ngôn ngữ thường ngày, có thể thiếu ngữ cảnh)
- Không tạo câu hỏi quá "sách giáo khoa"
- expected_docs phải chính xác — chỉ dùng doc_id từ corpus

Trả về JSON array thuần túy, không markdown, không giải thích."""


def generate_batch(client, group_config: dict, batch_size: int, batch_num: int) -> list:
    prompt = build_generation_prompt(group_config, batch_size)

    corpus_text = "\n".join(
        f"  - {doc_id}: {desc}"
        for doc_id, desc in CORPUS_DOCS.items()
    )
    system = SYSTEM_PROMPT.format(corpus_docs=corpus_text)

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={
                    "system_instruction": system,
                    "temperature": 0.8,  # Đủ creative để tạo câu đa dạng
                    "max_output_tokens": 16384,
                }
            )
            text = response.text.strip()

            # Clean JSON
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip().rstrip("```").strip()

            questions = json.loads(text)
            logger.info(f"  Batch {batch_num}: generated {len(questions)} questions")
            return questions

        except json.JSONDecodeError as e:
            logger.warning(f"  Batch {batch_num} attempt {attempt+1} JSON parse error: {e}")
            if attempt < 2:
                time.sleep(3)
                continue
            logger.error(f"  Batch {batch_num} failed after 3 attempts")
            return []
        except Exception as e:
            logger.error(f"  Batch {batch_num} error: {e}")
            return []
    return []


def run():
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in environment")

    client = genai.Client(api_key=api_key)
    all_questions = []
    question_counter = {g["group"]: 0 for g in GENERATION_PLAN}

    for group_config in GENERATION_PLAN:
        group = group_config["group"]
        total_needed = group_config["count"]
        batch_size = 5

        logger.info(f"\n=== Generating group: {group} ({total_needed} questions) ===")

        batches_needed = (total_needed + batch_size - 1) // batch_size

        for batch_num in range(batches_needed):
            remaining = total_needed - question_counter[group]
            current_batch_size = min(batch_size, remaining)

            if current_batch_size <= 0:
                break

            questions = generate_batch(client, group_config, current_batch_size, batch_num + 1)

            for q in questions:
                question_counter[group] += 1
                q["id"] = f"BT_{group.upper()}_{question_counter[group]:03d}"
                q["group"] = group
                all_questions.append(q)

            time.sleep(2)  # Rate limit

        logger.info(f"  {group}: {question_counter[group]} generated")

    # Save draft
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_questions, f, ensure_ascii=False, indent=2)

    logger.info(f"\n✅ Draft saved: {OUTPUT_PATH} ({len(all_questions)} questions)")
    logger.info("⚠️  NEXT STEP: Human review required before freezing!")
    logger.info("   → Verify expected_docs against actual law text")
    logger.info("   → Verify key_facts numbers are correct")
    logger.info("   → Remove questions too similar to dev set")
    logger.info("   → After review: cp blind_test_draft.json blind_test_frozen.json")

    # Summary
    print(f"\n{'='*50}")
    print(f"Generated: {len(all_questions)} questions")
    for group, count in question_counter.items():
        print(f"  {group}: {count}")
    print(f"Output: {OUTPUT_PATH}")
    print(f"{'='*50}")
    print("\n⚠️  FROZEN chưa! Cần human review trước.")


if __name__ == "__main__":
    run()
