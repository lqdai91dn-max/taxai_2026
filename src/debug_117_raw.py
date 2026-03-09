# debug_117_raw.py
import pdfplumber
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

with pdfplumber.open("data/raw/117_2025_NDCP.pdf") as pdf:
    total = len(pdf.pages)
    print(f"Total pages: {total}")
    
    # Scan tất cả trang tìm chữ "Chương"
    for i in range(total):
        page = pdf.pages[i]
        img = page.to_image(resolution=150).original
        text = pytesseract.image_to_string(img, lang='vie', config='--oem 3 --psm 6')
        
        if 'hương' in text or 'CHƯƠNG' in text.upper():
            print(f"\n=== TRANG {i+1} (có Chương) ===")
            # Tìm dòng chứa Chương
            for line in text.split('\n'):
                if 'hương' in line or 'CHƯƠNG' in line.upper():
                    print(f"  >>> {repr(line)}")