
from src.parsing.pdfplumber_helper import detect_pdf_type
from pathlib import Path

files = [
    '110_2025_UBTVQH15.pdf',
    '149_2025_QH15.pdf',
    '198_2025_QH15.pdf',
    '20_2026_NDCP.pdf',
    '373_2025_NDCP.pdf',
    '310_2025_NDCP.pdf',
    '152_2025_TTBTC.pdf',
]

for f in files:
    path = Path(f'data/raw/{f}')
    if path.exists():
        result = detect_pdf_type(path)
        print(f'{f}: {result}')
    else:
        print(f'{f}: NOT FOUND')
