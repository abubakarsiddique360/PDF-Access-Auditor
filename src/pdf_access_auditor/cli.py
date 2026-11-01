"""Command line entry point for pdf-audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .audit import FAIL_THRESHOLDS, audit_document
from .pdfobj import DocumentSpec, build_document
from .reader import PdfError, parse_pdf, read_pdf
from .report import (
    render_audit,
    render_checks,
    render_document,
    render_json,
    render_markdown,
    render_rules,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNREADABLE = 2


def _boolean(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {text!r}")


def _optional(text: str) -> str:
    """``none`` (or an empty string) means "leave the entry out", for shells that drop ``""``."""
    return "" if text.strip().lower() in ("", "none") else text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-audit",
        description="Write PDFs from scratch and grade their accessibility basics.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    writer = sub.add_parser("write", help="generate a PDF with the built-in writer")
    writer.add_argument("--out", required=True, help="path of the PDF to write")
    writer.add_argument("--lang", default="en", help="catalog /Lang; 'none' omits it")
    writer.add_argument("--tagged", type=_boolean, default=True, metavar="true|false")
    writer.add_argument("--alt", type=_boolean, default=True, metavar="true|false")
    writer.add_argument(
        "--embed-font", dest="embed_font", type=_boolean, default=False, metavar="true|false"
    )
    writer.add_argument("--title", default="PDF-Access-Auditor sample", help="'none' omits it")
    writer.add_argument("--pages", type=int, default=1)

    auditor = sub.add_parser("audit", help="grade one PDF and exit 1 when the gate fails")
    auditor.add_argument("--file", required=True)
    auditor.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    auditor.add_argument(
        "--fail-on", dest="fail_on", choices=FAIL_THRESHOLDS, default="high", metavar="LEVEL",
        help="lowest severity that fails the run: " + ", ".join(FAIL_THRESHOLDS),
    )

    info = sub.add_parser("info", help="print the structure the reader resolved")
    info.add_argument("--file", required=True)

    checks = sub.add_parser("checks", help="PASS or FAIL for every rule, for one file")
    checks.add_argument("--file", required=True)

    sub.add_parser("rules", help="print the static rule catalogue")
    return parser


def _write(args: argparse.Namespace) -> int:
    spec = DocumentSpec(
        title=_optional(args.title),
        lang=_optional(args.lang),
        tagged=args.tagged,
        alt_text=args.alt,
        embedded_font=args.embed_font,
        pages=args.pages,
    )
    try:
        payload = build_document(spec)
    except PdfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    target = Path(args.out)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError as exc:
        print(f"error: {target}: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    audited = audit_document(parse_pdf(payload, source=str(target)))
    detail = (
        "no findings"
        if audited.clean
        else ", ".join(item.code for item in audited.findings)
    )
    print(f"wrote {target}  {len(payload)} bytes  {spec.pages} page(s)")
    print(f"audit  risk {audited.risk}  grade {audited.grade}  {detail}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "rules":
        print(render_rules())
        return EXIT_OK

    if args.command == "write":
        return _write(args)

    try:
        document = read_pdf(args.file)
    except (OSError, PdfError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    audited = audit_document(document)

    if args.command == "info":
        print(render_document(document))
        return EXIT_OK

    if args.command == "checks":
        print(render_checks(document, audited))
        return EXIT_OK

    if args.format == "json":
        payload = {"file": args.file}
        payload.update(document.as_dict())
        payload.update(audited.as_dict())
        print(render_json(payload))
    elif args.format == "markdown":
        print(render_markdown(audited, source=args.file))
    else:
        print(render_audit(audited, source=args.file))

    return EXIT_FAILED if audited.should_fail(args.fail_on) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
