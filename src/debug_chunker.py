# thêm vào debug_chunker.py, chạy lại
import json
from pathlib import Path
from src.chunking.chunker import LegalDocumentChunker

# Load file JSON đã parse sẵn
json_path = Path("output/109_2025_QH15.json")  # file bạn đã có

with open(json_path, "r", encoding="utf-8") as f:
    document = json.load(f)

chunker = LegalDocumentChunker(chunk_dieu=True, chunk_khoan=True)
chunks = chunker.chunk_document(document)

# Lấy 1 Điều chunk để kiểm tra
dieu_chunks = [c for c in chunks if c.chunk_type == "dieu"]
if dieu_chunks:
    sample = dieu_chunks[0]
    print("=== DIEU CHUNK SAMPLE ===")
    print("chunk_id:", sample.chunk_id)
    print("content length:", sample.char_count)
    print("content preview:")
    print(sample.content[:300])
    print("...")