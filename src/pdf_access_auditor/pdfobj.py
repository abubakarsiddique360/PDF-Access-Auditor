"""The PDF object model, plus a writer that emits byte-accurate cross-reference tables.

Two things live here and they are deliberately the same thing seen from both ends:

* the *values* a PDF is made of - names, strings, numbers, arrays, dictionaries and streams;
* a :class:`PdfBuilder` that appends objects to a growing byte buffer, remembering the offset at
  which each one landed, so the xref table it writes contains the offsets a reader will really
  find on disk.

Nothing about that is optional. A PDF whose xref offsets are off by one byte is a PDF that some
readers repair and other readers refuse, which is exactly why
:mod:`pdf_access_auditor.reader` re-derives the offsets independently and
:mod:`pdf_access_auditor.audit` reports a ``xref-offset-wrong`` finding when the two disagree.

The writer is also honest about its limits: the "embedded font" it can produce is a structurally
valid TrueType container with a single ``head`` table and no glyphs at all. It exists so the
auditor has a real ``/FontFile2`` stream to follow, not so that anything can render with it.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

HEADER_PREFIX = b"%PDF-"
DEFAULT_VERSION = "1.7"

# A PDF is a binary format, and a binary comment line right after the header is the standard way
# to stop tools (and terminals) from treating the file as text.
BINARY_COMMENT = b"%\xe2\xe3\xcf\xd3\n"

XREF_ENTRY_SIZE = 20
FREE_GENERATION = 65535

STRING_ESCAPES = {
    0x28: b"\\(",  # (
    0x29: b"\\)",  # )
    0x5C: b"\\\\",  # backslash
    0x0A: b"\\n",
    0x0D: b"\\r",
    0x09: b"\\t",
    0x08: b"\\b",
    0x0C: b"\\f",
}

# Characters that may appear unescaped inside a name, per PDF 32000-1 section 7.3.5.
NAME_SAFE = frozenset(range(0x21, 0x7F)) - frozenset(b"()<>[]{}/%#")


class PdfError(ValueError):
    """Raised when a PDF cannot be built, or cannot be understood."""


@dataclass(frozen=True)
class Name:
    """A PDF name, stored without the leading slash."""

    value: str

    def __str__(self) -> str:
        return "/" + self.value


@dataclass(frozen=True)
class PdfString:
    """A PDF string, kept as the raw bytes that were between the delimiters."""

    data: bytes

    def text(self) -> str:
        """Best-effort decode: UTF-8 first, latin-1 as the never-failing fallback."""
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError:
            return self.data.decode("latin-1")


@dataclass(frozen=True)
class Reference:
    """An indirect reference such as ``12 0 R``."""

    number: int
    generation: int = 0

    def __str__(self) -> str:
        return f"{self.number} {self.generation} R"


@dataclass(frozen=True)
class PdfStream:
    """A stream: its dictionary plus the raw (still encoded) payload bytes."""

    dictionary: dict
    data: bytes

    def get(self, key: str, default=None):
        return self.dictionary.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.dictionary

    def __getitem__(self, key: str):
        return self.dictionary[key]

    @property
    def value(self) -> dict:
        return self.dictionary

    def decoded(self) -> bytes:
        """Undo ``/Filter`` chains this project can produce or expect."""
        filters = self.dictionary.get("Filter")
        if filters is None:
            return self.data
        if isinstance(filters, Name):
            chain = [filters.value]
        elif isinstance(filters, list):
            chain = [item.value for item in filters if isinstance(item, Name)]
        else:
            raise PdfError(f"unreadable /Filter entry: {filters!r}")
        payload = self.data
        for entry in chain:
            if entry in ("FlateDecode", "Fl"):
                try:
                    payload = zlib.decompress(payload)
                except zlib.error as exc:
                    raise PdfError(f"corrupt Flate stream: {exc}") from exc
            else:
                raise PdfError(f"unsupported stream filter /{entry}")
        return payload


def mapping(value) -> dict | None:
    """The dictionary behind ``value``: a stream is a dictionary that also carries a payload."""
    if isinstance(value, PdfStream):
        return value.dictionary
    if isinstance(value, dict):
        return value
    return None


def number(value: int | float) -> bytes:
    """Serialise an integer or a float, without the ``3.0`` that PDF dislikes."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PdfError(f"not a number: {value!r}")
    if isinstance(value, int):
        return str(value).encode("ascii")
    if value == int(value):
        return str(int(value)).encode("ascii")
    return f"{value:.6f}".rstrip("0").rstrip(".").encode("ascii")


def name(value: str) -> bytes:
    """Serialise a name, escaping anything outside the safe set as ``#xx``."""
    out = bytearray(b"/")
    for byte in value.encode("utf-8"):
        if byte in NAME_SAFE:
            out.append(byte)
        else:
            out += b"#%02X" % byte
    return bytes(out)


def literal_string(text: str) -> bytes:
    """Serialise a literal string.

    Parentheses and backslashes are escaped because they end and restart the string, and every
    byte outside printable ASCII is written as a three-digit octal escape so the file stays
    byte-clean while the text itself stays UTF-8 (and therefore round-trips exactly).
    """
    out = bytearray(b"(")
    for byte in text.encode("utf-8"):
        escape = STRING_ESCAPES.get(byte)
        if escape is not None:
            out += escape
        elif byte < 0x20 or byte > 0x7E:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    out += b")"
    return bytes(out)


def hex_string(data: bytes) -> bytes:
    """Serialise a hex string."""
    return b"<" + data.hex().upper().encode("ascii") + b">"


def array(items) -> bytes:
    """Serialise an array of already-serialised items."""
    return b"[" + b" ".join(items) + b"]"


def _serialise(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, Name):
        return name(value.value)
    if isinstance(value, int | float):
        return number(value)
    if isinstance(value, str):
        return literal_string(value)
    if isinstance(value, Reference):
        return str(value).encode("ascii")
    if isinstance(value, list | tuple):
        return array([_serialise(item) for item in value])
    if isinstance(value, dict):
        return dictionary(value)
    raise PdfError(f"cannot serialise a {type(value).__name__}")


def dictionary(mapping: dict) -> bytes:
    """Serialise a dictionary; insertion order is kept so output stays deterministic."""
    if not mapping:
        return b"<< >>"
    parts = [name(key) + b" " + _serialise(value) for key, value in mapping.items()]
    return b"<< " + b" ".join(parts) + b" >>"


def stream(data: bytes, extra: dict | None = None, compress: bool = False) -> bytes:
    """Serialise a stream, with ``/Length`` computed from the bytes actually written."""
    payload = zlib.compress(data, 9) if compress else data
    entries: dict = dict(extra or {})
    if compress:
        entries["Filter"] = name("FlateDecode")
    entries["Length"] = str(len(payload)).encode("ascii")
    return dictionary(entries) + b"\nstream\n" + payload + b"\nendstream"


class PdfBuilder:
    """Collects numbered objects and serialises them with a real cross-reference table."""

    def __init__(self, version: str = DEFAULT_VERSION) -> None:
        self.version = version
        self._objects: list[bytes] = []
        self._catalog: int | None = None
        self._info: int | None = None

    def __len__(self) -> int:
        return len(self._objects)

    def add_object(self, body: bytes) -> int:
        """Append ``body`` and return its 1-based object number."""
        if not isinstance(body, bytes):
            raise PdfError("object bodies must be bytes")
        self._objects.append(body)
        return len(self._objects)

    def reserve(self) -> int:
        """Claim an object number now, fill it in later with :meth:`set_object`."""
        self._objects.append(b"")
        return len(self._objects)

    def set_object(self, object_number: int, body: bytes) -> None:
        if not 1 <= object_number <= len(self._objects):
            raise PdfError(f"object {object_number} was never reserved")
        self._objects[object_number - 1] = body

    def object_body(self, object_number: int) -> bytes:
        if not 1 <= object_number <= len(self._objects):
            raise PdfError(f"object {object_number} does not exist")
        return self._objects[object_number - 1]

    def set_catalog(self, object_number: int) -> None:
        self._catalog = object_number

    def set_info(self, object_number: int) -> None:
        self._info = object_number

    def to_bytes(self) -> bytes:
        """Serialise header, objects, xref, trailer, startxref and the EOF marker."""
        if self._catalog is None:
            raise PdfError("no catalog was set, the trailer would have no /Root")
        out = bytearray(HEADER_PREFIX + self.version.encode("ascii") + b"\n")
        out += BINARY_COMMENT

        offsets: list[int] = []
        for index, body in enumerate(self._objects, start=1):
            if not body:
                raise PdfError(f"object {index} was reserved but never filled in")
            offsets.append(len(out))
            out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

        xref_offset = len(out)
        size = len(self._objects) + 1
        out += b"xref\n" + f"0 {size}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            entry = f"{offset:010d} 00000 n \n".encode("ascii")
            if len(entry) != XREF_ENTRY_SIZE:
                raise PdfError(f"xref entry for offset {offset} is not {XREF_ENTRY_SIZE} bytes")
            out += entry

        trailer = b"<< /Size " + str(size).encode("ascii")
        trailer += b" /Root " + str(Reference(self._catalog)).encode("ascii")
        if self._info is not None:
            trailer += b" /Info " + str(Reference(self._info)).encode("ascii")
        trailer += b" >>"
        out += b"trailer\n" + trailer + b"\n"
        out += b"startxref\n" + str(xref_offset).encode("ascii") + b"\n%%EOF\n"
        return bytes(out)

    def write(self, path: str | Path) -> int:
        """Write the document and return its size in bytes."""
        payload = self.to_bytes()
        target = Path(path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return len(payload)


# --------------------------------------------------------------------------------------------
# Document assembly
# --------------------------------------------------------------------------------------------

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
IMAGE_SIDE = 8


@dataclass(frozen=True)
class DocumentSpec:
    """Everything that can vary between two generated documents.

    Empty ``lang`` and empty ``title`` mean "leave the entry out entirely", which is how the
    failing examples are made: the audit rules only ever look at what is present.
    """

    title: str = "PDF-Access-Auditor sample"
    lang: str = "en"
    tagged: bool = True
    alt_text: bool = True
    embedded_font: bool = False
    pages: int = 1
    alt: str = "A greyscale test image"
    author: str = "PDF-Access-Auditor"

    def page_text(self, index: int) -> str:
        return f"Page {index + 1} of {self.pages}"


def font_file_data(size: int = 512) -> bytes:
    """Build a structurally plausible TrueType file containing one ``head`` table.

    There are no glyphs, no ``cmap`` and no outlines. The offset table, the table directory, the
    54-byte ``head`` table, its magic number and its big-endian checksum are all real, because
    those are the bytes a PDF reader - and this project's auditor - actually walks over.
    """
    head = struct.pack(
        ">HHIIIHHqqhhhhHHhhh",
        1, 0, 0x00010000, 0, 0x5F0F3CF5, 3, 1000,
        0, 0,
        -50, -200, 1050, 800,
        0, 8, 2, 0, 0,
    )
    if len(head) != 54:
        raise PdfError("the head table must be 54 bytes")
    checksum = sum(
        int.from_bytes(head[offset : offset + 4].ljust(4, b"\x00"), "big")
        for offset in range(0, len(head), 4)
    ) & 0xFFFFFFFF
    directory_offset = 12 + 16
    record = b"head" + struct.pack(">III", checksum, directory_offset, len(head))
    header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    blob = header + record + head
    if len(blob) < size:
        blob += b"\x00" * (size - len(blob))
    return blob


def _font_file_stream() -> bytes:
    payload = font_file_data()
    return stream(
        payload,
        {"Length1": str(len(payload)).encode("ascii")},
        compress=True,
    )


def _font_descriptor(font_file_id: int) -> bytes:
    return dictionary(
        {
            "Type": name("FontDescriptor"),
            "FontName": name("AuditSans"),
            "Flags": 32,
            "FontBBox": [b"0", b"-200", b"1000", b"800"],
            "ItalicAngle": 0,
            "Ascent": 800,
            "Descent": -200,
            "CapHeight": 700,
            "StemV": 80,
            "FontFile2": Reference(font_file_id),
        }
    )


def _embedded_font(descriptor_id: int) -> bytes:
    widths = [b"600"] * 95
    return dictionary(
        {
            "Type": name("Font"),
            "Subtype": name("TrueType"),
            "BaseFont": name("AuditSans"),
            "FirstChar": 32,
            "LastChar": 126,
            "Widths": widths,
            "FontDescriptor": Reference(descriptor_id),
            "Encoding": name("WinAnsiEncoding"),
        }
    )


def _helvetica_font() -> bytes:
    return dictionary(
        {
            "Type": name("Font"),
            "Subtype": name("Type1"),
            "BaseFont": name("Helvetica"),
            "Encoding": name("WinAnsiEncoding"),
        }
    )


def _image_xobject(alt_text: bool, alt: str) -> bytes:
    side = IMAGE_SIDE
    pixels = bytes((x * 17 + y * 23) % 256 for y in range(side) for x in range(side))
    entries: dict = {
        "Type": name("XObject"),
        "Subtype": name("Image"),
        "Width": side,
        "Height": side,
        "ColorSpace": name("DeviceGray"),
        "BitsPerComponent": 8,
    }
    if alt_text:
        entries["Alt"] = literal_string(alt)
    return stream(pixels, entries)


def _contents(spec: DocumentSpec, index: int) -> bytes:
    lines = [
        b"BT /F1 18 Tf 72 770 Td " + literal_string(spec.page_text(index)) + b" Tj ET",
        b"BT /F1 11 Tf 72 740 Td (Written by pdf_access_auditor.pdfobj) Tj ET",
    ]
    if index == 0:
        size = IMAGE_SIDE * 12
        lines.append(f"q {size} 0 0 {size} 72 600 cm /Im1 Do Q".encode("ascii"))
    return b"\n".join(lines) + b"\n"


def _page(pages_id: int, content_id: int, font_id: int, image_id: int | None) -> bytes:
    resources: dict = {"Font": {"F1": Reference(font_id)}}
    if image_id is not None:
        resources["XObject"] = {"Im1": Reference(image_id)}
    return dictionary(
        {
            "Type": name("Page"),
            "Parent": Reference(pages_id),
            "MediaBox": [b"0", b"0", str(PAGE_WIDTH).encode("ascii"),
                         str(PAGE_HEIGHT).encode("ascii")],
            "Resources": resources,
            "Contents": Reference(content_id),
        }
    )


def _pages(kids: list[int]) -> bytes:
    return dictionary(
        {
            "Type": name("Pages"),
            "Kids": [Reference(kid) for kid in kids],
            "Count": len(kids),
        }
    )


def _catalog(pages_id: int, lang: str, struct_root_id: int | None) -> bytes:
    entries: dict = {"Type": name("Catalog"), "Pages": Reference(pages_id)}
    if lang:
        entries["Lang"] = literal_string(lang)
    if struct_root_id is not None:
        entries["MarkInfo"] = {"Marked": True}
        entries["StructTreeRoot"] = Reference(struct_root_id)
    return dictionary(entries)


def _struct_tree_root(struct_elem_id: int) -> bytes:
    return dictionary(
        {
            "Type": name("StructTreeRoot"),
            "K": [Reference(struct_elem_id)],
        }
    )


def _struct_elem(root_id: int, page_id: int) -> bytes:
    return dictionary(
        {
            "Type": name("StructElem"),
            "S": name("Document"),
            "P": Reference(root_id),
            "Pg": Reference(page_id),
        }
    )


def _info(spec: DocumentSpec) -> bytes:
    entries: dict = {}
    if spec.title:
        entries["Title"] = literal_string(spec.title)
    if spec.author:
        entries["Author"] = literal_string(spec.author)
    entries["Producer"] = literal_string("PDF-Access-Auditor 1.0")
    return dictionary(entries)


def build_document(spec: DocumentSpec) -> bytes:
    """Assemble a complete PDF for ``spec`` and return its bytes."""
    if spec.pages < 1:
        raise PdfError("a document needs at least one page")

    builder = PdfBuilder()
    catalog_id = builder.reserve()
    pages_id = builder.reserve()
    page_ids = [builder.reserve() for _ in range(spec.pages)]
    content_ids = [builder.reserve() for _ in range(spec.pages)]

    if spec.embedded_font:
        font_file_id = builder.add_object(_font_file_stream())
        descriptor_id = builder.add_object(_font_descriptor(font_file_id))
        font_id = builder.add_object(_embedded_font(descriptor_id))
    else:
        font_id = builder.add_object(_helvetica_font())

    image_id = builder.add_object(_image_xobject(spec.alt_text, spec.alt))

    struct_root_id: int | None = None
    if spec.tagged:
        struct_root_id = builder.reserve()
        struct_elem_id = builder.add_object(_struct_elem(struct_root_id, page_ids[0]))
        builder.set_object(struct_root_id, _struct_tree_root(struct_elem_id))

    info_id = builder.add_object(_info(spec))

    for index, page_id in enumerate(page_ids):
        builder.set_object(
            page_id,
            _page(pages_id, content_ids[index], font_id, image_id if index == 0 else None),
        )
        builder.set_object(content_ids[index], stream(_contents(spec, index), compress=True))

    builder.set_object(pages_id, _pages(page_ids))
    builder.set_object(catalog_id, _catalog(pages_id, spec.lang, struct_root_id))
    builder.set_catalog(catalog_id)
    builder.set_info(info_id)
    return builder.to_bytes()


def write_document(path: str | Path, spec: DocumentSpec) -> int:
    """Build ``spec`` and write it to ``path``; return the byte count."""
    payload = build_document(spec)
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return len(payload)
