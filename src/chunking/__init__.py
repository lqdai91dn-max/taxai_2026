"""
Chunking Package - Legal Document Chunking for RAG

Exports:
- Chunk: Dataclass for chunks
- LegalDocumentChunker: Main chunker class
- chunk_all_documents: Batch chunking function
"""

from .chunker import (
    Chunk,
    LegalDocumentChunker,
    chunk_all_documents
)

__all__ = [
    'Chunk',
    'LegalDocumentChunker',
    'chunk_all_documents'
]