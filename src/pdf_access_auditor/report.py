"""Text, Markdown and JSON renderings of an audit.

Every renderer takes data that was already measured - nothing in this module inspects a PDF. That
keeps the CLI thin, the tests honest, and the terminal screenshots in the README a direct copy of
what :func:`pdf_access_auditor.cli.main` prints.
"""

from __future__ import annotations

import json
import textwrap

from .audit import RULES, SEVERITY_ORDER, Audit
from .pdfobj import mapping
from .reader import Document, PdfError

RULE_COLUMN = max(len(rule.code) for rule in RULES) + 2
WIDTH = 88


def _wrap(prefix: str, text: str, indent: int | None = None) -> list[str]:
    pad = " " * (len(prefix) if indent is None else indent)
    wrapped = textwrap.wrap(text, width=WIDTH, initial_indent=prefix, subsequent_indent=pad)
    return wrapped or [prefix.rstrip()]


def _heading(title: str, underline: str = "=") -> list[str]:
    return [title, underline * min(len(title), WIDTH)]


def _summary(audit: Audit) -> str:
    return "  ".join(f"{severity} {audit.counts[severity]}" for severity in SEVERITY_ORDER)


def render_audit(audit: Audit, source: str = "<document>") -> str:
    """The human report: one block per severity, each finding with its fix."""
    lines = _heading(f"PDF accessibility audit  -  {source}")
    lines.append(f"{'risk':<9}{audit.risk:>4}   grade {audit.grade}")
    lines.append(f"{'findings':<9}{len(audit.findings):>4}   {_summary(audit)}")

    if audit.clean:
        lines.append("")
        lines.append("no findings: every rule in this build passed")
        return "\n".join(lines)

    for severity in SEVERITY_ORDER:
        group = [item for item in audit.findings if item.severity == severity]
        if not group:
            continue
        lines.append("")
        lines.append(f"{severity} ({len(group)})")
        for finding in group:
            head = f"  {severity:<8}  {finding.code:<{RULE_COLUMN}}{finding.scope}"
            lines.append(head)
            lines.extend(_wrap(" " * (12 + RULE_COLUMN), finding.message, 12 + RULE_COLUMN))
            lines.extend(
                _wrap(" " * (12 + RULE_COLUMN) + "fix: ", finding.hint, 12 + RULE_COLUMN + 5)
            )
    return "\n".join(lines)


def render_markdown(audit: Audit, source: str = "<document>") -> str:
    """The same audit as a Markdown table, ready to paste into a report."""
    lines = [f"### `{source}`", ""]
    if audit.clean:
        lines.append(f"**risk {audit.risk} - grade {audit.grade}** (no findings)")
        return "\n".join(lines)
    lines.append(
        f"**risk {audit.risk} - grade {audit.grade}** ({len(audit.findings)} findings: "
        + ", ".join(f"{audit.counts[severity]} {severity}" for severity in SEVERITY_ORDER)
        + ")"
    )
    lines.append("")
    lines.append("| Severity | Code | Scope | What is wrong | Fix |")
    lines.append("| --- | --- | --- | --- | --- |")
    for finding in audit.findings:
        lines.append(
            f"| {finding.severity} | `{finding.code}` | `{finding.scope}` | "
            f"{finding.message} | {finding.hint} |"
        )
    return "\n".join(lines)


def render_json(payload) -> str:
    """Indented JSON with a stable key order."""
    return json.dumps(payload, indent=2)


def render_document(document: Document) -> str:
    """Structure report: what the reader actually found in the file."""
    catalog = document.catalog()
    info = document.info()
    lines = _heading(f"PDF structure  -  {document.path or '<bytes>'}")
    rows = [
        ("version", document.version),
        ("size", f"{document.size} bytes"),
        ("objects", str(document.object_count)),
        ("xref entries", f"{document.in_use_entries} in use"),
        ("offset checks", f"{len(document.mismatches)} disagreed"),
    ]
    if document.missing:
        rows.append(("missing objects", ", ".join(str(number) for number in document.missing)))
    try:
        pages = document.pages()
        rows.append(("pages", str(len(pages))))
    except PdfError as exc:
        rows.append(("pages", f"unreadable ({exc})"))
    rows.append(("title", document.text(info.get("Title")) or "<none>"))
    rows.append(("lang", document.text(catalog.get("Lang")) or "<none>"))
    rows.append(("tagged", "yes" if _tagged(document, catalog) else "no"))
    for label, value in rows:
        lines.append(f"{label:<16}{value}")
    return "\n".join(lines)


def _tagged(document: Document, catalog: dict) -> bool:
    mark_info = mapping(document.resolve(catalog.get("MarkInfo")))
    marked = mark_info is not None and document.resolve(mark_info.get("Marked")) is True
    return marked or catalog.get("StructTreeRoot") is not None


def render_checks(document: Document, audit: Audit) -> str:
    """Every rule, with PASS or FAIL for one file - including the ones that passed."""
    lines = _heading(f"Rule checks  -  {document.path or '<bytes>'}")
    lines.append(f"{'result':<8}{'code':<{RULE_COLUMN}}{'severity':<10}detail")
    lines.append("-" * WIDTH)
    for rule in RULES:
        scopes = audit.scopes(rule.code)
        status = "FAIL" if scopes else "PASS"
        detail = "failed at " + ", ".join(scopes) if scopes else rule.checks
        prefix = f"{status:<8}{rule.code:<{RULE_COLUMN}}{rule.severity:<10}"
        lines.extend(_wrap(prefix, detail, 8 + RULE_COLUMN + 10))
    lines.append("")
    lines.append(f"{len(RULES) - len({item.code for item in audit.findings})} of {len(RULES)} "
                 f"rules pass   risk {audit.risk}   grade {audit.grade}")
    return "\n".join(lines)


def render_rules() -> str:
    """The static rule catalogue, with the points each severity is worth."""
    lines = _heading("Rule catalogue")
    lines.append(f"{'code':<{RULE_COLUMN}}{'severity':<10}{'points':>6}  what it checks")
    lines.append("-" * WIDTH)
    for rule in RULES:
        prefix = f"{rule.code:<{RULE_COLUMN}}{rule.severity:<10}{rule.points:>6}  "
        lines.extend(_wrap(prefix, rule.checks, RULE_COLUMN + 18))
    lines.append("")
    lines.append("grade: 0 = A, up to 10 = B, up to 30 = C, up to 60 = D, above 60 = F")
    return "\n".join(lines)
