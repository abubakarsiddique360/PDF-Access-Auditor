"""Seven accessibility basics, scored the way the other audits in this family are scored.

A finding is a claim about a place in the file (``catalog``, ``page 1 /Im1``, ``object 4``), a
severity, and a hint that says what to change. The risk score is the sum of severity points, so
it is reproducible: nothing here samples, guesses or reads the clock.

The rules are a *basic subset* of what PDF/UA asks for. They are the seven that a structure-only
reader can actually decide, and no more - see the README's design notes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pdfobj import Reference, mapping
from .reader import Document, PdfError

SEVERITY_ORDER = ("critical", "high", "medium", "low")
SEVERITY_POINTS = {"critical": 30, "high": 15, "medium": 8, "low": 3}
FAIL_THRESHOLDS = ("never", "critical", "high", "medium", "low")


@dataclass(frozen=True)
class Rule:
    """The static catalogue entry for one check."""

    code: str
    severity: str
    checks: str
    hint: str

    @property
    def points(self) -> int:
        return SEVERITY_POINTS[self.severity]


RULES: tuple[Rule, ...] = (
    Rule(
        code="image-without-alt",
        severity="critical",
        checks="every image XObject reachable from a page carries /Alt or is marked /Artifact",
        hint="add /Alt to the image XObject, or mark a decorative image as an artifact",
    ),
    Rule(
        code="xref-offset-wrong",
        severity="critical",
        checks="each in-use xref entry points at the byte where its object really starts",
        hint="rewrite the file with a writer that measures offsets instead of guessing them",
    ),
    Rule(
        code="missing-lang",
        severity="high",
        checks="the document catalog declares a default language with /Lang",
        hint="set /Lang on the catalog, for example << /Lang (en) >>",
    ),
    Rule(
        code="untagged",
        severity="high",
        checks="the catalog has /MarkInfo << /Marked true >> and a /StructTreeRoot",
        hint="export the document with tagging enabled so that a structure tree exists",
    ),
    Rule(
        code="no-page-tree",
        severity="high",
        checks="/Root -> /Pages -> /Kids can be walked down to at least one page",
        hint="repair the page tree: /Root needs /Pages, and every node needs /Kids or /Type /Page",
    ),
    Rule(
        code="font-not-embedded",
        severity="medium",
        checks="every font in a page's resources resolves to a /FontFile, /FontFile2 or /FontFile3",
        hint="embed the font: add a FontDescriptor with a FontFile2 stream",
    ),
    Rule(
        code="missing-title",
        severity="medium",
        checks="the /Info dictionary carries a non-empty /Title",
        hint="set /Title in the document information dictionary",
    ),
)

RULES_BY_CODE = {rule.code: rule for rule in RULES}


def grade_for(risk: int) -> str:
    """Turn a risk score into a letter: 0 is A, and anything above 60 is F."""
    if risk <= 0:
        return "A"
    if risk <= 10:
        return "B"
    if risk <= 30:
        return "C"
    if risk <= 60:
        return "D"
    return "F"


@dataclass(frozen=True)
class Finding:
    """One rule violation, at one place in one file."""

    code: str
    severity: str
    scope: str
    message: str
    hint: str

    @property
    def points(self) -> int:
        return SEVERITY_POINTS[self.severity]

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "scope": self.scope,
            "message": self.message,
            "hint": self.hint,
            "points": self.points,
        }


@dataclass(frozen=True)
class Audit:
    """Every finding for one file, plus the score they add up to."""

    findings: tuple[Finding, ...]
    risk: int
    grade: str
    counts: dict[str, int]

    @classmethod
    def from_findings(cls, findings) -> Audit:
        ordered = tuple(
            sorted(
                findings,
                key=lambda item: (SEVERITY_ORDER.index(item.severity), item.scope, item.code),
            )
        )
        risk = sum(item.points for item in ordered)
        counts = {
            severity: sum(1 for item in ordered if item.severity == severity)
            for severity in SEVERITY_ORDER
        }
        return cls(findings=ordered, risk=risk, grade=grade_for(risk), counts=counts)

    @property
    def clean(self) -> bool:
        return not self.findings

    def at_or_above(self, severity: str) -> bool:
        """True when at least one finding is as severe as, or worse than, ``severity``."""
        if severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity {severity!r}")
        limit = SEVERITY_ORDER.index(severity)
        return any(SEVERITY_ORDER.index(item.severity) <= limit for item in self.findings)

    def should_fail(self, threshold: str = "high") -> bool:
        """Whether a CI gate set to ``threshold`` should reject this file."""
        if threshold == "never":
            return False
        if threshold not in SEVERITY_ORDER:
            raise ValueError(f"unknown threshold {threshold!r}")
        return self.at_or_above(threshold)

    def scopes(self, code: str) -> tuple[str, ...]:
        return tuple(item.scope for item in self.findings if item.code == code)

    def as_dict(self) -> dict:
        return {
            "risk": self.risk,
            "grade": self.grade,
            "clean": self.clean,
            "counts": dict(self.counts),
            "findings": [item.as_dict() for item in self.findings],
        }


def _is_embedded(document: Document, font: dict, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if any(key in font for key in ("FontFile", "FontFile2", "FontFile3")):
        return True
    descriptor = mapping(document.resolve(font.get("FontDescriptor")))
    if descriptor is not None and any(
        key in descriptor for key in ("FontFile", "FontFile2", "FontFile3")
    ):
        return True
    descendants = document.resolve(font.get("DescendantFonts"))
    if isinstance(descendants, list):
        for descendant in descendants:
            resolved = mapping(document.resolve(descendant))
            if resolved is not None and _is_embedded(document, resolved, depth + 1):
                return True
    return False


def _object_key(value, kind: str) -> tuple[str, int]:
    """Identify a resource once, even when several pages point at the same object."""
    if isinstance(value, Reference):
        return (kind, value.number)
    return (kind, id(value))


def _is_artifact(document: Document, xobject: dict) -> bool:
    """A decorative image may carry ``/Artifact true`` instead of alt text."""
    artifact = document.resolve(xobject.get("Artifact"))
    if artifact is not None and artifact is not False:
        return True
    return document.text(xobject.get("Subtype")).lower() == "artifact"


def audit_document(document: Document) -> Audit:
    """Run every rule against an already-parsed document."""
    findings: list[Finding] = []

    def add(code: str, scope: str, message: str) -> None:
        rule = RULES_BY_CODE[code]
        findings.append(
            Finding(
                code=code,
                severity=rule.severity,
                scope=scope,
                message=message,
                hint=rule.hint,
            )
        )

    catalog = document.catalog()

    if not document.text(catalog.get("Lang")).strip():
        add("missing-lang", "catalog", "the document catalog has no /Lang entry")

    mark_info = mapping(document.resolve(catalog.get("MarkInfo")))
    marked = mark_info is not None and document.resolve(mark_info.get("Marked")) is True
    if not marked and catalog.get("StructTreeRoot") is None:
        add(
            "untagged",
            "catalog",
            "no /MarkInfo << /Marked true >> and no /StructTreeRoot: the file is untagged",
        )

    pages: tuple[dict, ...] = ()
    try:
        pages = document.pages()
    except PdfError as exc:
        add("no-page-tree", "catalog", str(exc))

    reported: set[tuple[str, int]] = set()
    for index, page in enumerate(pages, start=1):
        resources = document.resources(page)

        xobjects = mapping(document.resolve(resources.get("XObject")))
        if xobjects is not None:
            for key, value in xobjects.items():
                xobject = mapping(document.resolve(value))
                if xobject is None:
                    continue
                if document.text(xobject.get("Subtype")) != "Image":
                    continue
                if xobject.get("Alt") is not None or _is_artifact(document, xobject):
                    continue
                identity = _object_key(value, "image")
                if identity in reported:
                    continue
                reported.add(identity)
                add(
                    "image-without-alt",
                    f"page {index} /{key}",
                    f"image XObject /{key} has no /Alt and is not marked /Artifact",
                )

        fonts = mapping(document.resolve(resources.get("Font")))
        if fonts is not None:
            for key, value in fonts.items():
                font = mapping(document.resolve(value))
                if font is None:
                    continue
                if _is_embedded(document, font):
                    continue
                identity = _object_key(value, "font")
                if identity in reported:
                    continue
                reported.add(identity)
                base_font = document.text(font.get("BaseFont")) or "unnamed"
                add(
                    "font-not-embedded",
                    f"page {index} /{key}",
                    f"font /{base_font} has no /FontFile, /FontFile2 or /FontFile3",
                )

    info = document.info()
    if not document.text(info.get("Title")).strip():
        where = "the /Info dictionary has no /Title" if info else "the file has no /Info dictionary"
        add("missing-title", "info", where)

    for mismatch in document.mismatches:
        add(
            "xref-offset-wrong",
            f"object {mismatch.number}",
            f"the xref says object {mismatch.number} starts at byte {mismatch.expected}, "
            f"but it starts at byte {mismatch.actual}",
        )

    return Audit.from_findings(findings)


def audit_bytes(data: bytes, source: str = "<bytes>") -> Audit:
    """Convenience wrapper for auditing an in-memory PDF."""
    from .reader import parse_pdf

    return audit_document(parse_pdf(data, source=source))


def rule_table() -> list[dict]:
    """The catalogue as plain data, for JSON output and the checks matrix."""
    return [
        {
            "code": rule.code,
            "severity": rule.severity,
            "points": rule.points,
            "checks": rule.checks,
            "hint": rule.hint,
        }
        for rule in RULES
    ]


def rule_codes() -> tuple[str, ...]:
    return tuple(rule.code for rule in RULES)


__all__ = [
    "FAIL_THRESHOLDS",
    "RULES",
    "RULES_BY_CODE",
    "SEVERITY_ORDER",
    "SEVERITY_POINTS",
    "Audit",
    "Finding",
    "Rule",
    "audit_bytes",
    "audit_document",
    "grade_for",
    "rule_codes",
    "rule_table",
]
