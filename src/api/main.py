"""
src/api/main.py — FastAPI backend cho TaxAI Legal Chatbot.

Endpoints:
  POST /chat          — trả lời câu hỏi pháp luật thuế
  GET  /health        — health check (ChromaDB + Neo4j + Gemini)
  GET  /docs_list     — danh sách văn bản đã index
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)

# ── Startup/shutdown ──────────────────────────────────────────────────────────

_generator = None
_agent     = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _generator, _agent
    logger.info("⏳ Loading TaxAI components...")
    from src.generation.answer_generator import AnswerGenerator
    from src.agent.planner import TaxAIAgent
    _generator = AnswerGenerator()
    _agent     = TaxAIAgent()
    logger.info("✅ TaxAI ready (pipeline + agent)")
    yield
    if _generator and hasattr(_generator, "_neo4j"):
        try:
            _generator._neo4j.close()
        except Exception:
            pass
    logger.info("👋 TaxAI shutdown")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="TaxAI Legal Chatbot",
    description="Trợ lý tư vấn pháp luật thuế Việt Nam — powered by Gemini 2.5",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question:      str              = Field(..., min_length=5, max_length=2000,
                                           description="Câu hỏi pháp luật thuế")
    filter_doc_id: Optional[str]   = Field(None, description="Giới hạn tìm trong 1 văn bản")
    show_sources:  bool             = Field(True, description="Trả về nguồn tham khảo")


class SourceItem(BaseModel):
    breadcrumb:      str
    document_number: str
    score:           float


class ChatResponse(BaseModel):
    answer:       str
    sources:      list[SourceItem]
    intent:       str
    model:        str
    latency_ms:   int


class AgentRequest(BaseModel):
    question:      str            = Field(..., min_length=5, max_length=2000,
                                          description="Câu hỏi pháp luật thuế")
    filter_doc_id: Optional[str] = Field(None, description="Giới hạn tìm trong 1 văn bản")
    show_sources:  bool           = Field(True, description="Trả về tool call log")


class ToolCallItem(BaseModel):
    tool:   str
    args:   dict
    result: Any


class AgentSourceItem(BaseModel):
    tool:       str
    doc_id:     str
    type:       str
    doc_number: Optional[str] = None
    reference:  Optional[str] = None
    breadcrumb: Optional[str] = None
    article_id: Optional[str] = None
    title:      Optional[str] = None
    status:     Optional[str] = None


class AgentResponse(BaseModel):
    answer:     str
    sources:    list[AgentSourceItem]
    tool_calls: list[ToolCallItem]
    model:      str
    iterations: int
    latency_ms: int


class HealthResponse(BaseModel):
    status:    str
    chroma_ok: bool
    neo4j_ok:  bool
    gemini_ok: bool
    chunks:    int


class DocItem(BaseModel):
    doc_id:          str
    document_number: str
    document_type:   str
    title:           str
    status:          str
    valid_from:      Optional[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Trả lời câu hỏi pháp luật thuế."""
    if _generator is None:
        raise HTTPException(503, "TaxAI chưa sẵn sàng — đang khởi động")

    t0 = time.perf_counter()
    try:
        from src.retrieval.query_classifier import classify
        cq = classify(req.question)

        result = _generator.answer(
            question      = req.question,
            filter_doc_id = req.filter_doc_id,
            show_sources  = req.show_sources,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    latency = int((time.perf_counter() - t0) * 1000)

    sources = [
        SourceItem(
            breadcrumb      = s.get("breadcrumb", ""),
            document_number = s.get("document_number", ""),
            score           = round(s.get("score", 0), 4),
        )
        for s in result.get("sources", [])
    ]

    return ChatResponse(
        answer     = result["answer"],
        sources    = sources,
        intent     = cq.intent.value,
        model      = result["model"],
        latency_ms = latency,
    )


@app.post("/agent", response_model=AgentResponse)
async def agent_chat(req: AgentRequest):
    """
    Trả lời câu hỏi thuế bằng agentic loop (Gemini function calling).

    LLM tự quyết định gọi tool nào, kết quả tính toán deterministic, có citation.
    """
    if _agent is None:
        raise HTTPException(503, "TaxAI agent chưa sẵn sàng — đang khởi động")

    t0 = time.perf_counter()
    try:
        result = _agent.answer(
            question      = req.question,
            filter_doc_id = req.filter_doc_id,
            show_sources  = req.show_sources,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    latency = int((time.perf_counter() - t0) * 1000)

    sources = [
        AgentSourceItem(
            tool       = s.get("tool", ""),
            doc_id     = s.get("doc_id", ""),
            type       = s.get("type", ""),
            doc_number = s.get("doc_number"),
            reference  = s.get("reference"),
            breadcrumb = s.get("breadcrumb"),
            article_id = s.get("article_id"),
            title      = s.get("title"),
            status     = s.get("status"),
        )
        for s in result.get("sources", [])
    ]

    tool_calls = [
        ToolCallItem(tool=tc["tool"], args=tc["args"], result=tc["result"])
        for tc in result.get("tool_calls", [])
    ]

    return AgentResponse(
        answer     = result["answer"],
        sources    = sources,
        tool_calls = tool_calls,
        model      = result["model"],
        iterations = result["iterations"],
        latency_ms = latency,
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    """Kiểm tra trạng thái hệ thống."""
    chroma_ok = False
    neo4j_ok  = False
    gemini_ok = False
    chunks    = 0

    # ChromaDB
    try:
        from src.retrieval.vector_store import VectorStore
        vs = VectorStore()
        chunks    = vs.count()
        chroma_ok = True
    except Exception:
        pass

    # Neo4j
    try:
        from src.graph.neo4j_client import Neo4jClient
        with Neo4jClient() as c:
            neo4j_ok = c.ping()
    except Exception:
        pass

    # Gemini — lightweight check (chỉ verify API key present)
    gemini_ok = bool(os.environ.get("GOOGLE_API_KEY"))

    overall = "ok" if (chroma_ok and neo4j_ok and gemini_ok) else "degraded"
    return HealthResponse(
        status    = overall,
        chroma_ok = chroma_ok,
        neo4j_ok  = neo4j_ok,
        gemini_ok = gemini_ok,
        chunks    = chunks,
    )


@app.get("/docs_list", response_model=list[DocItem])
async def docs_list():
    """Danh sách văn bản pháp luật đã index trong Neo4j."""
    try:
        from src.graph.neo4j_client import Neo4jClient
        with Neo4jClient() as c:
            rows = c.run("""
MATCH (d)
WHERE d:Document OR d:GuidanceDocument
RETURN d.doc_id AS doc_id, d.doc_number AS doc_number,
       d.doc_type AS doc_type, d.title AS title,
       d.status AS status, d.valid_from AS valid_from
ORDER BY d.hierarchy_rank, d.doc_id
""")
        return [
            DocItem(
                doc_id          = r["doc_id"] or "",
                document_number = r["doc_number"] or "",
                document_type   = r["doc_type"] or "",
                title           = r["title"] or "",
                status          = r["status"] or "",
                valid_from      = r["valid_from"],
            )
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, f"Neo4j error: {e}")


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=False)
