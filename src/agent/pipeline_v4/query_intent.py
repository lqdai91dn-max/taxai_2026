"""
src/agent/pipeline_v4/query_intent.py — P5.1 QueryIntent Builder

Implements the QueryIntent dataclass and two-stage extraction:
  Stage 1: Rule Parser  — deterministic, regex-based, ~0ms, NO LLM
  Stage 2: LLM Extractor — Gemini call, only when overall_confidence < threshold

Public API:
    build_query_intent(query, api_key, model, llm_threshold) -> QueryIntent

Canonical ENUMs (FROZEN 2026-03-26, source: PLAN.md §4.3):

  activity_group HKD:
    goods_distribution, services_without_materials, manufacturing_transport,
    asset_rental, e_commerce_platform

  activity_group PIT:
    salary_wages, real_estate_transfer, capital_investment, capital_transfer,
    lottery_prizes, royalties_franchising, inheritance_gifts

  Fallback: other_activities, UNSPECIFIED

  WHO:        individual | HKD | employer | employee | enterprise | UNSPECIFIED
  TAX_DOMAIN: PIT | HKD | VAT | TMDT | PENALTY | UNSPECIFIED
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FROZEN ENUMs
# ---------------------------------------------------------------------------

WHO_VALUES = frozenset({"individual", "HKD", "employer", "employee", "enterprise", "UNSPECIFIED"})

ACTIVITY_GROUP_HKD = frozenset({
    "goods_distribution",
    "services_without_materials",
    "manufacturing_transport",
    "asset_rental",
    "e_commerce_platform",
})

ACTIVITY_GROUP_PIT = frozenset({
    "salary_wages",
    "real_estate_transfer",
    "capital_investment",
    "capital_transfer",
    "lottery_prizes",
    "royalties_franchising",
    "inheritance_gifts",
})

ACTIVITY_GROUP_FALLBACK = frozenset({"other_activities", "UNSPECIFIED"})

ACTIVITY_GROUP_ALL = ACTIVITY_GROUP_HKD | ACTIVITY_GROUP_PIT | ACTIVITY_GROUP_FALLBACK

TAX_DOMAIN_VALUES = frozenset({"PIT", "HKD", "VAT", "TMDT", "PENALTY", "UNSPECIFIED"})


# ---------------------------------------------------------------------------
# FieldValue — typed container with confidence + source
# ---------------------------------------------------------------------------

@dataclass
class FieldValue:
    """Wraps a field value with extraction metadata."""
    value: Any                          # The extracted value
    confidence: float = 0.0            # 0.0 – 1.0
    source: str = "rule"               # "rule" | "llm" | "merged"

    def __repr__(self) -> str:
        return f"FieldValue({self.value!r}, conf={self.confidence:.2f}, src={self.source!r})"


# ---------------------------------------------------------------------------
# QueryIntent dataclass
# ---------------------------------------------------------------------------

@dataclass
class QueryIntent:
    """
    Structured representation of user query intent.

    Every top-level field carries FieldValue metadata (confidence + source).
    _confidence and _source dicts mirror the per-field metadata for convenience.
    """

    # Core semantic fields
    who: FieldValue = field(
        default_factory=lambda: FieldValue("UNSPECIFIED", 0.0, "rule")
    )
    activity_group: FieldValue = field(
        default_factory=lambda: FieldValue([], 0.0, "rule")
    )
    tax_domain: FieldValue = field(
        default_factory=lambda: FieldValue(["UNSPECIFIED"], 0.0, "rule")
    )
    financials: FieldValue = field(
        default_factory=lambda: FieldValue(
            {"revenue": None, "income_value": None, "dependent_count": None},
            0.0, "rule",
        )
    )
    time: FieldValue = field(
        default_factory=lambda: FieldValue({"year": None}, 0.0, "rule")
    )
    intent: FieldValue = field(
        default_factory=lambda: FieldValue(
            {
                "primary": "UNSPECIFIED",
                "secondary": None,
                "requires_calculation": False,
                "requires_conditions": False,
            },
            0.0, "rule",
        )
    )
    flags: FieldValue = field(
        default_factory=lambda: FieldValue(
            {"is_first_time": None, "is_sole_property": None, "is_online_platform": False},
            0.0, "rule",
        )
    )

    # Aggregated metadata (populated by merge step)
    _confidence: Dict[str, float] = field(default_factory=dict)
    _source: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def overall_confidence(self) -> float:
        """
        Compute overall confidence as weighted average of core fields.
        Fields with UNSPECIFIED / empty lists score 0.
        """
        weights = {
            "who": 0.25,
            "activity_group": 0.20,
            "tax_domain": 0.20,
            "financials": 0.10,
            "time": 0.05,
            "intent": 0.20,
        }
        total = 0.0
        for fname, w in weights.items():
            fv: FieldValue = getattr(self, fname)
            score = fv.confidence
            # Penalise UNSPECIFIED / empty
            if fname == "who" and fv.value == "UNSPECIFIED":
                score = 0.0
            elif fname in ("activity_group", "tax_domain"):
                if not fv.value or fv.value == ["UNSPECIFIED"]:
                    score = 0.0
            total += w * score
        return round(total, 3)

    def to_dict(self) -> dict:
        """Serialise to plain dict (for logging / JSON output)."""
        return {
            "who": {"value": self.who.value, "confidence": self.who.confidence, "source": self.who.source},
            "activity_group": {"value": self.activity_group.value, "confidence": self.activity_group.confidence, "source": self.activity_group.source},
            "tax_domain": {"value": self.tax_domain.value, "confidence": self.tax_domain.confidence, "source": self.tax_domain.source},
            "financials": {"value": self.financials.value, "confidence": self.financials.confidence, "source": self.financials.source},
            "time": {"value": self.time.value, "confidence": self.time.confidence, "source": self.time.source},
            "intent": {"value": self.intent.value, "confidence": self.intent.confidence, "source": self.intent.source},
            "flags": {"value": self.flags.value, "confidence": self.flags.confidence, "source": self.flags.source},
            "_overall_confidence": self.overall_confidence(),
            "_confidence": self._confidence,
            "_source": self._source,
        }


# ---------------------------------------------------------------------------
# Rule Parser — deterministic, NO LLM
# ---------------------------------------------------------------------------

# Pre-compiled regex patterns (module-level for reuse)

# Vietnamese currency units → multiplier to VND
_UNIT_MAP = {
    "tỷ": 1_000_000_000,
    "ty": 1_000_000_000,
    "triệu": 1_000_000,
    "trieu": 1_000_000,
    "tr": 1_000_000,
    "nghìn": 1_000,
    "ngan": 1_000,
    "ngàn": 1_000,
    "k": 1_000,
}

_MONEY_RE = re.compile(
    r"(\d[\d\.,]*)\s*(tỷ|ty|triệu|trieu|tr|nghìn|ngàn|ngan|k)\b",
    re.IGNORECASE | re.UNICODE,
)

_DEPENDENT_RE = re.compile(
    r"(\d+)\s*(?:con|người\s*phụ\s*thuộc|phụ\s*thuộc|người\s*pt)",
    re.IGNORECASE | re.UNICODE,
)

_YEAR_RE = re.compile(r"\bnăm\s*(20\d{2})\b|\b(20\d{2})\b", re.IGNORECASE | re.UNICODE)


def _parse_number(raw: str) -> float:
    """
    Convert a raw number string to float.

    Strategy:
      - If the string contains exactly one '.' or ',' and the fractional part
        is 1–2 digits → treat it as a decimal separator (e.g. "1.2", "1,5").
      - Otherwise treat '.' and ',' as thousands separators and strip them
        (e.g. "1.200.000", "1,200,000").
    """
    # Normalise: work with a copy that uses '.' as decimal candidate
    s = raw.strip()

    # Count separators
    dot_count = s.count(".")
    comma_count = s.count(",")

    # Case 1: single separator that looks like a decimal (e.g. "1.2", "1,5")
    # Condition: exactly one separator AND digits after it are 1–2 chars
    def _likely_decimal(sep: str) -> bool:
        idx = s.rfind(sep)
        if idx < 0:
            return False
        after = s[idx + 1:]
        return 1 <= len(after) <= 2 and after.isdigit()

    if dot_count == 1 and comma_count == 0 and _likely_decimal("."):
        try:
            return float(s)
        except ValueError:
            return 0.0

    if comma_count == 1 and dot_count == 0 and _likely_decimal(","):
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return 0.0

    # Case 2: thousands separators — strip both '.' and ','
    cleaned = s.replace(",", "").replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _extract_money(text: str) -> Optional[float]:
    """Return first monetary value in VND found in text, or None."""
    m = _MONEY_RE.search(text)
    if not m:
        return None
    num = _parse_number(m.group(1))
    unit = m.group(2).lower()
    multiplier = _UNIT_MAP.get(unit, 1)
    return num * multiplier


def _extract_all_money(text: str) -> List[float]:
    """Return all monetary values (VND) found in text."""
    results = []
    for m in _MONEY_RE.finditer(text):
        num = _parse_number(m.group(1))
        unit = m.group(2).lower()
        multiplier = _UNIT_MAP.get(unit, 1)
        results.append(num * multiplier)
    return results


# WHO detection patterns — ordered by specificity (most specific first)
_WHO_PATTERNS = [
    # employer / payroll — higher conf than individual to win when both "lương" signals present
    (re.compile(r"\btrả\s*lương\b|\bkhấu\s*trừ\s*tại\s*nguồn\b|\bnhà\s*tuyển\s*dụng\b", re.I | re.U), "employer", 0.95),
    # employee
    (re.compile(r"\bnhân\s*viên\b|\bngười\s*lao\s*động\b|\bcông\s*chức\b|\bviên\s*chức\b", re.I | re.U), "employee", 0.85),
    # enterprise / company
    (re.compile(r"\bcông\s*ty\b|\bdoanh\s*nghiệp\b|\btổ\s*chức\b|\bcorporate\b", re.I | re.U), "enterprise", 0.85),
    # HKD (must come before individual)
    (re.compile(
        r"\bhộ\s*kinh\s*doanh\b|\bhkd\b|\bkinh\s*doanh\s*cá\s*thể\b"
        r"|\btiệm\b|\bcửa\s*hàng\b|\bquán\b|\bshop\b|\bbán\s*hàng\b"
        r"|\bkinh\s*doanh\b|\bdoanh\s*thu\b",
        re.I | re.U,
    ), "HKD", 0.9),
    # individual (salary / personal income)
    (re.compile(r"\blương\b|\btiền\s*công\b|\bcá\s*nhân\b|\btncn\b|\btôi\s*(có|nhận|kiếm)\b", re.I | re.U), "individual", 0.9),
]

# ACTIVITY_GROUP patterns — (pattern, activity_group, implied_who, implied_domain, confidence)
_ACTIVITY_PATTERNS: List[tuple] = [
    # HKD activities
    (re.compile(r"\bvàng\b|\bkim\s*hoàn\b|\btrang\s*sức\b|\bvàng\s*bạc\b", re.I | re.U),
     "goods_distribution", "HKD", "HKD", 0.9),
    (re.compile(r"\bnhà\s*hàng\b|\bquán\s*ăn\b|\bcafé\b|\bcà\s*phê\b|\băn\s*uống\b|\bquán\b", re.I | re.U),
     "manufacturing_transport", "HKD", "HKD", 0.85),
    (re.compile(r"\bdịch\s*vụ\b|\bspa\b|\bsalon\b|\btóc\b|\bnail\b|\bmassage\b", re.I | re.U),
     "services_without_materials", "HKD", "HKD", 0.85),
    (re.compile(r"\bvận\s*tải\b|\bxe\b|\btaxi\b|\bgrab\b|\bgiao\s*hàng\b|\bship\b", re.I | re.U),
     "manufacturing_transport", "HKD", "HKD", 0.85),
    (re.compile(r"\bbán\s*hàng\s*online\b|\bshopee\b|\btiktok\b|\blazada\b|\bsàn\b|\btmđt\b|\be.?commerce\b|\btrực\s*tuyến\b", re.I | re.U),
     "e_commerce_platform", "HKD", "HKD", 0.95),
    (re.compile(r"\bcho\s*thuê\b|\bthuê\s*nhà\b|\bbất\s*động\s*sản\b|\bbđs\b|\bthueˆ\s*tài\s*sản\b", re.I | re.U),
     "asset_rental", None, None, 0.85),  # who/domain depends on context
    # PIT activities
    (re.compile(r"\bxổ\s*số\b|\bvé\s*số\b|\btrúng\s*thưởng\b|\bgiải\s*thưởng\b", re.I | re.U),
     "lottery_prizes", "individual", "PIT", 0.95),
    (re.compile(r"\bchứng\s*khoán\b|\bcổ\s*phiếu\b|\bcổ\s*tức\b|\bchuyển\s*nhượng\s*vốn\b", re.I | re.U),
     "capital_transfer", "individual", "PIT", 0.9),
    (re.compile(r"\bđầu\s*tư\s*vốn\b|\blãi\s*suất\b|\blợi\s*tức\b", re.I | re.U),
     "capital_investment", "individual", "PIT", 0.85),
    (re.compile(r"\bchuyển\s*nhượng\b|\bbán\s*nhà\b|\bbán\s*đất\b|\bbất\s*động\s*sản\b.*\bbán\b|\bbán\b.*\bbất\s*động\s*sản\b", re.I | re.U),
     "real_estate_transfer", "individual", "PIT", 0.9),
    (re.compile(r"\blương\b|\btiền\s*công\b|\bphụ\s*cấp\b|\bthưởng\s*lương\b", re.I | re.U),
     "salary_wages", "individual", "PIT", 0.9),
    (re.compile(r"\bbản\s*quyền\b|\bnhượng\s*quyền\b|\bfrachise\b|\bfranchise\b", re.I | re.U),
     "royalties_franchising", "individual", "PIT", 0.85),
    (re.compile(r"\bthừa\s*kế\b|\bquà\s*tặng\b|\bquà\s*biếu\b|\btặng\b", re.I | re.U),
     "inheritance_gifts", "individual", "PIT", 0.85),
    # HKD general goods (less specific — lower confidence)
    (re.compile(r"\bbán\s*hàng\b|\bhàng\s*hóa\b|\bphân\s*phối\b|\bnhập\s*khẩu\b|\bxuất\s*khẩu\b", re.I | re.U),
     "goods_distribution", "HKD", "HKD", 0.75),
]

# INTENT detection patterns
_INTENT_CALCULATE_RE = re.compile(
    r"\btính\b|\bbao\s*nhiêu\b|\bphải\s*nộp\b|\bthuế\s*(là|bằng)\b"
    r"|\bnộp\s*thuế\b|\bkhấu\s*trừ\b|\bnộp\s*bao\s*nhiêu\b"
    r"|\bthuế\s*bao\s*nhiêu\b|\bbao\s*nhiêu\s*tiền\b"
    r"|\bthuế\s*[?？]|\bthuế\s*gì\b|\bnộp\s*gì\b",
    re.I | re.U,
)

_INTENT_EXPLAIN_CONDITION_RE = re.compile(
    r"\bđiều\s*kiện\b|\bquy\s*định\b|\bnhư\s*thế\s*nào\b|\bcó\s*phải\b"
    r"|\bđược\s*không\b|\bhướng\s*dẫn\b|\bthủ\s*tục\b|\bcách\b|\bquy\s*trình\b",
    re.I | re.U,
)

_INTENT_EXEMPT_RE = re.compile(
    r"\bmiễn\s*thuế\b|\bkhông\s*phải\s*nộp\b|\bđược\s*miễn\b|\bmiễn\b",
    re.I | re.U,
)

_INTENT_POLICY_RE = re.compile(
    r"\bthay\s*đổi\b|\bmới\b|\bcập\s*nhật\b|\bquy\s*định\s*mới\b|\bluật\s*mới\b"
    r"|\bnăm\s*202[0-9]\b.*\bthay\b|\bsửa\s*đổi\b",
    re.I | re.U,
)

# Financial context clues
_REVENUE_RE = re.compile(r"\bdoanh\s*thu\b|\bdoanhg\s*thu\b", re.I | re.U)
_INCOME_RE = re.compile(r"\bthu\s*nhập\b|\blương\b|\btiền\s*công\b|\btiền\s*thưởng\b", re.I | re.U)


class RuleParser:
    """
    Deterministic rule-based parser.
    No API calls, no imports of LLM libraries.
    ~0ms latency.
    """

    def parse(self, query: str) -> QueryIntent:
        """Run all rule-based extractors and return a QueryIntent."""
        qi = QueryIntent()

        # Extraction order matters for conflict resolution
        who_val, who_conf = self._extract_who(query)
        activity_val, activity_conf, activity_implied_who, activity_implied_domain = self._extract_activity(query)
        financials_val, financials_conf = self._extract_financials(query)
        time_val, time_conf = self._extract_time(query)
        intent_val, intent_conf = self._extract_intent(query)
        flags_val, flags_conf = self._extract_flags(query)

        # Resolve WHO conflicts: explicit keyword wins over activity-implied
        if who_conf < 0.5 and activity_implied_who and activity_conf >= 0.8:
            who_val = activity_implied_who
            who_conf = min(activity_conf, 0.85)

        # Infer TAX_DOMAIN from WHO + activity
        tax_domain_val, tax_domain_conf = self._infer_tax_domain(
            who_val, who_conf, activity_val, activity_conf, activity_implied_domain
        )

        # Populate QueryIntent fields
        qi.who = FieldValue(who_val, who_conf, "rule")
        qi.activity_group = FieldValue(activity_val, activity_conf, "rule")
        qi.tax_domain = FieldValue(tax_domain_val, tax_domain_conf, "rule")
        qi.financials = FieldValue(financials_val, financials_conf, "rule")
        qi.time = FieldValue(time_val, time_conf, "rule")
        qi.intent = FieldValue(intent_val, intent_conf, "rule")
        qi.flags = FieldValue(flags_val, flags_conf, "rule")

        # Sync _confidence and _source dicts
        qi._confidence = {
            "who": who_conf,
            "activity_group": activity_conf,
            "tax_domain": tax_domain_conf,
            "financials": financials_conf,
            "time": time_conf,
            "intent": intent_conf,
        }
        qi._source = {k: "rule" for k in qi._confidence}

        return qi

    # ------------------------------------------------------------------
    # WHO extractor
    # ------------------------------------------------------------------

    def _extract_who(self, text: str) -> tuple[str, float]:
        best_val = "UNSPECIFIED"
        best_conf = 0.0
        for pat, val, conf in _WHO_PATTERNS:
            if pat.search(text):
                if conf > best_conf:
                    best_conf = conf
                    best_val = val
        return best_val, best_conf

    # ------------------------------------------------------------------
    # ACTIVITY_GROUP extractor
    # ------------------------------------------------------------------

    def _extract_activity(self, text: str) -> tuple[List[str], float, Optional[str], Optional[str]]:
        """
        Returns (activity_list, confidence, implied_who, implied_domain).
        Multiple activities can match; best confidence wins for scalar fields.
        """
        found: Dict[str, tuple] = {}  # activity -> (conf, implied_who, implied_domain)
        for pat, activity, implied_who, implied_domain, conf in _ACTIVITY_PATTERNS:
            if pat.search(text):
                if activity not in found or conf > found[activity][0]:
                    found[activity] = (conf, implied_who, implied_domain)

        if not found:
            return [], 0.0, None, None

        # Deduplicate, pick best single implied context
        best_conf = 0.0
        best_who: Optional[str] = None
        best_domain: Optional[str] = None
        activities = []
        for activity, (conf, iw, id_) in found.items():
            activities.append(activity)
            if conf > best_conf:
                best_conf = conf
                best_who = iw
                best_domain = id_

        return activities, best_conf, best_who, best_domain

    # ------------------------------------------------------------------
    # TAX_DOMAIN inference
    # ------------------------------------------------------------------

    def _infer_tax_domain(
        self,
        who: str,
        who_conf: float,
        activities: List[str],
        activity_conf: float,
        activity_implied_domain: Optional[str],
    ) -> tuple[List[str], float]:
        domains: List[str] = []
        conf = 0.0

        if who in ("HKD", "employer", "enterprise") and who_conf >= 0.5:
            domains.append("HKD")
            conf = max(conf, who_conf)

        if who in ("individual", "employee") and who_conf >= 0.5:
            domains.append("PIT")
            conf = max(conf, who_conf)

        # Activity-implied domain (higher specificity)
        if activity_implied_domain and activity_conf >= 0.7:
            if activity_implied_domain not in domains:
                domains.append(activity_implied_domain)
            conf = max(conf, activity_conf)

        # Check if any PIT-specific activity was detected
        pit_activities = ACTIVITY_GROUP_PIT & set(activities)
        if pit_activities and "PIT" not in domains:
            domains.append("PIT")
            conf = max(conf, activity_conf)

        # Check if any HKD-specific activity was detected
        hkd_activities = ACTIVITY_GROUP_HKD & set(activities)
        if hkd_activities and "HKD" not in domains:
            domains.append("HKD")
            conf = max(conf, activity_conf)

        # explicit TMDT keyword
        tmdt_re = re.compile(r"\btmđt\b|\bsàn\s*(thương\s*mại)?\s*(điện\s*tử)?\b|\be.?commerce\b", re.I | re.U)
        if tmdt_re.search(""):  # placeholder — handle separately in flags
            if "TMDT" not in domains:
                domains.append("TMDT")

        if not domains:
            return ["UNSPECIFIED"], 0.0

        return domains, round(conf, 3)

    # ------------------------------------------------------------------
    # FINANCIALS extractor
    # ------------------------------------------------------------------

    def _extract_financials(self, text: str) -> tuple[dict, float]:
        financials = {"revenue": None, "income_value": None, "dependent_count": None}
        found_any = False
        conf = 0.0

        # Find all money values with context
        for m in _MONEY_RE.finditer(text):
            num = _parse_number(m.group(1))
            unit = m.group(2).lower()
            multiplier = _UNIT_MAP.get(unit, 1)
            amount = int(num * multiplier)

            # Look back ~40 chars for context
            start = max(0, m.start() - 40)
            context = text[start:m.end()].lower()

            if _REVENUE_RE.search(context):
                financials["revenue"] = amount
                found_any = True
                conf = max(conf, 0.85)
            elif _INCOME_RE.search(context):
                financials["income_value"] = amount
                found_any = True
                conf = max(conf, 0.85)
            else:
                # No explicit label — try to infer from WHO context
                # If it's the only number and query mentions "doanh thu" or "lương" anywhere
                if _REVENUE_RE.search(text) and financials["revenue"] is None:
                    financials["revenue"] = amount
                    found_any = True
                    conf = max(conf, 0.75)
                elif _INCOME_RE.search(text) and financials["income_value"] is None:
                    financials["income_value"] = amount
                    found_any = True
                    conf = max(conf, 0.75)
                else:
                    # Ambiguous — assign as revenue (most common context)
                    if financials["revenue"] is None and financials["income_value"] is None:
                        financials["revenue"] = amount
                        found_any = True
                        conf = max(conf, 0.6)

        # Dependent count
        dm = _DEPENDENT_RE.search(text)
        if dm:
            financials["dependent_count"] = int(dm.group(1))
            found_any = True
            conf = max(conf, 0.9)

        return financials, conf if found_any else 0.0

    # ------------------------------------------------------------------
    # TIME extractor
    # ------------------------------------------------------------------

    def _extract_time(self, text: str) -> tuple[dict, float]:
        m = _YEAR_RE.search(text)
        if m:
            year = int(m.group(1) or m.group(2))
            return {"year": year}, 0.9
        return {"year": None}, 0.0

    # ------------------------------------------------------------------
    # INTENT extractor
    # ------------------------------------------------------------------

    def _extract_intent(self, text: str) -> tuple[dict, float]:
        primary = "UNSPECIFIED"
        secondary = None
        requires_calculation = False
        requires_conditions = False
        conf = 0.0

        is_calculate = bool(_INTENT_CALCULATE_RE.search(text))
        is_explain_condition = bool(_INTENT_EXPLAIN_CONDITION_RE.search(text))
        is_exempt = bool(_INTENT_EXEMPT_RE.search(text))
        is_policy = bool(_INTENT_POLICY_RE.search(text))

        if is_calculate and not is_exempt:
            primary = "calculate"
            requires_calculation = True
            conf = 0.85
            if is_explain_condition:
                secondary = "explain"
                requires_conditions = True
        elif is_exempt or (is_explain_condition and not is_calculate):
            primary = "explain"
            requires_conditions = True
            conf = 0.8
        elif is_policy:
            primary = "explain"
            secondary = "policy_update"
            conf = 0.75
        elif is_explain_condition:
            primary = "explain"
            requires_conditions = True
            conf = 0.75

        if primary == "UNSPECIFIED":
            conf = 0.0

        return {
            "primary": primary,
            "secondary": secondary,
            "requires_calculation": requires_calculation,
            "requires_conditions": requires_conditions,
        }, conf

    # ------------------------------------------------------------------
    # FLAGS extractor
    # ------------------------------------------------------------------

    def _extract_flags(self, text: str) -> tuple[dict, float]:
        flags = {"is_first_time": None, "is_sole_property": None, "is_online_platform": False}
        found_any = False

        first_time_re = re.compile(r"\blần\s*đầu\b|\bmới\s*bắt\s*đầu\b|\bvừa\s*mở\b", re.I | re.U)
        sole_prop_re = re.compile(r"\bnhà\s*duy\s*nhất\b|\bsở\s*hữu\s*duy\s*nhất\b|\bcăn\s*nhà\s*duy\s*nhất\b", re.I | re.U)
        online_re = re.compile(r"\bonline\b|\bshopee\b|\btiktok\b|\blazada\b|\bsàn\b|\btmđt\b|\btrực\s*tuyến\b", re.I | re.U)

        if first_time_re.search(text):
            flags["is_first_time"] = True
            found_any = True
        if sole_prop_re.search(text):
            flags["is_sole_property"] = True
            found_any = True
        if online_re.search(text):
            flags["is_online_platform"] = True
            found_any = True

        return flags, 0.7 if found_any else 0.0


# ---------------------------------------------------------------------------
# LLM Extractor — Gemini, called only when rule confidence is low
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = """Bạn là hệ thống phân tích ý định câu hỏi thuế Việt Nam.
Nhiệm vụ: trích xuất thông tin từ câu hỏi người dùng theo cấu trúc 5W2H.

Phân tích theo 7 chiều:
1. WHO  — Ai là người nộp thuế? (individual | HKD | employer | employee | enterprise | UNSPECIFIED)
2. WHAT — Thu nhập / doanh thu từ hoạt động gì?
3. WHICH LAW — Nhóm thuế nào? (PIT | HKD | VAT | TMDT | PENALTY | UNSPECIFIED)
4. HOW MUCH — Con số cụ thể? (chuẩn hóa ra VND, ví dụ: 1.2 tỷ = 1200000000)
5. WHEN — Năm tính thuế? (nếu không đề cập, để null)
6. HOW (intent) — Hỏi để tính thuế (calculate) hay hiểu điều kiện/quy định (explain)?
7. WHY (context) — Lần đầu? Bất động sản duy nhất? Kinh doanh trên sàn?

Canonical ENUMs bắt buộc dùng:
- who: individual | HKD | employer | employee | enterprise | UNSPECIFIED
- activity_group HKD: goods_distribution | services_without_materials | manufacturing_transport | asset_rental | e_commerce_platform
- activity_group PIT: salary_wages | real_estate_transfer | capital_investment | capital_transfer | lottery_prizes | royalties_franchising | inheritance_gifts
- activity_group fallback: other_activities | UNSPECIFIED
- tax_domain: PIT | HKD | VAT | TMDT | PENALTY | UNSPECIFIED

Trả về JSON duy nhất (không có text ngoài JSON):
{
  "who": "<enum>",
  "who_confidence": <0.0-1.0>,
  "activity_group": ["<enum>"],
  "activity_confidence": <0.0-1.0>,
  "tax_domain": ["<enum>"],
  "tax_domain_confidence": <0.0-1.0>,
  "financials": {
    "revenue": <number or null>,
    "income_value": <number or null>,
    "dependent_count": <integer or null>
  },
  "financials_confidence": <0.0-1.0>,
  "time": {"year": <integer or null>},
  "time_confidence": <0.0-1.0>,
  "intent": {
    "primary": "calculate|explain|UNSPECIFIED",
    "secondary": "<string or null>",
    "requires_calculation": <bool>,
    "requires_conditions": <bool>
  },
  "intent_confidence": <0.0-1.0>,
  "flags": {
    "is_first_time": <bool or null>,
    "is_sole_property": <bool or null>,
    "is_online_platform": <bool>
  }
}"""


class LLMExtractor:
    """
    Optional LLM-based extractor using Gemini.
    Only instantiated when needed (api_key present + confidence below threshold).
    """

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self._api_key = api_key
        self._model = model

    def extract(self, query: str) -> Optional[dict]:
        """
        Call Gemini with 5W2H prompt.
        Returns parsed JSON dict or None on failure.
        """
        try:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=self._api_key)
            model = genai.GenerativeModel(
                model_name=self._model,
                system_instruction=_LLM_SYSTEM_PROMPT,
            )
            response = model.generate_content(
                f"Phân tích câu hỏi sau:\n\n{query}",
                generation_config={"temperature": 0.0, "response_mime_type": "application/json"},
            )
            raw = response.text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
                raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
        except ImportError:
            logger.warning("google-generativeai not installed — LLM extraction skipped")
            return None
        except Exception as exc:
            logger.warning("LLM extraction failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Merge logic — rule wins if conf >= 0.8, else LLM fills gaps
# ---------------------------------------------------------------------------

def _merge(rule_qi: QueryIntent, llm_data: dict) -> QueryIntent:
    """
    Merge rule-based QueryIntent with LLM JSON output.
    Rule wins when rule confidence >= 0.8.
    LLM fills UNSPECIFIED / low-confidence fields.
    Conflict → higher confidence wins.
    """
    merged = QueryIntent()
    RULE_WIN_THRESHOLD = 0.8

    def merge_field(
        fname: str,
        rule_fv: FieldValue,
        llm_value: Any,
        llm_conf: float,
        unspecified_sentinel,
    ) -> FieldValue:
        is_rule_unspecified = rule_fv.value == unspecified_sentinel
        is_llm_present = llm_value is not None and llm_value != unspecified_sentinel

        if rule_fv.confidence >= RULE_WIN_THRESHOLD:
            return FieldValue(rule_fv.value, rule_fv.confidence, "rule")
        if is_rule_unspecified and is_llm_present:
            return FieldValue(llm_value, llm_conf, "llm")
        if is_llm_present and llm_conf > rule_fv.confidence:
            return FieldValue(llm_value, llm_conf, "llm")
        if is_llm_present and not is_rule_unspecified:
            # Both have values — pick higher confidence
            if llm_conf > rule_fv.confidence:
                return FieldValue(llm_value, llm_conf, "llm")
            else:
                return FieldValue(rule_fv.value, rule_fv.confidence, "rule")
        # Default: keep rule
        return FieldValue(rule_fv.value, rule_fv.confidence, "rule")

    merged.who = merge_field(
        "who", rule_qi.who,
        llm_data.get("who"), llm_data.get("who_confidence", 0.0),
        "UNSPECIFIED",
    )

    rule_ag = rule_qi.activity_group
    llm_ag = llm_data.get("activity_group", [])
    llm_ag_conf = llm_data.get("activity_confidence", 0.0)
    merged.activity_group = merge_field(
        "activity_group", rule_ag,
        llm_ag if llm_ag else None,
        llm_ag_conf,
        [],
    )

    rule_td = rule_qi.tax_domain
    llm_td = llm_data.get("tax_domain", ["UNSPECIFIED"])
    llm_td_conf = llm_data.get("tax_domain_confidence", 0.0)
    merged.tax_domain = merge_field(
        "tax_domain", rule_td,
        llm_td if llm_td and llm_td != ["UNSPECIFIED"] else None,
        llm_td_conf,
        ["UNSPECIFIED"],
    )

    # Financials — merge sub-fields individually
    rule_fin = rule_qi.financials.value.copy()
    llm_fin = llm_data.get("financials", {}) or {}
    llm_fin_conf = llm_data.get("financials_confidence", 0.0)
    merged_fin = dict(rule_fin)
    for subfield in ("revenue", "income_value", "dependent_count"):
        llm_sub = llm_fin.get(subfield)
        rule_sub = rule_fin.get(subfield)
        if rule_sub is None and llm_sub is not None:
            merged_fin[subfield] = llm_sub
        elif rule_sub is not None and llm_sub is not None:
            # Keep rule value (already extracted deterministically)
            pass
    fin_source = "merged" if llm_fin else "rule"
    fin_conf = max(rule_qi.financials.confidence, llm_fin_conf)
    merged.financials = FieldValue(merged_fin, fin_conf, fin_source)

    # Time
    rule_time = rule_qi.time
    llm_time = llm_data.get("time", {})
    llm_time_conf = llm_data.get("time_confidence", 0.0)
    merged.time = merge_field(
        "time", rule_time,
        llm_time if llm_time and llm_time.get("year") else None,
        llm_time_conf,
        {"year": None},
    )

    # Intent
    rule_intent = rule_qi.intent
    llm_intent = llm_data.get("intent", {})
    llm_intent_conf = llm_data.get("intent_confidence", 0.0)
    merged.intent = merge_field(
        "intent", rule_intent,
        llm_intent if llm_intent and llm_intent.get("primary", "UNSPECIFIED") != "UNSPECIFIED" else None,
        llm_intent_conf,
        {"primary": "UNSPECIFIED", "secondary": None, "requires_calculation": False, "requires_conditions": False},
    )

    # Flags — merge sub-fields
    rule_flags = rule_qi.flags.value.copy()
    llm_flags = llm_data.get("flags", {}) or {}
    merged_flags = dict(rule_flags)
    for subfield in ("is_first_time", "is_sole_property", "is_online_platform"):
        llm_sub = llm_flags.get(subfield)
        if merged_flags.get(subfield) is None and llm_sub is not None:
            merged_flags[subfield] = llm_sub
    flags_source = "merged" if llm_flags else "rule"
    flags_conf = max(rule_qi.flags.confidence, 0.0)
    merged.flags = FieldValue(merged_flags, flags_conf, flags_source)

    # Sync metadata dicts
    merged._confidence = {
        "who": merged.who.confidence,
        "activity_group": merged.activity_group.confidence,
        "tax_domain": merged.tax_domain.confidence,
        "financials": merged.financials.confidence,
        "time": merged.time.confidence,
        "intent": merged.intent.confidence,
    }
    merged._source = {
        "who": merged.who.source,
        "activity_group": merged.activity_group.source,
        "tax_domain": merged.tax_domain.source,
        "financials": merged.financials.source,
        "time": merged.time.source,
        "intent": merged.intent.source,
    }

    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_rule_parser = RuleParser()


def build_query_intent(
    query: str,
    api_key: Optional[str] = None,
    model: str = "gemini-2.5-flash",
    llm_threshold: float = 0.7,
) -> QueryIntent:
    """
    Build a QueryIntent from a user query.

    Stage 1: Rule Parser (always runs, ~0ms, no API).
    Stage 2: LLM Extractor (only when overall_confidence < llm_threshold AND api_key provided).

    Args:
        query:          Raw user query string.
        api_key:        Google Gemini API key (optional). If None, LLM is skipped.
        model:          Gemini model name.
        llm_threshold:  Call LLM when overall rule confidence < this value (default 0.7).

    Returns:
        QueryIntent — populated with best available extraction.
        Never raises — on LLM failure, returns rule-only result.
    """
    if not query or not query.strip():
        return QueryIntent()

    # Stage 1 — Rule Parser
    qi = _rule_parser.parse(query.strip())
    overall_conf = qi.overall_confidence()

    logger.debug(
        "Rule parser done. overall_conf=%.3f, who=%s, activity=%s, domain=%s",
        overall_conf, qi.who.value, qi.activity_group.value, qi.tax_domain.value,
    )

    # Stage 2 — LLM Extractor (conditional)
    if api_key and overall_conf < llm_threshold:
        logger.debug("overall_conf %.3f < threshold %.3f — calling LLM extractor", overall_conf, llm_threshold)
        extractor = LLMExtractor(api_key=api_key, model=model)
        llm_data = extractor.extract(query)
        if llm_data:
            qi = _merge(qi, llm_data)
            logger.debug("Merge done. new overall_conf=%.3f", qi.overall_confidence())
        else:
            logger.warning("LLM extraction returned None — using rule-only result")
    else:
        if not api_key:
            logger.debug("No API key — LLM extraction skipped")
        else:
            logger.debug("overall_conf %.3f >= threshold %.3f — LLM not needed", overall_conf, llm_threshold)

    return qi
