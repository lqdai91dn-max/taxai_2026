"""
Test SmartPDFHelper - BƯỚC 2 Validation
Tests extraction from both digital and scanned PDFs
"""

from pathlib import Path
from src.parsing.pdfplumber_helper import SmartPDFHelper

print("="*60)
print("SMART PDF HELPER - VALIDATION TEST")
print("="*60)

# Initialize
helper = SmartPDFHelper()
print("✅ SmartPDFHelper initialized\n")

# Test file
pdf_path = Path("data/raw/109_2025_QH15.pdf")

if not pdf_path.exists():
    print(f"❌ PDF not found: {pdf_path}")
    exit(1)

print(f"📄 Testing: {pdf_path.name}\n")

try:
    # Extract
    text, tables, pages, metadata = helper.extract_text_and_tables(
        pdf_path=pdf_path,
        extract_tables=True,
        clean_output=True
    )
    
    print("="*60)
    print("RESULTS")
    print("="*60)
    
    # Metadata
    print(f"\n📊 Metadata:")
    for key, value in metadata.items():
        print(f"   {key}: {value}")
    
    # Text
    print(f"\n📄 Text:")
    print(f"   Length: {len(text)} chars")
    print(f"   'Việt Nam': {text.count('Việt Nam')}")
    print(f"   'QUỐC HỘI': {text.count('QUỐC HỘI')}")
    print(f"   'Điều': {text.count('Điều')}")
    print(f"\n   Preview:\n   {text[:200]}")
    
    # Tables
    print(f"\n📊 Tables: {len(tables)}")
    for i, t in enumerate(tables, 1):
        print(f"   {i}. Page {t['page_number']}: "
              f"{t['row_count']}×{t['col_count']} "
              f"({t.get('extraction_strategy', 'N/A')})")
    
    # Validation
    print(f"\n{'='*60}")
    print("VALIDATION")
    print("="*60)
    
    passed = 0
    total = 4
    
    # Test 1
    if len(text) > 1000:
        print("✅ Text extracted")
        passed += 1
    else:
        print("❌ Text too short")
    
    # Test 2
    if text.count('Việt Nam') > 0:
        print("✅ Text quality good")
        passed += 1
    else:
        print("⚠️  Text quality check")
    
    # Test 3
    if text.count('Điều') > 10:
        print("✅ Structure detected")
        passed += 1
    else:
        print("⚠️  Low structure markers")
    
    # Test 4
    if metadata['pdf_type'] in ['digital', 'scanned_ocr', 'scanned_pure']:
        print(f"✅ PDF type detected: {metadata['pdf_type']}")
        passed += 1
    else:
        print("❌ PDF type detection failed")
    
    # Summary
    print(f"\n{'='*60}")
    if passed == total:
        print(f"🎉 ALL {total} TESTS PASSED!")
        print("✅ SmartPDFHelper working correctly")
        print("✅ Ready for BƯỚC 3")
    else:
        print(f"⚠️  {passed}/{total} tests passed")
    print("="*60)

except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()