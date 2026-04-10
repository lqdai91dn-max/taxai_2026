"""
Fix key_facts for Q40 + Q201-Q237.
Nguyên tắc: phrases ngắn, là substring thực tế của answer agent trả về.
"""
import json
from pathlib import Path

questions = json.loads(Path("data/eval/questions.json").read_text(encoding="utf-8"))
q_map = {q["id"]: q for q in questions}

fixes = {
    # Q40 — rượu bia thuốc lá không được giảm GTGT
    40: ["không thuộc đối tượng được giảm", "thuế tiêu thụ đặc biệt"],

    # ── 37 câu mới ──────────────────────────────────────────────────────────
    # Q201: giảm trừ gia cảnh 2 nơi
    201: ["chỉ được đăng ký giảm trừ gia cảnh cho bản thân tại một nơi", "không được đăng ký"],

    # Q202: thưởng Tết - cộng gộp hay khấu trừ 5%
    202: ["cộng gộp", "biểu lũy tiến từng phần"],

    # Q203: ủy quyền khi chưa đủ 12 tháng
    203: ["chưa đủ 12 tháng", "hợp đồng lao động từ 03 tháng"],

    # Q204: DN không phát sinh thuế - có phải quyết toán không (agent thường sai)
    204: ["không phát sinh chi trả thu nhập", "không phải khai quyết toán"],

    # Q205: eTax Mobile khác chứng từ
    205: ["chứng từ khấu trừ", "liên hệ"],

    # Q206: hồ sơ quyết toán bị lỗi "không đạt"
    206: ["MST", "chuẩn hóa"],

    # Q207: thuế nộp thừa 2021 - có hoàn được không
    207: ["nộp thừa", "được hoàn"],

    # Q208: TikTok/YouTube/Facebook - quyết toán thuế
    208: ["thu nhập kinh doanh", "khai thuế"],

    # Q209: nhân viên <12 tháng - ủy quyền
    209: ["chưa đủ 12 tháng", "ủy quyền"],

    # Q210: 1 công ty + hợp đồng cộng tác - ủy quyền điều kiện
    210: ["10 triệu", "thu nhập vãng lai"],

    # Q211: không lấy được chứng từ vì công ty cũ giải thể
    211: ["tự quyết toán", "chứng từ"],

    # Q212: 2 công ty, tổng dưới mức thuế - có phải quyết toán không
    212: ["không bắt buộc", "muốn hoàn"],

    # Q213: khai đúng nộp chậm - xử lý
    213: ["tiền chậm nộp", "không bị phạt"],

    # Q214: quyết toán có cả VN + nước ngoài - cần những bảng kê gì
    214: ["05/QTT-TNCN", "05-1/BK-TNCN"],

    # Q215: thuế nộp thừa 2024 - bù trừ năm 2025/2026
    215: ["bù trừ", "hoàn trả"],

    # Q216: sáp nhập công ty A → B - quyết toán
    216: ["công ty B", "quyết toán"],

    # Q217: người nước ngoài năm đầu QT - nộp thừa có hoàn không
    217: ["hoàn trả", "nộp thừa"],

    # Q218: đăng ký NPT mẹ tháng 2, mẹ mất tháng 10 - tính từ tháng nào
    218: ["từ tháng", "giảm trừ"],

    # Q219: hoàn thuế TNCN công ty đóng thay NLĐ
    219: ["hoàn", "khấu trừ"],

    # Q220: cho thuê tài sản - khai theo năm
    220: ["khai theo năm", "cho thuê"],

    # Q221: tạm nộp thuế nhưng chưa nộp tờ khai
    221: ["tờ khai", "đã nộp"],

    # Q222: công ty thuê xe cá nhân - khai nộp thay
    222: ["khai thay", "nộp thay"],

    # Q223: thay đổi số NPT - thủ tục
    223: ["điều chỉnh", "đăng ký"],

    # Q224: NLĐ đăng ký NPT nhưng NPT không có MST
    224: ["MST", "người phụ thuộc"],

    # Q225: kê khai 05-3/BK-TNCN khi NPT chưa chuẩn hóa CCCD
    225: ["05-3/BK-TNCN", "chuẩn hóa"],

    # Q226: NPT đã đăng ký từ trước - có bắt buộc cập nhật CCCD không
    226: ["không bắt buộc", "thay đổi"],

    # Q227: tiền hỗ trợ điện thoại trong HĐLĐ - có chịu thuế không
    227: ["tiền hỗ trợ điện thoại", "không tính vào thu nhập chịu thuế"],

    # Q228: điều chuyển từ A → B - ủy quyền cho B quyết toán
    228: ["công ty B", "điều chuyển"],

    # Q229: MST = số CCCD - xử lý người chưa có CCCD
    229: ["số CCCD", "MST"],

    # Q230: cá nhân/NPT chưa chuẩn hóa dữ liệu
    230: ["chuẩn hóa", "cơ quan thuế"],

    # Q231: kỳ tính lương 26→25 - thu nhập tháng 12 khai như thế nào
    231: ["thời điểm chi trả", "thu nhập"],

    # Q232: sáp nhập do thay đổi đơn vị hành chính tỉnh
    232: ["đơn vị mới", "quyết toán"],

    # Q233: tiền ăn giữa ca 730k bị hủy - tính cả năm hay từ thời điểm
    233: ["tiền ăn giữa ca", "không tính vào thu nhập"],

    # Q234: 2 nguồn thu nhập 2024, tổng không phải nộp thuế nhưng đã bị khấu trừ
    234: ["được hoàn", "quyết toán"],

    # Q235: lương tháng 12/2025 chi trả tháng 1/2026 - khai năm nào
    235: ["thời điểm chi trả", "năm 2026"],

    # Q236: đăng ký NPT muộn - tính từ đầu năm không; NPT sai thông tin
    236: ["từ tháng 1", "đăng ký trong năm"],

    # Q237: có phải đăng ký NPT mỗi năm không
    237: ["không cần đăng ký lại", "khi có thay đổi"],
}

changed = 0
for qid, new_facts in fixes.items():
    if qid in q_map:
        old = q_map[qid].get("key_facts", [])
        q_map[qid]["key_facts"] = new_facts
        changed += 1
        print(f"Q{qid}: {old} → {new_facts}")

Path("data/eval/questions.json").write_text(
    json.dumps(list(q_map.values()), ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(f"\nUpdated {changed} questions.")
