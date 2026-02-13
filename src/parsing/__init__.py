from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime
import json

@dataclass
class Point:
    """Điểm (a, b, c...)"""
    letter: str
    content: str
    subpoints: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return asdict(self)

@dataclass
class Clause:
    """Khoản (1, 2, 3...)"""
    number: str
    content: str
    points: List[Point] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "number": self.number,
            "content": self.content,
            "points": [p.to_dict() for p in self.points]
        }

@dataclass
class Article:
    """Điều luật"""
    number: str
    title: str
    content: str
    clauses: List[Clause] = field(default_factory=list)
    chapter: Optional[str] = None
    chapter_title: Optional[str] = None
    
    # Metadata
    effective_date: Optional[str] = None  # Ngày hiệu lực riêng (nếu có)
    supersedes: List[str] = field(default_factory=list)  # Thay thế điều nào
    amended_by: List[str] = field(default_factory=list)  # Bị sửa bởi điều nào
    
    def to_dict(self) -> Dict:
        return {
            "number": self.number,
            "title": self.title,
            "content": self.content,
            "clauses": [c.to_dict() for c in self.clauses],
            "chapter": self.chapter,
            "chapter_title": self.chapter_title,
            "effective_date": self.effective_date,
            "supersedes": self.supersedes,
            "amended_by": self.amended_by,
        }

@dataclass
class Appendix:
    """Phụ lục"""
    number: str
    title: str
    content: str
    type: str = "text"  # text, table, image
    structured_data: Optional[Dict] = None  # For tables
    
    def to_dict(self) -> Dict:
        return asdict(self)

@dataclass
class LegalDocument:
    """Văn bản pháp luật hoàn chỉnh"""
    # Identity
    doc_id: str
    doc_type: str  # law, resolution, decree, circular
    number: str
    title: str
    
    # Issuer
    issued_by: str
    issued_date: str
    
    # Effectiveness
    effective_date: str
    end_date: Optional[str] = None  # Ngày hết hiệu lực
    
    # Relations
    supersedes: List[str] = field(default_factory=list)  # Thay thế văn bản nào
    amended_by: List[str] = field(default_factory=list)  # Bị sửa bởi văn bản nào
    implements: List[str] = field(default_factory=list)  # Hướng dẫn văn bản nào
    amends: List[str] = field(default_factory=list)  # Sửa đổi văn bản nào
    
    # Content
    preamble: str = ""  # Phần mở đầu
    articles: List[Article] = field(default_factory=list)
    appendices: List[Appendix] = field(default_factory=list)
    
    # Source
    source_file: str = ""
    
    # Metadata
    category: str = ""  # TNCN, GTGT, Policy, etc.
    priority: int = 3  # 1=highest, 4=lowest
    tags: List[str] = field(default_factory=list)
    
    # Processing
    parsed_date: str = field(default_factory=lambda: datetime.now().isoformat())
    version: str = "1.0"
    
    def to_dict(self) -> Dict:
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "number": self.number,
            "title": self.title,
            "issued_by": self.issued_by,
            "issued_date": self.issued_date,
            "effective_date": self.effective_date,
            "end_date": self.end_date,
            "supersedes": self.supersedes,
            "amended_by": self.amended_by,
            "implements": self.implements,
            "amends": self.amends,
            "preamble": self.preamble,
            "articles": [a.to_dict() for a in self.articles],
            "appendices": [a.to_dict() for a in self.appendices],
            "source_file": self.source_file,
            "category": self.category,
            "priority": self.priority,
            "tags": self.tags,
            "parsed_date": self.parsed_date,
            "version": self.version,
        }
    
    def save_json(self, filepath: str):
        """Save to JSON file"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
    
    @classmethod
    def load_json(cls, filepath: str) -> 'LegalDocument':
        """Load from JSON file"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Reconstruct objects
        articles = [
            Article(
                number=a["number"],
                title=a["title"],
                content=a["content"],
                clauses=[
                    Clause(
                        number=c["number"],
                        content=c["content"],
                        points=[
                            Point(
                                letter=p["letter"],
                                content=p["content"],
                                subpoints=p.get("subpoints", [])
                            ) for p in c.get("points", [])
                        ]
                    ) for c in a.get("clauses", [])
                ],
                chapter=a.get("chapter"),
                chapter_title=a.get("chapter_title"),
                effective_date=a.get("effective_date"),
                supersedes=a.get("supersedes", []),
                amended_by=a.get("amended_by", []),
            ) for a in data.get("articles", [])
        ]
        
        appendices = [
            Appendix(**app) for app in data.get("appendices", [])
        ]
        
        return cls(
            doc_id=data["doc_id"],
            doc_type=data["doc_type"],
            number=data["number"],
            title=data["title"],
            issued_by=data["issued_by"],
            issued_date=data["issued_date"],
            effective_date=data["effective_date"],
            end_date=data.get("end_date"),
            supersedes=data.get("supersedes", []),
            amended_by=data.get("amended_by", []),
            implements=data.get("implements", []),
            amends=data.get("amends", []),
            preamble=data.get("preamble", ""),
            articles=articles,
            appendices=appendices,
            source_file=data.get("source_file", ""),
            category=data.get("category", ""),
            priority=data.get("priority", 3),
            tags=data.get("tags", []),
            parsed_date=data.get("parsed_date"),
            version=data.get("version", "1.0"),
        )