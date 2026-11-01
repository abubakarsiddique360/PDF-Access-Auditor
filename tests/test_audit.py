"""Every rule, both ways: it has to trigger on a real defect and stay quiet on a clean file."""

import json
import re

import pytest
from conftest import EXAMPLES, GOOD_SPEC, patch_xref, xref_offsets

from pdf_access_auditor.audit import (
    RULES,
    SEVERITY_ORDER,
    SEVERITY_POINTS,
    Audit,
    Finding,
    audit_bytes,
    audit_document,
    grade_for,
    rule_codes,
    rule_table,
)
from pdf_access_auditor.pdfobj import DocumentSpec, build_document
from pdf_access_auditor.reader import parse_pdf, read_pdf


def blank(data: bytes, pattern: bytes) -> bytes:
    """Erase a construct while keeping every byte offset in the file unchanged."""
    return re.sub(pattern, lambda match: b" " * len(match.group(0)), data, count=1)


def audit_of(data: bytes) -> Audit:
    return audit_document(parse_pdf(data))


def codes(audit: Audit) -> set[str]:
    return {item.code for item in audit.findings}


def test_missing_lang_triggers_and_declared_lang_does_not():
    missing = audit_of(build_document(DocumentSpec(lang="")))
    assert "missing-lang" in codes(missing)
    assert missing.scopes("missing-lang") == ("catalog",)
    assert "missing-lang" not in codes(audit_of(build_document(DocumentSpec(lang="en-GB"))))


def test_untagged_triggers_and_either_marker_is_enough():
    assert "untagged" in codes(audit_of(build_document(DocumentSpec(tagged=False))))
    tagged = build_document(DocumentSpec(tagged=True))
    without_struct_tree = blank(tagged, rb"/StructTreeRoot \d+ 0 R")
    without_mark_info = blank(tagged, rb"/MarkInfo << /Marked true >>")
    assert "untagged" not in codes(audit_of(without_struct_tree))
    assert "untagged" not in codes(audit_of(without_mark_info))


def test_image_without_alt_triggers_and_alt_text_does_not():
    missing = audit_of(build_document(DocumentSpec(alt_text=False)))
    assert missing.scopes("image-without-alt") == ("page 1 /Im1",)
    assert missing.counts["critical"] == 1
    assert "image-without-alt" not in codes(audit_of(build_document(DocumentSpec(alt_text=True))))


def test_artifact_image_is_exempt():
    data = build_document(DocumentSpec(alt_text=False))
    original = b"/ColorSpace /DeviceGray"
    replacement = b"/Artifact true"
    marked = data.replace(original, replacement + b" " * (len(original) - len(replacement)), 1)
    assert len(marked) == len(data)
    audit = audit_of(marked)
    assert "image-without-alt" not in codes(audit)
    assert "xref-offset-wrong" not in codes(audit)


def test_font_not_embedded_triggers_and_embedded_font_does_not():
    missing = audit_of(build_document(DocumentSpec(embedded_font=False)))
    assert missing.scopes("font-not-embedded") == ("page 1 /F1",)
    embedded = audit_of(build_document(DocumentSpec(embedded_font=True)))
    assert "font-not-embedded" not in codes(embedded)


def test_one_font_shared_by_three_pages_is_reported_once():
    audit = audit_of(build_document(DocumentSpec(pages=3)))
    assert len(audit.scopes("font-not-embedded")) == 1


def test_missing_title_triggers_and_a_real_title_does_not():
    assert audit_of(build_document(DocumentSpec(title=""))).scopes("missing-title") == ("info",)
    assert "missing-title" in codes(audit_of(build_document(DocumentSpec(title="   "))))
    assert "missing-title" not in codes(audit_of(build_document(DocumentSpec(title="A title"))))


def test_page_tree_can_be_walked_or_reported():
    broken = blank(build_document(DocumentSpec(pages=1)), rb"/Pages \d+ 0 R")
    audit = audit_of(broken)
    assert audit.scopes("no-page-tree") == ("catalog",)
    message = next(item.message for item in audit.findings if item.code == "no-page-tree")
    assert "/Pages" in message
    assert "no-page-tree" not in codes(audit_of(build_document(DocumentSpec(pages=2))))


def test_xref_offset_wrong_triggers_and_correct_offsets_never_do():
    good = build_document(DocumentSpec())
    first = xref_offsets(good)[1]
    audit = audit_of(patch_xref(good, 1, first + 5))
    assert audit.scopes("xref-offset-wrong") == ("object 1",)
    assert audit.counts["critical"] == 1
    assert "xref-offset-wrong" not in codes(audit_of(build_document(DocumentSpec(pages=2))))


def test_grade_boundaries():
    expected = [
        (0, "A"), (1, "B"), (10, "B"), (11, "C"),
        (30, "C"), (31, "D"), (60, "D"), (61, "F"),
    ]
    for risk, grade in expected:
        assert grade_for(risk) == grade, risk


def test_severity_weights_match_the_documented_table():
    assert SEVERITY_POINTS == {"critical": 30, "high": 15, "medium": 8, "low": 3}
    assert SEVERITY_ORDER == ("critical", "high", "medium", "low")


def test_risk_is_the_sum_of_the_sorted_findings():
    audit = audit_of(build_document(DocumentSpec(title="", lang="", tagged=False, alt_text=False)))
    assert [item.code for item in audit.findings] == [
        "image-without-alt",
        "missing-lang",
        "untagged",
        "missing-title",
        "font-not-embedded",
    ]
    assert audit.risk == sum(SEVERITY_POINTS[item.severity] for item in audit.findings) == 76
    assert audit.grade == "F"
    assert audit.counts == {"critical": 1, "high": 2, "medium": 2, "low": 0}
    assert sum(audit.counts.values()) == len(audit.findings)


def test_finding_and_audit_as_dict():
    finding = Finding("x", "low", "scope", "message", "hint")
    assert finding.points == 3
    assert finding.as_dict()["points"] == 3
    audit = audit_of(build_document(DocumentSpec(lang="")))
    payload = json.loads(json.dumps(audit.as_dict()))
    assert payload["risk"] == audit.risk
    assert payload["findings"][0]["code"] == "missing-lang"


def test_clean_audit_and_at_or_above():
    clean = audit_of(build_document(GOOD_SPEC))
    assert clean.clean is True
    assert (clean.risk, clean.grade) == (0, "A")
    assert clean.at_or_above("critical") is False
    low = Audit.from_findings([Finding("x", "low", "s", "m", "h")])
    assert low.at_or_above("low") is True
    assert low.at_or_above("medium") is False
    with pytest.raises(ValueError):
        low.at_or_above("nope")


def test_should_fail_thresholds():
    audit = audit_of(build_document(DocumentSpec(title="", lang="", tagged=False)))
    for threshold, expected in [
        ("never", False),
        ("critical", False),
        ("high", True),
        ("medium", True),
        ("low", True),
    ]:
        assert audit.should_fail(threshold) is expected, threshold
    with pytest.raises(ValueError):
        audit.should_fail("sometimes")


def test_rule_catalogue_covers_the_seven_documented_rules():
    assert rule_codes() == (
        "image-without-alt",
        "xref-offset-wrong",
        "missing-lang",
        "untagged",
        "no-page-tree",
        "font-not-embedded",
        "missing-title",
    )
    assert {rule.code: rule.severity for rule in RULES} == {
        "missing-lang": "high",
        "untagged": "high",
        "image-without-alt": "critical",
        "font-not-embedded": "medium",
        "missing-title": "medium",
        "xref-offset-wrong": "critical",
        "no-page-tree": "high",
    }


def test_rule_table_is_json_serialisable_and_audit_bytes_matches():
    payload = json.loads(json.dumps(rule_table()))
    assert len(payload) == 7
    assert all("points" in row and "hint" in row for row in payload)
    data = build_document(DocumentSpec(lang=""))
    assert audit_bytes(data).as_dict() == audit_of(data).as_dict()


def test_example_good_grades_a():
    audit = audit_document(read_pdf(EXAMPLES / "good.pdf"))
    assert audit.clean
    assert (audit.risk, audit.grade) == (0, "A")


def test_example_bad_grades_f():
    audit = audit_document(read_pdf(EXAMPLES / "bad.pdf"))
    assert audit.grade == "F"
    assert audit.risk > 60
    assert codes(audit) == {
        "image-without-alt",
        "missing-lang",
        "untagged",
        "missing-title",
        "font-not-embedded",
    }


def test_example_legacy_grades_between_the_two():
    audit = audit_document(read_pdf(EXAMPLES / "legacy.pdf"))
    assert audit.grade == "D"
    assert codes(audit) == {"missing-lang", "untagged", "font-not-embedded"}
