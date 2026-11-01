"""A minimal PDF reader: the xref table goes in, an object graph comes out.

There are two ways to read a PDF. One is to hunt for the bytes ``obj`` and hope; the other is to
believe the cross-reference table. This reader believes the xref table, and only *then* checks it:
every in-use entry is parsed at the offset the table claims, and if that offset does not hold the
object it promised, the reader falls back to a full scan and records the disagreement as an
:class:`OffsetMismatch`.

That single decision is what makes ``xref-offset-wrong`` in :mod:`pdf_access_auditor.audit` a real
measurement instead of a decoration: a file whose table lies still opens, but the lie is reported.

Unknown constructs must never crash the reader, because the reader's job is to survive files it did
not write. Anything the tokenizer does not recognise comes back as a :class:`PdfString` holding the
raw token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .pdfobj import HEADER_PREFIX, Name, PdfError, PdfStream, PdfString, Reference

WHITESPACE = b"\x00\t\n\f\r "
DELIMITERS = b"()<>[]{}/%"

INTEGER = re.compile(rb"^[+-]?\d+$")
REAL = re.compile(rb"^[+-]?(?:\d+\.\d*|\.\d+)$")
VERSION = re.compile(r"\d\.\d")
OBJECT_HEADER = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")
XREF_LINE = re.compile(rb"^\s*(\d{1,10})\s+(\d{1,5})\s+([nf])")
XREF_SUBSECTION = re.compile(rb"^\s*(\d{1,10})\s+(\d{1,10})\s*$")

STARTXREF = b"startxref"
TRAILER = b"trailer"
MAX_DEPTH = 32

_ESCAPE_BYTES = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C}


class Parser:
    """A lenient recursive-descent parser for PDF values."""

    def __init__(self, data: bytes, pos: int = 0, objects: dict | None = None) -> None:
        self.data = data
        self.pos = pos
        self.objects = objects if objects is not None else {}

    def skip_space(self) -> None:
        data = self.data
        while self.pos < len(data):
            char = data[self.pos]
            if char in WHITESPACE:
                self.pos += 1
            elif char == 0x25:
                while self.pos < len(data) and data[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                return

    def read_token(self) -> bytes:
        """Read one raw token; an empty token means end of input."""
        self.skip_space()
        return self._token()

    def _token(self) -> bytes:
        data = self.data
        start = self.pos
        while (
            self.pos < len(data)
            and data[self.pos] not in WHITESPACE
            and data[self.pos] not in DELIMITERS
        ):
            self.pos += 1
        if self.pos == start:
            if start >= len(data):
                return b""
            self.pos += 1
            return data[start : start + 1]
        return data[start : self.pos]

    def parse(self):
        self.skip_space()
        if self.pos >= len(self.data):
            raise PdfError("unexpected end of input while parsing a value")
        data = self.data
        if data[self.pos : self.pos + 2] == b"<<":
            return self._dictionary()
        char = data[self.pos]
        if char == 0x5B:
            return self._array()
        if char == 0x2F:
            return self._name()
        if char == 0x28:
            return self._literal_string()
        if char == 0x3C:
            return self._hex_string()
        token = self._token()
        if not token:
            raise PdfError("unexpected end of input while reading a token")
        return self._from_token(token)

    def _from_token(self, token: bytes):
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None
        if INTEGER.match(token):
            value = int(token)
            if value >= 0:
                reference = self._reference_after(value)
                if reference is not None:
                    return reference
            return value
        if REAL.match(token):
            return float(token)
        return PdfString(token)

    def _reference_after(self, object_number: int) -> Reference | None:
        saved = self.pos
        self.skip_space()
        generation_token = self._token()
        if not generation_token or not INTEGER.match(generation_token):
            self.pos = saved
            return None
        self.skip_space()
        if self._token() != b"R":
            self.pos = saved
            return None
        return Reference(object_number, int(generation_token))

    def _name(self) -> Name:
        self.pos += 1
        data = self.data
        out = bytearray()
        while self.pos < len(data):
            char = data[self.pos]
            if char in WHITESPACE or char in DELIMITERS:
                break
            if char == 0x23 and self.pos + 3 <= len(data):
                try:
                    out.append(int(data[self.pos + 1 : self.pos + 3], 16))
                except ValueError:
                    out.append(char)
                    self.pos += 1
                    continue
                self.pos += 3
                continue
            out.append(char)
            self.pos += 1
        return Name(out.decode("utf-8", "replace"))

    def _literal_string(self) -> PdfString:
        self.pos += 1
        data = self.data
        out = bytearray()
        depth = 1
        while self.pos < len(data):
            char = data[self.pos]
            if char == 0x5C:
                self.pos += 1
                if self.pos >= len(data):
                    break
                escape = data[self.pos]
                mapped = _ESCAPE_BYTES.get(escape)
                if mapped is not None:
                    out.append(mapped)
                    self.pos += 1
                elif escape in b"()\\":
                    out.append(escape)
                    self.pos += 1
                elif 0x30 <= escape <= 0x37:
                    digits = bytearray()
                    while (
                        len(digits) < 3
                        and self.pos < len(data)
                        and 0x30 <= data[self.pos] <= 0x37
                    ):
                        digits.append(data[self.pos])
                        self.pos += 1
                    out.append(int(bytes(digits).decode("ascii"), 8) & 0xFF)
                elif escape in b"\r\n":
                    self.pos += 1
                    if (
                        escape == 0x0D
                        and self.pos < len(data)
                        and data[self.pos] == 0x0A
                    ):
                        self.pos += 1
                else:
                    out.append(escape)
                    self.pos += 1
                continue
            if char == 0x28:
                depth += 1
                out.append(char)
                self.pos += 1
                continue
            if char == 0x29:
                depth -= 1
                self.pos += 1
                if depth == 0:
                    return PdfString(bytes(out))
                out.append(char)
                continue
            out.append(char)
            self.pos += 1
        raise PdfError("unterminated literal string")

    def _hex_string(self) -> PdfString:
        self.pos += 1
        data = self.data
        digits = bytearray()
        while self.pos < len(data) and data[self.pos] != 0x3E:
            if data[self.pos] not in WHITESPACE:
                digits.append(data[self.pos])
            self.pos += 1
        if self.pos < len(data):
            self.pos += 1
        text = bytes(digits)
        if len(text) % 2:
            text += b"0"
        try:
            return PdfString(bytes.fromhex(text.decode("ascii")))
        except ValueError:
            return PdfString(text)

    def _array(self) -> list:
        self.pos += 1
        items: list = []
        while True:
            self.skip_space()
            if self.pos >= len(self.data):
                raise PdfError("unterminated array")
            if self.data[self.pos] == 0x5D:
                self.pos += 1
                return items
            items.append(self.parse())

    def _dictionary(self) -> dict | PdfStream:
        self.pos += 2
        entries: dict = {}
        while True:
            self.skip_space()
            if self.pos >= len(self.data):
                raise PdfError("unterminated dictionary")
            if self.data[self.pos : self.pos + 2] == b">>":
                self.pos += 2
                break
            key = self.parse()
            key_name = key.value if isinstance(key, Name) else str(key)
            entries[key_name] = self.parse()
        self.skip_space()
        if self.data[self.pos : self.pos + 6] != b"stream":
            return entries
        return self._stream(entries)

    def _stream(self, entries: dict) -> PdfStream:
        self.pos += 6
        data = self.data
        if data[self.pos : self.pos + 2] == b"\r\n":
            self.pos += 2
        elif data[self.pos : self.pos + 1] in (b"\n", b"\r"):
            self.pos += 1
        length = entries.get("Length")
        if isinstance(length, Reference):
            length = self.objects.get(length.number)
        start = self.pos
        payload: bytes | None = None
        if isinstance(length, int) and 0 <= length and start + length <= len(data):
            payload = data[start : start + length]
            self.pos = start + length
        if payload is None:
            end = data.find(b"endstream", start)
            if end < 0:
                payload = data[start:]
                self.pos = len(data)
            else:
                payload = data[start:end].rstrip(b"\r\n")
                self.pos = end
        marker = data.find(b"endstream", self.pos)
        if marker >= 0:
            self.pos = marker + len(b"endstream")
        return PdfStream(entries, payload)


@dataclass(frozen=True)
class XrefEntry:
    """One line of the cross-reference table."""

    number: int
    offset: int
    generation: int
    in_use: bool

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "offset": self.offset,
            "generation": self.generation,
            "in_use": self.in_use,
        }


@dataclass(frozen=True)
class OffsetMismatch:
    """The table said ``expected``; the object was really at ``actual``."""

    number: int
    expected: int
    actual: int

    def as_dict(self) -> dict:
        return {"object": self.number, "xref_offset": self.expected, "actual_offset": self.actual}


@dataclass
class Document:
    """A parsed PDF: objects, trailer, the xref table and any offsets that disagreed."""

    version: str
    trailer: dict
    objects: dict[int, object]
    xref: dict[int, XrefEntry] = field(default_factory=dict)
    mismatches: tuple[OffsetMismatch, ...] = ()
    missing: tuple[int, ...] = ()
    size: int = 0
    path: Path | None = None

    @property
    def object_count(self) -> int:
        return len(self.objects)

    @property
    def in_use_entries(self) -> int:
        return sum(1 for entry in self.xref.values() if entry.in_use)

    def resolve(self, value):
        """Follow indirect references until a direct object is reached."""
        hops = 0
        while isinstance(value, Reference):
            value = self.objects.get(value.number)
            hops += 1
            if hops > MAX_DEPTH:
                raise PdfError("indirect reference chain does not terminate")
        return value

    def object_at(self, object_number: int):
        return self.objects.get(object_number)

    def catalog_raw(self) -> dict:
        root = self.resolve(self.trailer.get("Root"))
        return root if isinstance(root, dict) else {}

    def catalog(self) -> dict:
        return self.catalog_raw()

    def info(self) -> dict:
        info = self.resolve(self.trailer.get("Info"))
        return info if isinstance(info, dict) else {}

    def has(self, entry: str) -> bool:
        """True when the catalog or the trailer carries ``entry``."""
        return entry in self.catalog_raw() or entry in self.trailer

    def text(self, value) -> str:
        value = self.resolve(value)
        if isinstance(value, PdfString):
            return value.text()
        if isinstance(value, Name):
            return value.value
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        if value is None:
            return ""
        return str(value)

    def pages(self) -> tuple[dict, ...]:
        """Every page in reading order, walked through /Root -> /Pages -> /Kids."""
        root = self.resolve(self.trailer.get("Root"))
        if not isinstance(root, dict):
            raise PdfError("the trailer has no /Root dictionary")
        node = root.get("Pages")
        if node is None:
            raise PdfError("/Root has no /Pages entry")
        found: list[dict] = []
        self._walk_pages(node, found, 0)
        if not found:
            raise PdfError("the page tree contains no pages")
        return tuple(found)

    def _walk_pages(self, node, found: list[dict], depth: int) -> None:
        if depth > MAX_DEPTH:
            raise PdfError(f"the page tree is nested more than {MAX_DEPTH} levels deep")
        resolved = self.resolve(node)
        if not isinstance(resolved, dict):
            raise PdfError("a page tree node is not a dictionary")
        kids = self.resolve(resolved.get("Kids"))
        if kids is None:
            node_type = self.text(resolved.get("Type"))
            if node_type and node_type != "Page":
                raise PdfError(f"a /{node_type} node in the page tree has no /Kids")
            found.append(resolved)
            return
        if not isinstance(kids, list):
            raise PdfError("/Kids is not an array")
        for kid in kids:
            self._walk_pages(kid, found, depth + 1)

    def resources(self, page: dict) -> dict:
        """The page's resource dictionary, inherited from /Parent when it is absent."""
        node = page
        for _ in range(MAX_DEPTH):
            if not isinstance(node, dict):
                return {}
            resources = self.resolve(node.get("Resources"))
            if isinstance(resources, dict):
                return resources
            node = self.resolve(node.get("Parent"))
        return {}

    def as_dict(self) -> dict:
        return {
            "path": str(self.path) if self.path else None,
            "version": self.version,
            "size": self.size,
            "objects": self.object_count,
            "in_use_entries": self.in_use_entries,
            "mismatched_offsets": [item.as_dict() for item in self.mismatches],
            "missing_objects": list(self.missing),
        }


def _skip_space(data: bytes, pos: int) -> int:
    while pos < len(data):
        if data[pos] in WHITESPACE:
            pos += 1
        elif data[pos] == 0x25:
            while pos < len(data) and data[pos] not in b"\r\n":
                pos += 1
        else:
            return pos
    return pos


def _read_line(data: bytes, pos: int) -> tuple[bytes, int]:
    end = pos
    while end < len(data) and data[end] not in b"\r\n":
        end += 1
    line = data[pos:end]
    while end < len(data) and data[end] in b"\r\n":
        end += 1
    return line, end


def find_startxref(data: bytes) -> int:
    """Return the byte offset stored in the last ``startxref``, without validating it."""
    marker = data.rfind(STARTXREF)
    if marker < 0:
        raise PdfError("no startxref keyword: this file does not end like a PDF")
    tail = data[marker + len(STARTXREF) :].split()
    if not tail:
        raise PdfError("startxref is not followed by an offset")
    try:
        offset = int(tail[0])
    except ValueError as exc:
        raise PdfError(f"startxref is not a number: {tail[0]!r}") from exc
    if offset < 0 or offset >= len(data):
        raise PdfError(
            f"startxref points at byte {offset}, outside this {len(data)}-byte file"
        )
    return offset


def parse_xref(data: bytes, offset: int) -> tuple[dict[int, XrefEntry], dict]:
    """Parse the cross-reference table at ``offset`` and the trailer that follows it."""
    if data[offset : offset + 4] != b"xref":
        snippet = data[offset : offset + 12]
        if snippet[:1].isdigit():
            raise PdfError(
                f"startxref {offset} points at a cross-reference stream, which is not supported"
            )
        raise PdfError(f"startxref {offset} does not point at an xref table (found {snippet!r})")

    pos = offset + 4
    entries: dict[int, XrefEntry] = {}
    while True:
        pos = _skip_space(data, pos)
        if data[pos : pos + len(TRAILER)] == TRAILER:
            pos += len(TRAILER)
            break
        if pos >= len(data):
            raise PdfError("the xref table ends without a trailer")
        line, pos = _read_line(data, pos)
        section = XREF_SUBSECTION.match(line)
        if not section:
            raise PdfError(f"malformed xref subsection header at byte {pos}")
        first, count = int(section.group(1)), int(section.group(2))
        for index in range(count):
            line, pos = _read_line(data, pos)
            match = XREF_LINE.match(line)
            if not match:
                raise PdfError(f"malformed xref entry for object {first + index}")
            number = first + index
            entries[number] = XrefEntry(
                number=number,
                offset=int(match.group(1)),
                generation=int(match.group(2)),
                in_use=match.group(3) == b"n",
            )

    trailer_parser = Parser(data, pos)
    trailer = trailer_parser.parse()
    if not isinstance(trailer, dict):
        raise PdfError("the trailer is not a dictionary")
    return entries, trailer


def scan_object_offsets(data: bytes) -> dict[int, int]:
    """Find ``N G obj`` headers independently of the xref table.

    This is repair-scan territory: it is only consulted when a table entry does not hold what it
    promised, and a mismatch is always reported rather than silently accepted.
    """
    found: dict[int, int] = {}
    for match in OBJECT_HEADER.finditer(data):
        number = int(match.group(1))
        found.setdefault(number, match.start())
    return found


def _object_at(data: bytes, offset: int, expected_number: int):
    """Parse indirect object ``expected_number`` at ``offset``, or return None."""
    if offset < 0 or offset >= len(data):
        return None
    parser = Parser(data, offset)
    try:
        number = parser.parse()
        generation = parser.parse()
        if not isinstance(number, int) or not isinstance(generation, int):
            return None
        if number != expected_number:
            return None
        if parser.read_token() != b"obj":
            return None
        return parser.parse()
    except PdfError:
        return None


def parse_pdf(data: bytes, source: str = "<bytes>") -> Document:
    """Parse a PDF held in memory."""
    if not data:
        raise PdfError(f"{source}: the file is empty")
    header = data.find(HEADER_PREFIX, 0, 1024)
    if header < 0:
        raise PdfError(f"{source}: no %PDF- header in the first 1024 bytes")
    start = header + len(HEADER_PREFIX)
    version = data[start : start + 3].decode("latin-1")
    if not VERSION.fullmatch(version):
        raise PdfError(f"{source}: unreadable PDF version {version!r}")

    xref_offset = find_startxref(data)
    entries, trailer = parse_xref(data, xref_offset)
    scan = scan_object_offsets(data)

    objects: dict[int, object] = {}
    mismatches: list[OffsetMismatch] = []
    missing: list[int] = []
    for number in sorted(entries):
        entry = entries[number]
        if not entry.in_use:
            continue
        value = _object_at(data, entry.offset, number)
        if value is not None:
            objects[number] = value
            continue
        actual = scan.get(number)
        value = None if actual is None else _object_at(data, actual, number)
        if value is None:
            missing.append(number)
            continue
        objects[number] = value
        mismatches.append(OffsetMismatch(number, entry.offset, actual))

    return Document(
        version=version,
        trailer=trailer,
        objects=objects,
        xref=entries,
        mismatches=tuple(mismatches),
        missing=tuple(missing),
        size=len(data),
        path=Path(source) if source != "<bytes>" else None,
    )


def read_pdf(path: str | Path) -> Document:
    """Read and parse the PDF at ``path``."""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise PdfError(f"{target}: {exc.strerror or exc}") from exc
    return parse_pdf(data, source=str(target))
