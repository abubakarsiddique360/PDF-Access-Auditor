import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # noqa: E402

from pdf_access_auditor.pdfobj import DocumentSpec, build_document  # noqa: E402
from pdf_access_auditor.reader import find_startxref, parse_xref  # noqa: E402

EXAMPLES = ROOT / "examples"

GOOD_SPEC = DocumentSpec(
    title="Accessible quarterly report",
    lang="en",
    tagged=True,
    alt_text=True,
    embedded_font=True,
    pages=2,
)
BAD_SPEC = DocumentSpec(title="", lang="", tagged=False, alt_text=False, pages=1)


@pytest.fixture
def good_bytes() -> bytes:
    return build_document(GOOD_SPEC)


@pytest.fixture
def bad_bytes() -> bytes:
    return build_document(BAD_SPEC)


@pytest.fixture
def tiny_pdf(tmp_path) -> Path:
    path = tmp_path / "tiny.pdf"
    path.write_bytes(build_document(DocumentSpec(pages=1)))
    return path


def xref_offsets(data: bytes) -> dict[int, int]:
    """The in-use offsets straight out of the cross-reference table."""
    entries, _ = parse_xref(data, find_startxref(data))
    return {number: entry.offset for number, entry in entries.items() if entry.in_use}


def xref_trailer(data: bytes) -> dict:
    _, trailer = parse_xref(data, find_startxref(data))
    return trailer


def patch_xref(data: bytes, object_number: int, new_offset: int) -> bytes:
    """Rewrite one xref entry, keeping the file exactly the same length."""
    entries, _ = parse_xref(data, find_startxref(data))
    entry = entries[object_number]
    original = f"{entry.offset:010d} {entry.generation:05d} n \n".encode()
    replacement = f"{new_offset:010d} {entry.generation:05d} n \n".encode()
    index = data.rindex(original)
    return data[:index] + replacement + data[index + len(original) :]


def blank_out(data: bytes, needle: bytes) -> bytes:
    """Replace ``needle`` with spaces, keeping every byte offset in the file valid."""
    return data.replace(needle, b" " * len(needle), 1)
