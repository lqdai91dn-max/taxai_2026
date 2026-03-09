import json
with open('data/parsed/109_2025_QH15.json', encoding='utf-8') as f:
    doc = json.load(f)

data = doc.get('data', [])
print('Root nodes:', len(data))
print()
for node in data:
    print(f\"  [{node['node_type']}] {node['node_index']}: {node.get('title','')[:50]}\")
    print(f\"  Children: {len(node.get('children', []))}\")
    print()