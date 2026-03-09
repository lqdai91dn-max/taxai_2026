# debug_117_text.py
import json

with open('data/parsed/117_2025_NDCP.json', encoding='utf-8') as f:
    doc = json.load(f)

# In metadata để xem pdf_metadata
print("=== METADATA ===")
print(json.dumps(doc.get('metadata', {}), ensure_ascii=False, indent=2))

# Xem Chương IV có đúng không
data = doc.get('data', [])
for chuong in data:
    print(f"\n[{chuong['node_type']} {chuong['node_index']}]")
    print(f"lead_in: {chuong.get('lead_in_text','')[:80]}")