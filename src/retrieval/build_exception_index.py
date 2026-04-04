"""
src/retrieval/build_exception_index.py
C2 — Build exception index từ parsed JSON documents.

Logic:
  Scan all nodes for exception patterns containing article references.
  For each exception node E referencing rule R:
    index[R_chunk_id].append(E_chunk_id)

Output: data/exceptions/exception_index.json
  {
    "doc_109_2025_QH15_dieu_5_chunk": [
      "doc_109_2025_QH15_dieu_12_chunk"
    ],
    ...
  }

Usage:
  python -m src.retrieval.build_exception_index
  (run after parse_all_documents.py, before indexing)
"""

from __future__ import annotations

import json
import re
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

EXCEPTION_INDEX_PATH = Path("data/exceptions/exception_index.json")

# ── Exception patterns ────────────────────────────────────────────────────

# Patterns that signal "this node is an exception of rule [ref]"
_EXCEPTION_SIGNAL = re.compile(
    r"(?:"
    r"trừ\s+trường\s+hợp"
    r"|không\s+áp\s+dụng"
    r"|ngoại\s+trừ"
    r"|trường\s+hợp\s+(?:quy\s+định\s+tại|nêu\s+tại)"
    r"|không\s+tính\s+(?:vào\s+)?thu\s+nhập\s+chịu\s+thuế"
    r"|được\s+miễn\s+(?:thuế|nộp)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Article reference patterns (same as A2, reused)
_REF_PATTERNS = [
    # điểm a khoản 3 Điều 4
    re.compile(
        r"[Ðđ]iểm\s+([a-zđ])\s+khoản\s+(\d+)\s+[Ðđ]iều\s+(\d+)",
        re.IGNORECASE,
    ),
    # khoản X Điều Y
    re.compile(r"khoản\s+(\d+)\s+[Ðđ]iều\s+(\d+)", re.IGNORECASE),
    # Điều X khoản Y
    re.compile(r"[Ðđ]iều\s+(\d+)\s+khoản\s+(\d+)", re.IGNORECASE),
    # Điều X standalone
    re.compile(r"[Ðđ]iều\s+(\d+)(?!\s+khoản)(?!\s+này)", re.IGNORECASE),
]


# ── Node registry builder (reused from embedder logic) ───────────────────

def _build_registry(
    nodes: list,
    registry: dict,
    dieu_idx: str = "",
    khoan_idx: str = "",
):
    """Map (dieu, khoan, point) → chunk_id for a document."""
    for node in nodes:
        ntype = node.get("node_type", "")
        nidx  = str(node.get("node_index", ""))
        nid   = node.get("node_id", "")
        cid   = f"{nid}_chunk"

        if ntype == "Điều":
            dieu_idx  = nidx
            khoan_idx = ""
            registry[(nidx, "", "")] = cid
        elif ntype == "Khoản":
            khoan_idx = nidx
            registry[(dieu_idx, nidx, "")] = cid
        elif ntype == "Điểm":
            registry[(dieu_idx, khoan_idx, nidx)] = cid

        _build_registry(node.get("children", []), registry, dieu_idx, khoan_idx)


def _resolve(
    registry: dict,
    di: str,
    kh: str = "",
    pt: str = "",
) -> Optional[str]:
    """Resolve (dieu, khoan, point) → chunk_id, most-specific first."""
    for key in [(di, kh, pt), (di, kh, ""), (di, "", "")]:
        cid = registry.get(key)
        if cid:
            return cid
    return None


# ── Exception extraction per node ─────────────────────────────────────────

def _extract_exception_refs(
    text: str,
    registry: dict,
    self_dieu: str = "",
) -> List[str]:
    """Check if text contains exception signal + article reference.

    Returns list of rule chunk_ids this node is an exception of.
    Only triggers when text contains an exception signal keyword.
    """
    if not _EXCEPTION_SIGNAL.search(text):
        return []

    rule_ids: list[str] = []
    seen: set[str] = set()

    def add(cid: Optional[str]):
        if cid and cid not in seen:
            seen.add(cid)
            rule_ids.append(cid)

    # Pattern: điểm a khoản X Điều Y
    for m in re.finditer(
        r"[Ðđ]iểm\s+([a-zđ])\s+khoản\s+(\d+)\s+[Ðđ]iều\s+(\d+)",
        text, re.IGNORECASE,
    ):
        add(_resolve(registry, m.group(3), m.group(2), m.group(1).lower()))

    # Pattern: khoản X Điều Y
    for m in re.finditer(r"khoản\s+(\d+)\s+[Ðđ]iều\s+(\d+)", text, re.IGNORECASE):
        add(_resolve(registry, m.group(2), m.group(1)))

    # Pattern: Điều X khoản Y
    for m in re.finditer(r"[Ðđ]iều\s+(\d+)\s+khoản\s+(\d+)", text, re.IGNORECASE):
        add(_resolve(registry, m.group(1), m.group(2)))

    # Pattern: Điều X standalone
    for m in re.finditer(
        r"[Ðđ]iều\s+(\d+)(?!\s+khoản)(?!\s+này)", text, re.IGNORECASE
    ):
        di = m.group(1)
        if di != self_dieu:           # skip self-reference
            add(_resolve(registry, di))

    return rule_ids


# ── Document traversal ────────────────────────────────────────────────────

def _scan_nodes(
    nodes: list,
    registry: dict,
    index: Dict[str, List[str]],
    dieu_idx: str = "",
):
    """Recursively scan nodes, populating exception index."""
    for node in nodes:
        ntype  = node.get("node_type", "")
        nidx   = str(node.get("node_index", ""))
        nid    = node.get("node_id", "")
        my_cid = f"{nid}_chunk"

        # Track current Điều for self-reference guard
        if ntype == "Điều":
            dieu_idx = nidx

        # Collect text to scan
        parts = [
            node.get("content") or "",
            node.get("lead_in_text") or "",
            node.get("trailing_text") or "",
        ]
        text = " ".join(p for p in parts if p)

        # Find rule IDs this node is an exception of
        rule_ids = _extract_exception_refs(text, registry, self_dieu=dieu_idx)
        for rule_id in rule_ids:
            if rule_id != my_cid:                  # no self-loop
                index[rule_id].append(my_cid)

        _scan_nodes(node.get("children", []), registry, index, dieu_idx)


# ── Main build function ───────────────────────────────────────────────────

def build_exception_index(
    parsed_dir: str = "data/parsed",
    output_path: Path = EXCEPTION_INDEX_PATH,
) -> Dict[str, List[str]]:
    """Scan all parsed JSON docs → build exception index → save to JSON.

    Returns:
        index dict {rule_chunk_id: [exception_chunk_ids]}
    """
    parsed_path = Path(parsed_dir)
    json_files  = sorted(parsed_path.glob("*.json"))

    if not json_files:
        logger.error(f"❌ No JSON files in {parsed_dir}")
        return {}

    index: Dict[str, List[str]] = defaultdict(list)
    total_rules = 0

    for json_path in json_files:
        with open(json_path, encoding="utf-8") as f:
            doc = json.load(f)

        data_nodes = doc.get("data", [])
        if not data_nodes:
            continue          # skip Type B guidance docs

        # Build node registry for this document
        registry: dict = {}
        _build_registry(data_nodes, registry)

        # Scan for exception links
        before = len(index)
        _scan_nodes(data_nodes, registry, index)
        n_rules = len(index) - before

        if n_rules:
            n_exceptions = sum(
                len(v) for k, v in index.items()
                if any(json_path.stem in k for _ in [1])
            )
            logger.info(f"  {json_path.stem}: {n_rules} rules with exceptions")
        total_rules += n_rules

    # Deduplicate exception lists
    final_index: Dict[str, List[str]] = {
        k: list(dict.fromkeys(v))          # preserve order, remove dups
        for k, v in index.items()
        if v                               # skip empty lists
    }

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_index, f, ensure_ascii=False, indent=2)

    n_exceptions_total = sum(len(v) for v in final_index.values())
    logger.info(
        f"✅ Exception index built: {len(final_index)} rules "
        f"→ {n_exceptions_total} exception links "
        f"→ saved to {output_path}"
    )
    return final_index


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    index = build_exception_index()
    print(f"\n📊 Summary:")
    print(f"   Rules with exceptions: {len(index)}")
    print(f"   Total exception links: {sum(len(v) for v in index.values())}")
    print(f"\nTop rules (most exceptions):")
    for rule_id, exc_ids in sorted(index.items(), key=lambda x: -len(x[1]))[:10]:
        print(f"   {rule_id[-50:]:50} → {len(exc_ids)} exceptions")
