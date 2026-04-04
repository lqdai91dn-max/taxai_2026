"""
src/retrieval/node_annotator.py
P5.2 — NodeMetadata Annotation cho TaxAI

Annotate toàn bộ nodes trong ChromaDB với NodeMetadata schema, dùng LLM offline.

Usage:
    python -m src.retrieval.node_annotator --dry-run --limit 5
    python -m src.retrieval.node_annotator --doc-id 68_2026_NDCP
    python -m src.retrieval.node_annotator --all
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Canonical ENUMs (FROZEN) ──────────────────────────────────────────────────

CANONICAL_WHO = {"individual", "HKD", "employer", "employee", "enterprise", "UNSPECIFIED"}

CANONICAL_TAX_DOMAIN = {"PIT", "HKD", "VAT", "TMDT", "PENALTY", "UNSPECIFIED"}

CANONICAL_CONTENT_TYPE = {"tax_rate", "threshold", "condition_rule", "procedure", "definition"}

CANONICAL_ACTIVITY_GROUP = {
    # HKD
    "goods_distribution",
    "services_without_materials",
    "manufacturing_transport",
    "asset_rental",
    "e_commerce_platform",
    # PIT
    "salary_wages",
    "real_estate_transfer",
    "capital_investment",
    "capital_transfer",
    "lottery_prizes",
    "royalties_franchising",
    "inheritance_gifts",
    # Fallback
    "other_activities",
    "UNSPECIFIED",
}

# ── Paths ─────────────────────────────────────────────────────────────────────

ANNOTATIONS_DIR = Path("data/annotations")
PROGRESS_FILE = ANNOTATIONS_DIR / "progress.json"
CHROMA_DIR = "data/chroma"
COLLECTION_NAME = "taxai_legal_docs"

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Bạn là chuyên gia pháp lý thuế Việt Nam. Nhiệm vụ: phân tích đoạn văn bản pháp lý và gán metadata theo schema.

OUTPUT: JSON hợp lệ ONLY (không thêm text ngoài JSON).

Schema:
{
  "applies_to": {
    "who": [<"individual"|"HKD"|"employer"|"employee"|"enterprise"|"UNSPECIFIED">],
    "source": <"explicit"|"inferred">
  },
  "activity_group": [<canonical enum values>],
  "tax_domain": [<"PIT"|"HKD"|"VAT"|"TMDT"|"PENALTY"|"UNSPECIFIED">],
  "content_type": <"tax_rate"|"threshold"|"condition_rule"|"procedure"|"definition">,
  "legal": {
    "effective_from": <"YYYY-MM-DD" hoặc null>,
    "effective_to": <"YYYY-MM-DD" hoặc null>
  },
  "confidence": <0.0–1.0>
}

Canonical activity_group values:
HKD: goods_distribution, services_without_materials, manufacturing_transport, asset_rental, e_commerce_platform
PIT: salary_wages, real_estate_transfer, capital_investment, capital_transfer, lottery_prizes, royalties_franchising, inheritance_gifts
Fallback: other_activities, UNSPECIFIED

Quy tắc:
- Chỉ dùng canonical values — KHÔNG tự đặt tên mới
- Nếu không xác định được → dùng UNSPECIFIED, confidence thấp (0.3–0.5)
- source="explicit" nếu văn bản nêu rõ đối tượng, "inferred" nếu suy luận từ context
- confidence phản ánh mức độ chắc chắn của annotation (0.0=không chắc, 1.0=chắc chắn)"""

# ── Few-shot examples ─────────────────────────────────────────────────────────

FEW_SHOT_EXAMPLES = """=== Example 1 (HKD tax rate) ===
Text: "Hộ kinh doanh phân phối, cung cấp hàng hóa nộp thuế GTGT theo tỷ lệ 1% trên doanh thu."
→ {"applies_to": {"who": ["HKD"], "source": "explicit"}, "activity_group": ["goods_distribution"], "tax_domain": ["HKD"], "content_type": "tax_rate", "legal": {"effective_from": null, "effective_to": null}, "confidence": 0.95}

=== Example 2 (PIT lottery) ===
Text: "Thu nhập từ trúng thưởng xổ số, khuyến mại chịu thuế suất 10% trên phần thu nhập vượt 10 triệu đồng."
→ {"applies_to": {"who": ["individual"], "source": "explicit"}, "activity_group": ["lottery_prizes"], "tax_domain": ["PIT"], "content_type": "tax_rate", "legal": {"effective_from": null, "effective_to": null}, "confidence": 0.92}

=== Example 3 (ambiguous condition) ===
Text: "Trường hợp doanh thu không xác định được hoặc không phù hợp với thực tế kinh doanh, cơ quan thuế có quyền ấn định doanh thu."
→ {"applies_to": {"who": ["HKD", "individual"], "source": "inferred"}, "activity_group": ["UNSPECIFIED"], "tax_domain": ["HKD"], "content_type": "condition_rule", "legal": {"effective_from": null, "effective_to": null}, "confidence": 0.65}"""

# ── Fallback annotation ───────────────────────────────────────────────────────

FALLBACK_ANNOTATION = {
    "applies_to": {"who": ["UNSPECIFIED"], "source": "inferred"},
    "activity_group": ["UNSPECIFIED"],
    "tax_domain": ["UNSPECIFIED"],
    "content_type": "definition",
    "legal": {"effective_from": None, "effective_to": None},
    "confidence": 0.0,
}


# ── Validator ─────────────────────────────────────────────────────────────────


def validate_annotation(data: dict) -> tuple[bool, list[str]]:
    """Validate annotation dict against canonical schema.

    Returns:
        (is_valid, errors) — errors is empty list if valid.
    """
    errors: list[str] = []

    # Check required top-level fields
    required_fields = ["applies_to", "activity_group", "tax_domain", "content_type", "legal", "confidence"]
    for field in required_fields:
        if field not in data:
            errors.append(f"Missing required field: '{field}'")

    if errors:
        # Cannot proceed with further checks if required fields are missing
        return False, errors

    # Validate applies_to
    applies_to = data["applies_to"]
    if not isinstance(applies_to, dict):
        errors.append("'applies_to' must be a dict")
    else:
        who = applies_to.get("who", [])
        if not isinstance(who, list) or len(who) == 0:
            errors.append("'applies_to.who' must be a non-empty list")
        else:
            for w in who:
                if w not in CANONICAL_WHO:
                    errors.append(f"Invalid 'who' value: '{w}'. Must be one of {sorted(CANONICAL_WHO)}")

        source = applies_to.get("source")
        if source not in ("explicit", "inferred"):
            errors.append(f"Invalid 'source' value: '{source}'. Must be 'explicit' or 'inferred'")

    # Validate activity_group
    ag = data["activity_group"]
    if not isinstance(ag, list) or len(ag) == 0:
        errors.append("'activity_group' must be a non-empty list")
    else:
        for a in ag:
            if a not in CANONICAL_ACTIVITY_GROUP:
                errors.append(f"Invalid 'activity_group' value: '{a}'. Must be one of canonical values")

    # Validate tax_domain
    td = data["tax_domain"]
    if not isinstance(td, list) or len(td) == 0:
        errors.append("'tax_domain' must be a non-empty list")
    else:
        for t in td:
            if t not in CANONICAL_TAX_DOMAIN:
                errors.append(f"Invalid 'tax_domain' value: '{t}'. Must be one of {sorted(CANONICAL_TAX_DOMAIN)}")

    # Validate content_type
    ct = data["content_type"]
    if ct not in CANONICAL_CONTENT_TYPE:
        errors.append(f"Invalid 'content_type' value: '{ct}'. Must be one of {sorted(CANONICAL_CONTENT_TYPE)}")

    # Validate legal
    legal = data["legal"]
    if not isinstance(legal, dict):
        errors.append("'legal' must be a dict")
    else:
        for date_key in ("effective_from", "effective_to"):
            val = legal.get(date_key)
            if val is not None:
                # Must be YYYY-MM-DD format
                import re
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(val)):
                    errors.append(f"'legal.{date_key}' must be 'YYYY-MM-DD' or null, got: '{val}'")

    # Validate confidence range
    conf = data["confidence"]
    if not isinstance(conf, (int, float)):
        errors.append(f"'confidence' must be a number, got: {type(conf).__name__}")
    elif not (0.0 <= float(conf) <= 1.0):
        errors.append(f"'confidence' must be between 0.0 and 1.0, got: {conf}")

    return len(errors) == 0, errors


# ── ChromaDB metadata serialization ──────────────────────────────────────────


def serialize_annotation_for_chroma(annotation: dict) -> dict:
    """Convert annotation dict to ChromaDB-compatible flat metadata.

    ChromaDB only supports str/int/float/bool — serialize lists to
    comma-separated strings, prefix keys with 'nm_'.

    Returns dict with keys: nm_who, nm_activity_group, nm_tax_domain,
    nm_content_type, nm_confidence, nm_effective_from, nm_effective_to,
    nm_source, nm_annotated (bool flag).
    """
    applies_to = annotation.get("applies_to", {})
    legal = annotation.get("legal", {})

    who_list = applies_to.get("who", ["UNSPECIFIED"])
    ag_list = annotation.get("activity_group", ["UNSPECIFIED"])
    td_list = annotation.get("tax_domain", ["UNSPECIFIED"])

    return {
        "nm_who":            ",".join(who_list) if isinstance(who_list, list) else str(who_list),
        "nm_who_source":     applies_to.get("source", "inferred"),
        "nm_activity_group": ",".join(ag_list) if isinstance(ag_list, list) else str(ag_list),
        "nm_tax_domain":     ",".join(td_list) if isinstance(td_list, list) else str(td_list),
        "nm_content_type":   annotation.get("content_type", "definition"),
        "nm_confidence":     float(annotation.get("confidence", 0.0)),
        "nm_effective_from": legal.get("effective_from") or "",
        "nm_effective_to":   legal.get("effective_to") or "",
        "nm_annotated":      True,
    }


def deserialize_annotation_from_chroma(meta: dict) -> dict:
    """Reverse serialize_annotation_for_chroma — reconstruct annotation dict."""

    def split_field(val: str) -> list[str]:
        if not val:
            return ["UNSPECIFIED"]
        return [v.strip() for v in val.split(",") if v.strip()]

    return {
        "applies_to": {
            "who":    split_field(meta.get("nm_who", "")),
            "source": meta.get("nm_who_source", "inferred"),
        },
        "activity_group": split_field(meta.get("nm_activity_group", "")),
        "tax_domain":     split_field(meta.get("nm_tax_domain", "")),
        "content_type":   meta.get("nm_content_type", "definition"),
        "legal": {
            "effective_from": meta.get("nm_effective_from") or None,
            "effective_to":   meta.get("nm_effective_to") or None,
        },
        "confidence": float(meta.get("nm_confidence", 0.0)),
    }


# ── NodeAnnotator ─────────────────────────────────────────────────────────────


class NodeAnnotator:
    """Offline LLM-based annotator for ChromaDB nodes.

    Calls Gemini Flash to annotate each node with NodeMetadata schema.
    Includes retry logic, rate limiting, and progress persistence.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash",
        rate_limit_sleep: float = 1.0,
    ):
        self.model_name = model
        self.rate_limit_sleep = rate_limit_sleep
        self._client = None

        # Resolve API key
        self._api_key = api_key or os.getenv("GOOGLE_API_KEY")

        if self._api_key:
            self._init_client()
        else:
            logger.warning("No GOOGLE_API_KEY found — LLM calls will use fallback only")

    def _init_client(self):
        """Initialize Gemini client using google.genai SDK."""
        try:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
            logger.info(f"Gemini client initialized: {self.model_name}")
        except ImportError:
            logger.error("google-genai package not installed. Run: pip install google-genai")
            self._client = None
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")
            self._client = None

    def _call_llm(self, node_text: str, breadcrumb: str = "") -> Optional[dict]:
        """Call LLM and return parsed JSON annotation, or None on failure."""
        if not self._client:
            return None

        from google.genai import types as genai_types

        context_line = f"Breadcrumb: {breadcrumb}\n" if breadcrumb else ""
        user_message = (
            f"{FEW_SHOT_EXAMPLES}\n\n"
            f"=== Now annotate this node ===\n"
            f"{context_line}"
            f"Text: \"{node_text[:1000]}\"\n"
            f"→"
        )

        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=user_message,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                ),
            )
            raw_text = response.text.strip()

            # Strip markdown code block if present (defensive — shouldn't be needed with JSON mode)
            if raw_text.startswith("```"):
                lines = raw_text.split("\n")
                inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
                raw_text = inner.strip()

            return json.loads(raw_text)

        except json.JSONDecodeError as e:
            logger.debug(f"JSON parse error: {e}")
            return None
        except Exception as e:
            logger.debug(f"LLM call error: {e}")
            return None

    def annotate_node(
        self,
        node_text: str,
        node_id: str,
        doc_id: str,
        breadcrumb: str = "",
    ) -> dict:
        """Annotate a single node with up to 3 retry attempts.

        Returns annotation dict (may be fallback with confidence=0.0 if all attempts fail).
        """
        if not node_text or not node_text.strip():
            logger.debug(f"Empty text for node {node_id} — returning fallback")
            return dict(FALLBACK_ANNOTATION)

        for attempt in range(1, 4):
            result = self._call_llm(node_text, breadcrumb)

            if result is None:
                logger.debug(f"Attempt {attempt}/3 — LLM returned None for {node_id}")
                if attempt < 3:
                    time.sleep(self.rate_limit_sleep)
                continue

            is_valid, errors = validate_annotation(result)
            if is_valid:
                logger.debug(f"Node {node_id} annotated successfully (attempt {attempt})")
                return result
            else:
                logger.debug(
                    f"Attempt {attempt}/3 — validation failed for {node_id}: {errors[:2]}"
                )
                if attempt < 3:
                    time.sleep(self.rate_limit_sleep)

        # All attempts failed — return fallback
        logger.warning(f"All 3 attempts failed for node {node_id} — using fallback annotation")
        return dict(FALLBACK_ANNOTATION)

    def annotate_batch(
        self,
        nodes: list[dict],
        progress_callback=None,
    ) -> dict[str, dict]:
        """Annotate a list of nodes.

        Each node dict must have: node_id, text, doc_id, breadcrumb (optional).

        Args:
            nodes: list of dicts with keys node_id, text, doc_id, breadcrumb
            progress_callback: optional callable(current, total, node_id) for progress

        Returns:
            dict mapping node_id -> annotation dict
        """
        results: dict[str, dict] = {}
        total = len(nodes)

        for i, node in enumerate(nodes):
            node_id   = node.get("node_id", f"unknown_{i}")
            text      = node.get("text", "")
            doc_id    = node.get("doc_id", "")
            breadcrumb = node.get("breadcrumb", "")

            try:
                annotation = self.annotate_node(text, node_id, doc_id, breadcrumb)
                results[node_id] = annotation
            except Exception as e:
                logger.error(f"Unexpected error annotating node {node_id}: {e}")
                results[node_id] = dict(FALLBACK_ANNOTATION)

            if progress_callback:
                progress_callback(i + 1, total, node_id)

            # Rate limiting between calls (skip after last item)
            if i < total - 1 and self._client:
                time.sleep(self.rate_limit_sleep)

        return results


# ── ChromaDB update ───────────────────────────────────────────────────────────


def save_annotations_to_chroma(
    annotations: dict[str, dict],
    collection,
    batch_size: int = 100,
) -> dict:
    """Write annotations into ChromaDB collection metadata (nm_* prefix).

    ChromaDB collection.update() updates metadata for existing documents.
    Metadata is merged — existing keys are preserved unless overwritten.

    Args:
        annotations: dict mapping chunk_id -> annotation dict
        collection: ChromaDB collection object
        batch_size: number of updates per batch

    Returns:
        summary dict: {updated, failed, skipped}
    """
    chunk_ids = list(annotations.keys())
    updated = 0
    failed = 0
    skipped = 0

    for i in range(0, len(chunk_ids), batch_size):
        batch_ids = chunk_ids[i:i + batch_size]

        # Fetch existing metadata to merge
        try:
            existing = collection.get(ids=batch_ids, include=["metadatas"])
        except Exception as e:
            logger.error(f"Failed to fetch batch {i}-{i+batch_size} from ChromaDB: {e}")
            failed += len(batch_ids)
            continue

        existing_ids = existing.get("ids", [])
        existing_metas = existing.get("metadatas", [])

        # Build id->existing_meta map
        meta_map = {eid: emeta for eid, emeta in zip(existing_ids, existing_metas)}

        valid_ids = []
        merged_metas = []

        for cid in batch_ids:
            if cid not in meta_map:
                logger.warning(f"chunk_id '{cid}' not found in ChromaDB — skipping")
                skipped += 1
                continue

            annotation = annotations[cid]
            is_valid, errors = validate_annotation(annotation)
            if not is_valid:
                logger.warning(f"Invalid annotation for '{cid}': {errors} — skipping")
                skipped += 1
                continue

            # Serialize annotation to flat ChromaDB-compatible metadata
            nm_meta = serialize_annotation_for_chroma(annotation)

            # Merge with existing metadata
            merged = dict(meta_map[cid])
            merged.update(nm_meta)

            valid_ids.append(cid)
            merged_metas.append(merged)

        if not valid_ids:
            continue

        try:
            collection.update(ids=valid_ids, metadatas=merged_metas)
            updated += len(valid_ids)
            logger.info(f"Updated {len(valid_ids)} chunks (batch {i // batch_size + 1})")
        except Exception as e:
            logger.error(f"Failed to update batch in ChromaDB: {e}")
            failed += len(valid_ids)

    summary = {"updated": updated, "failed": failed, "skipped": skipped}
    logger.info(f"ChromaDB update complete: {summary}")
    return summary


# ── Progress persistence ──────────────────────────────────────────────────────


def load_progress(progress_file: Path = PROGRESS_FILE) -> dict[str, dict]:
    """Load previously saved annotations from progress file."""
    if not progress_file.exists():
        return {}
    try:
        with open(progress_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load progress file: {e}")
        return {}


def save_progress(
    annotations: dict[str, dict],
    progress_file: Path = PROGRESS_FILE,
):
    """Persist annotations to JSON file (append/overwrite)."""
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    # Merge with existing progress
    existing = load_progress(progress_file)
    existing.update(annotations)
    try:
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.debug(f"Progress saved: {len(existing)} total annotations")
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")


# ── CLI helpers ───────────────────────────────────────────────────────────────


def _fetch_nodes_from_chroma(
    collection,
    doc_id: Optional[str] = None,
    limit: Optional[int] = None,
    already_annotated_ids: Optional[set] = None,
) -> list[dict]:
    """Fetch node records from ChromaDB, optionally filtered by doc_id."""
    try:
        kwargs: dict = {"include": ["documents", "metadatas"]}
        if doc_id:
            kwargs["where"] = {"doc_id": doc_id}
        if limit:
            kwargs["limit"] = limit

        results = collection.get(**kwargs)
    except Exception as e:
        logger.error(f"Failed to fetch from ChromaDB: {e}")
        return []

    ids       = results.get("ids", [])
    documents = results.get("documents", [])
    metadatas = results.get("metadatas", [])

    nodes = []
    for cid, text, meta in zip(ids, documents, metadatas):
        # Skip already annotated nodes (resume support)
        if already_annotated_ids and cid in already_annotated_ids:
            continue
        nodes.append({
            "node_id":   cid,
            "text":      text or "",
            "doc_id":    meta.get("doc_id", ""),
            "breadcrumb": meta.get("breadcrumb", ""),
        })

    return nodes


def _run_dry_test():
    """Run self-tests without API/ChromaDB — validate logic only."""
    print("=" * 60)
    print("DRY-RUN TEST — node_annotator.py")
    print("=" * 60)

    # ── Test 1: validate_annotation with valid input ──────────────────────
    print("\n[Test 1] validate_annotation — valid input")
    valid_annotation = {
        "applies_to": {"who": ["HKD"], "source": "explicit"},
        "activity_group": ["goods_distribution"],
        "tax_domain": ["HKD"],
        "content_type": "tax_rate",
        "legal": {"effective_from": "2026-03-05", "effective_to": None},
        "confidence": 0.95,
    }
    ok, errors = validate_annotation(valid_annotation)
    print(f"  Result: valid={ok}, errors={errors}")
    assert ok, f"Expected valid, got errors: {errors}"

    # ── Test 2: validate_annotation with invalid input ────────────────────
    print("\n[Test 2] validate_annotation — invalid input (bad who, bad content_type)")
    invalid_annotation = {
        "applies_to": {"who": ["UNKNOWN_ENTITY"], "source": "explicit"},
        "activity_group": ["goods_distribution"],
        "tax_domain": ["HKD"],
        "content_type": "INVALID_TYPE",
        "legal": {"effective_from": None, "effective_to": None},
        "confidence": 1.5,  # out of range
    }
    ok, errors = validate_annotation(invalid_annotation)
    print(f"  Result: valid={ok}")
    print(f"  Errors: {errors}")
    assert not ok, "Expected invalid"
    assert len(errors) >= 3, f"Expected >= 3 errors, got {len(errors)}"

    # ── Test 3: validate_annotation — missing required field ─────────────
    print("\n[Test 3] validate_annotation — missing 'legal' field")
    missing_field = {
        "applies_to": {"who": ["individual"], "source": "inferred"},
        "activity_group": ["lottery_prizes"],
        "tax_domain": ["PIT"],
        "content_type": "tax_rate",
        # 'legal' is missing
        "confidence": 0.8,
    }
    ok, errors = validate_annotation(missing_field)
    print(f"  Result: valid={ok}, errors={errors}")
    assert not ok
    assert any("legal" in e for e in errors)

    # ── Test 4: fallback annotation is valid ──────────────────────────────
    print("\n[Test 4] validate_annotation — fallback annotation")
    ok, errors = validate_annotation(FALLBACK_ANNOTATION)
    print(f"  Result: valid={ok}, errors={errors}")
    assert ok, f"FALLBACK_ANNOTATION should be valid: {errors}"

    # ── Test 5: serialize/deserialize round-trip ──────────────────────────
    print("\n[Test 5] serialize/deserialize round-trip")
    original = {
        "applies_to": {"who": ["HKD", "individual"], "source": "inferred"},
        "activity_group": ["goods_distribution", "e_commerce_platform"],
        "tax_domain": ["HKD", "VAT"],
        "content_type": "condition_rule",
        "legal": {"effective_from": "2026-01-01", "effective_to": None},
        "confidence": 0.72,
    }
    serialized = serialize_annotation_for_chroma(original)
    print(f"  Serialized: {serialized}")

    # Validate serialized types (all must be str/int/float/bool)
    for key, val in serialized.items():
        assert isinstance(val, (str, int, float, bool)), (
            f"Key '{key}' has non-primitive type {type(val).__name__}: {val}"
        )
        print(f"    {key}: {type(val).__name__} = {val!r}")

    # Deserialize and check round-trip
    deserialized = deserialize_annotation_from_chroma(serialized)
    assert deserialized["applies_to"]["who"] == ["HKD", "individual"]
    assert deserialized["activity_group"] == ["goods_distribution", "e_commerce_platform"]
    assert deserialized["tax_domain"] == ["HKD", "VAT"]
    assert deserialized["legal"]["effective_from"] == "2026-01-01"
    assert deserialized["legal"]["effective_to"] is None
    assert abs(deserialized["confidence"] - 0.72) < 1e-6
    print("  Round-trip: OK")

    # ── Test 6: NodeAnnotator fallback (no API key) ───────────────────────
    print("\n[Test 6] NodeAnnotator fallback — no API key")
    annotator = NodeAnnotator(api_key="FAKE_KEY_THAT_WONT_INIT")
    # _client should be None (init will fail with fake key)
    # annotate_node should return fallback
    result = annotator.annotate_node(
        node_text="Hộ kinh doanh phân phối hàng hóa nộp thuế 1%.",
        node_id="test_node_001",
        doc_id="test_doc",
        breadcrumb="Test > Điều 1 > Khoản 1",
    )
    print(f"  Annotation result: {result}")
    ok, errors = validate_annotation(result)
    print(f"  Fallback is valid: {ok}, confidence={result.get('confidence')}")
    assert ok, f"Fallback should be valid: {errors}"
    assert result["confidence"] == 0.0

    # ── Test 7: annotate_batch with empty text ────────────────────────────
    print("\n[Test 7] annotate_batch — nodes with empty text")
    annotator2 = NodeAnnotator(api_key=None)  # no key → fallback
    nodes = [
        {"node_id": "n1", "text": "", "doc_id": "doc1", "breadcrumb": ""},
        {"node_id": "n2", "text": "  ", "doc_id": "doc1", "breadcrumb": ""},
    ]
    batch_results = annotator2.annotate_batch(nodes)
    print(f"  Batch results: {list(batch_results.keys())}")
    assert "n1" in batch_results
    assert "n2" in batch_results
    assert batch_results["n1"]["confidence"] == 0.0
    assert batch_results["n2"]["confidence"] == 0.0

    # ── Test 8: progress save/load ────────────────────────────────────────
    print("\n[Test 8] progress save/load")
    test_progress_file = ANNOTATIONS_DIR / "test_progress_dryrun.json"
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)

    test_data = {
        "chunk_001": valid_annotation,
        "chunk_002": FALLBACK_ANNOTATION,
    }
    save_progress(test_data, test_progress_file)
    loaded = load_progress(test_progress_file)
    assert "chunk_001" in loaded
    assert "chunk_002" in loaded
    assert loaded["chunk_001"]["confidence"] == 0.95
    print(f"  Saved and loaded {len(loaded)} annotations: OK")

    # Cleanup test file
    test_progress_file.unlink(missing_ok=True)

    print("\n" + "=" * 60)
    print("ALL DRY-RUN TESTS PASSED")
    print("=" * 60)


# ── Main CLI ──────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="P5.2 NodeMetadata Annotator — annotate ChromaDB nodes with LLM"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run self-tests only — no API calls, no ChromaDB needed",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of nodes to annotate",
    )
    parser.add_argument(
        "--doc-id",
        type=str,
        default=None,
        help="Annotate only nodes from this document ID",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Annotate all nodes in ChromaDB (respects --limit)",
    )
    parser.add_argument(
        "--write-chroma",
        action="store_true",
        help="Write annotations back to ChromaDB (default: save to JSON only)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip nodes already in progress.json",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Override GOOGLE_API_KEY environment variable",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gemini-2.5-flash",
        help="Gemini model to use (default: gemini-2.5-flash)",
    )

    args = parser.parse_args()

    # ── Dry-run mode ──────────────────────────────────────────────────────
    if args.dry_run:
        _run_dry_test()
        import sys
        sys.exit(0)

    # ── Real annotation mode ──────────────────────────────────────────────
    if not args.all and not args.doc_id:
        print("Error: specify --all, --doc-id DOC_ID, or --dry-run")
        parser.print_help()
        import sys
        sys.exit(1)

    # Connect to ChromaDB
    import chromadb
    from chromadb.config import Settings

    logger.info(f"Connecting to ChromaDB at: {CHROMA_DIR}")
    client = chromadb.PersistentClient(
        path=CHROMA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )
    try:
        collection = client.get_collection(name=COLLECTION_NAME)
        logger.info(f"Collection '{COLLECTION_NAME}': {collection.count()} chunks")
    except Exception as e:
        logger.error(f"Failed to get collection '{COLLECTION_NAME}': {e}")
        import sys
        sys.exit(1)

    # Load progress for resume support
    already_done: set[str] = set()
    if args.resume:
        existing_progress = load_progress()
        already_done = set(existing_progress.keys())
        logger.info(f"Resuming — {len(already_done)} nodes already annotated")

    # Fetch nodes from ChromaDB
    logger.info("Fetching nodes from ChromaDB...")
    nodes = _fetch_nodes_from_chroma(
        collection,
        doc_id=args.doc_id,
        limit=args.limit,
        already_annotated_ids=already_done if args.resume else None,
    )

    if not nodes:
        logger.warning("No nodes to annotate (empty result or all already done)")
        import sys
        sys.exit(0)

    logger.info(f"Nodes to annotate: {len(nodes)}")

    # Initialize annotator
    annotator = NodeAnnotator(
        api_key=args.api_key,
        model=args.model,
    )

    # Progress callback
    def progress_cb(current: int, total: int, node_id: str):
        if current % 10 == 0 or current == total:
            logger.info(f"Progress: {current}/{total} ({100*current//total}%) — last: {node_id}")

    # Annotate in batches, saving progress every 50 nodes
    all_annotations: dict[str, dict] = {}
    save_interval = 50

    for i in range(0, len(nodes), save_interval):
        batch = nodes[i:i + save_interval]
        logger.info(f"Annotating batch {i // save_interval + 1}: nodes {i+1}-{i+len(batch)}")

        batch_annotations = annotator.annotate_batch(batch, progress_callback=progress_cb)
        all_annotations.update(batch_annotations)

        # Save progress after each batch
        save_progress(batch_annotations)
        logger.info(f"Progress saved — {len(all_annotations)} annotations so far")

    logger.info(f"Annotation complete: {len(all_annotations)} nodes annotated")

    # Optionally write back to ChromaDB
    if args.write_chroma:
        logger.info("Writing annotations to ChromaDB...")
        summary = save_annotations_to_chroma(all_annotations, collection)
        logger.info(f"ChromaDB update: {summary}")
    else:
        logger.info(
            "Annotations saved to JSON only. Use --write-chroma to persist to ChromaDB."
        )

    # Print summary
    confidence_values = [a["confidence"] for a in all_annotations.values()]
    if confidence_values:
        avg_conf = sum(confidence_values) / len(confidence_values)
        fallback_count = sum(1 for c in confidence_values if c == 0.0)
        print(f"\nSummary:")
        print(f"  Total annotated:  {len(all_annotations)}")
        print(f"  Avg confidence:   {avg_conf:.3f}")
        print(f"  Fallback (conf=0): {fallback_count}")
        print(f"  Progress file:    {PROGRESS_FILE.resolve()}")
