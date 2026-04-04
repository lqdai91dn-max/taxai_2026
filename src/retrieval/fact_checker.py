"""
src/retrieval/fact_checker.py — Fact Consistency Check (Tầng 1, Rule-based)

Kiểm tra 3 loại mâu thuẫn giữa câu trả lời và chunks được retrieve:
  1. Numeric match  : số liệu trong answer có xuất hiện trong chunks không?
  2. Comparator flip: "trên 500 triệu" vs chunk "từ 500 triệu trở xuống" → mâu thuẫn
  3. Polarity       : "miễn thuế" trong answer vs "phải nộp" trong chunk → mâu thuẫn

Chi phí: 0 API call, ~0ms latency.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Numeric pattern ────────────────────────────────────────────────────────────
# Bắt: "500 triệu", "1 tỷ", "1,000,000", "10%", "20 triệu đồng"
_NUM_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d{3})*(?:\.\d+)?)\s*(tỷ|triệu|nghìn|ngàn|%|đồng|vnd)?",
    re.IGNORECASE,
)

# ── Comparator words ───────────────────────────────────────────────────────────
# Nhóm: TRÊN (>), TỪ/KHÔNG DƯỚI (>=), DƯỚI (<), TRỞ XUỐNG/KHÔNG QUÁ (<=)
_CMP_GT  = {"trên", "vượt quá", "quá", "hơn", "lớn hơn"}
_CMP_GTE = {"từ", "ít nhất", "tối thiểu", "không dưới", "đủ", "đạt"}
_CMP_LT  = {"dưới", "chưa đến", "chưa tới", "nhỏ hơn"}
_CMP_LTE = {"trở xuống", "không quá", "tối đa", "không vượt quá", "không hơn"}

# Comparator đối lập — dùng để phát hiện flip
_OPPOSITES: dict[str, tuple[str, ...]] = {
    ">":  ("<", "<="),
    ">=": ("<",),
    "<":  (">", ">="),
    "<=": (">",),
}

# ── Polarity pairs ─────────────────────────────────────────────────────────────
# Mỗi cặp (positive_set, negative_set):
#   nếu answer dùng positive nhưng chunk chỉ có negative → mâu thuẫn (và ngược lại)
_POLARITY_PAIRS: list[tuple[set[str], set[str]]] = [
    (
        {"miễn thuế", "không phải nộp", "không thuộc diện", "được miễn",
         "không chịu thuế", "không phải đóng"},
        {"phải nộp", "chịu thuế", "thuộc diện chịu", "có nghĩa vụ nộp",
         "bắt buộc nộp", "phải đóng thuế"},
    ),
    (
        {"được hoàn", "hoàn thuế", "được hoàn trả", "được hoàn lại"},
        {"không được hoàn", "không hoàn", "không được hoàn trả"},
    ),
    (
        {"được khấu trừ", "được trừ vào chi phí", "được tính vào chi phí"},
        {"không được khấu trừ", "không được trừ", "không được tính chi phí"},
    ),
    (
        {"được giảm trừ", "được giảm"},
        {"không được giảm trừ", "không được giảm"},
    ),
]


# ── Data class ─────────────────────────────────────────────────────────────────
@dataclass
class FactCheckResult:
    passed: bool
    level: str                      # "ok" | "warning" | "fail"
    issues: list[str] = field(default_factory=list)
    numeric_checked: int = 0
    numeric_matched: int = 0

    def to_dict(self) -> dict:
        return {
            "level":           self.level,
            "passed":          self.passed,
            "issues":          self.issues,
            "numeric_checked": self.numeric_checked,
            "numeric_matched": self.numeric_matched,
        }

    def __str__(self) -> str:
        summary = "; ".join(self.issues) if self.issues else "OK"
        return f"[{self.level.upper()}] {summary}"


# ── Main function ──────────────────────────────────────────────────────────────
def check_facts(answer: str, tool_calls: list[dict]) -> FactCheckResult:
    """
    Kiểm tra fact consistency giữa answer và retrieved chunks.

    Args:
        answer    : Câu trả lời của agent.
        tool_calls: tool_calls_log từ planner (chứa result của search_legal_docs).

    Returns:
        FactCheckResult với level ok / warning / fail.
    """
    # Thu thập snippet từ tất cả search results
    chunk_texts: list[str] = []
    for tc in tool_calls:
        if tc.get("tool") != "search_legal_docs":
            continue
        for r in tc.get("result", {}).get("results", []):
            snippet = r.get("snippet", "")
            if snippet:
                chunk_texts.append(snippet)

    if not chunk_texts:
        return FactCheckResult(passed=True, level="ok")

    all_chunks   = " ".join(chunk_texts).lower()
    answer_lower = answer.lower()
    issues: list[str] = []

    # ── Check 1 & 2: Numeric match + Comparator flip ──────────────────────────
    numeric_checked = 0
    numeric_matched = 0

    for num, unit in _NUM_RE.findall(answer):
        num_norm = _normalize_num(num)
        if len(num_norm) < 2:          # bỏ qua số 1 chữ số (ngày, điều khoản)
            continue
        if num_norm == "0":
            continue

        numeric_checked += 1
        label = f"{num}{' ' + unit if unit else ''}"

        # Kiểm tra số có trong chunks không
        found_in_chunk = _num_in_text(num_norm, all_chunks)
        if found_in_chunk:
            numeric_matched += 1

            # Comparator flip: chỉ check khi số tìm thấy trong cả hai
            ans_cmp   = _get_comparator(answer_lower, num.lower(), unit)
            chunk_cmp = _get_comparator(all_chunks,   num_norm,    unit)
            if ans_cmp and chunk_cmp and chunk_cmp in _OPPOSITES.get(ans_cmp, ()):
                issues.append(
                    f"Comparator flip: answer '{ans_cmp} {label}' mâu thuẫn "
                    f"với chunk '{chunk_cmp} {label}'"
                )
        else:
            issues.append(f"So lieu '{label}' khong tim thay trong chunks")

    # ── Check 3: Polarity contradiction ───────────────────────────────────────
    for pos_set, neg_set in _POLARITY_PAIRS:
        ans_pos   = _any_in(answer_lower, pos_set)
        ans_neg   = _any_in(answer_lower, neg_set)
        chunk_pos = _any_in(all_chunks,   pos_set)
        chunk_neg = _any_in(all_chunks,   neg_set)

        # Answer nói "miễn" nhưng chunks chỉ nói "phải nộp"
        if ans_pos and chunk_neg and not chunk_pos:
            issues.append(
                f"Polarity: answer co '{_first_match(answer_lower, pos_set)}' "
                f"nhung chunks chi co '{_first_match(all_chunks, neg_set)}'"
            )
        # Answer nói "phải nộp" nhưng chunks chỉ nói "miễn"
        elif ans_neg and chunk_pos and not chunk_neg:
            issues.append(
                f"Polarity: answer co '{_first_match(answer_lower, neg_set)}' "
                f"nhung chunks chi co '{_first_match(all_chunks, pos_set)}'"
            )

    # ── Quyết định level ──────────────────────────────────────────────────────
    has_flip     = any("Comparator flip" in i for i in issues)
    has_polarity = any("Polarity" in i       for i in issues)
    has_miss     = any("khong tim thay"  in i for i in issues)

    if has_flip or has_polarity:
        level = "fail"      # Mâu thuẫn logic rõ ràng — nguy hiểm
    elif has_miss and numeric_matched == 0 and numeric_checked > 0:
        level = "warning"   # Không verify được số liệu nào
    else:
        level = "ok"

    return FactCheckResult(
        passed          = (level == "ok"),
        level           = level,
        issues          = issues,
        numeric_checked = numeric_checked,
        numeric_matched = numeric_matched,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────
def _normalize_num(s: str) -> str:
    """Bỏ dấu phẩy/chấm ngăn cách: '1,000,000' → '1000000'."""
    return re.sub(r"[,.]", "", s)


def _num_in_text(num_norm: str, text: str) -> bool:
    """Kiểm tra số (đã normalize) có trong text không."""
    text_norm = re.sub(r"[,.]", "", text)
    return num_norm in text_norm


def _get_comparator(text: str, num: str, unit: str) -> str | None:
    """
    Tìm comparator gần số trong text.
    Nhìn 40 ký tự TRƯỚC và 30 ký tự SAU số (tiếng Việt hay đặt sau).
    """
    # Tìm vị trí số trong text
    pos = text.find(num)
    if pos == -1:
        # Thử tìm không có unit
        pos = text.find(num.replace(",", "").replace(".", ""))
    if pos == -1:
        return None

    before = text[max(0, pos - 40):pos]
    after  = text[pos:min(len(text), pos + len(num) + 30)]

    ctx = before + " " + after

    for w in _CMP_GT:
        if w in ctx:
            return ">"
    for w in _CMP_GTE:
        if w in ctx:
            return ">="
    for w in _CMP_LTE:      # Kiểm tra LTE trước LT vì "trở xuống" dài hơn "xuống"
        if w in ctx:
            return "<="
    for w in _CMP_LT:
        if w in ctx:
            return "<"
    return None


def _any_in(text: str, word_set: set[str]) -> bool:
    return any(w in text for w in word_set)


def _first_match(text: str, word_set: set[str]) -> str:
    for w in word_set:
        if w in text:
            return w
    return ""
