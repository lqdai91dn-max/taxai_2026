# debug_pages.py
import pdfplumber

with pdfplumber.open("data/raw/109_2025_QH15.pdf") as pdf:
    for i in range(0, 15):  
        page = pdf.pages[i]
        text = page.extract_text()
        print(f"\n=== TRANG {i+1} ===")
        print(text[:500] if text else "KHÔNG CÓ TEXT")
        print("...")