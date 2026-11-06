"""Second-engine check of numbers in OCR text. Reports disagreements; never changes the text.

Dates, application numbers and citations are where a misread does the most harm, and
Windows OCR exposes no confidence score. Tesseract reads the same rendered pages and any
number only one engine saw is listed for review. Only a conflict (two similar numbers, one
from each engine, as in 7/2025 and 10/17/2025) flags the document, since a number one engine
simply missed is common and usually harmless. A disagreement does not show which engine
is right, and agreement does not prove a number correct. Kept apart from pdf_ocr.py because
that adapter's hash pins OCR checkpoints and the Source Corpus snapshot.
"""
import json
import os
from difflib import SequenceMatcher
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .worker_runtime import WorkerRuntimeError, read_json_output, run_extraction_worker

MAX_CHECK_PAGES = 64            # new pages per call; earlier pages come from the cache
RENDER_BATCH_PAGES = 8
TIME_BUDGET_SECONDS = 75.0      # no new page starts after this
WORKER_TIMEOUT_SECONDS = 110
PAGE_TIMEOUT_SECONDS = 30
MAX_TOKENS_PER_PAGE = 20
CONFLICT_SIMILARITY = 0.6
RENDER_DPI = 200
ENGINE = 'tesseract'
DEFAULT_TESSERACT = Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')
HELPER = Path(__file__).with_name('windows_pdf_render.ps1')
NOTE = 'Numbers only one OCR engine read. This does not show which is right; check the original image.'
_SPLIT_NUMBER = re.compile(r'(?<=\d)([,/.\-])\s+(?=\d)')
_NUMBER = re.compile(r'(?<![\w])\d[\d,./\-]*\d(?![\w])')


def numeric_tokens(text):
    """Map digits-only keys to the text as read, so punctuation misreads don't count."""
    tokens = {}
    for token in _NUMBER.findall(_SPLIT_NUMBER.sub(r'\1', text)):
        digits = re.sub(r'\D', '', token)
        if len(digits) >= 3:
            tokens.setdefault(digits, token)
    return tokens


def compare_page(primary, second):
    first, other = numeric_tokens(primary), numeric_tokens(second)
    return ([first[k] for k in first if k not in other][:MAX_TOKENS_PER_PAGE],
            [other[k] for k in other if k not in first][:MAX_TOKENS_PER_PAGE])


def find_conflicts(primary_only, second_only):
    """Pair each number with the other engine's closest one, when close enough to be the same number misread."""
    digits = lambda token: re.sub(r'\D', '', token)
    pairs = []
    for token in primary_only:
        scored = [(SequenceMatcher(None, digits(token), digits(other)).ratio(), other) for other in second_only]
        best = max(scored, default=(0, None), key=lambda item: item[0])
        if best[0] >= CONFLICT_SIMILARITY:
            pairs.append([token, best[1]])
    return pairs


def tesseract_path():
    configured = os.environ.get('CONTEXT_FORGE_TESSERACT')
    path = Path(configured) if configured else DEFAULT_TESSERACT
    return path if path.is_file() else None


def _render(source, pages, scratch):
    executable = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG'}}
    # Process-scope Bypass, as for the OCR helper: a default Restricted policy would otherwise stop this fixed script.
    process = subprocess.Popen([str(executable), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(HELPER),
                                '-InputPdf', str(source), '-OutputDir', str(scratch), '-PagesCsv', ','.join(map(str, pages))],
                               env=environment, cwd=scratch, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        process.communicate(b'go\n', timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _recognize(tesseract, image):
    completed = subprocess.run([str(tesseract), str(image), 'stdout', '-l', 'eng', '--dpi', str(RENDER_DPI)],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=PAGE_TIMEOUT_SECONDS,
                               creationflags=getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode:
        raise RuntimeError('Second OCR engine failed')
    return completed.stdout.decode('utf-8', 'replace')


def read_pages(source, pages, tesseract, scratch, render=_render, recognize=_recognize, progress=None):
    """Worker body: returns {page: text}. Pages not reached in the budget are simply absent.
    progress is called after each batch, so a worker stopped at its deadline keeps what it read."""
    started = time.monotonic()
    texts = {}
    for index in range(0, len(pages), RENDER_BATCH_PAGES):
        batch = pages[index:index + RENDER_BATCH_PAGES]
        if time.monotonic() - started >= TIME_BUDGET_SECONDS:
            break
        render(source, batch, scratch)
        for page in batch:
            image = Path(scratch) / f'page-{page}.png'
            if not image.is_file() or time.monotonic() - started >= TIME_BUDGET_SECONDS:
                continue
            try:
                texts[page] = recognize(tesseract, image)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                continue
            finally:
                image.unlink(missing_ok=True)
        if progress:
            progress(texts)
    return texts


def summarize(page_texts, second_texts, version=None):
    """Compare what each engine read. Result is stored in the manifest as ocr_numeric_check."""
    checked = sorted(p for p in page_texts if p in second_texts)
    disputed = []
    for page in checked:
        primary_only, second_only = compare_page(page_texts[page], second_texts[page])
        if primary_only or second_only:
            disputed.append({'page': page, 'windows_only': primary_only, 'second_engine_only': second_only,
                             'conflicts': find_conflicts(primary_only, second_only)})
    not_checked = sorted(p for p in page_texts if p not in second_texts)
    result = {'engine': ENGINE, 'status': 'complete' if not not_checked else ('partial' if checked else 'failed'),
              'checked_pages': checked, 'not_checked_pages': not_checked, 'disputed_pages': disputed, 'note': NOTE}
    if version:
        result['engine_version'] = version
    return result


def _version(tesseract):
    try:
        output = subprocess.run([str(tesseract), '--version'], stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
        return output.decode('utf-8', 'replace').splitlines()[0][:60]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def _load_cache(path):
    try:
        return {int(k): v for k, v in json.loads(Path(path).read_text(encoding='utf-8')).items() if isinstance(v, str)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_cache(path, texts):
    target = Path(path)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(texts), encoding='utf-8')
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)


def check_recognized_pages(source, extraction, work_dir, python_executable=None, cache_path=None):
    """Runs in its own bounded worker. Any failure is reported in the result and never fails the document.
    cache_path holds the second engine's text by page, so OCR continuations don't re-read earlier pages."""
    ocr = extraction.metadata.get('ocr') or {}
    recognized = sorted(ocr.get('recognized_pages') or [])
    if not recognized:
        return None
    if os.name != 'nt':
        return {'engine': ENGINE, 'status': 'unavailable', 'note': NOTE}
    tesseract = tesseract_path()
    if tesseract is None:
        return {'engine': ENGINE, 'status': 'unavailable', 'note': NOTE}
    sections = {section.locator: section.text for section in extraction.sections}
    page_texts = {p: sections[f'page:{p}'] for p in recognized if sections.get(f'page:{p}', '').strip()}
    second_texts = {p: t for p, t in (_load_cache(cache_path) if cache_path else {}).items() if p in page_texts}
    pages = [p for p in page_texts if p not in second_texts][:MAX_CHECK_PAGES]
    if not pages:
        return summarize(page_texts, second_texts, _version(tesseract))
    scratch = Path(work_dir) / ('crosscheck-' + uuid.uuid4().hex)
    scratch.mkdir()
    try:
        request, output = scratch / 'request.json', scratch / 'result.json'
        request.write_text(json.dumps({'pages': pages, 'tesseract': str(tesseract)}), encoding='utf-8')
        try:
            run_extraction_worker(Path(python_executable or sys.executable), ['--numeric-check', str(source), str(request), str(output)],
                                  Path(__file__).resolve().parents[1], scratch, WORKER_TIMEOUT_SECONDS)
        except (OSError, WorkerRuntimeError):
            pass  # keep whatever batches it finished
        if output.is_file():
            second = read_json_output(output, 64 * 1024 * 1024)
            second_texts.update({int(k): v for k, v in second.get('pages', {}).items() if isinstance(v, str)})
    except (OSError, ValueError, TypeError, WorkerRuntimeError):
        pass
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if cache_path and second_texts:
        _save_cache(cache_path, second_texts)
    return summarize(page_texts, second_texts, _version(tesseract))


def conflict_pages(check):
    return [item.get('page') for item in (check or {}).get('disputed_pages') or [] if item.get('conflicts')]


def worker_main(source, request_path, output_path):
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    scratch = Path(request_path).parent
    target = Path(output_path)

    def save(texts):
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_text(json.dumps({'pages': texts}), encoding='utf-8')
        temporary.replace(target)
    save(read_pages(Path(source), request['pages'], Path(request['tesseract']), scratch, progress=save))
