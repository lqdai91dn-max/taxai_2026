"""
Phase B Step 2: Add 37 real TNCN questions from CauHoi_TNCN_P1.pdf to questions.json
Q8 is skipped (out-of-scope: asking for phone number).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.parsing.pdfplumber_helper import SmartPDFHelper

helper = SmartPDFHelper()
text, _, _, _ = helper.extract_text_and_tables(
    Path("data/raw/CauHoi/CauHoi_TNCN_P1.pdf"), extract_tables=False
)

blocks = re.split(r"(?=Người nộp thuế:)", text.strip())
blocks = [b.strip() for b in blocks if b.strip() and "Người nộp thuế:" in b]

def clean(s):
    return re.sub(r"\s+", " ", s).strip()

qa = []
for i, b in enumerate(blocks, 1):
    lines = b.split("\n")
    body = "\n".join(lines[2:])
    ans_split = re.split(r"Tr\s*ả lời:", body, maxsplit=1)
    q = clean(ans_split[0])
    a = clean(ans_split[1]) if len(ans_split) > 1 else ""
    qa.append({"seq": i, "q": q, "a": a})

# Metadata per question seq (skipping Q8)
entries_meta = {
    1: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC"],
        "expected_articles": ["Điều 9 khoản 1 điểm c.1 TT111/2013"],
        "key_facts": [
            "chỉ được đăng ký giảm trừ gia cảnh cho bản thân tại một nơi",
            "không được đăng ký giảm trừ bản thân tại Công ty B",
        ],
    },
    2: {
        "topic": "Khấu trừ thuế TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC"],
        "expected_articles": ["Điều 25 TT111/2013"],
        "key_facts": [
            "phải cộng gộp tiền thưởng vào thu nhập tháng 12/2025",
            "tính thuế theo biểu lũy tiến từng phần",
            "không khấu trừ riêng 5% đối với tiền thưởng Tết",
        ],
    },
    3: {
        "topic": "Ủy quyền quyết toán TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "373_2025_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 điểm d.2 NĐ126/2020"],
        "key_facts": [
            "được ủy quyền kể cả không làm đủ 12 tháng",
            "điều kiện: hợp đồng lao động từ 03 tháng trở lên tại một nơi",
            "đang làm việc tại đó vào thời điểm tổ chức trả thu nhập quyết toán",
        ],
    },
    4: {
        "topic": "Quyết toán thuế TNCN",
        "user_type": "employer",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 điểm d.1 NĐ126/2020"],
        "key_facts": [
            "không phát sinh chi trả thu nhập thì không phải khai quyết toán",
            "có chi trả nhưng không phát sinh thuế khấu trừ thì phải khai mẫu 05/QTT-TNCN",
        ],
    },
    5: {
        "topic": "Quyết toán thuế TNCN",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "111_2013_TTBTC"],
        "expected_articles": ["NĐ126/2020"],
        "key_facts": [
            "khai quyết toán theo số liệu trên chứng từ khấu trừ do công ty cấp",
            "liên hệ công ty để xác nhận lại số liệu nếu có chênh lệch",
        ],
    },
    6: {
        "topic": "Quyết toán thuế TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["NĐ126/2020"],
        "key_facts": [
            "kiểm tra và chuẩn hóa dữ liệu MST/CCCD của người lao động trong Phụ lục 05-1/BK-TNCN hoặc 05-2/BK-TNCN",
        ],
    },
    7: {
        "topic": "Hoàn thuế TNCN",
        "user_type": "employee",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["Điều 141 Luật quản lý thuế 38/2019"],
        "key_facts": [
            "thời hạn 5 năm kể từ ngày phát sinh số tiền thuế nộp thừa",
            "thuế nộp thừa năm 2021 vẫn được hoàn khi quyết toán năm 2025",
        ],
    },
    9: {
        "topic": "Quyết toán thuế TNCN",
        "user_type": "employee",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["117_2025_NDCP", "111_2013_TTBTC"],
        "expected_articles": ["NĐ117/2025"],
        "key_facts": [
            "thu nhập từ kinh doanh trên nền tảng số (TikTok/YouTube/Facebook) là thu nhập từ hoạt động kinh doanh",
            "phải khai và nộp thuế TNCN riêng cho phần thu nhập kinh doanh",
        ],
    },
    10: {
        "topic": "Ủy quyền quyết toán TNCN",
        "user_type": "employer",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "373_2025_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 điểm d.2 NĐ126/2020"],
        "key_facts": [
            "được ủy quyền kể cả không làm đủ 12 tháng",
            "điều kiện: hợp đồng lao động từ 03 tháng trở lên và đang làm việc tại đơn vị vào thời điểm quyết toán",
        ],
    },
    11: {
        "topic": "Ủy quyền quyết toán TNCN",
        "user_type": "employee",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "373_2025_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 NĐ126/2020"],
        "key_facts": [
            "có thể ủy quyền nếu thu nhập vãng lai bình quân tháng không quá 10 triệu đồng và đã khấu trừ 10%",
            "nếu thu nhập vãng lai vượt 10 triệu/tháng phải tự thực hiện quyết toán",
        ],
    },
    12: {
        "topic": "Quyết toán thuế TNCN",
        "user_type": "employee",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["NĐ126/2020"],
        "key_facts": [
            "tổ chức trả thu nhập đã giải thể/phá sản thì cá nhân tự thực hiện quyết toán",
            "khai theo thu nhập thực tế và chứng từ hiện có",
        ],
    },
    13: {
        "topic": "Quyết toán thuế TNCN",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 điểm d NĐ126/2020"],
        "key_facts": [
            "không bắt buộc quyết toán nếu tổng thu nhập không vượt mức chịu thuế",
            "nếu đã bị khấu trừ thuế và muốn hoàn thì phải thực hiện quyết toán",
        ],
    },
    14: {
        "topic": "Xử lý vi phạm thuế",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["Điều 7 khoản 4 điểm a NĐ126/2020"],
        "key_facts": [
            "khai đúng nhưng nộp chậm chỉ bị tính tiền chậm nộp",
            "không bị phạt hành chính về hành vi khai sai nếu khai đúng số thuế",
        ],
    },
    15: {
        "topic": "Quyết toán thuế TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 điểm d NĐ126/2020"],
        "key_facts": [
            "khai mẫu 05/QTT-TNCN kèm 05-1/BK-TNCN (lao động cư trú) và 05-2/BK-TNCN (lao động không cư trú)",
            "phụ lục 05-3/BK-TNCN dành cho người phụ thuộc",
        ],
    },
    16: {
        "topic": "Hoàn thuế TNCN",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["Điều 60 Luật quản lý thuế 38/2019"],
        "key_facts": [
            "thuế TNCN nộp thừa năm 2024 được bù trừ vào nghĩa vụ thuế năm tiếp theo hoặc hoàn trả",
        ],
    },
    17: {
        "topic": "Ủy quyền quyết toán TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "373_2025_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 điểm d.1 NĐ126/2020"],
        "key_facts": [
            "sau sáp nhập, công ty B tiếp nhận toàn bộ nghĩa vụ thuế",
            "công ty B thực hiện quyết toán thuế TNCN thay cho toàn bộ NLĐ kể cả NLĐ từ công ty A",
        ],
    },
    18: {
        "topic": "Hoàn thuế TNCN",
        "user_type": "individual",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP"],
        "expected_articles": ["Luật quản lý thuế 38/2019"],
        "key_facts": [
            "người nước ngoài có số thuế đã nộp lớn hơn số phải nộp được hoàn trả",
            "khi kết thúc hợp đồng rời Việt Nam được hoàn thuế theo quy định",
        ],
    },
    19: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employee",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC"],
        "expected_articles": ["Điều 9 khoản 1 điểm c.2.4 TT111/2013"],
        "key_facts": [
            "được tính giảm trừ từ tháng phát sinh nghĩa vụ nuôi dưỡng (tháng 2/2025)",
            "người phụ thuộc mất trong năm vẫn được tính giảm trừ đến tháng hết nghĩa vụ nuôi dưỡng",
        ],
    },
    20: {
        "topic": "Hoàn thuế TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC", "126_2020_NDCP"],
        "expected_articles": ["Điều 28 TT111/2013"],
        "key_facts": [
            "tổ chức trả thu nhập được hoàn số thuế đã khấu trừ thừa của người lao động",
            "thủ tục hoàn thuế thực hiện qua quyết toán hoặc đề nghị hoàn theo TT111/2013",
        ],
    },
    21: {
        "topic": "Thuế cho thuê tài sản",
        "user_type": "individual",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["68_2026_NDCP", "18_2026_TTBTC"],
        "expected_articles": ["Điều 18 khoản 1 NĐ68/2026"],
        "key_facts": [
            "cá nhân cho thuê tài sản được lựa chọn khai theo năm",
            "khai một lần cho cả năm thay vì khai từng lần phát sinh",
        ],
    },
    22: {
        "topic": "Thuế cho thuê tài sản",
        "user_type": "individual",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["68_2026_NDCP", "18_2026_TTBTC"],
        "expected_articles": ["NĐ68/2026"],
        "key_facts": [
            "thuế đã tạm nộp được tính vào số thuế phải nộp khi quyết toán",
            "cần nộp tờ khai để hợp thức hóa số tiền thuế đã tạm nộp",
        ],
    },
    23: {
        "topic": "Thuế cho thuê tài sản",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["68_2026_NDCP", "18_2026_TTBTC"],
        "expected_articles": ["NĐ68/2026", "TT18/2026"],
        "key_facts": [
            "tổ chức thuê tài sản của cá nhân được khai và nộp thuế thay cho cá nhân",
            "khai theo mẫu quy định tại TT18/2026",
        ],
    },
    24: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["86_2024_TTBTC", "111_2013_TTBTC"],
        "expected_articles": ["TT86/2024"],
        "key_facts": [
            "khi thay đổi số lượng người phụ thuộc phải nộp hồ sơ đăng ký điều chỉnh",
        ],
    },
    25: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employee",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC", "86_2024_TTBTC"],
        "expected_articles": ["Điều 9 khoản 1 TT111/2013"],
        "key_facts": [
            "người phụ thuộc phải có MST để được tính giảm trừ gia cảnh",
            "cần đăng ký MST cho người phụ thuộc trước khi đăng ký giảm trừ",
        ],
    },
    26: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["86_2024_TTBTC", "126_2020_NDCP"],
        "expected_articles": ["TT86/2024"],
        "key_facts": [
            "kê khai 05-3/BK-TNCN theo dữ liệu hiện có",
            "liên hệ cơ quan thuế để chuẩn hóa dữ liệu NPT chưa được cập nhật CCCD",
        ],
    },
    27: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["86_2024_TTBTC"],
        "expected_articles": ["Điều 39 khoản 3 TT86/2024"],
        "key_facts": [
            "NPT đã đăng ký từ trước không bắt buộc phải đăng ký lại theo CCCD",
            "chỉ cập nhật khi có thay đổi thông tin hoặc khi cơ quan thuế yêu cầu chuẩn hóa",
        ],
    },
    28: {
        "topic": "Thu nhập chịu thuế",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC"],
        "expected_articles": ["Điều 2 khoản 2 điểm đ TT111/2013"],
        "key_facts": [
            "tiền hỗ trợ điện thoại ghi trong hợp đồng lao động và quy chế lương thưởng không tính vào thu nhập chịu thuế",
            "chỉ miễn nếu mức khoán phù hợp với thực tế công việc",
        ],
    },
    29: {
        "topic": "Ủy quyền quyết toán TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "373_2025_NDCP"],
        "expected_articles": ["Điều 8 khoản 6 điểm d.1 NĐ126/2020"],
        "key_facts": [
            "NLĐ điều chuyển từ A sang B được ủy quyền cho công ty B quyết toán thay",
            "công ty B quyết toán gộp thu nhập từ cả 2 nơi nếu cùng hệ thống",
        ],
    },
    30: {
        "topic": "Đăng ký MST",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["86_2024_TTBTC"],
        "expected_articles": ["Điều 22, 23, 25 TT86/2024"],
        "key_facts": [
            "MST cá nhân đã được đồng bộ với số CCCD/định danh cá nhân",
            "người chưa có CCCD đăng ký MST bằng giấy tờ hợp lệ khác theo TT86/2024",
        ],
    },
    31: {
        "topic": "Đăng ký MST",
        "user_type": "employer",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["86_2024_TTBTC"],
        "expected_articles": ["TT86/2024"],
        "key_facts": [
            "liên hệ cơ quan thuế quản lý để chuẩn hóa dữ liệu MST/CCCD",
            "tra cứu và cập nhật qua ứng dụng eTax Mobile hoặc cổng thông tin điện tử thuế",
        ],
    },
    32: {
        "topic": "Khấu trừ thuế TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC"],
        "expected_articles": ["Điều 9 khoản 1 điểm c TT111/2013"],
        "key_facts": [
            "thu nhập tính theo thời điểm chi trả thực tế cho người lao động",
            "kỳ tính lương từ 26 tháng trước đến 25 tháng này, chi trả ngày 25: tính là thu nhập của tháng chi trả",
        ],
    },
    33: {
        "topic": "Ủy quyền quyết toán TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "373_2025_NDCP"],
        "expected_articles": ["NĐ126/2020"],
        "key_facts": [
            "đơn vị mới sau sáp nhập do thay đổi đơn vị hành chính quyết toán thay cho toàn bộ NLĐ điều chuyển",
            "tổng hợp thu nhập cả đơn vị cũ và mới trong năm để quyết toán",
        ],
    },
    34: {
        "topic": "Thu nhập chịu thuế",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC"],
        "expected_articles": ["TT111/2013"],
        "key_facts": [
            "từ ngày 15/6/2025 không còn mức trần tiền ăn giữa ca 730.000đ",
            "tiền ăn giữa ca thực tế chi trả phù hợp quy chế công ty không tính vào thu nhập chịu thuế",
        ],
    },
    35: {
        "topic": "Hoàn thuế TNCN",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["126_2020_NDCP", "111_2013_TTBTC"],
        "expected_articles": ["NĐ126/2020"],
        "key_facts": [
            "tổng thu nhập dưới mức chịu thuế nhưng đã bị khấu trừ thì được hoàn thuế",
            "phải thực hiện quyết toán thuế TNCN để được hoàn số thuế đã khấu trừ",
        ],
    },
    36: {
        "topic": "Khấu trừ thuế TNCN",
        "user_type": "employer",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC"],
        "expected_articles": ["TT111/2013"],
        "key_facts": [
            "thời điểm xác định thu nhập là thời điểm chi trả thực tế",
            "lương tháng 12/2025 chi trả vào tháng 1/2026 được tính là thu nhập năm 2026",
        ],
    },
    37: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employee",
        "difficulty": "medium",
        "needs_calculation": False,
        "expected_docs": ["111_2013_TTBTC", "86_2024_TTBTC"],
        "expected_articles": ["Điều 9 khoản 1 điểm h.2 TT111/2013"],
        "key_facts": [
            "đăng ký NPT trong năm được tính giảm trừ từ tháng 1 của năm tính thuế",
            "sai thông tin NPT: nộp đơn điều chỉnh thông tin đăng ký người phụ thuộc",
        ],
    },
    38: {
        "topic": "Giảm trừ gia cảnh",
        "user_type": "employee",
        "difficulty": "easy",
        "needs_calculation": False,
        "expected_docs": ["86_2024_TTBTC", "111_2013_TTBTC"],
        "expected_articles": ["TT86/2024"],
        "key_facts": [
            "không cần đăng ký lại người phụ thuộc mỗi năm nếu không có thay đổi",
            "chỉ đăng ký lại khi có thay đổi thông tin hoặc thay đổi người phụ thuộc",
        ],
    },
}

existing = json.loads(Path("data/eval/questions.json").read_text(encoding="utf-8"))
max_id = max(x["id"] for x in existing)
new_entries = []
next_id = max_id + 1

for item in qa:
    seq = item["seq"]
    if seq == 8:
        continue  # skip out-of-scope
    meta = entries_meta[seq]
    entry = {
        "id": next_id,
        "topic": meta["topic"],
        "question": item["q"],
        "user_type": meta["user_type"],
        "difficulty": meta["difficulty"],
        "needs_calculation": meta["needs_calculation"],
        "needs_law_retrieval": True,
        "expected_docs": meta["expected_docs"],
        "expected_articles": meta["expected_articles"],
        "expected_tools": ["search_legal_docs"],
        "key_facts": meta["key_facts"],
        "source": "CauHoi_TNCN_P1_Q" + str(seq),
    }
    new_entries.append(entry)
    next_id += 1

all_entries = existing + new_entries
Path("data/eval/questions.json").write_text(
    json.dumps(all_entries, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Added {len(new_entries)} new questions")
print(f"Total questions: {len(all_entries)}")
print(f"New ID range: {min(x['id'] for x in new_entries)} - {max(x['id'] for x in new_entries)}")
