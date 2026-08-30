import io

import pytest
from werkzeug.datastructures import FileStorage

from resumescreener.parsing import ParsingError, extract_text


def _upload(data: bytes, filename: str) -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=filename)


def test_extracts_plain_text():
    text = extract_text(_upload(b"Jane Doe\nPython engineer", "cv.txt"))
    assert "Python engineer" in text


def test_rejects_unsupported_extension():
    with pytest.raises(ParsingError, match="Unsupported file format"):
        extract_text(_upload(b"MZ\x90\x00", "resume.exe"))


def test_corrupt_pdf_raises_user_safe_error():
    with pytest.raises(ParsingError) as exc:
        extract_text(_upload(b"not actually a pdf", "resume.pdf"))
    # The message must be safe to show a user - no library internals, no paths.
    assert "could not be read" in str(exc.value).lower()
    assert "Traceback" not in str(exc.value)


def test_handles_invalid_utf8_without_crashing():
    text = extract_text(_upload(b"caf\xe9 Python", "cv.txt"))
    assert "Python" in text
