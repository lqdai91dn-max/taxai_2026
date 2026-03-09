"""Test logger and helpers"""

from src.utils.logger import logger, log_parsing_progress
from src.utils.helpers import *

def test_logger():
    """Test logger"""
    print("\n" + "="*60)
    print("TESTING LOGGER")
    print("="*60)
    
    logger.debug("This is a debug message")
    logger.info("This is an info message")
    logger.warning("This is a warning")
    logger.error("This is an error")
    
    log_parsing_progress(5, 10, "Test Document")
    
    print("✅ Logger test complete")


def test_helpers():
    """Test helper functions"""
    print("\n" + "="*60)
    print("TESTING HELPERS")
    print("="*60)
    
    # Number parsing
    tests = [
        ("5 triệu", 5_000_000),
        ("10 tỷ", 10_000_000_000),
        ("100 nghìn", 100_000),
        ("5%", 5.0),
    ]
    
    print("\n1. Number Parsing:")
    for text, expected in tests:
        result = parse_vietnamese_number(text)
        status = "✅" if result == expected else "❌"
        print(f"  {status} '{text}' → {result:,} (expected: {expected:,})")
    
    # Date parsing
    print("\n2. Date Parsing:")
    date_tests = [
        "ngày 15 tháng 12 năm 2025",
        "15/12/2025",
        "2025-12-15",
    ]
    for text in date_tests:
        result = parse_date_vietnamese(text)
        print(f"  ✅ '{text}' → {result}")
    
    # Legal extraction
    print("\n3. Legal Structure Extraction:")
    print(f"  Article: {extract_article_number('Điều 10. Nội dung')}")
    print(f"  Clause: {extract_clause_number('1. Nội dung khoản')}")
    print(f"  Point: {extract_point_letter('a) Nội dung điểm')}")
    print(f"  Chapter: {extract_chapter_number('Chương II')}")
    
    print("\n✅ Helpers test complete")


if __name__ == "__main__":
    test_logger()
    test_helpers()