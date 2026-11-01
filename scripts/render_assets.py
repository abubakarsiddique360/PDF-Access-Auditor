"""Regenerate README assets from real runs.

Usage:  python scripts/render_assets.py
Writes: examples/*.pdf, docs/results.json,
        docs/images/review.png, docs/images/checks-matrix.png, docs/images/risk-bars.png
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from make_examples import write_examples  # noqa: E402

from pdf_access_auditor.audit import RULES, audit_document, rule_table  # noqa: E402
from pdf_access_auditor.pdfobj import PdfError  # noqa: E402
from pdf_access_auditor.reader import read_pdf  # noqa: E402
from pdf_access_auditor.report import render_audit  # noqa: E402

BG = "#0d1117"
CHROME = "#161b22"
FG = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#79c0ff"
GOOD = "#2ea043"
BAD = "#cf222e"

GRADE_COLOURS = {
    "A": "#1a7f37",
    "B": "#2da44e",
    "C": "#bf8700",
    "D": "#bc4c00",
    "F": "#cf222e",
}


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"):
        candidate = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def render_terminal(text: str, out_path: Path, title: str) -> None:
    font = load_font(15)
    lines = text.expandtabs(4).splitlines() or [""]
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    char_width = probe.textlength("M", font=font)
    line_height = 21
    pad = 24
    width = int(max(len(line) for line in lines) * char_width) + pad * 2
    height = pad * 2 + len(lines) * line_height + 44
    image = Image.new("RGB", (max(width, 760), height), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, image.width, 34], fill=CHROME)
    for index, color in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        draw.ellipse([16 + index * 22, 10, 28 + index * 22, 22], fill=color)
    draw.text((92, 9), title, font=load_font(13), fill=MUTED)

    for row, line in enumerate(lines):
        color = FG
        stripped = line.strip()
        if line.startswith("$"):
            color = ACCENT
        elif stripped.endswith("%") and "recover" not in stripped:
            color = GOOD
        elif "recover" in stripped or "frames" in stripped or stripped.startswith("scenario"):
            color = MUTED
        draw.text((pad, 50 + row * line_height), line, font=font, fill=color)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)


def collect() -> list[dict]:
    """Audit every example and keep only JSON-friendly numbers."""
    records: list[dict] = []
    for path in write_examples():
        document = read_pdf(path)
        audit = audit_document(document)
        try:
            pages = len(document.pages())
        except PdfError:
            pages = 0
        records.append(
            {
                "file": path.relative_to(ROOT).as_posix(),
                "bytes": path.stat().st_size,
                "version": document.version,
                "objects": document.object_count,
                "pages": pages,
                "risk": audit.risk,
                "grade": audit.grade,
                "clean": audit.clean,
                "counts": dict(audit.counts),
                "findings": [item.as_dict() for item in audit.findings],
                "audit": audit,
            }
        )
    return records


def render_review(records: list[dict], out_path: Path) -> None:
    by_name = {Path(record["file"]).name: record for record in records}
    blocks = ["$ pdf-audit audit --file examples/bad.pdf", ""]
    blocks.append(render_audit(by_name["bad.pdf"]["audit"], "examples/bad.pdf"))
    blocks.append("")
    blocks.append("$ pdf-audit audit --file examples/good.pdf")
    blocks.append("")
    blocks.append(render_audit(by_name["good.pdf"]["audit"], "examples/good.pdf"))
    render_terminal("\n".join(blocks), out_path, "pdf-audit  -  one failing file, one clean file")


def render_checks_matrix(records: list[dict], out_path: Path) -> None:
    rows, columns = len(RULES), len(records)
    figure, axis = plt.subplots(figsize=(9.4, 4.9))
    figure.patch.set_facecolor("#ffffff")
    for column, record in enumerate(records):
        audit = record["audit"]
        for row, rule in enumerate(RULES):
            y = rows - row - 1
            passed = not audit.scopes(rule.code)
            colour = GOOD if passed else BAD
            axis.add_patch(
                Rectangle(
                    (column + 0.04, y + 0.07),
                    0.92,
                    0.86,
                    facecolor=colour,
                    alpha=0.13 if passed else 0.16,
                    edgecolor="none",
                )
            )
            axis.text(
                column + 0.5,
                y + 0.5,
                "PASS" if passed else "FAIL",
                ha="center",
                va="center",
                fontsize=9.5,
                fontweight="bold",
                color=colour,
            )
        axis.text(
            column + 0.5,
            rows + 0.30,
            Path(record["file"]).name,
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
            color="#1f2328",
        )
        axis.text(
            column + 0.5,
            rows + 0.10,
            f"risk {record['risk']}  -  grade {record['grade']}",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#57606a",
        )
    axis.set_xlim(0, columns)
    axis.set_ylim(0, rows + 0.62)
    axis.set_xticks([])
    axis.set_yticks([rows - index - 0.5 for index in range(rows)])
    axis.set_yticklabels([f"{rule.code}  ({rule.severity})" for rule in RULES], fontsize=8.5)
    axis.tick_params(length=0)
    for spine in ("top", "right", "left", "bottom"):
        axis.spines[spine].set_visible(False)
    axis.set_title("Every rule against every example file", loc="left", fontsize=11)
    figure.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, format="png", dpi=150)
    plt.close(figure)


def render_risk_bars(records: list[dict], out_path: Path) -> None:
    names = [Path(record["file"]).name for record in records]
    risks = [record["risk"] for record in records]
    grades = [record["grade"] for record in records]
    figure, axis = plt.subplots(figsize=(7.8, 3.8))
    figure.patch.set_facecolor("#ffffff")
    bars = axis.bar(
        range(len(records)),
        risks,
        width=0.52,
        color=[GRADE_COLOURS[grade] for grade in grades],
    )
    for bar, risk, name in zip(bars, risks, names, strict=True):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            max(risk, 0) + 1.6,
            f"{name}\n{risk} points",
            ha="center",
            va="bottom",
            fontsize=8.5,
            color="#1f2328",
        )
    axis.set_xticks(range(len(records)))
    axis.set_xticklabels(grades, fontsize=14, fontweight="bold")
    axis.set_xlabel("grade  (0 = A, up to 10 = B, up to 30 = C, up to 60 = D, above 60 = F)",
                    fontsize=8.5)
    axis.set_ylabel("risk score")
    axis.set_ylim(0, max(risks) * 1.40 + 6)
    axis.set_xlim(-0.6, len(records) - 0.25)
    axis.axhline(60, color=MUTED, linestyle=":", linewidth=1)
    axis.axhline(31, color=MUTED, linestyle=":", linewidth=1)
    axis.text(
        -0.55, 60.8, "60: F from here", fontsize=7.5, color="#57606a", ha="left", va="bottom"
    )
    axis.text(
        -0.55, 31.8, "31: D from here", fontsize=7.5, color="#57606a", ha="left", va="bottom"
    )
    axis.grid(axis="y", linestyle=":", alpha=0.45)
    axis.set_axisbelow(True)
    axis.set_title(
        "Risk score per example, graded the same way CI grades it", fontsize=10, loc="left"
    )
    for spine in ("top", "right"):
        axis.spines[spine].set_visible(False)
    figure.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_path, format="png", dpi=150)
    plt.close(figure)


def main() -> int:
    os.chdir(ROOT)
    images = ROOT / "docs" / "images"
    images.mkdir(parents=True, exist_ok=True)

    records = collect()
    payload = {
        "generated_by": "scripts/render_assets.py",
        "examples": [
            {key: value for key, value in record.items() if key != "audit"} for record in records
        ],
        "rules": rule_table(),
        "totals": {
            "files": len(records),
            "clean": sum(1 for record in records if record["clean"]),
            "findings": sum(len(record["findings"]) for record in records),
            "bytes": sum(record["bytes"] for record in records),
        },
    }
    (ROOT / "docs").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    render_review(records, images / "review.png")
    render_checks_matrix(records, images / "checks-matrix.png")
    render_risk_bars(records, images / "risk-bars.png")

    for record in records:
        print(
            f"{Path(record['file']).name:<12}{record['bytes']:>6} bytes  "
            f"{record['objects']:>2} objects  risk {record['risk']:>3}  "
            f"grade {record['grade']}  {len(record['findings'])} finding(s)"
        )
    print(
        f"\n{payload['totals']['findings']} findings across "
        f"{payload['totals']['files']} files, {payload['totals']['bytes']} bytes of examples"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
