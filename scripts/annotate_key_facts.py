"""
annotate_key_facts.py — Tự động generate key_facts cho câu hỏi chưa annotate.

Dùng Gemini 2.5 Flash để extract key_facts từ question + expected_value.text.
Chạy: python scripts/annotate_key_facts.py [--dry-run] [--ids 1,4,10]
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

try:
    from google import genai
except ImportError:
    print("ERROR: google-genai not installed. Run: pip install google-genai")
    sys.exit(1)

QUESTIONS_PATH = Path("data/eval/questions.json")
MODEL = "gemini-2.5-pro"

SYSTEM_PROMPT = """Bạn là trợ lý annotate dữ liệu cho hệ thống đánh giá chatbot tư vấn thuế.

Nhiệm vụ: Từ câu hỏi + câu trả lời mẫu, trích xuất danh sách key_facts.

key_facts là danh sách các chuỗi ngắn (số, %, từ khóa kỹ thuật, mã sổ sách...) mà một câu trả lời ĐÚNG BẮT BUỘC phải đề cập. Dùng để kiểm tra tự động xem chatbot có trả lời đúng không.

Quy tắc:
- Mỗi fact là 1 chuỗi ngắn (1-5 từ), ưu tiên số/tỷ lệ/mã cụ thể
- Chỉ lấy thông tin thiết yếu, không lấy những điều hiển nhiên
- Nếu có số: dùng cả dạng ngắn ("500 triệu") lẫn dạng đầy đủ ("500.000.000") nếu cần
- Tối đa 4 facts, tối thiểu 1
- Nếu câu trả lời mẫu là câu định tính (yes/no, mô tả): lấy từ khóa chính của kết luận
- Trả về JSON array, không giải thích gì thêm

Ví dụ:
- Question: "Doanh thu bao nhiêu thì miễn thuế GTGT?"
  expected: "500.000.000 VND"
  → ["500 triệu", "500.000.000"]

- Question: "Có phải mở sổ kế toán không?"
  expected: "S1a-HKD, S2b-HKD"
  → ["S1a-HKD", "S2b-HKD"]

- Question: "Tôi bán tạp hóa phải tính thuế kiểu gì?"
  expected: "Kê khai tỷ lệ % hoặc Lợi nhuận"
  → ["tỷ lệ %", "lợi nhuận"]

- Question: "Có được miễn thuế TNCN không?"
  expected: "Được miễn thuế TNCN"
  → ["miễn thuế TNCN"]"""


def build_prompt(question: str, expected_text: str) -> str:
    return f"""Question: "{question}"
expected_value: "{expected_text}"

Trả về JSON array key_facts:"""


def extract_text_from_response(response) -> str:
    """Extract text from Gemini response, handling thinking mode (2.5 Pro)."""
    # Try simple .text first
    if response.text:
        return response.text
    # 2.5 Pro thinking mode: iterate parts, skip thought parts
    try:
        for part in response.candidates[0].content.parts:
            if not getattr(part, "thought", False) and part.text:
                return part.text
    except Exception:
        pass
    return ""


def call_gemini(client, question: str, expected_text: str) -> list[str] | None:
    prompt = build_prompt(question, expected_text)
    response = None
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config={
                "system_instruction": SYSTEM_PROMPT,
                "temperature": 1,
                "max_output_tokens": 8192,
            }
        )
        raw = extract_text_from_response(response).strip()
        if not raw:
            print(f"    ERROR: empty response")
            return None
        # Strip markdown code block if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        facts = json.loads(raw)
        if isinstance(facts, list):
            return [str(f).strip() for f in facts if str(f).strip()]
    except Exception as e:
        raw_preview = extract_text_from_response(response)[:100] if response else "?"
        print(f"    ERROR: {e} | raw: {raw_preview}")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="In kết quả, không lưu file")
    parser.add_argument("--ids", help="Chỉ xử lý các id cụ thể, VD: 1,4,10")
    parser.add_argument("--overwrite", action="store_true", help="Ghi đè key_facts đã có")
    args = parser.parse_args()

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY env var not set")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        questions = json.load(f)

    # Filter target questions
    target_ids = None
    if args.ids:
        target_ids = set(int(x.strip()) for x in args.ids.split(","))

    targets = []
    for q in questions:
        if target_ids and q["id"] not in target_ids:
            continue
        already_has = bool(q.get("key_facts"))
        if already_has and not args.overwrite:
            continue
        exp_text = q.get("expected_value", {})
        if isinstance(exp_text, dict):
            exp_text = exp_text.get("text") or exp_text.get("annotation_value") or ""
        if not exp_text:
            exp_text = q.get("annotation_value", "")
        if not exp_text:
            print(f"  Q{q['id']}: SKIP — không có expected_value.text")
            continue
        targets.append((q, exp_text))

    print(f"Sẽ annotate {len(targets)} câu với model={MODEL}")
    if args.dry_run:
        print("[DRY RUN — không lưu file]\n")

    # Build a lookup for in-place update
    q_by_id = {q["id"]: q for q in questions}

    updated = 0
    errors = 0
    for i, (q, exp_text) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] Q{q['id']} [{q.get('topic','?')}] [{q.get('difficulty','?')}]")
        print(f"  Q: {q['question'][:80]}")
        print(f"  expected: {exp_text[:80]}")

        facts = call_gemini(client, q["question"], exp_text)

        if facts is None:
            print(f"  → ERROR: skip")
            errors += 1
        else:
            print(f"  → key_facts: {facts}")
            if not args.dry_run:
                q_by_id[q["id"]]["key_facts"] = facts
            updated += 1

        # Rate limit: 2.5 Flash free tier = 10 RPM; paid = 1000 RPM
        # With paid API, 0.1s delay is fine
        time.sleep(0.1)

    print(f"\nDone: {updated} annotated, {errors} errors")

    if not args.dry_run and updated > 0:
        with open(QUESTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(questions, f, ensure_ascii=False, indent=2)
        print(f"Saved → {QUESTIONS_PATH}")
    elif args.dry_run:
        print("Dry run — không lưu")


if __name__ == "__main__":
    main()
