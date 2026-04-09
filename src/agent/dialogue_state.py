"""
src/agent/dialogue_state.py — Lightweight Dialogue State Tracker (DST)

P2 — Multi-turn conversation support.

Kiến trúc 2 lớp:
  - State layer: {entity_type, tax_type, income_info, scenario} → dùng cho retrieval filter
  - Intent layer: dialogue intent (continuation / new_topic / clarification) → dùng cho LLM reasoning

Lifecycle:
  - Update: overwrite nếu có info mới, giữ nếu follow-up
  - Reset: nếu turn hoàn toàn không liên quan đến state hiện tại
  - Conflict: log warning nếu phát hiện mâu thuẫn (e.g., vừa là HKD vừa là doanh nghiệp)

Không dùng LLM — keyword + regex, ~0ms latency.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Patterns ──────────────────────────────────────────────────────────────────

_ENTITY_PATTERNS: dict[str, list[str]] = {
    "HKD": [
        "hộ kinh doanh", "hkd", "hộ cá thể", "kinh doanh hộ",
        "cơ sở kinh doanh", "hộ buôn bán",
    ],
    "CNKD": [
        "cá nhân kinh doanh", "cnkd", "cá nhân có thu nhập kinh doanh",
        "bán hàng online", "buôn bán online", "kinh doanh online",
    ],
    "individual": [
        "tôi", "mình", "nhân viên", "người lao động", "người làm công",
        "cá nhân", "người có thu nhập", "cá nhân cư trú",
    ],
    "enterprise": [
        "doanh nghiệp", "công ty", "tnhh", "cổ phần", "tập đoàn",
    ],
}

_TAX_PATTERNS: dict[str, list[str]] = {
    "TNCN": [
        "tncn", "thu nhập cá nhân", "thuế thu nhập", "lương", "tiền lương",
        "giảm trừ gia cảnh", "người phụ thuộc", "khấu trừ tại nguồn",
        "quyết toán thuế", "khởi điểm chịu thuế",
    ],
    "GTGT": [
        "gtgt", "giá trị gia tăng", "vat", "ngưỡng 100 triệu",
        "thuế đầu ra", "thuế đầu vào",
    ],
    "HKD_TAX": [
        "thuế khoán", "thuế hộ kinh doanh", "thuế môn bài",
        "lệ phí môn bài", "thuế hộ", "nd68", "68/2026",
        "khai thuế theo tháng", "khai thuế theo quý",
    ],
    "TMDT": [
        "tmđt", "thương mại điện tử", "sàn tmđt", "shopee", "tiktok shop",
        "lazada", "sàn khai thay", "sàn khấu trừ", "bán hàng online",
    ],
    "Penalty": [
        "xử phạt", "phạt vi phạm", "chế tài", "tiền phạt", "vi phạm hành chính",
        "thanh tra", "kiểm tra thuế",
    ],
}

_INCOME_PATTERN = re.compile(
    r"(\d+(?:[,\.]\d+)?)\s*"
    r"(triệu|tỷ|nghìn|tr|ty|k)?\s*"
    r"(?:/|trên)?\s*"
    r"(tháng|năm|quý|ngày)?",
    re.IGNORECASE | re.UNICODE,
)

_DEPENDENT_PATTERN = re.compile(
    r"(\d+)\s*(?:người phụ thuộc|npt|con nhỏ|con|người thân)",
    re.IGNORECASE | re.UNICODE,
)

_CONTINUATION_WORDS = [
    "ngoài ra", "còn", "thêm nữa", "bên cạnh đó", "thêm",
    "cũng", "như vậy", "vậy thì", "vậy", "ở trường hợp đó",
    "trường hợp đó", "trường hợp này", "trường hợp trên",
    "ý tôi là", "cụ thể hơn", "chi tiết hơn",
    "trên", "đó", "câu hỏi trước", "ví dụ trên",
]

_RESET_THRESHOLD = 0   # số keywords overlap tối thiểu để không reset

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DialogueState:
    entity_type:  Optional[str] = None   # "individual", "HKD", "CNKD", "enterprise"
    tax_type:     Optional[str] = None   # "TNCN", "GTGT", "HKD_TAX", "TMDT", "Penalty"
    income_info:  Optional[str] = None   # e.g., "20 triệu/tháng"
    scenario:     Optional[str] = None   # e.g., "2 người phụ thuộc"
    turn_count:   int = 0

    def is_empty(self) -> bool:
        return (
            self.entity_type is None
            and self.tax_type is None
            and self.income_info is None
            and self.scenario is None
        )


@dataclass
class DialogueIntent:
    intent:  str = "new"           # "continuation" | "new_topic" | "clarification" | "new"
    refs_prev: bool = False         # True nếu có từ reference đến turn trước
    confidence: float = 0.0


# ── DST ───────────────────────────────────────────────────────────────────────

class DialogueStateTracker:
    """
    Lightweight DST cho TaxAI multi-turn conversation.

    Usage:
        tracker = DialogueStateTracker()
        for turn in session_messages:
            if turn["role"] == "user":
                state, intent = tracker.process_turn(turn["content"])

        context_str = tracker.build_context_string()
        # → pass vào planner.answer(context_hint=context_str)
    """

    def __init__(self) -> None:
        self._state  = DialogueState()
        self._intent = DialogueIntent()

    # ── Public API ────────────────────────────────────────────────────────────

    def process_history(self, messages: list[dict]) -> None:
        """
        Replay toàn bộ conversation history để build DST state.
        Chỉ process user messages.
        """
        self._state  = DialogueState()
        self._intent = DialogueIntent()
        for msg in messages:
            if msg.get("role") == "user":
                self._process_turn(msg.get("content", ""))

    def process_current_turn(self, question: str) -> tuple[DialogueState, DialogueIntent]:
        """Process câu hỏi hiện tại, cập nhật state."""
        self._process_turn(question)
        return self._state, self._intent

    def build_context_string(self) -> str:
        """
        Build context hint string cho LLM system prompt.
        Trả về empty string nếu state rỗng (turn đầu tiên).
        """
        if self._state.is_empty() and not self._intent.refs_prev:
            return ""

        lines: list[str] = []

        if self._state.entity_type:
            _ENTITY_LABELS = {
                "HKD":        "Hộ kinh doanh",
                "CNKD":       "Cá nhân kinh doanh",
                "individual": "Cá nhân (người lao động)",
                "enterprise": "Doanh nghiệp",
            }
            lines.append(f"- Đối tượng: {_ENTITY_LABELS.get(self._state.entity_type, self._state.entity_type)}")

        if self._state.tax_type:
            _TAX_LABELS = {
                "TNCN":     "Thuế thu nhập cá nhân (TNCN)",
                "GTGT":     "Thuế giá trị gia tăng (GTGT)",
                "HKD_TAX":  "Thuế hộ kinh doanh / thuế khoán",
                "TMDT":     "Thuế thương mại điện tử",
                "Penalty":  "Xử phạt vi phạm hành chính thuế",
            }
            lines.append(f"- Chủ đề thuế: {_TAX_LABELS.get(self._state.tax_type, self._state.tax_type)}")

        if self._state.income_info:
            lines.append(f"- Thu nhập/Doanh thu đã đề cập: {self._state.income_info}")

        if self._state.scenario:
            lines.append(f"- Tình huống đã đề cập: {self._state.scenario}")

        if self._intent.intent == "continuation":
            lines.append("- Câu hỏi hiện tại là TIẾP NỐI câu hỏi trước (cùng chủ đề/đối tượng)")
        elif self._intent.intent == "clarification":
            lines.append("- Câu hỏi hiện tại là LÀM RÕ / bổ sung thêm cho câu hỏi trước")

        if not lines:
            return ""

        return "### Ngữ cảnh hội thoại (từ các lượt trước):\n" + "\n".join(lines)

    @property
    def state(self) -> DialogueState:
        return self._state

    @property
    def intent(self) -> DialogueIntent:
        return self._intent

    # ── Internal ──────────────────────────────────────────────────────────────

    def _process_turn(self, text: str) -> None:
        """Process 1 user turn, cập nhật state + intent."""
        text_lower = text.lower()

        # 1. Detect intent
        refs_prev = any(w in text_lower for w in _CONTINUATION_WORDS)
        self._intent.refs_prev = refs_prev

        # 2. Extract entities từ turn hiện tại
        new_entity   = _detect_entity(text_lower)
        new_tax      = _detect_tax(text_lower)
        new_income   = _extract_income(text)
        new_scenario = _extract_scenario(text)

        # 3. Reset detection: nếu state đã có tax_type nhưng turn mới hoàn toàn khác
        should_reset = (
            not refs_prev
            and not self._state.is_empty()
            and new_tax is not None
            and new_tax != self._state.tax_type
            and new_entity is not None
            and new_entity != self._state.entity_type
        )
        if should_reset:
            logger.info(
                "[DST] State reset: old_tax=%s new_tax=%s old_entity=%s new_entity=%s",
                self._state.tax_type, new_tax,
                self._state.entity_type, new_entity,
            )
            self._state = DialogueState()
            self._intent.intent = "new_topic"
        elif refs_prev:
            self._intent.intent = "continuation"
        elif self._state.is_empty():
            self._intent.intent = "new"
        else:
            self._intent.intent = "clarification" if not new_tax and not new_entity else "continuation"

        # 4. Update state (overwrite nếu mới cụ thể hơn, giữ nếu None)
        if new_entity:
            self._state.entity_type = new_entity
        if new_tax:
            self._state.tax_type = new_tax
        if new_income:
            self._state.income_info = new_income
        if new_scenario:
            self._state.scenario = new_scenario

        self._state.turn_count += 1
        logger.debug(
            "[DST] turn=%d entity=%s tax=%s intent=%s",
            self._state.turn_count, self._state.entity_type,
            self._state.tax_type, self._intent.intent,
        )


# ── Extraction helpers ────────────────────────────────────────────────────────

def _detect_entity(text_lower: str) -> Optional[str]:
    """Ưu tiên: HKD > CNKD > enterprise > individual"""
    for label in ("HKD", "CNKD", "enterprise", "individual"):
        if any(kw in text_lower for kw in _ENTITY_PATTERNS[label]):
            return label
    return None


def _detect_tax(text_lower: str) -> Optional[str]:
    """Ưu tiên: TMDT > HKD_TAX > Penalty > TNCN > GTGT"""
    for label in ("TMDT", "HKD_TAX", "Penalty", "TNCN", "GTGT"):
        if any(kw in text_lower for kw in _TAX_PATTERNS[label]):
            return label
    return None


def _extract_income(text: str) -> Optional[str]:
    """Trích xuất thông tin thu nhập/doanh thu (ví dụ: '20 triệu/tháng')."""
    # Chỉ extract nếu có từ gợi ý income
    income_kws = ("lương", "thu nhập", "doanh thu", "doanh số", "kiếm", "nhận")
    text_lower = text.lower()
    if not any(k in text_lower for k in income_kws):
        return None

    m = _INCOME_PATTERN.search(text)
    if m and m.group(1):
        amount = m.group(1)
        unit   = m.group(2) or ""
        period = m.group(3) or ""
        result = f"{amount} {unit}".strip()
        if period:
            result += f"/{period}"
        return result if len(result) > 2 else None
    return None


def _extract_scenario(text: str) -> Optional[str]:
    """Trích xuất tình huống (số NPT, số con, v.v.)."""
    m = _DEPENDENT_PATTERN.search(text)
    if m:
        n    = m.group(1)
        rest = m.group(0).replace(n, "").strip()
        return f"{n} {rest}"
    return None
