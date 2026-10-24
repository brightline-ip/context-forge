"""Regression benchmark on synthetic scanned PDFs.

Fixtures: test-fixtures/synthetic-scans/*.pdf are image-only PDFs rendered by
scripts/make_ocr_fixtures.py from invented text (no real documents). They are hash-pinned;
the *.reference.txt files hold the exact rendered text, one page per form feed, so the
agreement floor below is measured against ground truth rather than a second OCR engine.
Floors were measured on Windows 11 (build 26200) with the en-US recognizer, with margin.

The test is skipped when the Windows en-US OCR language pack is unavailable.
"""
import hashlib
import json
import os
import re
import shutil
import unittest
import uuid
from collections import Counter
from pathlib import Path

from context_forge.extractors import extract

FIXTURES = Path(__file__).resolve().parent.parent / 'test-fixtures' / 'synthetic-scans'
EXPECTED = {
    'synthetic-letter': {'sha256': '031fa792a951331b9031b3b62a5bdf98bab9a349eec95cbfc2a6df2c833390ae', 'pages': 3,
                         'min_word_recall': 0.90,
                         'anchors': {1: ['Northwind', '12 March 2030', '4471'],
                                     2: ['Priya Ramanathan', '5520'],
                                     3: ['Marcus Oyelaran']}},
    'synthetic-form': {'sha256': '630803f23176135beff127b492729fb0bbe4b6501df8d09ff9a8c3266c1054ea', 'pages': 1,
                       'min_word_recall': 0.85,
                       'anchors': {1: ['TRANSMITTAL FORM', 'BSH-2031-777US1', '7788123']}},
}


def words(text):
    return re.findall(r'[A-Za-z0-9]+', text.lower())


def word_recall(candidate, reference):
    """Share of reference word occurrences present in the candidate (order-insensitive)."""
    a, b = Counter(words(candidate)), Counter(words(reference))
    return sum((a & b).values()) / sum(b.values()) if b else 1.0


@unittest.skipUnless(os.name == 'nt', 'Windows local OCR backend')
@unittest.skipUnless(FIXTURES.is_dir(), 'synthetic scan fixtures missing')
class SyntheticOcrBenchmark(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ('ob-' + uuid.uuid4().hex[:8])
        self.root.mkdir()

    def tearDown(self):
        if self.root.resolve().parent != Path(__file__).resolve().parent:
            raise RuntimeError('Fixture cleanup outside test directory')
        shutil.rmtree(self.root)

    def run_document(self, name):
        expected = EXPECTED[name]
        source = FIXTURES / f'{name}.pdf'
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), expected['sha256'], 'fixture changed')
        copy = self.root / f'{name}.pdf'
        shutil.copyfile(source, copy)
        result = extract(copy, ocr_checkpoint=self.root / f'{name}.sqlite3')
        self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), expected['sha256'], 'original rewritten')
        if result.metadata['ocr']['status'] == 'unavailable_or_failed':
            self.skipTest('Windows OCR unavailable (is the en-US OCR language pack installed?)')
        return result, (FIXTURES / f'{name}.reference.txt').read_text(encoding='utf-8').split('\f')

    def test_every_page_has_a_status_and_the_run_is_pinned(self):
        for name, expected in EXPECTED.items():
            with self.subTest(document=name):
                result, _ = self.run_document(name)
                ocr = result.metadata['ocr']
                self.assertEqual(ocr['status'], 'complete', ocr['summary'])
                self.assertEqual([r['page'] for r in ocr['pages']], list(range(1, expected['pages'] + 1)))
                self.assertEqual({r['status'] for r in ocr['pages']}, {'recognized'})
                self.assertEqual((ocr['pending_pages'], ocr['failed_pages'], ocr['blank_pages']), ([], [], []))
                self.assertEqual(ocr['config']['render_dpi'], 200)
                self.assertEqual(len(ocr['config_sha256']), 64)
                self.assertFalse(ocr['recognition_verified'])
                for record in ocr['pages']:
                    self.assertFalse(record['confidence_available'])
                    self.assertGreater(record['chars'], 0)
                    self.assertEqual(record['render']['dpi'], 200)
                    self.assertEqual(len(record['text_sha256']), 64)

    def test_text_agrees_with_ground_truth_within_measured_floors(self):
        report = {}
        for name, expected in EXPECTED.items():
            result, reference = self.run_document(name)
            page_texts = [section.text for section in result.sections]
            self.assertEqual(len(page_texts), len(reference), 'reference page count')
            recalls = [round(word_recall(c, r), 3) for c, r in zip(page_texts, reference)]
            report[name] = recalls
            for page, recall in enumerate(recalls, 1):
                self.assertGreaterEqual(recall, expected['min_word_recall'], f'{name} page {page}: {recalls}')
            for page, anchors in expected['anchors'].items():
                for anchor in anchors:
                    self.assertIn(anchor, page_texts[page - 1], f'{name} page {page} lacks known text')
        (self.root / 'agreement.json').write_text(json.dumps(report, indent=1))


if __name__ == '__main__':
    unittest.main()
