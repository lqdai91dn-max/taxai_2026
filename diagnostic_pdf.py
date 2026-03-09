"""
Diagnostic Script - Check PDF Structure
"""

import fitz
from pathlib import Path

pdf_path = Path("data/raw/109_2025_QH15.pdf")

if not pdf_path.exists():
    print(f"❌ PDF not found: {pdf_path}")
    exit()

doc = fitz.open(pdf_path)

print("=" * 70)
print(f"📄 PDF: {pdf_path.name}")
print("=" * 70)
print(f"Total Pages: {len(doc)}")
print()

# Check each page
for page_num in range(len(doc)):
    page = doc[page_num]
    text = page.get_text().strip()
    
    # Look for "Điều" markers
    dieu_count = text.count("Điều ")
    
    print(f"Page {page_num + 1}:")
    print(f"  Length: {len(text)} chars")
    print(f"  Điều markers: {dieu_count}")
    
    # Show Điều numbers on this page
    import re
    dieus = re.findall(r'Điều\s+(\d+)', text)
    if dieus:
        print(f"  Điều found: {', '.join(set(dieus))}")
    
    print()

doc.close()

print("=" * 70)
print("✅ Diagnostic complete")
print("=" * 70)