"""
Test OCR fixes - Quick validation
"""

from src.parsing.ocr_helper import clean_ocr_text

print("=" * 60)
print("TESTING OCR FIXES")
print("=" * 60)

# Test 1: Việt năm → Việt Nam
test1_input = "Người nộp thuếtại Việt năm"
test1_output = clean_ocr_text(test1_input)
print(f"\nTest 1: Việt năm fix")
print(f"Input:  {test1_input}")
print(f"Output: {test1_output}")
print(f"✅ PASS" if "Việt Nam" in test1_output else "❌ FAIL")

# Test 2: Merged words
test2_input = "thuếquy định tại Điều 3 từkinh doanh"
test2_output = clean_ocr_text(test2_input)
print(f"\nTest 2: Merged words fix")
print(f"Input:  {test2_input}")
print(f"Output: {test2_output}")
print(f"✅ PASS" if "thuế quy" in test2_output and "từ kinh" in test2_output else "❌ FAIL")

# Test 3: Multiple patterns
test3_input = "vềngười nộp thuếthu nhập từtiền lương tại Việt năm"
test3_output = clean_ocr_text(test3_input)
print(f"\nTest 3: Multiple patterns")
print(f"Input:  {test3_input}")
print(f"Output: {test3_output}")
expected_fixes = ["về người", "thuế thu", "từ tiền", "Việt Nam"]
all_fixed = all(fix in test3_output for fix in expected_fixes)
print(f"✅ PASS" if all_fixed else "❌ FAIL")

# Test 4: sở fixes
test4_input = "cơ sởkinh doanh có sởhữu tài sản"
test4_output = clean_ocr_text(test4_input)
print(f"\nTest 4: 'sở' fixes")
print(f"Input:  {test4_input}")
print(f"Output: {test4_output}")
print(f"✅ PASS" if "cơ sở kinh" in test4_output and "sở hữu" in test4_output else "❌ FAIL")

# Test 5: trở lên
test5_input = "từ 183 ngày trởlên"
test5_output = clean_ocr_text(test5_input)
print(f"\nTest 5: 'trở lên' fix")
print(f"Input:  {test5_input}")
print(f"Output: {test5_output}")
print(f"✅ PASS" if "trở lên" in test5_output else "❌ FAIL")

print("\n" + "=" * 60)
print("TEST COMPLETE")
print("=" * 60)