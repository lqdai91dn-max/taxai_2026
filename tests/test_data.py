
from src.parsing.pdfplumber_helper import PDFPlumberHelper
import os

files = [
    '110_2025_UBTVQH15.pdf',
    '149_2025_QH15.pdf', 
    '198_2025_QH15.pdf',
    '20_2026_NDCP.pdf',
    '373_2025_NDCP.pdf',
    '310_2025_NDCP.pdf',
    '152_2025_TTBTC.pdf',
]

helper = PDFPlumberHelper()
for f in files:
    path = f'data/raw/{f}'
    if os.path.exists(path):
        result = helper.detect_pdf_type(path)
        print(f'{f}: {result}')
    else:
        print(f'{f}: NOT FOUND')
