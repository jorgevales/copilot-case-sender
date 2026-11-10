"""Tests for the synthetic Azure-reviewer test data: the scorer (stdlib, always runs when SENDER_TEST_ROOT is set) and
the generator (only where the laptop-only synthetic venv packages and the downloads exist - skipped elsewhere).
Files are written ONLY under SENDER_TEST_ROOT, like the rest of the suite.  Made-up data only; never calls Azure."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import score as SC      # noqa: E402


@pytest.fixture
def root():
    base = os.environ.get("SENDER_TEST_ROOT")
    if not base:
        pytest.skip("SENDER_TEST_ROOT is not set (tests never write to system Temp)")
    p = Path(base) / f"synth-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True)
    yield p
    shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- score.py

def expected_two_cases():
    f = lambda field, value, hand=True, kind="text", **k: {"file": "Form.pdf", "form": "change-address-form", "field": field,
                                                          "value": value, "kind": kind, "handwritten": hand, "illegible": False, **k}
    fields = [f(f"h{i}", f"VALUE {i}") for i in range(20)] + [f("addr", "1 HIGH ST, TOWN"), f("sig", "signed", False, "signature"),
                                                            f("acct", "4000108", alternatives=["CO4000108"])]
    return {"generator": {"seed": 7}, "di_pages": 9, "cases": [
        {"iid": "a" * 32, "n": 1, "form": "change-address-form", "trap": None, "expected": ["Correct"], "fields": fields},
        {"iid": "b" * 32, "n": 2, "form": "change-address-form", "trap": "value_mismatch", "expected": ["Incorrect"], "fields": []}]}


def result(iid, verdict, fields):
    return {"iid": iid, "state": "result_" + verdict.lower(), "result": {"REVIEW_RESULT": verdict},
            "DOCUMENTS": [{"DOC_ID": "D1", "FILE": "Form.pdf"}],
            "EXTRACTED_FIELDS": [{"FIELD_ID": f"D1.{k}", "DOC_ID": "D1", "FIELD": k, "VALUE": v, "CONFIDENCE": c,
                                  "LOW_CONFIDENCE": c < 0.8} for k, v, c in fields]}


def perfect_fields():
    return [(f"h{i}", f"value  {i}", 0.95) for i in range(20)] + [("addr", "1 High St\nTown", 0.9), ("sig", "Signed", 0.9),
                                                                   ("acct", "CO4000108", 0.9)]


def test_a_perfect_run_passes_with_strict_but_case_and_space_blind_matching():
    s = SC.score(expected_two_cases(), {"a" * 32: result("a" * 32, "Correct", perfect_fields()),
                                        "b" * 32: result("b" * 32, "Incorrect", [])})
    assert s["pass"] and s["rates"]["hand"] == 1.0


def test_a_trap_case_judged_correct_or_missing_fails_the_bar():
    exp = expected_two_cases()
    s = SC.score(exp, {"a" * 32: result("a" * 32, "Correct", perfect_fields()), "b" * 32: result("b" * 32, "Correct", [])})
    assert not s["pass"] and not s["bars"]["traps"]["ok"]
    s = SC.score(exp, {"a" * 32: result("a" * 32, "Correct", perfect_fields())})
    assert not s["bars"]["traps"]["ok"]                    # no result = nothing proves the trap was caught


def test_flagged_wrong_values_still_count_and_unflagged_score_is_shown():
    fields = perfect_fields()
    fields[0] = ("h0", "VALUE O", 0.5)                     # wrong, flagged
    fields[1] = ("h1", "VALUE 7", 0.9)                     # wrong, not flagged
    s = SC.score(expected_two_cases(), {"a" * 32: result("a" * 32, "Correct", fields), "b" * 32: result("b" * 32, "Incorrect", [])})
    assert s["bars"]["handwritten"]["ok"] is False         # 20/22 = 90.9% < 95%
    assert "unflagged only: 20 of 21" in s["bars"]["handwritten"]["text"]


def test_low_confidence_without_the_flag_fails_the_flagging_bar(root):
    res = result("a" * 32, "Correct", perfect_fields())
    res["EXTRACTED_FIELDS"][0]["CONFIDENCE"], res["EXTRACTED_FIELDS"][0]["LOW_CONFIDENCE"] = 0.42, False
    (root / "res").mkdir()
    (root / "res" / ("a" * 32 + ".json")).write_text(json.dumps(res), "utf-8")
    (root / "res" / ("b" * 32 + ".json")).write_text(json.dumps(result("b" * 32, "Incorrect", [])), "utf-8")
    (root / "exp.json").write_text(json.dumps(expected_two_cases()), "utf-8")
    code = SC.main(["--expected", str(root / "exp.json"), "--results", str(root / "res"), "--out", str(root / "r.md")])
    text = (root / "r.md").read_text("utf-8")
    assert code == 1 and "not flagged: 1" in text and "**Overall: FAIL**" in text


def test_value_normalising_ignores_only_case_and_spacing():
    assert SC.norm("J.SMITH @ EXAMPLE.ORG") == SC.norm("j.smith@example.org")
    assert SC.norm("14 CHAPEL ROW\nWEST HADDON") == SC.norm("14 Chapel Row, West Haddon")


@pytest.mark.parametrize("field,written,read,ok", [
    ("new_postcode", "CH1 4HG", "CHI 4HG", True), ("new_postcode", "YO17 7EG", "Y017 7EG", True),
    ("new_postcode", "HR8 1EE", "HR8IEE", True), ("new_postcode", "NN6 7AP", "NN6 7PA", False),
    ("account_number_1", "CO4000106", "C04000106", True), ("client_id_numbers", "CO4000109", "C0 4000109", True),
    ("account_number_1", "CO4000107", "CD4000107", False), ("account_number_1", "CO4000107", "C0400010", False),
    ("daytime_phone", "01904 901010", "01904901010", True), ("trustee_2_date_of_birth", "17/11/1977", "17////1977", False),
    ("signed_date", "05/11/2026", "5/11/2O26", True), ("bank_account_number", "43917265", "43917285", False),
])
def test_format_safe_normalisation_only_where_the_format_decides(field, written, read, ok):
    kind = "date" if "date" in field else "text"
    assert SC.right({"field": field, "kind": kind, "value": written}, read) is ok


def test_signatures_are_signed_or_not_however_worded():
    exp = {"field": "trustee_1_signature", "kind": "signature", "value": "signed"}
    assert SC.right(exp, "Signed") and SC.right(exp, True)
    assert not SC.right(exp, "possibly not signed") and not SC.right(exp, "not signed") and not SC.right(exp, "")


# ---------------------------------------------------------------------------------------------- generate.py (laptop)

def _generator():
    for mod in ("fitz", "numpy", "PIL", "bs4"):
        pytest.importorskip(mod)
    import handwriting
    if not handwriting.available() or not (HERE / "_downloads" / "ccla").is_dir():
        pytest.skip("the synthetic downloads are not here (laptop only: download.py)")
    import generate
    return generate


def tree_hashes(folder: Path) -> dict:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_generator_makes_10_cases_with_one_trap_each_kind_and_is_byte_identical(root):
    gen = _generator()
    a, b = gen.generate(root / "a", seed=7), gen.generate(root / "b", seed=7)
    assert tree_hashes(root / "a") == tree_hashes(root / "b") and a == b
    assert len(a["cases"]) == 10 and a["di_pages"] <= 90
    assert sorted(c["trap"] for c in a["cases"] if c["trap"]) == ["illegible_key", "missing_page", "value_mismatch", "wrong_signer"]
    assert all("Correct" not in c["expected"] for c in a["cases"] if c["trap"])
    assert len({c["form"] for c in a["cases"]}) == 7
    assert sum(f["handwritten"] for c in a["cases"] for f in c["fields"]) >= 100
    import re
    assert all(re.fullmatch(r"[A-Z]{2}\d{7}", c["account"]) for c in a["cases"])     # CCLA: 2 letters + 7 digits
    digits_only = [f for c in a["cases"] for f in c["fields"] if f["field"] == "account_number_1" and f["value"].isdigit()]
    assert digits_only and all(len(f["value"]) == 7 for f in digits_only)


def _ink(pdf, page_no, rect):
    """Share of dark pixels inside `rect` (PDF points) of a page, as a reader would see it."""
    import fitz
    import numpy as np
    page = fitz.open(pdf)[page_no]
    pix = page.get_pixmap(dpi=100, colorspace=fitz.csGRAY, clip=fitz.Rect(rect))
    return float((np.frombuffer(pix.samples, np.uint8) < 128).mean())


def test_every_signature_box_and_the_illegible_field_carry_visible_handwriting(root):
    gen = _generator()
    exp = gen.generate(root / "s", seed=7)
    sys.path.insert(0, str(PROJECT))
    from shared import pipeline as P
    maps = gen.form_maps()
    import cases as C
    for case, c in zip([x for x in C.CASES], exp["cases"]):
        fmap = {f["id"]: f for f in maps[case["form"]]["fields"]}
        pdf = root / "s" / "cases" / P.folder_name(c["iid"], c["account"]) / c["fields"][0]["file"]
        for f in c["fields"]:
            if f["kind"] == "signature" or f["illegible"]:
                fd = fmap[f["field"]]
                page = fd["page"] - 1 - sum(p < fd["page"] for p in case["drop_pages"])
                assert _ink(pdf, page, fd["rect"]) > 0.02, (c["n"], f["field"])


def test_generated_cases_read_back_through_the_pipeline_and_part2_checks(root):
    gen = _generator()
    exp = gen.generate(root / "g", seed=7)
    sys.path.insert(0, str(PROJECT))
    from shared import pipeline as P
    import register
    p2 = register.part2()
    for c in exp["cases"]:
        folder = root / "g" / "cases" / P.folder_name(c["iid"], c["account"])
        snap = json.loads((folder / f"{c['account']}{P.SNAPSHOT_SUFFIX}").read_text("utf-8"))
        assert p2.snapshot_problems(snap, c["account"]) == []
        assert "Audit Trail.pdf" in c["files"] and ("Email from client.eml" in c["files"]) == (c["case_source"] == "Email")
    text = (root / "g" / "studio.csv").read_text("utf-8-sig")
    assert text.splitlines()[0] == "Request Id,Client Account Number,Client Number,Process Status,Sub Worktype,End Date,Case Source"
    assert sorted(p.stem for p in (root / "g" / "guidance").glob("*.md")) == sorted(
        ["Change of Address", "Change of Signatory", "Change of Organisation Name", "Change of Correspondent",
         "Account Instructions Update"])


def test_the_form_maps_cover_the_7_forms_with_unique_ids():
    maps = json.loads((PROJECT / "3_azure_reviewer" / "form_maps.json").read_text("utf-8"))["forms"]
    assert len(maps) == 7
    for slug, form in maps.items():
        ids = [f["id"] for f in form["fields"]]
        assert len(ids) == len(set(ids)) and form["match"] and form["pages"] >= 2
        assert all(f["kind"] in ("text", "date", "checkbox", "signature") and 1 <= f["page"] <= form["pages"] for f in form["fields"])
