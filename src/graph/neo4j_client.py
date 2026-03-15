"""
Neo4j client wrapper — connection, health check, query helpers.
"""

from __future__ import annotations
import os
import logging
from contextlib import contextmanager
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_DEFAULT_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
_DEFAULT_USER     = os.getenv("NEO4J_USER",     "neo4j")
_DEFAULT_PASSWORD = os.getenv("NEO4J_PASSWORD", "taxai2026")


class Neo4jClient:
    def __init__(
        self,
        uri:      str = _DEFAULT_URI,
        user:     str = _DEFAULT_USER,
        password: str = _DEFAULT_PASSWORD,
    ):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info(f"Neo4j client created → {uri}")

    def close(self):
        self.driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Health ────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            self.driver.verify_connectivity()
            return True
        except ServiceUnavailable:
            return False

    # ── Query helpers ─────────────────────────────────────────────────────

    def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Read query — returns list of record dicts."""
        with self.driver.session() as s:
            result = s.run(cypher, params or {})
            return [dict(r) for r in result]

    def run_write(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Write query inside an explicit write transaction."""
        with self.driver.session() as s:
            return s.execute_write(
                lambda tx: [dict(r) for r in tx.run(cypher, params or {})]
            )

    def run_write_batch(self, cypher: str, rows: list[dict], batch_size: int = 500):
        """UNWIND batch write — efficient for large node/edge creation."""
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            self.run_write(cypher, {"rows": batch})

    # ── Convenience ───────────────────────────────────────────────────────

    def node_count(self, label: str | None = None) -> int:
        q = f"MATCH (n{':' + label if label else ''}) RETURN count(n) AS c"
        return self.run(q)[0]["c"]

    def wipe_database(self):
        """Delete ALL nodes and relationships — use only in dev/testing."""
        self.run_write("MATCH (n) DETACH DELETE n")
        logger.warning("Database wiped.")
