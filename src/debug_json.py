# debug_json.py
import json

# ← đổi sang 117
with open('data/parsed/117_2025_NDCP.json', encoding='utf-8') as f:
    doc = json.load(f)

data = doc.get('data', [])
print(f'Root nodes: {len(data)}')

for chuong in data:
    children = chuong.get('children', [])
    print(f"\n[{chuong['node_type']} {chuong['node_index']}] - {len(children)} Điều")
    for dieu in children:
        print(f"  Điều {dieu['node_index']}: {dieu.get('title','')[:45]}")