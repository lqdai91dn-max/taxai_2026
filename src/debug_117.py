# debug_117.py
import pdfplumber

with pdfplumber.open("data/raw/117_2025_NDCP.pdf") as pdf:
    # Xem 3 trang đầu
    for i in range(3):
        page = pdf.pages[i]
        text = page.extract_text()
        print(f"\n=== TRANG {i+1} ===")
        print(text[:800] if text else "KHÔNG CÓ TEXT")
        print("...")