"""Regenerate the committed example PDFs.

Usage:  python scripts/make_examples.py
Writes: examples/good.pdf, examples/bad.pdf, examples/legacy.pdf

They are committed so that the README, the screenshots and the tests all talk about the same three
files. Each one is built by the project's own writer, so their xref offsets are correct by
construction - which is what makes ``bad.pdf`` a *content* failure rather than a broken file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdf_access_auditor.pdfobj import DocumentSpec, build_document  # noqa: E402

EXAMPLES: dict[str, DocumentSpec] = {
    "good.pdf": DocumentSpec(
        title="Accessible quarterly report",
        lang="en",
        tagged=True,
        alt_text=True,
        embedded_font=True,
        pages=2,
        alt="Bar chart: revenue rises from 12 to 31 between Q1 and Q4",
    ),
    "bad.pdf": DocumentSpec(
        title="",
        lang="",
        tagged=False,
        alt_text=False,
        embedded_font=False,
        pages=1,
    ),
    "legacy.pdf": DocumentSpec(
        title="Legacy vendor export",
        lang="",
        tagged=False,
        alt_text=True,
        embedded_font=False,
        pages=2,
        alt="Scanned signature mark",
    ),
}


def write_examples(directory: Path | None = None) -> list[Path]:
    """Write every example and return the paths, in a deterministic order."""
    out_dir = directory or (ROOT / "examples")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, spec in EXAMPLES.items():
        path = out_dir / name
        path.write_bytes(build_document(spec))
        written.append(path)
    return written


def main() -> int:
    total = 0
    for path in write_examples():
        size = path.stat().st_size
        total += size
        print(f"{path.name:<12}{size:>7} bytes")
    print(f"{'total':<12}{total:>7} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
