"""The writer and the reader, tested against each other.

The point of this file is the byte offsets: if the xref table and the objects ever disagree, these
tests fail before an audit result can hide the problem.
"""

import struct

import pytest
from conftest import patch_xref, xref_offsets, xref_trailer

from pdf_access_auditor.pdfobj import (
    DocumentSpec,
    Name,
    PdfBuilder,
    PdfError,
    PdfStream,
    PdfString,
    Reference,
    array,
    build_document,
    dictionary,
    font_file_data,
    hex_string,
    literal_string,
    name,
    number,
    stream,
    write_document,
)
from pdf_access_auditor.reader import (
    Parser,
    find_startxref,
    parse_pdf,
    read_pdf,
    scan_object_offsets,
)

# --- value serialisation ---------------------------------------------------------------------


def test_literal_string_escapes_the_reserved_characters():
    assert literal_string("a(b)c") == b"(a\\(b\\)c)"
    assert literal_string("back\\slash") == b"(back\\\\slash)"
    assert literal_string("a\nb\tc") == b"(a\\nb\\tc)"


def test_literal_string_escapes_non_ascii_as_octal():
    encoded = literal_string("café")
    assert encoded.startswith(b"(caf\\3")
    assert encoded.isascii()


@pytest.mark.parametrize("text", ["", "a(b)c", "back\\slash", "café — ünïcode"])
def test_literal_string_round_trip(text):
    assert Parser(literal_string(text)).parse().text() == text


def test_hex_string_round_trip_and_padding():
    assert hex_string(b"Hi") == b"<4869>"
    assert Parser(hex_string(b"\x00\xff")).parse().data == b"\x00\xff"
    assert Parser(b"<4869F>").parse().data == b"Hi\xf0"


def test_name_serialisation_and_escaping():
    assert name("F1") == b"/F1"
    assert name("A#B") == b"/A#23B"
    assert Parser(name("A#B")).parse() == Name("A#B")


def test_number_serialisation():
    assert number(3) == b"3"
    assert number(3.0) == b"3"
    assert number(2.5) == b"2.5"
    for value in (True, "3", None):
        with pytest.raises(PdfError):
            number(value)


def test_array_and_dictionary_serialisation():
    assert array([b"1", b"2"]) == b"[1 2]"
    assert dictionary({"Type": name("Catalog")}) == b"<< /Type /Catalog >>"
    assert dictionary({}) == b"<< >>"
    nested = dictionary({"Kids": [Reference(3), Reference(4)], "Count": 2})
    assert nested == b"<< /Kids [3 0 R 4 0 R] /Count 2 >>"


def test_stream_length_is_measured_not_guessed():
    body = stream(b"hello")
    assert b"/Length 5" in body
    assert body.endswith(b"stream\nhello\nendstream")


def test_compressed_stream_declares_its_filter_and_round_trips():
    body = stream(b"hello" * 100, {"Length1": b"500"}, compress=True)
    assert b"/Filter /FlateDecode" in body
    parsed = Parser(body).parse()
    assert isinstance(parsed, PdfStream)
    assert parsed["Length1"] == 500
    assert parsed.decoded() == b"hello" * 100


# --- the builder -----------------------------------------------------------------------------


def test_builder_numbers_objects_and_fills_reservations():
    builder = PdfBuilder()
    assert builder.add_object(b"<< /A 1 >>") == 1
    assert builder.add_object(b"<< /A 2 >>") == 2
    reserved = builder.reserve()
    assert builder.object_body(reserved) == b""
    builder.set_object(reserved, b"<< /Type /Catalog >>")
    assert builder.object_body(reserved) == b"<< /Type /Catalog >>"
    assert len(builder) == 3
    with pytest.raises(PdfError):
        builder.set_object(99, b"2")


def test_builder_refuses_incomplete_documents():
    builder = PdfBuilder()
    builder.add_object(b"<< /Type /Pages >>")
    with pytest.raises(PdfError):
        builder.to_bytes()
    builder.set_catalog(builder.reserve())
    with pytest.raises(PdfError):
        builder.to_bytes()


# --- a whole document ------------------------------------------------------------------------


def test_header_binary_comment_and_eof(good_bytes):
    assert good_bytes.startswith(b"%PDF-1.7\n")
    assert good_bytes[9:14] == b"%\xe2\xe3\xcf\xd3"
    assert good_bytes.rstrip().endswith(b"%%EOF")


def test_trailer_has_size_root_and_info(good_bytes):
    trailer = xref_trailer(good_bytes)
    assert trailer["Size"] == len(xref_offsets(good_bytes)) + 1
    assert isinstance(trailer["Root"], Reference)
    assert isinstance(trailer["Info"], Reference)


def test_every_xref_offset_is_byte_accurate(good_bytes):
    offsets = xref_offsets(good_bytes)
    assert offsets
    for object_number, offset in offsets.items():
        header = f"{object_number} 0 obj".encode()
        assert good_bytes[offset : offset + len(header)] == header


def test_xref_entries_are_twenty_bytes_and_startxref_points_at_xref(good_bytes):
    offset = find_startxref(good_bytes)
    assert good_bytes[offset : offset + 4] == b"xref"
    body = good_bytes[offset:]
    start = body.index(b"\n") + 1
    start = body.index(b"\n", start) + 1
    for index in range(len(xref_offsets(good_bytes))):
        entry = body[start + index * 20 : start + index * 20 + 20]
        assert len(entry) == 20
        assert entry.endswith(b" \n")


def test_written_pdf_round_trips(good_bytes):
    document = parse_pdf(good_bytes)
    assert document.version == "1.7"
    assert document.object_count == len(xref_offsets(good_bytes))
    assert document.in_use_entries == document.object_count
    assert document.mismatches == ()
    assert document.missing == ()
    assert len(document.pages()) == 2


def test_reader_resolves_every_object_the_table_promises(good_bytes):
    document = parse_pdf(good_bytes)
    assert set(document.objects) == set(xref_offsets(good_bytes))
    assert all(value is not None for value in document.objects.values())


def test_catalog_info_and_text_helpers(good_bytes):
    document = parse_pdf(good_bytes)
    catalog = document.catalog()
    assert catalog["Type"] == Name("Catalog")
    assert document.text(catalog["Lang"]) == "en"
    assert document.text(Name("Page")) == "Page"
    assert document.text(PdfString(b"hello")) == "hello"
    assert document.text(None) == ""
    assert document.has("Root") is True
    assert document.has("Lang") is True
    assert document.has("NoSuchEntry") is False
    assert document.text(document.info()["Title"]) == "Accessible quarterly report"


def test_pages_keep_kids_order():
    document = parse_pdf(build_document(DocumentSpec(pages=3)))
    pages = document.pages()
    contents = [document.resolve(page["Contents"]) for page in pages]
    assert len(pages) == 3
    assert b"Page 1 of 3" in contents[0].decoded()
    assert b"Page 3 of 3" in contents[2].decoded()


def test_page_media_box_and_resources(good_bytes):
    document = parse_pdf(good_bytes)
    page = document.pages()[0]
    assert document.resolve(page["MediaBox"]) == [0, 0, 595, 842]
    resources = document.resources(page)
    assert "Font" in resources
    assert "XObject" in resources


def test_resources_are_inherited_from_parent():
    builder = PdfBuilder()
    catalog_id = builder.reserve()
    pages_id = builder.reserve()
    font_id = builder.add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_id = builder.add_object(
        f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 10 10] >>".encode()
    )
    builder.set_object(
        pages_id,
        f"<< /Type /Pages /Kids [{page_id} 0 R] /Count 1 "
        f"/Resources << /Font << /F9 {font_id} 0 R >> >> >>".encode(),
    )
    builder.set_object(catalog_id, f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    builder.set_catalog(catalog_id)
    document = parse_pdf(builder.to_bytes())
    page = document.pages()[0]
    assert "Resources" not in page
    assert "Font" in document.resources(page)


def test_embedded_font_has_a_plausible_font_file(good_bytes):
    document = parse_pdf(good_bytes)
    page = document.pages()[0]
    font = document.resolve(document.resolve(page["Resources"])["Font"]["F1"])
    descriptor = document.resolve(font["FontDescriptor"])
    font_file = document.resolve(descriptor["FontFile2"])
    payload = font_file.decoded()
    assert len(payload) >= 512
    assert payload[:4] == b"\x00\x01\x00\x00"
    assert struct.pack(">I", 0x5F0F3CF5) in payload
    assert font_file["Length1"] == len(payload)


def test_font_file_checksum_is_real_true_type_arithmetic():
    payload = font_file_data()
    head_offset = int.from_bytes(payload[20:24], "big")
    head_length = int.from_bytes(payload[24:28], "big")
    head = payload[head_offset : head_offset + head_length]
    total = sum(
        int.from_bytes(head[index : index + 4], "big") for index in range(0, len(head), 4)
    ) & 0xFFFFFFFF
    assert int.from_bytes(payload[16:20], "big") == total


def test_untagged_document_has_no_structure_tree(bad_bytes):
    catalog = parse_pdf(bad_bytes).catalog()
    assert "StructTreeRoot" not in catalog
    assert "MarkInfo" not in catalog
    assert "Lang" not in catalog


def test_document_needs_at_least_one_page_and_writes_directories(tmp_path):
    with pytest.raises(PdfError):
        build_document(DocumentSpec(pages=0))
    target = tmp_path / "nested" / "deeper" / "doc.pdf"
    size = write_document(target, DocumentSpec(pages=1))
    assert size == target.stat().st_size
    document = read_pdf(target)
    assert document.path == target
    assert len(document.pages()) == 1
    with pytest.raises(PdfError):
        read_pdf(tmp_path / "absent.pdf")


# --- the lexer's tolerance -------------------------------------------------------------------


def test_comments_are_skipped_and_unknown_constructs_become_strings():
    assert Parser(b"% a comment\n42").parse() == 42
    assert Parser(b"weird!token").parse() == PdfString(b"weird!token")
    assert Parser(b"<< /Widget zzz >>").parse()["Widget"] == PdfString(b"zzz")


def test_nested_structures_and_scalars_parse():
    parsed = Parser(b"<< /A [1 2 << /B (x) >>] /C 3 0 R >>").parse()
    assert parsed["A"][0] == 1
    assert parsed["A"][2] == {"B": PdfString(b"x")}
    assert parsed["C"] == Reference(3)
    assert Parser(b"[3 0]").parse() == [3, 0]
    assert Parser(b"[true false null]").parse() == [True, False, None]
    assert Parser(b"/A#20B").parse() == Name("A B")


def test_lenient_stream_without_a_usable_length():
    parsed = Parser(b"<< /NoLength 1 >>\nstream\nabc\nendstream").parse()
    assert isinstance(parsed, PdfStream)
    assert parsed.data == b"abc"


# --- the reader's honesty checks -------------------------------------------------------------


def test_scan_finds_every_object_header(good_bytes):
    scanned = scan_object_offsets(good_bytes)
    assert set(scanned) == set(xref_offsets(good_bytes))
    for object_number, offset in scanned.items():
        header = f"{object_number} 0 obj".encode()
        assert good_bytes[offset : offset + len(header)] == header


def test_patched_xref_offset_is_reported_but_still_opens(good_bytes):
    first = xref_offsets(good_bytes)[1]
    broken = patch_xref(good_bytes, 1, first + 5)
    document = parse_pdf(broken)
    assert len(broken) == len(good_bytes)
    assert len(document.mismatches) == 1
    mismatch = document.mismatches[0]
    assert (mismatch.number, mismatch.expected, mismatch.actual) == (1, first + 5, first)
    assert document.catalog()["Type"] == Name("Catalog")
    assert len(document.pages()) == 2


def test_files_that_are_not_pdfs_are_rejected():
    for payload in (b"", b"not a pdf at all"):
        with pytest.raises(PdfError):
            parse_pdf(payload)


def test_a_broken_startxref_is_reported_clearly(good_bytes):
    marker = good_bytes.rindex(b"startxref")
    broken = good_bytes[:marker] + b"startxref\n5\n%%EOF\n"
    with pytest.raises(PdfError) as caught:
        parse_pdf(broken)
    assert "xref" in str(caught.value)


def test_truncated_and_malformed_tables_are_rejected(good_bytes):
    with pytest.raises(PdfError):
        parse_pdf(good_bytes[: good_bytes.index(b"xref")])
    malformed = good_bytes.replace(b"0000000000 65535 f", b"0000000000 65535 x", 1)
    with pytest.raises(PdfError):
        parse_pdf(malformed)
