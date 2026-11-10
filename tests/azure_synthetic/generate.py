"""Synthetic test cases for the Azure reviewer (LAPTOP ONLY, made-up data).

    tests/azure_synthetic/.synthetic-venv/Scripts/python.exe -I tests/azure_synthetic/generate.py --out <empty folder> [--seed 7]

Writes, into --out (a new or empty folder, never inside the repo):
  studio.csv     the BAM Studio report: Request Id, Client Account Number, Client Number, Process Status, Sub Worktype,
                 End Date, Case Source (10 Ended cases + 1 Voided row that must be skipped)
  cases\\<id> - <ACCOUNT>\\   each case folder as Part 1 + Part 2 leave it: the CCLA form (typed into the PDF for the 5
                 baseline cases, EMNIST block capitals on a 150 dpi "scan" for the 5 handwritten ones), Audit Trail.pdf,
                 Email from client.eml when the case came by email, and <ACCOUNT>_Complete_Account_Profile.json written
                 by Part 2's own write_result
  guidance\\<Sub Worktype>.md   5 made-up SYNTHETIC rules files
  expected.json  per case: trap, the results a correct reviewer may give, and every field value written on the form

Same --seed => byte-identical files (fixed PDF ids/metadata, fixed snapshot times), so the reviewer's Document
Intelligence cache is reused and a re-run costs no Azure pages.  Needs the downloads (download.py) and the synthetic venv
(pymupdf, pillow, numpy, beautifulsoup4).  Never calls Azure or any website.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import cases as C          # noqa: E402
import guidance_rules      # noqa: E402

FORMS_DIR = HERE / "_downloads" / "ccla"
FORM_MAPS = PROJECT / "3_azure_reviewer" / "form_maps.json"
DPI = 150
BANNER = "SYNTHETIC TEST DATA - made-up case for testing the reviewer, not a real customer"
PDF_DATE = "D:20261106150000+00'00'"
VERSION = 1
CSV_HEADER = ["Request Id", "Client Account Number", "Client Number", "Process Status", "Sub Worktype", "End Date", "Case Source"]


def form_maps() -> dict:
    return json.loads(FORM_MAPS.read_text("utf-8"))["forms"]


def _meta(doc, title):
    doc.set_metadata({"title": title, "author": "SYNTHETIC", "subject": BANNER, "creator": "azure_synthetic/generate.py",
                      "producer": "azure_synthetic", "creationDate": PDF_DATE, "modDate": PDF_DATE, "keywords": "synthetic"})


def _bytes(doc) -> bytes:
    return doc.tobytes(garbage=3, deflate=True, no_new_id=True)


def _banner(page):
    page.insert_text((36, 12), BANNER, fontsize=6.5, fontname="helv", color=(0.55, 0, 0))


def _ink_png(alpha) -> bytes:
    """An ink-alpha image -> dark blue-black PNG with transparency (for the typed PDFs' signatures)."""
    from PIL import Image
    rgba = Image.merge("RGBA", (Image.new("L", alpha.size, 20), Image.new("L", alpha.size, 25), Image.new("L", alpha.size, 70), alpha))
    buf = io.BytesIO()
    rgba.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def signer(case, field_id) -> str:
    """Who signs in this signature box: the name written in the same block of the form."""
    prefix = field_id[: -len("_signature")]
    v = case["values"]
    for key in ("_name", "_surname"):
        if v.get(prefix + key):
            first = v.get(prefix + "_forename", "")
            return (first[:1] + " " + v[prefix + key]).strip() if key == "_surname" else v[prefix + key]
    return "SIGNED"


def _widget(page, name, index):
    found = [w for w in page.widgets() or [] if w.field_name == name]
    if index >= len(found):
        raise RuntimeError(f"widget {name!r} #{index} not on page {page.number + 1}")
    return found[index]


def typed_form(case, fmap) -> bytes:
    """The baseline: values typed into the PDF's own fields (as a client filling the PDF on screen), signature images."""
    import fitz
    import handwriting as hw
    doc = fitz.open(FORMS_DIR / f"{case['form']}.pdf")
    for fd in fmap["fields"]:
        value = case["values"].get(fd["id"])
        if value is None:
            continue
        page = doc[fd["page"] - 1]
        if fd["kind"] == "signature":
            x0, y0, x1, y1 = fd["rect"]
            w, h = int((x1 - x0) * 3), int((y1 - y0) * 3)
            png = _ink_png(hw.signature(signer(case, fd["id"]), w, h, case["n"]))
            page.insert_image(fitz.Rect(x0 + 2, y0 + 2, x1 - 2, y1 - 2), stream=png)
            continue
        wd = _widget(page, fd["widget"], fd.get("widget_index", 0))
        if fd["kind"] == "checkbox":
            if value:
                wd.field_value = wd.on_state()
                wd.update()
            continue
        if "\n" in value:
            wd.field_flags |= fitz.PDF_TX_FIELD_IS_MULTILINE
        wd.text_font = "Helv"
        wd.text_fontsize = 9 if "\n" in value else 10
        wd.field_value = value
        wd.update()
    for page in doc:
        _banner(page)
    if case["drop_pages"]:
        doc.delete_pages([p - 1 for p in case["drop_pages"]])
    _meta(doc, C.DOC_TITLE[case["form"]])
    return _bytes(doc)


def handwritten_form(case, fmap, seed) -> bytes:
    """Block capitals from EMNIST written into the form boxes, the page 'scanned' at 150 dpi (greyscale JPEG)."""
    import fitz
    import numpy as np
    from PIL import Image, ImageDraw
    import handwriting as hw
    src = fitz.open(FORMS_DIR / f"{case['form']}.pdf")
    out = fitz.open()
    rng = np.random.default_rng([seed, case["n"]])
    scale = DPI / 72
    for pno in range(src.page_count):
        if pno + 1 in case["drop_pages"]:
            continue
        pix = src[pno].get_pixmap(dpi=DPI, colorspace=fitz.csGRAY)
        paper = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).astype(np.float32)
        for fd in fmap["fields"]:
            value = case["values"].get(fd["id"])
            if fd["page"] != pno + 1 or not value:
                continue
            x0, y0, x1, y1 = (int(round(v * scale)) for v in fd["rect"])
            w, h = x1 - x0, y1 - y0
            if fd["kind"] == "signature":
                ink = hw.signature(signer(case, fd["id"]), w - 6, h - 6, seed * 100 + case["n"])
                x0, y0 = x0 + 3, y0 + 3
            elif fd["kind"] == "checkbox":
                ink = Image.new("L", (w, h), 0)
                ImageDraw.Draw(ink).line([(w * 0.15, h * 0.55), (w * 0.4, h * 0.85), (w * 0.9, h * 0.1)], fill=255,
                                         width=max(2, w // 7))
            else:
                ink = hw.render_box(value, w, h, rng, case["writer"], illegible=fd["id"] in case["illegible"])
            a = np.asarray(ink, np.float32)[: paper.shape[0] - y0, : paper.shape[1] - x0] / 255.0 * 0.92
            region = paper[y0:y0 + a.shape[0], x0:x0 + a.shape[1]]
            paper[y0:y0 + a.shape[0], x0:x0 + a.shape[1]] = region * (1 - a) + 35 * a
        buf = io.BytesIO()
        Image.fromarray(paper.clip(0, 255).astype(np.uint8), "L").save(buf, "JPEG", quality=80, optimize=False)
        page = out.new_page(width=src[pno].rect.width, height=src[pno].rect.height)
        page.insert_image(page.rect, stream=buf.getvalue())
        _banner(page)
    _meta(out, C.DOC_TITLE[case["form"]] + " (scan)")
    return _bytes(out)


def audit_trail(case, doc_names) -> bytes:
    """System 1's Audit Trail as a short typed PDF: how the request arrived and what the processor did."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    _banner(page)
    y = 60
    def line(text, size=9.5, bold=False, x=40):
        nonlocal y
        page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")
        y += size + 6
    line("Audit Trail", 16, True)
    line(f"Request Id: {case['iid']}")
    line(f"Client Account Number: {case['account']}    Client Number: {case['client']}")
    line(f"Sub Worktype: {case['sub']}")
    y += 8
    def row(cols, size=8.5, bold=False):
        nonlocal y
        for x, text in zip((40, 130, 470), cols):
            page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")
        y += size + 6
    row(("Date / time", "Event", "By"), 9, True)
    via = f"Email from {case['email_from']}" if case["source"] == "Email" else "Post (scanned by the mailroom)"
    events = [(f"{case['received']} 09:12", f"Request received - channel: {case['source']} ({via})", "Intake"),
              *[(f"{case['received']} 09:15", f"Document added: {n}", "Intake") for n in doc_names],
              (f"{case['received']} 11:40", "Task assigned to processor", "Workflow"),
              (case["end"][:10] + " " + "10:00", f"Register updated: {case['updated']}", "Processor PR-17"),
              (case["end"][:10] + " " + "10:05", "Checks completed, process ended", "Processor PR-17")]
    for when, what, who in events:
        row((when, what[:75], who))
    _meta(doc, "Audit Trail")
    return _bytes(doc)


def email(case, attachment) -> bytes:
    text = (f"From: {case['email_from']}\r\nTo: clientservices@example.invalid\r\nDate: {case['received']} 09:10\r\n"
            f"Subject: {case['sub']} - {case['register']['name']} ({case['account']})\r\nX-Note: {BANNER}\r\n\r\n"
            f"Dear Client Services,\r\n\r\nPlease find our signed instruction attached for account {case['account']} "
            f"({attachment}).\r\n\r\nKind regards\r\n{case['register']['name']}\r\n")
    return text.encode("utf-8")


def expected_fields(case, fmap, form_file):
    out = []
    for fd in fmap["fields"]:
        if fd["id"] not in case["values"] or fd["page"] in case["drop_pages"]:
            continue
        value = case["values"][fd["id"]]
        text = fd["kind"] in ("text", "date")
        item = {"file": form_file, "form": case["form"], "field": fd["id"], "label": fd["label"], "kind": fd["kind"],
                "value": value.replace("\n", ", "), "handwritten": case["style"] == "hand" and text,
                "illegible": fd["id"] in case["illegible"]}
        if fd["id"] in case["alternatives"]:
            item["alternatives"] = case["alternatives"][fd["id"]]
        out.append(item)
    return out


def generate(out: Path, seed: int = 7, only=None) -> dict:
    import fitz
    sys.path.insert(0, str(PROJECT))
    from shared import pipeline as P
    import register
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out must be a new or empty folder: {out}")
    if PROJECT in out.resolve().parents or out.resolve() == PROJECT:
        raise SystemExit("--out must be outside the repository (generated cases are never committed)")
    (out / "cases").mkdir(parents=True)
    maps = form_maps()
    rows, cases_out, pages = [], [], 0
    for case in C.CASES:
        if only and case["n"] not in only:
            continue
        fmap = maps[case["form"]]
        folder = out / "cases" / P.folder_name(case["iid"], case["account"])
        folder.mkdir()
        form_file = f"{C.DOC_TITLE[case['form']]}.pdf"
        body = handwritten_form(case, fmap, seed) if case["style"] == "hand" else typed_form(case, fmap)
        (folder / form_file).write_bytes(body)
        docs = [form_file]
        if case["source"] == "Email":
            (folder / "Email from client.eml").write_bytes(email(case, form_file))
            docs.insert(0, "Email from client.eml")
        (folder / "Audit Trail.pdf").write_bytes(audit_trail(case, docs))
        row = {"Request Id": case["iid"], "Client Account Number": case["account"], "Client Number": case["client"],
               "Process Status": "Ended", "Sub Worktype": case["sub"], "End Date": case["end"], "Case Source": case["source"]}
        rows.append(row)
        register.write_snapshot(folder / f"{P.norm_account(case['account'])}{P.SNAPSHOT_SUFFIX}", case["register"], row)
        n_pages = fitz.open(folder / form_file).page_count + fitz.open(folder / "Audit Trail.pdf").page_count
        pages += n_pages
        cases_out.append({"iid": case["iid"], "n": case["n"], "account": case["account"], "sub_worktype": case["sub"],
                          "case_source": case["source"], "form": case["form"], "style": case["style"], "trap": case["trap"],
                          "expected": case["expected"], "files": sorted(p.name for p in folder.iterdir()),
                          "di_pages": n_pages, "fields": expected_fields(case, fmap, form_file)})
    rows.append({"Request Id": C.iid(99), "Client Account Number": "CO4000199", "Client Number": "7000199",
                 "Process Status": "Voided", "Sub Worktype": C.SUB_ADDRESS, "End Date": "", "Case Source": "Post"})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_HEADER, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    (out / "studio.csv").write_bytes(buf.getvalue().encode("utf-8-sig"))
    (out / "guidance").mkdir()
    for name, text in guidance_rules.RULES.items():
        (out / "guidance" / f"{name}.md").write_bytes(text.encode("utf-8"))
    expected = {"note": "SYNTHETIC TEST DATA - made-up cases; see tests/azure_synthetic/README.md",
                "generator": {"version": VERSION, "seed": seed}, "di_pages": pages, "cases": cases_out}
    (out / "expected.json").write_bytes((json.dumps(expected, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    return expected


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="a new or empty folder outside the repository")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only", type=int, nargs="*", help="case numbers 1-10 (default: all)")
    a = ap.parse_args(argv)
    import handwriting
    if not handwriting.available() or not FORMS_DIR.is_dir():
        print("The downloads are missing: run tests/azure_synthetic/download.py first (laptop only).")
        return 2
    exp = generate(Path(a.out), a.seed, a.only)
    print(f"Generated {len(exp['cases'])} synthetic cases in {a.out} - {exp['di_pages']} Document Intelligence pages "
          f"(traps: {', '.join(c['trap'] for c in exp['cases'] if c['trap'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
