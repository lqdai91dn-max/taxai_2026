"""
schema_setup.py — Tạo constraints và indexes trong Neo4j.

Chạy một lần trước khi ingest (idempotent — safe to re-run).

Usage:
    python src/graph/schema_setup.py
"""

from __future__ import annotations
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


# ── Constraints (enforce unique id on every node label) ───────────────────

CONSTRAINTS = [
    # Type A — legal document nodes
    ("Document",       "doc_id"),
    ("Chapter",        "id"),
    ("Section",        "id"),
    ("Article",        "id"),
    ("Clause",         "id"),
    ("Point",          "id"),
    ("SubPoint",       "id"),
    # Type B — guidance nodes
    ("GuidanceDocument", "doc_id"),
    ("GuidanceChunk",    "id"),
    # Tables
    ("Table",          "id"),
]

# ── Additional indexes ─────────────────────────────────────────────────────

INDEXES = [
    # Document filtering
    ("Document",  "doc_type"),
    ("Document",  "status"),
    ("Document",  "hierarchy_rank"),
    ("Document",  "valid_from"),
    # Content node look-up by doc_id (cross-document traversal)
    ("Article",   "doc_id"),
    ("Clause",    "doc_id"),
    ("Point",     "doc_id"),
    # Guidance
    ("GuidanceChunk", "doc_id"),
    ("GuidanceChunk", "source_type"),
]


def create_constraints(client: Neo4jClient):
    for label, prop in CONSTRAINTS:
        name = f"unique_{label.lower()}_{prop}"
        cypher = (
            f"CREATE CONSTRAINT {name} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        )
        client.run_write(cypher)
        logger.info(f"  constraint: {name}")


def create_indexes(client: Neo4jClient):
    for label, prop in INDEXES:
        name = f"idx_{label.lower()}_{prop}"
        cypher = (
            f"CREATE INDEX {name} IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.{prop})"
        )
        client.run_write(cypher)
        logger.info(f"  index: {name}")


def run(client: Neo4jClient | None = None):
    own = client is None
    if own:
        client = Neo4jClient()

    if not client.ping():
        raise RuntimeError("Cannot connect to Neo4j — is the container running?")

    logger.info("Creating constraints...")
    create_constraints(client)

    logger.info("Creating indexes...")
    create_indexes(client)

    logger.info("Schema setup complete.")

    if own:
        client.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )
    run()
    print("✅ Schema setup done.")
