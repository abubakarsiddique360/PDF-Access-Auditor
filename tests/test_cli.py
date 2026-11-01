"""The command line and every report format."""

import json

import pytest
from conftest import EXAMPLES, GOOD_SPEC

from pdf_access_auditor.audit import audit_document
from pdf_access_auditor.cli import main
from pdf_access_auditor.pdfobj import build_document
from pdf_access_auditor.reader import parse_pdf, read_pdf
from pdf_access_auditor.report import (
    render_audit,
    render_checks,
    render_document,
    render_json,
    render_markdown,
    render_rules,
)


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert "pdf-audit 1.0.0" in capsys.readouterr().out


def test_write_generates_a_clean_pdf(tmp_path, capsys):
    out = tmp_path / "made.pdf"
    assert main(["write", "--out", str(out), "--embed-font", "true"]) == 0
    output = capsys.readouterr().out
    assert "wrote" in output
    assert "grade A" in output
    document = parse_pdf(out.read_bytes())
    assert len(document.pages()) == 1
    assert audit_document(document).clean


def test_write_honours_the_flags(tmp_path, capsys):
    out = tmp_path / "bad.pdf"
    argv = [
        "write", "--out", str(out), "--lang", "none", "--title", "none",
        "--tagged", "false", "--alt", "false", "--pages", "2",
    ]
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert "2 page(s)" in output
    assert "image-without-alt" in output
    audit = audit_document(read_pdf(out))
    assert audit.grade == "F"
    assert {"missing-lang", "missing-title"} <= {item.code for item in audit.findings}


def test_write_rejects_zero_pages(tmp_path, capsys):
    assert main(["write", "--out", str(tmp_path / "x.pdf"), "--pages", "0"]) == 2
    assert "error:" in capsys.readouterr().err


def test_write_rejects_a_non_boolean(tmp_path):
    with pytest.raises(SystemExit) as caught:
        main(["write", "--out", str(tmp_path / "x.pdf"), "--tagged", "maybe"])
    assert caught.value.code == 2


def test_audit_text_report_and_exit_codes(capsys):
    assert main(["audit", "--file", str(EXAMPLES / "bad.pdf")]) == 1
    output = capsys.readouterr().out
    assert "PDF accessibility audit" in output
    assert "fix:" in output
    assert "grade F" in output
    assert main(["audit", "--file", str(EXAMPLES / "good.pdf")]) == 0
    assert "no findings" in capsys.readouterr().out


def test_audit_markdown_format(capsys):
    assert main(["audit", "--file", str(EXAMPLES / "legacy.pdf"), "--format", "markdown"]) == 1
    output = capsys.readouterr().out
    assert "| Severity | Code | Scope | What is wrong | Fix |" in output
    assert "`missing-lang`" in output


def test_audit_json_format(capsys):
    assert main(["audit", "--file", str(EXAMPLES / "good.pdf"), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["risk"] == 0
    assert payload["grade"] == "A"
    assert payload["findings"] == []
    assert payload["version"] == "1.7"


def test_audit_fail_on_never_and_critical(capsys):
    assert main(["audit", "--file", str(EXAMPLES / "bad.pdf"), "--fail-on", "never"]) == 0
    assert main(["audit", "--file", str(EXAMPLES / "bad.pdf"), "--fail-on", "critical"]) == 1
    assert main(["audit", "--file", str(EXAMPLES / "legacy.pdf"), "--fail-on", "critical"]) == 0
    assert main(["audit", "--file", str(EXAMPLES / "legacy.pdf"), "--fail-on", "medium"]) == 1
    capsys.readouterr()


def test_audit_unreadable_inputs_exit_two(tmp_path, capsys):
    assert main(["audit", "--file", str(tmp_path / "gone.pdf")]) == 2
    assert "error:" in capsys.readouterr().err
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"definitely not a pdf")
    assert main(["audit", "--file", str(junk)]) == 2
    assert "error:" in capsys.readouterr().err


def test_info_subcommand(capsys):
    assert main(["info", "--file", str(EXAMPLES / "legacy.pdf")]) == 0
    output = capsys.readouterr().out
    assert "PDF structure" in output
    assert "Legacy vendor export" in output
    assert "tagged          no" in output


def test_checks_subcommand(capsys):
    assert main(["checks", "--file", str(EXAMPLES / "bad.pdf")]) == 0
    output = capsys.readouterr().out
    assert "PASS" in output
    assert "FAIL" in output
    assert "of 7 rules pass" in output


def test_rules_subcommand(capsys):
    assert main(["rules"]) == 0
    output = capsys.readouterr().out
    for code in ("image-without-alt", "xref-offset-wrong", "missing-title"):
        assert code in output
    assert "grade: 0 = A" in output


def test_no_subcommand_is_an_error():
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2


def test_render_audit_groups_by_severity(tmp_path):
    document = parse_pdf(build_document(GOOD_SPEC))
    audit = audit_document(document)
    text = render_audit(audit, "clean.pdf")
    assert "grade A" in text
    assert "no findings" in text
    failing = audit_document(read_pdf(EXAMPLES / "bad.pdf"))
    grouped = render_audit(failing, "bad.pdf")
    assert grouped.index("critical (1)") < grouped.index("high (2)") < grouped.index("medium (2)")
    assert grouped.count("fix:") == len(failing.findings)


def test_render_markdown_and_json_shapes():
    failing = audit_document(read_pdf(EXAMPLES / "bad.pdf"))
    markdown = render_markdown(failing, "bad.pdf")
    assert markdown.startswith("### `bad.pdf`")
    assert markdown.count("\n| ") == len(failing.findings) + 2
    payload = json.loads(render_json(failing.as_dict()))
    assert payload["counts"]["critical"] == 1


def test_render_document_reports_structure():
    document = parse_pdf(build_document(GOOD_SPEC))
    text = render_document(document)
    assert "version         1.7" in text
    assert "pages           2" in text
    assert "Accessible quarterly report" in text
    broken = parse_pdf(build_document(GOOD_SPEC).replace(b"/Pages 2 0 R", b"/Pages      ", 1))
    assert "unreadable" in render_document(broken)


def test_render_checks_and_rules():
    document = parse_pdf(build_document(GOOD_SPEC))
    checks = render_checks(document, audit_document(document))
    assert checks.count("PASS") == 7
    assert "7 of 7 rules pass" in checks
    rules = render_rules()
    assert rules.count("critical") >= 2
    assert "up to 60 = D" in rules
