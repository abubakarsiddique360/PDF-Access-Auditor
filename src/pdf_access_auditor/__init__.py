"""PDF-Access-Auditor: write PDFs from scratch, then grade their accessibility basics.

Standard library only. The writer produces real byte offsets in a real xref table; the reader
believes that table and reports it when the table lies; the auditor turns what is left into seven
severity-scored findings.
"""

__version__ = "1.0.0"

from .audit import (  # noqa: E402
    RULES,
    SEVERITY_ORDER,
    SEVERITY_POINTS,
    Audit,
    Finding,
    audit_document,
    grade_for,
)
from .pdfobj import (  # noqa: E402
    DocumentSpec,
    PdfBuilder,
    PdfError,
    build_document,
    write_document,
)
from .reader import Document, OffsetMismatch, parse_pdf, read_pdf  # noqa: E402

__all__ = [
    "RULES",
    "SEVERITY_ORDER",
    "SEVERITY_POINTS",
    "Audit",
    "Document",
    "DocumentSpec",
    "Finding",
    "OffsetMismatch",
    "PdfBuilder",
    "PdfError",
    "__version__",
    "audit_document",
    "build_document",
    "grade_for",
    "parse_pdf",
    "read_pdf",
    "write_document",
]
