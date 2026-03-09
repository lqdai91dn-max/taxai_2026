"""Test pdfplumber installation"""
import pdfplumber
from pathlib import Path

pdf_path = Path("data/raw/109_2025_QH15.pdf")

print("Testing pdfplumber...")

with pdfplumber.open(pdf_path) as pdf:
    first_page = pdf.pages[0]
    text = first_page.extract_text()
    
    print(f"✅ Extracted {len(text)} chars from page 1")
    print(f"📊 Total pages: {len(pdf.pages)}")
    print(f"\nPreview:\n{text[:200]}")
    
    # Critical test
    if "Việt Nam" in text and "Việt năm" not in text:
        print("\n✅ TEXT QUALITY EXCELLENT - No spacing errors!")
    elif "Việt năm" in text:
        print("\n⚠️  Still has spacing errors")
    else:
        print("\n⚠️  No 'Việt Nam' found - check extraction")