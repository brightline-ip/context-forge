"""One bounded-lifetime extraction process; never calls a model or a tool."""
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .extractors import extract
from .safety import InputError, MAX_TEXT_CHARS, safe_error, validate_file


def main():
    if len(sys.argv) == 5 and sys.argv[1] == '--numeric-check':
        from .ocr_crosscheck import worker_main
        return worker_main(*sys.argv[2:5])
    source, target = map(Path, sys.argv[1:3])
    checkpoint = Path(sys.argv[4]) if len(sys.argv) == 5 and sys.argv[3] == '--ocr-checkpoint' else None
    if len(sys.argv) != 3 and checkpoint is None:
        raise SystemExit(2)
    try:
        validate_file(source)
        result = extract(source, ocr_checkpoint=checkpoint)
        if sum(len(section.text) + len(section.heading) for section in result.sections) > MAX_TEXT_CHARS:
            raise InputError("Extracted text exceeds 8 million characters")
        payload = asdict(result)
    except Exception as exc:
        payload = {"error": safe_error(exc)}
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
