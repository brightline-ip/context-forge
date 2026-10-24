"""Regenerate the synthetic scanned-PDF fixtures used by tests/test_pdf_ocr_benchmark.py.

All text is invented (fictional company, people and numbers). Each page is rendered as an
image and saved as an image-only PDF, so a PDF text extractor finds no native text and the OCR
path is exercised. Needs Pillow and Windows' Arial font. The committed files are pinned by
SHA-256 in the test; if you regenerate them, update the hashes it prints.

    python scripts/make_ocr_fixtures.py
"""
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / 'test-fixtures' / 'synthetic-scans'
FONT = r'C:\Windows\Fonts\arial.ttf'
DPI = 200
FIXED_DATE = 'D:20300101000000Z'  # keeps output byte-reproducible
PAGE = (1700, 2200)  # US Letter at 200 dpi

DOCUMENTS = {
    'letter': [
        ['Northwind Example Laboratories', 'Fictitious Correspondence 2030-0417', '',
         'Date: 12 March 2030', 'Reference number: 4471', '',
         'Dear Example Reader,', '',
         'This synthetic letter exists only to test OCR.',
         'The invented shipment holds forty-two widgets.',
         'Please acknowledge receipt before April fifteenth.'],
        ['Section 2. Delivery schedule', '',
         'Widgets arrive on Tuesday and gaskets on Thursday.',
         'The warehouse on Harbor Road is closed on Sundays.',
         'Contact Priya Ramanathan at extension 5520.', '',
         'Total declared value: 1260 fictional units.'],
        ['Section 3. Closing remarks', '',
         'Thank you for reviewing this entirely invented document.',
         'Sincerely,', 'Marcus Oyelaran', 'Director of Imaginary Operations'],
    ],
    'form': [
        ['FICTIONAL TRANSMITTAL FORM', '',
         'Application number: 99/123,456', 'Filing date: 01-Feb-2031',
         'Applicant: Bellwether Sample Holdings', 'Docket: BSH-2031-777US1', '',
         'Fee code: 1001   Amount: 350.00', 'Receipt number: 7788123'],
    ],
}


def render(lines, size=54):
    image = Image.new('RGB', PAGE, 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, size)
    y = 220
    for line in lines:
        draw.text((180, y), line, fill='black', font=font)
        y += 110
    return image


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, pages in DOCUMENTS.items():
        images = [render(lines) for lines in pages]
        target = OUT / f'synthetic-{name}.pdf'
        images[0].save(target, 'PDF', resolution=DPI, save_all=True, append_images=images[1:],
                       creationDate=FIXED_DATE, modDate=FIXED_DATE)
        reference = '\f'.join('\n'.join(lines) for lines in pages)
        (OUT / f'synthetic-{name}.reference.txt').write_text(reference, encoding='utf-8', newline='\n')
        print(target.name, len(pages), 'pages', hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
