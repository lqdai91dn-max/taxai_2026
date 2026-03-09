"""
State Machine Package - Legal Document Parser

Components:
- NodeBuilder: Data structures (LegalNode, LegalDocument, etc.)
- IndentationChecker: Text classification (lead_in/trailing/continuation)
- ReferenceDetector: Reference detection (internal/external/self)
- ParserCore: State machine parser (main engine)
"""

from .node_builder import (
    NodeBuilder,
    LegalNode,
    LegalDocument,
    LegalReference
)

from .indentation_checker import (
    IndentationChecker,
    IndentLevel
)

from .reference_detector import (
    ReferenceDetector,
    ReferenceMatch
)

from .parser_core import (
    StateMachineParser,
    ParserState,
    parse_legal_document
)

__all__ = [
    # Node Builder
    "NodeBuilder",
    "LegalNode",
    "LegalDocument",
    "LegalReference",
    
    # Indentation Checker
    "IndentationChecker",
    "IndentLevel",
    
    # Reference Detector
    "ReferenceDetector",
    "ReferenceMatch",
    
    # Parser Core
    "StateMachineParser",
    "ParserState",
    "parse_legal_document"
]