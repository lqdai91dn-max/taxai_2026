"""
Legal Document Chunker - Hierarchical Chunking Strategy

Converts parsed JSON into chunks optimized for RAG retrieval.

Chunking levels:
1. Điều (Article) - Broad context
2. Khoản (Clause) - PRIMARY chunks  
3. Điểm (Point) - Granular details

Author: TaxAI Team
"""

from pathlib import Path
from typing import List, Dict, Any, Optional
import json
from dataclasses import dataclass, asdict, field


@dataclass
class Chunk:
    """Single chunk for embedding"""
    
    chunk_id: str
    chunk_type: str  # 'dieu', 'khoan', 'diem'
    content: str
    breadcrumb: str
    
    # Metadata
    document_id: str
    document_type: str
    document_number: str
    
    # Hierarchy info
    node_type: str
    node_index: str
    parent_dieu: Optional[str] = None
    parent_khoan: Optional[str] = None
    
    # Additional context
    title: Optional[str] = None
    lead_in_text: Optional[str] = None
    trailing_text: Optional[str] = None
    parent_context: Optional[str] = None  # For Điểm chunks
    
    # References
    references: List[Dict] = field(default_factory=list)
    
    # Metrics
    char_count: int = 0
    token_estimate: int = 0  # Rough estimate: chars / 4
    
    def __post_init__(self):
        self.char_count = len(self.content)
        self.token_estimate = self.char_count // 3  # ✅ tiếng Việt Unicode thường có tỷ lệ chars/tokens khoảng 3:1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON export"""
        return asdict(self)


class LegalDocumentChunker:
    """
    Chunker for parsed legal documents
    
    Usage:
        chunker = LegalDocumentChunker()
        chunks = chunker.chunk_document(parsed_json)
        chunker.save_chunks(chunks, "output/chunks/109_chunks.json")
    """
    
    def __init__(
        self,
        chunk_dieu: bool = True,
        chunk_khoan: bool = True,
        chunk_diem: bool = False,  # Disabled by default
        max_chunk_size: int = 2000  # characters
    ):
        """
        Initialize chunker
        
        Args:
            chunk_dieu: Create Điều-level chunks
            chunk_khoan: Create Khoản-level chunks (PRIMARY - recommended)
            chunk_diem: Create Điểm-level chunks (granular)
            max_chunk_size: Maximum chunk size in characters
        """
        self.chunk_dieu = chunk_dieu
        self.chunk_khoan = chunk_khoan
        self.chunk_diem = chunk_diem
        self.max_chunk_size = max_chunk_size
    
    def chunk_document(self, document: Dict[str, Any]) -> List[Chunk]:
        """
        Chunk a parsed document
        
        Args:
            document: Parsed JSON document
        
        Returns:
            List of Chunk objects
        """
        chunks = []
        
        # Extract metadata
        metadata = document.get('metadata', {})
        document_id = metadata.get('document_id', '')
        document_type = metadata.get('document_type', '')
        document_number = metadata.get('document_number', '')
        
        # Process all nodes
        nodes = document.get('data', [])
        
        for node in nodes:
            node_chunks = self._chunk_node(
                node=node,
                document_id=document_id,
                document_type=document_type,
                document_number=document_number
            )
            chunks.extend(node_chunks)
        
        return chunks
    
    def _chunk_node(
        self,
        node: Dict[str, Any],
        document_id: str,
        document_type: str,
        document_number: str,
        parent_dieu: Optional[str] = None,
        parent_khoan: Optional[str] = None,
        parent_context: Optional[str] = None
    ) -> List[Chunk]:
        """
        Recursively chunk a node and its children
        """
        chunks = []
        
        node_type = node.get('node_type', '')
        node_index = node.get('node_index', '')
        
        # Track hierarchy
        if node_type == 'Điều':
            parent_dieu = node_index
            parent_khoan = None
        elif node_type == 'Khoản':
            parent_khoan = node_index
        
        # Assemble content
        content_parts = []
        
        # Add title if exists
        if node.get('title'):
            content_parts.append(f"{node_type} {node_index}. {node['title']}")
        
        # Add main content (đầu câu)
        if node.get('content'):
            content_parts.append(node['content'])

        # Add lead_in_text (phần tiếp theo, thường dẫn vào danh sách con)
        if node.get('lead_in_text'):
            content_parts.append(node['lead_in_text'])
        
        # Add children content (for Điều chunks)
        children_text = ""
        if node.get('children'):
            for child in node['children']:
                child_content = self._get_node_full_text(child)
                if child_content:
                    children_text += child_content + "\n\n"
        
        # Add trailing_text if exists
        if node.get('trailing_text'):
            content_parts.append(node['trailing_text'])
        
        full_content = "\n\n".join(filter(None, content_parts))
        
        # Create chunks based on node type
        if node_type == 'Điều' and self.chunk_dieu:
            # Điều-level chunk (full article with all children)
            dieu_chunk = Chunk(
                chunk_id=node.get('node_id', ''),
                chunk_type='dieu',
                 content=full_content,  # ✅ chỉ nội dung Điều, không lẫn children
                breadcrumb=node.get('breadcrumb', ''),
                document_id=document_id,
                document_type=document_type,
                document_number=document_number,
                node_type=node_type,
                node_index=node_index,
                parent_dieu=parent_dieu,
                title=node.get('title'),
                references=node.get('references', [])
            )
            chunks.append(dieu_chunk)
        
        elif node_type == 'Khoản' and self.chunk_khoan:
            # Khoản-level chunk (PRIMARY chunks)
            khoan_content = full_content
            
            # Add children inline for Khoản
            if children_text:
                khoan_content += "\n\n" + children_text.strip()
            
            khoan_chunk = Chunk(
                chunk_id=node.get('node_id', ''),
                chunk_type='khoan',
                content=khoan_content,
                breadcrumb=node.get('breadcrumb', ''),
                document_id=document_id,
                document_type=document_type,
                document_number=document_number,
                node_type=node_type,
                node_index=node_index,
                parent_dieu=parent_dieu,
                parent_khoan=parent_khoan,
                lead_in_text=node.get('lead_in_text'),
                trailing_text=node.get('trailing_text'),
                references=node.get('references', [])
            )
            chunks.append(khoan_chunk)
        
        elif node_type == 'Điểm' and self.chunk_diem:
            # Điểm-level chunk (granular)
            diem_chunk = Chunk(
                chunk_id=node.get('node_id', ''),
                chunk_type='diem',
                content=full_content,
                breadcrumb=node.get('breadcrumb', ''),
                document_id=document_id,
                document_type=document_type,
                document_number=document_number,
                node_type=node_type,
                node_index=node_index,
                parent_dieu=parent_dieu,
                parent_khoan=parent_khoan,
                parent_context=parent_context,
                references=node.get('references', [])
            )
            chunks.append(diem_chunk)
        
        # Recurse into children
        if node.get('children'):
            child_context = parent_context
            if node_type == 'Khoản' and node.get('lead_in_text'):
                child_context = node['lead_in_text']
            
            for child in node['children']:
                child_chunks = self._chunk_node(
                    node=child,
                    document_id=document_id,
                    document_type=document_type,
                    document_number=document_number,
                    parent_dieu=parent_dieu,
                    parent_khoan=parent_khoan if node_type != 'Khoản' else node_index,
                    parent_context=child_context
                )
                chunks.extend(child_chunks)
        
        return chunks
    
    def _get_node_full_text(self, node: Dict[str, Any]) -> str:
        """Get full text of a node"""
        parts = []
        
        node_type = node.get('node_type', '')
        node_index = node.get('node_index', '')
        
        # Node header
        if node.get('title'):
            parts.append(f"{node_type} {node_index}. {node['title']}")
        elif node_type in ['Khoản', 'Điểm']:
            parts.append(f"{node_index}. " if node_type == 'Khoản' else f"{node_index}) ")
        
        # Content (đầu câu trước, lead_in_text sau)
        if node.get('content'):
            parts.append(node['content'])

        if node.get('lead_in_text'):
            parts.append(node['lead_in_text'])
        
        # Children
        if node.get('children'):
            for child in node['children']:
                child_text = self._get_node_full_text(child)
                if child_text:
                    parts.append(child_text)
        
        if node.get('trailing_text'):
            parts.append(node['trailing_text'])
        
        return "\n".join(filter(None, parts))
    
    def save_chunks(self, chunks: List[Chunk], output_path: Path):
        """Save chunks to JSON file"""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        chunks_dict = {
            'total_chunks': len(chunks),
            'chunk_types': {
                'dieu': len([c for c in chunks if c.chunk_type == 'dieu']),
                'khoan': len([c for c in chunks if c.chunk_type == 'khoan']),
                'diem': len([c for c in chunks if c.chunk_type == 'diem'])
            },
            'chunks': [chunk.to_dict() for chunk in chunks]
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(chunks_dict, f, ensure_ascii=False, indent=2)
        
        print(f"✅ Saved {len(chunks)} chunks to: {output_path}")
    
    def get_stats(self, chunks: List[Chunk]) -> Dict[str, Any]:
        """Get statistics about chunks"""
        if not chunks:
            return {}
        
        stats = {
            'total': len(chunks),
            'by_type': {
                'dieu': len([c for c in chunks if c.chunk_type == 'dieu']),
                'khoan': len([c for c in chunks if c.chunk_type == 'khoan']),
                'diem': len([c for c in chunks if c.chunk_type == 'diem'])
            },
            'size': {
                'min_chars': min([c.char_count for c in chunks]),
                'max_chars': max([c.char_count for c in chunks]),
                'avg_chars': sum([c.char_count for c in chunks]) / len(chunks),
                'avg_tokens': sum([c.token_estimate for c in chunks]) / len(chunks)
            },
            'with_references': len([c for c in chunks if c.references])
        }
        return stats


def chunk_all_documents(
    parsed_dir: Path,
    output_dir: Path,
    **chunker_kwargs
) -> Dict[str, Any]:
    """
    Chunk all parsed documents in a directory
    """
    parsed_dir = Path(parsed_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find all parsed JSON files
    json_files = list(parsed_dir.glob("*.json"))
    json_files = [f for f in json_files if not f.name.startswith("_")]
    
    if not json_files:
        print(f"❌ No JSON files found in {parsed_dir}")
        return {}
    
    print("=" * 70)
    print(f"📦 BATCH CHUNKING: {len(json_files)} documents")
    print("=" * 70)
    
    chunker = LegalDocumentChunker(**chunker_kwargs)
    
    results = {
        'total_documents': len(json_files),
        'total_chunks': 0,
        'documents': []
    }
    
    for json_path in json_files:
        print(f"\n📄 Chunking: {json_path.name}")
        
        try:
            # Load document
            with open(json_path, 'r', encoding='utf-8') as f:
                document = json.load(f)
            
            # Chunk
            chunks = chunker.chunk_document(document)
            
            # Save
            doc_id = json_path.stem
            output_path = output_dir / f"{doc_id}_chunks.json"
            chunker.save_chunks(chunks, output_path)
            
            # Stats
            stats = chunker.get_stats(chunks)
            
            results['total_chunks'] += len(chunks)
            results['documents'].append({
                'document_id': doc_id,
                'chunks': len(chunks),
                'stats': stats
            })
            
            print(f"   ✅ {len(chunks)} chunks")
            if stats:
                print(f"   📊 Types: {stats['by_type']}")
                print(f"   📏 Avg size: {stats['size']['avg_chars']:.0f} chars ({stats['size']['avg_tokens']:.0f} tokens)")
            
        except Exception as e:
            print(f"   ❌ Error: {e}")
            import traceback
            traceback.print_exc()
    
    # Save summary
    summary_path = output_dir / "_chunking_summary.json"
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print("\n" + "=" * 70)
    print("✅ BATCH CHUNKING COMPLETE")
    print("=" * 70)
    print(f"Total documents: {results['total_documents']}")
    print(f"Total chunks: {results['total_chunks']}")
    print(f"Summary saved: {summary_path}")
    
    return results


if __name__ == "__main__":
    # Test chunking
    chunk_all_documents(
        parsed_dir=Path("output"),
        output_dir=Path("output/chunks"),
        chunk_dieu=True,
        chunk_khoan=True,
        chunk_diem=False
    )