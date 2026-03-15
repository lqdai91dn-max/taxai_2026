"""
ingest.py — Đọc parsed JSON + cross_doc_relationships.json → ingest vào Neo4j.

Runs in 3 passes:
  Pass 1: Document nodes + content node hierarchy (Chapter/Article/Clause/Point)
  Pass 2: REFERENCES edges (nội bộ, cross-node)
  Pass 3: Cross-document edges (AMENDS / SUPERSEDES / IMPLEMENTS)

Usage:
    python src/graph/ingest.py               # ingest all docs
    python src/graph/ingest.py 109_2025_QH15 # ingest 1 doc (+ cross-doc rels)
    python src/graph/ingest.py --wipe        # wipe DB first, then ingest all
"""

from __future__ import annotations
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.graph.neo4j_client import Neo4jClient
from src.graph.schema_setup import run as setup_schema

logger = logging.getLogger(__name__)

PROJECT_ROOT       = Path(__file__).parent.parent.parent
PARSED_DIR         = PROJECT_ROOT / "data" / "parsed"
CROSS_DOC_FILE     = PROJECT_ROOT / "data" / "graph" / "cross_doc_relationships.json"

# ── Node type → Neo4j label ───────────────────────────────────────────────

NODE_LABEL: dict[str, str] = {
    "Chương":  "Chapter",
    "Mục":     "Section",
    "Điều":    "Article",
    "Khoản":   "Clause",
    "khoản":   "Clause",
    "Điểm":    "Point",
    "điểm":    "Point",
    "Tiết":    "SubPoint",
    "Phụ lục": "Chapter",   # top-level appendix — treat as chapter
}

# ── Document hierarchy rank ───────────────────────────────────────────────

HIERARCHY_RANK: dict[str, int] = {
    "Hiến pháp":          1,
    "Luật":               2,
    "Nghị quyết":         2,
    "Pháp lệnh":          3,
    "Nghị định":          4,
    "Thông tư":           5,
    "Quyết định":         6,
    "Công văn":           7,
    "Sổ tay hướng dẫn":  7,
}

# ── Relationship type by parent → child label ─────────────────────────────

def _rel_type(parent_label: str, child_label: str) -> str:
    if parent_label in ("Document", "GuidanceDocument"):
        return "HAS_CHAPTER"
    if child_label == "Section":
        return "HAS_SECTION"
    if child_label == "Article":
        return "HAS_ARTICLE"
    if child_label == "Clause":
        return "HAS_CLAUSE"
    if child_label == "Point":
        return "HAS_POINT"
    if child_label == "SubPoint":
        return "HAS_SUBPOINT"
    return "HAS_CHILD"


# ─────────────────────────────────────────────────────────────────────────
# Pass 1 helpers — collect nodes + hierarchy edges
# ─────────────────────────────────────────────────────────────────────────

def _collect_nodes_and_edges(
    tree_nodes: list[dict],
    doc_id: str,
    parent_id: str,
    parent_label: str,
    content_nodes: list[dict],
    hierarchy_edges: list[dict],
    ref_edges: list[dict],
):
    """Recursively walk the parsed node tree."""
    for node in tree_nodes:
        raw_type = node.get("node_type", "")
        label    = NODE_LABEL.get(raw_type, "Article")  # fallback
        nid      = node.get("node_id", "")

        content_nodes.append({
            "id":          nid,
            "label":       label,
            "doc_id":      doc_id,
            "node_type":   raw_type,
            "node_index":  str(node.get("node_index", "") or ""),
            "title":       node.get("title") or "",
            "content":     node.get("content") or "",
            "lead_in_text": node.get("lead_in_text") or "",
            "breadcrumb":  node.get("breadcrumb") or "",
        })

        hierarchy_edges.append({
            "parent_id":    parent_id,
            "parent_label": parent_label,
            "child_id":     nid,
            "child_label":  label,
            "rel_type":     _rel_type(parent_label, label),
        })

        # Collect references for Pass 2
        for ref in node.get("references", []):
            ref_edges.append({
                "from_id":    nid,
                "target_id":  ref.get("target_id", ""),
                "text_match": ref.get("text_match", ""),
            })

        _collect_nodes_and_edges(
            node.get("children", []),
            doc_id, nid, label,
            content_nodes, hierarchy_edges, ref_edges,
        )


# ─────────────────────────────────────────────────────────────────────────
# Cypher — create content nodes by label
# ─────────────────────────────────────────────────────────────────────────

_CREATE_NODE_BY_LABEL = {
    label: f"""
UNWIND $rows AS r
MERGE (n:{label} {{id: r.id}})
SET n.doc_id      = r.doc_id,
    n.node_type   = r.node_type,
    n.node_index  = r.node_index,
    n.title       = r.title,
    n.content     = r.content,
    n.lead_in_text = r.lead_in_text,
    n.breadcrumb  = r.breadcrumb
"""
    for label in set(NODE_LABEL.values())
}

_CREATE_HIERARCHY_EDGE = """
UNWIND $rows AS r
CALL apoc.cypher.doIt(
  'MATCH (parent {id: $pid}) MATCH (child {id: $cid}) MERGE (parent)-[:' + r.rel_type + ']->(child)',
  {pid: r.parent_id, cid: r.child_id}
) YIELD value RETURN value
"""

# Simpler version without APOC — separate queries per rel_type
_CREATE_EDGE_TEMPLATES: dict[str, str] = {}
for _rt in ["HAS_CHAPTER", "HAS_SECTION", "HAS_ARTICLE", "HAS_CLAUSE",
            "HAS_POINT", "HAS_SUBPOINT", "HAS_CHILD"]:
    _CREATE_EDGE_TEMPLATES[_rt] = f"""
UNWIND $rows AS r
MATCH (parent {{id: r.parent_id}})
MATCH (child  {{id: r.child_id}})
MERGE (parent)-[:{_rt}]->(child)
"""

_CREATE_REFERENCES = """
UNWIND $rows AS r
MATCH (src {id: r.from_id})
MATCH (tgt {id: r.target_id})
MERGE (src)-[:REFERENCES {text_match: r.text_match}]->(tgt)
"""

_CREATE_TABLE = """
UNWIND $rows AS r
MERGE (t:Table {id: r.id})
SET t.doc_id      = r.doc_id,
    t.page_number = r.page_number,
    t.headers     = r.headers,
    t.row_count   = r.row_count,
    t.col_count   = r.col_count,
    t.description = r.description
WITH t, r
MATCH (doc {doc_id: r.doc_id})
MERGE (doc)-[:HAS_TABLE]->(t)
"""


# ─────────────────────────────────────────────────────────────────────────
# Ingest a single document
# ─────────────────────────────────────────────────────────────────────────

def ingest_document(client: Neo4jClient, data: dict):
    meta   = data.get("metadata", {})
    doc_id = meta.get("document_id", "")
    is_type_b = not data.get("data")

    doc_type = meta.get("document_type", "")

    # ── Document / GuidanceDocument node ──────────────────────────────────
    if is_type_b:
        client.run_write("""
MERGE (d:GuidanceDocument {doc_id: $doc_id})
SET d.id            = $doc_id,
    d.doc_number    = $doc_number,
    d.doc_type      = $doc_type,
    d.title         = $title,
    d.issue_date    = $issue_date,
    d.effective_date = $effective_date,
    d.status        = $status,
    d.hierarchy_rank = $rank,
    d.source_org    = $source_org
""", {
            "doc_id":       doc_id,
            "doc_number":   meta.get("document_number", ""),
            "doc_type":     doc_type,
            "title":        meta.get("title", ""),
            "issue_date":   meta.get("issue_date", ""),
            "effective_date": meta.get("effective_date", ""),
            "status":       meta.get("status", "active"),
            "rank":         meta.get("hierarchy_rank", HIERARCHY_RANK.get(doc_type, 7)),
            "source_org":   meta.get("source_org", ""),
        })
        logger.info(f"  [{doc_id}] GuidanceDocument node created")
    else:
        client.run_write("""
MERGE (d:Document {doc_id: $doc_id})
SET d.id             = $doc_id,
    d.doc_number     = $doc_number,
    d.doc_type       = $doc_type,
    d.title          = $title,
    d.issue_date     = $issue_date,
    d.effective_date = $effective_date,
    d.valid_from     = $valid_from,
    d.valid_to       = $valid_to,
    d.status         = $status,
    d.hierarchy_rank = $rank
""", {
            "doc_id":       doc_id,
            "doc_number":   meta.get("document_number", ""),
            "doc_type":     doc_type,
            "title":        meta.get("title", ""),
            "issue_date":   meta.get("issue_date", ""),
            "effective_date": meta.get("effective_date", ""),
            "valid_from":   meta.get("effective_date", ""),
            "valid_to":     meta.get("valid_to"),   # None unless superseded
            "status":       meta.get("status", "active"),
            "rank":         meta.get("hierarchy_rank", HIERARCHY_RANK.get(doc_type, 4)),
        })
        logger.info(f"  [{doc_id}] Document node created")

    # ── Content nodes + hierarchy edges (Type A only) ─────────────────────
    if not is_type_b:
        content_nodes:   list[dict] = []
        hierarchy_edges: list[dict] = []
        ref_edges:       list[dict] = []

        _collect_nodes_and_edges(
            data["data"], doc_id,
            parent_id=doc_id, parent_label="Document",
            content_nodes=content_nodes,
            hierarchy_edges=hierarchy_edges,
            ref_edges=ref_edges,
        )

        # Group nodes by label for batch MERGE
        by_label: dict[str, list] = {}
        for n in content_nodes:
            by_label.setdefault(n["label"], []).append(n)

        for label, rows in by_label.items():
            client.run_write_batch(_CREATE_NODE_BY_LABEL[label], rows)
        logger.info(f"  [{doc_id}] {len(content_nodes)} content nodes merged")

        # Hierarchy edges — group by rel_type
        by_rel: dict[str, list] = {}
        for e in hierarchy_edges:
            by_rel.setdefault(e["rel_type"], []).append(e)

        for rel_type, rows in by_rel.items():
            client.run_write_batch(_CREATE_EDGE_TEMPLATES[rel_type], rows)
        logger.info(f"  [{doc_id}] {len(hierarchy_edges)} hierarchy edges merged")

        # Tables for Type A
        _ingest_tables(client, doc_id, data)

        # Store ref_edges for Pass 2 (caller handles)
        return ref_edges

    # ── Type B — GuidanceChunks + tables ─────────────────────────────────
    _ingest_guidance_chunks(client, doc_id, data)
    _ingest_tables(client, doc_id, data)
    return []


def _ingest_guidance_chunks(client: Neo4jClient, doc_id: str, data: dict):
    """
    Tạo GuidanceChunk nodes từ tables của Type B docs.
    Mỗi table → 1 GuidanceChunk với content = headers + serialized rows.
    """
    tables = data.get("tables", [])
    if not tables:
        return

    chunk_rows = []
    for t in tables:
        idx = t.get("table_index", t.get("page_number", 0))
        chunk_id = f"{doc_id}_chunk_{idx}"

        headers = t.get("headers", [])
        rows    = t.get("rows", [])

        # Serialize table as readable text
        parts = []
        if headers:
            parts.append(" | ".join(str(h) for h in headers if h))
        for row in rows[:20]:  # cap at 20 rows per chunk
            if isinstance(row, list):
                parts.append(" | ".join(str(c) for c in row if c))
            elif isinstance(row, dict):
                parts.append(" | ".join(str(v) for v in row.values() if v))
        content = "\n".join(parts).strip()

        if not content:
            continue

        # Derive topic_tags from headers keywords
        header_text = " ".join(str(h) for h in headers).lower()
        tags = []
        if any(w in header_text for w in ["đăng ký", "thủ tục", "cách"]):
            tags.append("thủ_tục")
        if any(w in header_text for w in ["thuế", "khấu trừ", "nộp"]):
            tags.append("thuế")
        if any(w in header_text for w in ["hộ kinh doanh", "hkd", "kinh doanh"]):
            tags.append("hkd")
        if any(w in header_text for w in ["quyết toán", "hoàn thuế"]):
            tags.append("quyết_toán")

        chunk_rows.append({
            "id":          chunk_id,
            "doc_id":      doc_id,
            "content":     content[:2000],
            "source_type": "table",
            "topic_tags":  json.dumps(tags, ensure_ascii=False),
            "page_number": t.get("page_number", 0),
        })

    if not chunk_rows:
        return

    client.run_write_batch("""
UNWIND $rows AS r
MATCH (gd:GuidanceDocument {doc_id: r.doc_id})
MERGE (gc:GuidanceChunk {id: r.id})
SET gc.doc_id      = r.doc_id,
    gc.content     = r.content,
    gc.source_type = r.source_type,
    gc.topic_tags  = r.topic_tags,
    gc.page_number = r.page_number
MERGE (gd)-[:HAS_CHUNK]->(gc)
""", chunk_rows)
    logger.info(f"  [{doc_id}] {len(chunk_rows)} GuidanceChunk nodes merged")


def _ingest_tables(client: Neo4jClient, doc_id: str, data: dict):
    tables = data.get("tables", [])
    if not tables:
        return
    table_rows = []
    for i, t in enumerate(tables):
        tid = f"{doc_id}_table_{t.get('table_index', t.get('page_number', i))}"
        table_rows.append({
            "id":          tid,
            "doc_id":      doc_id,
            "page_number": t.get("page_number", 0),
            "headers":     json.dumps(t.get("headers", []), ensure_ascii=False),
            "row_count":   t.get("row_count", 0),
            "col_count":   t.get("col_count", 0),
            "description": t.get("description", ""),
        })
    client.run_write_batch(_CREATE_TABLE, table_rows)
    logger.info(f"  [{doc_id}] {len(table_rows)} tables merged")


# ─────────────────────────────────────────────────────────────────────────
# Pass 2 — REFERENCES edges
# ─────────────────────────────────────────────────────────────────────────

def ingest_references(client: Neo4jClient, all_ref_edges: list[dict]):
    """Create REFERENCES edges. Skips if target_id node doesn't exist."""
    valid = [r for r in all_ref_edges if r.get("target_id")]
    if not valid:
        return

    # REFERENCES only work between internal nodes (doc_ prefix)
    internal = [r for r in valid if r["target_id"].startswith("doc_")]
    external = [r for r in valid if r["target_id"].startswith("external_")]

    if internal:
        # Only create edge if both nodes exist (MATCH, not MERGE on non-existent)
        client.run_write_batch("""
UNWIND $rows AS r
MATCH (src {id: r.from_id})
MATCH (tgt {id: r.target_id})
MERGE (src)-[:REFERENCES {text_match: r.text_match}]->(tgt)
""", internal)
        logger.info(f"  {len(internal)} internal REFERENCES edges merged")

    if external:
        logger.info(f"  {len(external)} external references skipped (stub nodes not created yet)")


# ─────────────────────────────────────────────────────────────────────────
# Pass 3 — Cross-document edges
# ─────────────────────────────────────────────────────────────────────────

def ingest_cross_doc(client: Neo4jClient):
    if not CROSS_DOC_FILE.exists():
        logger.warning(f"cross_doc_relationships.json not found: {CROSS_DOC_FILE}")
        return

    data = json.loads(CROSS_DOC_FILE.read_text(encoding="utf-8"))
    count = 0

    for rel in data.get("relationships", []):
        from_doc = rel["from_doc"]
        to_doc   = rel.get("to_doc")
        rel_type = rel["rel_type"]
        eff_date = rel.get("effective_date", "")
        note     = rel.get("note", "")

        if to_doc:
            # Internal relationship — both docs exist in DB
            client.run_write(f"""
MATCH (a:Document {{doc_id: $from_doc}})
MATCH (b:Document {{doc_id: $to_doc}})
MERGE (a)-[r:{rel_type}]->(b)
SET r.effective_date = $eff_date,
    r.note           = $note
""", {"from_doc": from_doc, "to_doc": to_doc,
      "eff_date": eff_date, "note": note})
        else:
            # External stub — target is a stub node
            to_number = rel.get("to_number", "")
            stub_id   = f"stub_{to_number.replace('/', '_').replace(' ', '_')}"
            client.run_write(f"""
MERGE (stub:ExternalDocument {{stub_id: $stub_id}})
SET stub.doc_number = $to_number,
    stub.status     = "external_stub"
WITH stub
MATCH (a:Document {{doc_id: $from_doc}})
MERGE (a)-[r:{rel_type}]->(stub)
SET r.effective_date = $eff_date,
    r.note           = $note
""", {"stub_id": stub_id, "to_number": to_number,
      "from_doc": from_doc, "eff_date": eff_date, "note": note})

        count += 1
        logger.info(f"  [{from_doc}] -{rel_type}-> [{to_doc or rel.get('to_number')}]")

    logger.info(f"  {count} cross-document relationships merged")


# ─────────────────────────────────────────────────────────────────────────
# Pass 4 — EXPLAINED_BY edges (GuidanceChunk → Article)
# ─────────────────────────────────────────────────────────────────────────

GUIDANCE_LINKS_FILE = Path("data/graph/guidance_links.json")


def ingest_explained_by(client: Neo4jClient):
    """
    Tạo EXPLAINED_BY edges từ guidance_links.json.
    Format:
      [{"chunk_id": "...", "article_id": "...", "confidence": 0.9, "method": "manual"}]
    """
    if not GUIDANCE_LINKS_FILE.exists():
        logger.info("  guidance_links.json not found — skipping EXPLAINED_BY")
        return

    links = json.loads(GUIDANCE_LINKS_FILE.read_text(encoding="utf-8"))
    if not links:
        return

    client.run_write_batch("""
UNWIND $rows AS r
MATCH (art {id: r.article_id})
MATCH (gc:GuidanceChunk {id: r.chunk_id})
MERGE (art)-[e:EXPLAINED_BY]->(gc)
SET e.confidence = r.confidence,
    e.method     = r.method
""", links)
    logger.info(f"  {len(links)} EXPLAINED_BY edges merged")


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

def ingest_all(client: Neo4jClient, doc_filter: str | None = None):
    json_files = sorted(PARSED_DIR.glob("*.json"))
    if doc_filter:
        json_files = [f for f in json_files if f.stem == doc_filter]

    if not json_files:
        logger.error(f"No parsed JSON found for filter '{doc_filter}'")
        return

    all_ref_edges: list[dict] = []

    # Pass 1 — nodes + hierarchy
    for path in json_files:
        logger.info(f"Ingesting {path.name}...")
        data = json.loads(path.read_text(encoding="utf-8"))
        ref_edges = ingest_document(client, data)
        all_ref_edges.extend(ref_edges)

    # Pass 2 — internal references
    logger.info("Pass 2: REFERENCES edges...")
    ingest_references(client, all_ref_edges)

    # Pass 3 — cross-document
    logger.info("Pass 3: Cross-document edges...")
    ingest_cross_doc(client)

    # Pass 4 — EXPLAINED_BY edges
    logger.info("Pass 4: EXPLAINED_BY edges...")
    ingest_explained_by(client)

    # Summary
    for label in ["Document", "GuidanceDocument", "Article", "Clause", "Point", "GuidanceChunk"]:
        n = client.node_count(label)
        logger.info(f"  {label}: {n} nodes")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    args = sys.argv[1:]
    wipe    = "--wipe" in args
    filters = [a for a in args if not a.startswith("--")]
    doc_filter = filters[0] if filters else None

    with Neo4jClient() as client:
        if not client.ping():
            print("❌ Cannot connect to Neo4j. Start with: docker compose up -d")
            sys.exit(1)

        if wipe:
            print("⚠️  Wiping database...")
            client.wipe_database()

        setup_schema(client)
        ingest_all(client, doc_filter)

    print("✅ Ingest complete.")
