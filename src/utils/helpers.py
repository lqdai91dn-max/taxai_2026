import re
from typing import Optional, Dict, List
from datetime import datetime

def normalize_text(text: str) -> str:
    """Normalize Vietnamese text"""
    # Remove multiple spaces
    text = re.sub(r'\s+', ' ', text)
    # Remove leading/trailing spaces
    text = text.strip()
    return text

def parse_vietnamese_number(text: str) -> Optional[float]:
    """
    Parse Vietnamese number expressions
    
    Examples:
    - "5 triệu" → 5,000,000
    - "10 tỷ" → 10,000,000,000
    - "5.5 triệu" → 5,500,000
    """
    text = text.lower().strip()
    
    # Remove commas and dots used as thousands separators
    text = re.sub(r'[,\.](?=\d{3})', '', text)
    
    # Extract number
    number_match = re.search(r'([\d,.]+)', text)
    if not number_match:
        return None
    
    number_str = number_match.group(1).replace(',', '.')
    try:
        base_number = float(number_str)
    except ValueError:
        return None
    
    # Check for unit
    if 'tỷ' in text or 'billion' in text:
        return base_number * 1_000_000_000
    elif 'triệu' in text or 'million' in text:
        return base_number * 1_000_000
    elif 'nghìn' in text or 'ngàn' in text or 'thousand' in text:
        return base_number * 1_000
    else:
        return base_number

def parse_percentage(text: str) -> Optional[float]:
    """
    Parse percentage
    
    Examples:
    - "5%" → 0.05
    - "10.5%" → 0.105
    """
    match = re.search(r'([\d,.]+)\s*%', text)
    if match:
        try:
            return float(match.group(1).replace(',', '.')) / 100
        except ValueError:
            return None
    return None

def parse_date_vietnamese(text: str) -> Optional[str]:
    """
    Parse Vietnamese date format
    
    Examples:
    - "ngày 15 tháng 12 năm 2025" → "2025-12-15"
    - "15/12/2025" → "2025-12-15"
    """
    # Pattern 1: ngày X tháng Y năm Z
    pattern1 = r'ngày\s+(\d+)\s+tháng\s+(\d+)\s+năm\s+(\d+)'
    match = re.search(pattern1, text, re.IGNORECASE)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    
    # Pattern 2: DD/MM/YYYY
    pattern2 = r'(\d{1,2})/(\d{1,2})/(\d{4})'
    match = re.search(pattern2, text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    
    return None

def extract_article_number(text: str) -> Optional[str]:
    """Extract article number from text"""
    patterns = [
        r'Điều\s+(\d+)',
        r'Article\s+(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    
    return None

def format_currency_vnd(amount: float) -> str:
    """Format number as VND currency"""
    if amount >= 1_000_000_000:
        return f"{amount/1_000_000_000:.1f} tỷ đồng"
    elif amount >= 1_000_000:
        return f"{amount/1_000_000:.1f} triệu đồng"
    elif amount >= 1_000:
        return f"{amount/1_000:.1f} nghìn đồng"
    else:
        return f"{amount:.0f} đồng"