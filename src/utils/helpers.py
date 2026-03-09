"""
TaxAI 2026 - Helper Utilities
Common functions for text processing, number parsing, date handling
"""

import re
from typing import Optional, List, Tuple
from datetime import datetime, date
from pathlib import Path
import json


# ==========================================
# TEXT NORMALIZATION
# ==========================================

def normalize_text(text: str) -> str:
    """
    Normalize Vietnamese text
    - Remove excessive whitespace
    - Normalize line breaks
    - Strip leading/trailing spaces
    """
    # Replace multiple spaces with single space
    text = re.sub(r'\s+', ' ', text)
    
    # Remove spaces before punctuation
    text = re.sub(r'\s+([.,;:!?])', r'\1', text)
    
    # Normalize quotes
    text = text.replace('"', '"').replace('"', '"')
    text = text.replace(''', "'").replace(''', "'")
    
    # Strip
    text = text.strip()
    
    return text


def clean_line_breaks(text: str) -> str:
    """Clean excessive line breaks"""
    # Replace 3+ line breaks with 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def remove_extra_spaces(text: str) -> str:
    """Remove extra spaces while preserving structure"""
    lines = text.split('\n')
    cleaned_lines = [re.sub(r'\s+', ' ', line).strip() for line in lines]
    return '\n'.join(cleaned_lines)


# ==========================================
# NUMBER PARSING (Vietnamese)
# ==========================================

def parse_vietnamese_number(text: str) -> Optional[float]:
    """
    Parse Vietnamese number expressions to float
    
    Examples:
        "5 triệu" → 5,000,000
        "10 tỷ" → 10,000,000,000
        "5.5 triệu" → 5,500,000
        "100 nghìn" → 100,000
        "5%" → 5.0 (not converted to decimal)
    
    Returns:
        float or None if cannot parse
    """
    if not text:
        return None
    
    text = text.lower().strip()
    
    # Remove commas and dots used as thousand separators (Vietnamese style)
    # But keep dots before numbers (decimal points)
    text = re.sub(r'\.(?=\d{3})', '', text)  # Remove thousand separator dots
    text = text.replace(',', '.')  # Convert comma to dot for decimals
    
    # Extract number
    number_match = re.search(r'([\d.]+)', text)
    if not number_match:
        return None
    
    try:
        base_number = float(number_match.group(1))
    except ValueError:
        return None
    
    # Check for unit multipliers
    if any(word in text for word in ['tỷ', 'ty', 'billion']):
        return base_number * 1_000_000_000
    elif any(word in text for word in ['triệu', 'trieu', 'million']):
        return base_number * 1_000_000
    elif any(word in text for word in ['nghìn', 'ngàn', 'nghин', 'ngan', 'thousand', 'k']):
        return base_number * 1_000
    elif '%' in text:
        return base_number  # Return as-is for percentages
    else:
        return base_number


def parse_percentage(text: str) -> Optional[float]:
    """
    Parse percentage to decimal
    
    Examples:
        "5%" → 0.05
        "10.5%" → 0.105
    
    Returns:
        float (as decimal) or None
    """
    match = re.search(r'([\d,.]+)\s*%', text)
    if match:
        try:
            number_str = match.group(1).replace(',', '.')
            return float(number_str) / 100
        except ValueError:
            return None
    return None


def parse_currency_vnd(text: str) -> Optional[float]:
    """
    Parse Vietnamese currency to number
    
    Examples:
        "5 triệu đồng" → 5,000,000
        "100.000 đ" → 100,000
    """
    # Remove "đồng", "đ", "VND"
    text = re.sub(r'(đồng|đ|VND)', '', text, flags=re.IGNORECASE)
    return parse_vietnamese_number(text)


def format_currency_vnd(amount: float, short: bool = False) -> str:
    """
    Format number as VND currency
    
    Args:
        amount: Number to format
        short: If True, use "triệu", "tỷ" notation
    
    Examples:
        format_currency_vnd(5000000) → "5,000,000 đồng"
        format_currency_vnd(5000000, short=True) → "5 triệu đồng"
    """
    if short:
        if amount >= 1_000_000_000:
            return f"{amount/1_000_000_000:.1f} tỷ đồng"
        elif amount >= 1_000_000:
            return f"{amount/1_000_000:.1f} triệu đồng"
        elif amount >= 1_000:
            return f"{amount/1_000:.1f} nghìn đồng"
        else:
            return f"{amount:.0f} đồng"
    else:
        return f"{amount:,.0f} đồng".replace(',', '.')


# ==========================================
# DATE PARSING (Vietnamese)
# ==========================================

def parse_date_vietnamese(text: str) -> Optional[date]:
    """
    Parse Vietnamese date format to date object
    
    Examples:
        "ngày 15 tháng 12 năm 2025" → date(2025, 12, 15)
        "15/12/2025" → date(2025, 12, 15)
        "15-12-2025" → date(2025, 12, 15)
    
    Returns:
        date object or None
    """
    # Pattern 1: ngày X tháng Y năm Z
    pattern1 = r'ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})'
    match = re.search(pattern1, text, re.IGNORECASE)
    if match:
        day, month, year = match.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            pass
    
    # Pattern 2: DD/MM/YYYY
    pattern2 = r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})'
    match = re.search(pattern2, text)
    if match:
        day, month, year = match.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            pass
    
    # Pattern 3: YYYY-MM-DD (ISO format)
    pattern3 = r'(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})'
    match = re.search(pattern3, text)
    if match:
        year, month, day = match.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            pass
    
    return None


def format_date_vietnamese(d: date) -> str:
    """
    Format date to Vietnamese
    
    Example:
        format_date_vietnamese(date(2025, 12, 15)) → "ngày 15 tháng 12 năm 2025"
    """
    return f"ngày {d.day} tháng {d.month} năm {d.year}"


# ==========================================
# LEGAL DOCUMENT HELPERS
# ==========================================

def extract_article_number(text: str) -> Optional[str]:
    """
    Extract article number from text
    
    Examples:
        "Điều 10" → "10"
        "Điều 5." → "5"
    """
    patterns = [
        r'Điều\s+(\d+)',
        r'Article\s+(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    
    return None


def extract_clause_number(text: str) -> Optional[str]:
    """
    Extract clause number
    
    Examples:
        "1. Nội dung..." → "1"
        "2." → "2"
    """
    match = re.match(r'^(\d+)[\.\s]', text.strip())
    if match:
        return match.group(1)
    return None


def extract_point_letter(text: str) -> Optional[str]:
    """
    Extract point letter
    
    Examples:
        "a) Nội dung..." → "a"
        "b)" → "b"
    """
    match = re.match(r'^([a-z])\)', text.strip())
    if match:
        return match.group(1)
    return None


def is_chapter_heading(text: str) -> bool:
    """
    Check if text is a chapter heading
    
    Examples:
        "Chương II" → True
        "CHƯƠNG I: NHỮNG QUY ĐỊNH CHUNG" → True
    """
    pattern = r'^Chương\s+([IVXLCDM]+|[0-9]+)'
    return bool(re.match(pattern, text.strip(), re.IGNORECASE))


def extract_chapter_number(text: str) -> Optional[str]:
    """
    Extract chapter number/roman
    
    Examples:
        "Chương II" → "II"
        "Chương 2" → "2"
    """
    pattern = r'Chương\s+([IVXLCDM]+|[0-9]+)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


# ==========================================
# FILE UTILITIES
# ==========================================

def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, create if not"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(filename: str) -> str:
    """
    Make filename safe (remove invalid characters)
    
    Example:
        "Nghị định 20/2026" → "Nghi_dinh_20_2026"
    """
    # Remove or replace invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Remove Vietnamese characters for filename safety
    replacements = {
        'á': 'a', 'à': 'a', 'ả': 'a', 'ã': 'a', 'ạ': 'a',
        'ă': 'a', 'ắ': 'a', 'ằ': 'a', 'ẳ': 'a', 'ẵ': 'a', 'ặ': 'a',
        'â': 'a', 'ấ': 'a', 'ầ': 'a', 'ẩ': 'a', 'ẫ': 'a', 'ậ': 'a',
        'é': 'e', 'è': 'e', 'ẻ': 'e', 'ẽ': 'e', 'ẹ': 'e',
        'ê': 'e', 'ế': 'e', 'ề': 'e', 'ể': 'e', 'ễ': 'e', 'ệ': 'e',
        'í': 'i', 'ì': 'i', 'ỉ': 'i', 'ĩ': 'i', 'ị': 'i',
        'ó': 'o', 'ò': 'o', 'ỏ': 'o', 'õ': 'o', 'ọ': 'o',
        'ô': 'o', 'ố': 'o', 'ồ': 'o', 'ổ': 'o', 'ỗ': 'o', 'ộ': 'o',
        'ơ': 'o', 'ớ': 'o', 'ờ': 'o', 'ở': 'o', 'ỡ': 'o', 'ợ': 'o',
        'ú': 'u', 'ù': 'u', 'ủ': 'u', 'ũ': 'u', 'ụ': 'u',
        'ư': 'u', 'ứ': 'u', 'ừ': 'u', 'ử': 'u', 'ữ': 'u', 'ự': 'u',
        'ý': 'y', 'ỳ': 'y', 'ỷ': 'y', 'ỹ': 'y', 'ỵ': 'y',
        'đ': 'd', 'Đ': 'D',
    }
    for vn, en in replacements.items():
        filename = filename.replace(vn, en)
    
    # Collapse multiple underscores
    filename = re.sub(r'_+', '_', filename)
    
    return filename.strip('_')


def save_json_pretty(data: dict, filepath: Path):
    """Save JSON with pretty formatting"""
    ensure_dir(filepath.parent)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json_safe(filepath: Path) -> Optional[dict]:
    """Load JSON safely, return None on error"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


# ==========================================
# VALIDATION HELPERS
# ==========================================

def is_valid_article_number(number: str) -> bool:
    """Check if article number is valid"""
    try:
        num = int(number)
        return 1 <= num <= 1000  # Reasonable range
    except ValueError:
        return False


def is_valid_clause_number(number: str) -> bool:
    """Check if clause number is valid"""
    try:
        num = int(number)
        return 1 <= num <= 100  # Reasonable range
    except ValueError:
        return False


def is_valid_point_letter(letter: str) -> bool:
    """Check if point letter is valid"""
    return len(letter) == 1 and 'a' <= letter <= 'z'


# ==========================================
# STRING SIMILARITY
# ==========================================

def simple_similarity(s1: str, s2: str) -> float:
    """
    Simple string similarity (0.0 to 1.0)
    Uses character overlap
    """
    if not s1 or not s2:
        return 0.0
    
    s1 = s1.lower().strip()
    s2 = s2.lower().strip()
    
    if s1 == s2:
        return 1.0
    
    # Character set overlap
    set1 = set(s1)
    set2 = set(s2)
    
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    
    return intersection / union if union > 0 else 0.0


# ==========================================
# EXPORTS
# ==========================================

__all__ = [
    # Text processing
    "normalize_text",
    "clean_line_breaks",
    "remove_extra_spaces",
    
    # Number parsing
    "parse_vietnamese_number",
    "parse_percentage",
    "parse_currency_vnd",
    "format_currency_vnd",
    
    # Date parsing
    "parse_date_vietnamese",
    "format_date_vietnamese",
    
    # Legal document helpers
    "extract_article_number",
    "extract_clause_number",
    "extract_point_letter",
    "is_chapter_heading",
    "extract_chapter_number",
    
    # File utilities
    "ensure_dir",
    "safe_filename",
    "save_json_pretty",
    "load_json_safe",
    
    # Validation
    "is_valid_article_number",
    "is_valid_clause_number",
    "is_valid_point_letter",
    
    # Similarity
    "simple_similarity",
]