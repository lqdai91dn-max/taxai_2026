# debug_ids.py
import json

with open('data/parsed/117_2025_NDCP.json', encoding='utf-8') as f:
    doc = json.load(f)

for chuong in doc['data']:
    print(f"node_id: {chuong['node_id']}")
    print(f"node_index: {chuong['node_index']}")
    print()