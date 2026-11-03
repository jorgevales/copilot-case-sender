"""Tests for the case timing of the base message (shared/timing.py) and the results workbook (shared/review_workbook.py).
No browser, no Copilot; files are written ONLY under SENDER_TEST_ROOT (like the other test files).  Synthetic data only."""
import os
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from shared import review_workbook as W              # noqa: E402
from shared import timing as T                      # noqa: E402

ROOT = os.environ.get("SENDER_TEST_ROOT")
if not ROOT:
    pytest.skip("set SENDER_TEST_ROOT to a folder on approved storage", allow_module_level=True)

SNAP = "2026-10-14T12:23:38.123456+00:00"            # a Wednesday, 13:23 in the UK (summer time)


@pytest.fixture
def folder():
    f = Path(ROOT) / f"w-{uuid.uuid4().hex[:8]}"
    f.mkdir(parents=True)
    yield f
    shutil.rmtree(f, ignore_errors=True)


def timing(end, snap=SNAP, holidays=None, **kw):
    return T.case_timing(end, snap, holidays or T.Holidays(), **kw)


# ---------------------------------------------------------------------------------------------- business days

def test_business_days_count_from_the_day_after_the_case_end_to_the_snapshot_day():
    t = timing("12/10/2026 02:17 PM")                                   # Monday -> Wednesday
    assert (t["case_end"], t["snapshot"], t["gap"], t["age"], t["notes"]) == \
        ("12/10/2026 02:17 PM", "2026-10-14T12:23:38+00:00", "2", T.WITHIN, [])
    t = timing("09/10/2026 17:00", "2026-10-15T09:00:00+00:00")         # Friday -> Thursday: the weekend does not count
    assert (t["gap"], t["age"]) == ("4", T.AGED)
    t = timing("09/10/2026 17:00", "2026-10-14T09:00:00Z")              # Friday -> Wednesday = 3: still within
    assert (t["gap"], t["age"]) == ("3", T.WITHIN)
    assert timing("14/10/2026 09:00 AM")["gap"] == "0"                 # same day


def test_christmas_bank_holidays_are_skipped():
    t = timing("24/12/2026 10:00 AM", "2026-12-30T09:00:00Z")           # 25 Fri and 28 Mon are holidays: 29, 30 count
    assert t["gap"] == "2"


def test_uk_summer_time_edges():
    at = lambda s: T.uk_local(T.parse_utc(s)).strftime("%Y-%m-%d %H:%M")
    assert at("2026-03-29T00:59:00+00:00") == "2026-03-29 00:59" and at("2026-03-29T01:00:00+00:00") == "2026-03-29 02:00"
    assert at("2026-10-25T00:30:00+00:00") == "2026-10-25 01:30" and at("2026-10-25T01:30:00+00:00") == "2026-10-25 01:30"
    assert at("2026-01-10T12:00:00+00:00") == "2026-01-10 12:00"
    # the snapshot's UK date decides the day: 23:30 UTC in summer is already the next day in the UK
    assert timing("12/10/2026 09:00 AM", "2026-10-13T23:30:00+00:00")["gap"] == "2"


def test_excel_serial_end_dates_are_read_and_written_readably():
    t = timing("46308.48958")
    assert (t["case_end"], t["gap"], t["notes"]) == ("13/10/2026 11:45 AM", "1", [])
    assert timing("46308,48958")["case_end"] == "13/10/2026 11:45 AM"   # a comma decimal too
    t = timing("46308")                                                 # a whole number is a day
    assert (t["case_end"], t["gap"]) == ("13/10/2026", "1")
    assert T.parse_end("46308.48958").strftime("%Y-%m-%d %H:%M") == "2026-10-13 11:45"


def test_what_cannot_be_worked_out_is_not_provided_with_a_check_note():
    t = timing("garbage")
    assert (t["case_end"], t["gap"], t["age"]) == ("Not provided",) * 3 and "could not be read" in t["notes"][0]
    assert timing("4630.5")["case_end"] == "Not provided"              # not a 5-digit serial
    assert "no End Date in the CSV" in timing("")["notes"][0]
    assert timing("", from_csv=False)["notes"] == []                    # send-only: no CSV, nothing to flag
    t = timing("12/10/2026 02:17 PM", None)
    assert (t["snapshot"], t["gap"]) == ("Not provided", "Not provided") and "retrieval time" in t["notes"][0]
    t = timing("15/10/2026 03:00 PM", "2026-10-15T13:30:00+00:00")      # 14:30 UK, before the case end
    assert (t["case_end"], t["gap"], t["age"]) == ("15/10/2026 03:00 PM", "Not provided", "Not provided")
    assert "older than the case end" in t["notes"][0]
    assert timing("15/10/2026", "2026-10-15T13:30:00+00:00")["gap"] == "0"   # a date alone: same day is fine
    t = timing("02/01/2029", "2029-01-05T09:00:00+00:00")
    assert t["gap"] == "Not provided" and "no bank-holiday list for 2029" in t["notes"][0]


def test_bank_holidays_file_extends_the_years_and_reports_bad_lines(folder):
    (folder / T.HOLIDAY_FILE).write_text("# England & Wales 2029\n2029-01-01\n03/01/2029  # an extra day\n\nnot a date\n",
                                         "utf-8")
    h = T.Holidays(folder)
    assert 2029 in h.years and h.problems == [f"{T.HOLIDAY_FILE} line 5 is not a date (write 2029-01-01 or 01/01/2029)"]
    t = timing("29/12/2028", "2029-01-05T09:00:00+00:00", h)           # Fri -> Fri: 1 Jan and 3 Jan are holidays
    assert t["gap"] == "3" and t["notes"] == []
    assert T.Holidays(folder / "missing").problems == []


def test_the_message_is_filled_and_placeholders_are_checked():
    template = "a {{CASE_END_DATETIME}} b {{REGISTER_SNAPSHOT_DATETIME}} c {{BUSINESS_DAYS_GAP}} d {{CASE_AGE_STATEMENT}} {{CASE_END_DATETIME}}"
    text = T.fill(template, timing("12/10/2026 02:17 PM"))
    assert text == f"a 12/10/2026 02:17 PM b 2026-10-14T12:23:38+00:00 c 2 d {T.WITHIN} 12/10/2026 02:17 PM"
    assert T.fill(template, {}).count("Not provided") == 5
    assert T.missing_placeholders(template) == []
    assert T.missing_placeholders("only {{CASE_END_DATETIME}}") == list(T.PLACEHOLDERS[1:])


def test_expected_discrepancy_follows_the_age_then_the_gap():
    assert T.expected_discrepancy({"age": T.AGED, "gap": "1"}) == T.AGED_CASE
    assert T.expected_discrepancy({"age": T.WITHIN, "gap": "9"}) == T.PROCESSOR_ERROR
    assert T.expected_discrepancy({"age": "Not provided", "gap": "4"}) == T.AGED_CASE
    assert T.expected_discrepancy({"age": "Not provided", "gap": "3"}) == T.PROCESSOR_ERROR
    assert T.expected_discrepancy({"age": "Not provided", "gap": "Not provided"}) == T.PROCESSOR_ERROR


# ---------------------------------------------------------------------------------------------- the workbook

def comparison(item, match, **over):
    return {"ITEM": item, "Customer_expectation": "Ticked", "CUSTOMER_SOURCE": "form", "System_data": "Unticked",
            "SYSTEM_SOURCE": "Account Summary > Signatory", "MATCH": match,
            "EXPLANATION": f"{item} explained", **over}


RESULT = {"COPILOT_RESULT": "Incorrect", "DISCREPANCY_TYPE": "Potential processor error", "HEADLINE": "Signatory not set",
          "CASE_END_DATETIME": "12/10/2026",
          "REGISTER_SNAPSHOT_DATETIME": "2026-10-14T12:23:38+00:00", "CLIENT_REQUEST": "Add <Person A> & co",
          "JUSTIFICATION": "Potential processor error: x",
          "A_B_COMPARISONS": [comparison("Signatory", False), comparison("Name", True), comparison("DOB", None)],
          "RULE_CHECKS": [{"RULE": "Signed", "STATUS": "Satisfied", "EVIDENCE": "e", "NOTE": "ok"},
                          {"RULE": "Witness", "STATUS": "Not determinable", "EVIDENCE": "e", "NOTE": "no page"},
                          {"RULE": "Fee", "STATUS": "Not applicable", "EVIDENCE": "e", "NOTE": "none"}]}
INFO = {"iid": "a" * 32, "account": "AB100000", "sub_worktype": "Change of Address", "client_number": "C1", "gap": "2",
        "age": T.WITHIN}


def cells(sheet):
    return [[text for text, _ in row] for row in sheet.rows]


def test_each_wrong_field_gets_its_own_explanation_and_one_row_per_case():
    summary, cases, issues, checks = W.build([(INFO, RESULT), ({**INFO, "iid": "b" * 32}, {**RESULT, "COPILOT_RESULT": "Correct",
                                              "A_B_COMPARISONS": [comparison("Name", True)], "RULE_CHECKS": []})])
    c, i, a = cells(cases), cells(issues), cells(checks)
    assert [s.name for s in (summary, cases, issues, checks)] == list(W.SHEETS)
    assert len(c) == 3 and c[0] == W.CASE_HEAD                                    # ONE row per case
    col = {h: n for n, h in enumerate(W.CASE_HEAD)}
    assert c[1][col["Headline"]] == "Signatory not set" and c[1][col["Issues"]] == "3"
    assert c[1][col["Issue details (one per field)"]] == ("1. [No match] Signatory: Signatory explained\n\n"
                                                          "2. [Could not compare] DOB: DOB explained\n\n"
                                                          "3. [Not determinable] Witness: no page")
    assert c[2][col["Issue details (one per field)"]] == "No issues"
    assert [r[6] for r in i[1:]] == ["Signatory: Signatory explained", "DOB: DOB explained", "Witness: no page"]
    assert [r[4] for r in i[1:]] == ["No match", "Could not compare", "Not determinable"]
    assert issues.links[0] == ("A2", "'Cases'!A2") and len(issues.links) == 3
    assert [r[10] for r in a[1:7]] == ["No match", "Match", "Could not compare", "Satisfied", "Not determinable", "Not applicable"]
    keys = [k for _, k in issues.rows[1]]
    assert keys[0][1] == W.RED and keys[0][3] is True and issues.rows[2][0][1][3] is False   # thick line above a new case
    flat = [r for r in cells(summary) if r]
    assert ["Incorrect", "1"] in flat and ["No match", "1"] in flat and ["Not available in Register snapshot", "0"] in flat


def test_the_xlsx_is_a_valid_package_with_escaped_text_and_no_control_characters():
    data = W.xlsx_bytes([(INFO, {**RESULT, "JUSTIFICATION": "bad\x01char\x0b"})])
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted(z.namelist())
        assert names == sorted(["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                                "xl/styles.xml"] + [f"xl/worksheets/sheet{n}.xml" for n in range(1, 5)])
        sheet = z.read("xl/worksheets/sheet2.xml").decode("utf-8")
        book = z.read("xl/workbook.xml").decode("utf-8")
        assert "<hyperlinks>" in z.read("xl/worksheets/sheet3.xml").decode("utf-8")
    assert "Add &lt;Person A&gt; &amp; co" in sheet and "badchar" in sheet and "\x01" not in sheet and "\x0b" not in sheet
    assert '<autoFilter ref="A1:' in sheet and 'state="frozen"' in sheet and '"Y,N"' in sheet and "tabSelected" in sheet
    assert all(f'name="{n}"' in book for n in W.SHEETS)
    assert W.column_letter(1) == "A" and W.column_letter(27) == "AA"


def test_an_older_workbook_is_set_aside_and_an_open_one_is_waited_for(folder, monkeypatch):
    assert W.set_aside(folder) is None
    (folder / W.NAME).write_bytes(b"old")
    moved = W.set_aside(folder)
    assert moved.name.startswith("Copilot review results - older ") and moved.read_bytes() == b"old"
    assert not (folder / W.NAME).exists()
    real, calls, said, waits = W.set_aside, [], [], []

    def locked_once(f):
        calls.append(f)
        if len(calls) == 1:
            raise PermissionError("in use")
        return real(f)
    monkeypatch.setattr(W, "set_aside", locked_once)
    (folder / W.NAME).write_bytes(b"older")
    moved2 = W.make_room(folder, wait=lambda prompt: waits.append(prompt), say=said.append)
    assert len(calls) == 2 and len(waits) == 1 and "open" in said[0] and moved2 != moved and moved2.exists()

    def no_console(prompt):
        raise EOFError
    calls.clear()
    with pytest.raises(RuntimeError):
        W.make_room(folder, wait=no_console, say=said.append)


def test_the_workbook_is_written_after_every_answer_and_a_copy_is_made_when_it_stays_open(folder, monkeypatch):
    book = W.ReviewWorkbook(folder)
    assert book.close() is None and not (folder / W.NAME).exists()     # no answer, no file
    assert book.add(INFO, RESULT) and (folder / W.NAME).exists() and not book.dirty
    real = os.replace

    def excel_has_it(src, dst):
        if Path(dst).name == W.NAME:
            raise PermissionError("in use")
        return real(src, dst)
    monkeypatch.setattr(W.os, "replace", excel_has_it)
    assert book.add({**INFO, "iid": "b" * 32}, RESULT) is False and book.dirty
    copy = book.close()
    assert copy.name.startswith("Copilot review results - ") and "older" not in copy.name and copy.exists()
    with zipfile.ZipFile(copy) as z:
        sheet = z.read("xl/worksheets/sheet2.xml").decode("utf-8")
    assert "a" * 32 in sheet and "b" * 32 in sheet                      # the copy has every answer
    assert not any(p.name.startswith(".") for p in folder.iterdir())   # no temp file left behind


def test_the_discrepancy_type_sits_with_the_timing_facts_not_beside_the_result():
    # Beside "Copilot result" the aged label read as the reason for an Incorrect (user, 2026-11-03).
    head = W.CASE_HEAD
    assert head.index("Discrepancy type") == head.index("Case age statement") + 1
    assert head.index("Discrepancy type") - head.index("Copilot result") > 10
    assert len(W.CASE_WIDTHS) == len(head)
    aged = {**RESULT, "DISCREPANCY_TYPE": T.AGED_CASE}
    _, cases, _, _ = W.build([({**INFO, "gap": "5", "age": T.AGED}, aged)])
    row = dict(zip(head, cells(cases)[1]))
    assert row["Discrepancy type"] == "Possible discrepancy due to aged case" and row["Copilot result"] == "Incorrect"
    assert row["Case age statement"] == T.AGED and row["Business days gap"] == "5"
    assert T.AGED_CASE == "Possible discrepancy due to aged case" and "Discrepancy from" not in T.AGED_CASE


def test_the_workbook_folder_is_never_created_by_the_workbook(folder):
    missing = folder / "not-there"
    with pytest.raises(RuntimeError, match="CASES_ROOT"):
        W.make_room(missing, wait=lambda p: None, say=lambda t: None)
    book = W.ReviewWorkbook(missing)
    with pytest.raises(OSError):
        book.add(INFO, RESULT)
    assert not missing.exists()


@pytest.mark.parametrize("end, snap, gap, age", [
    ("12/10/2026 02:17 PM", "2026-10-14T12:23:38+00:00", "2", T.WITHIN),     # Mon -> Wed: younger than 3
    ("09/10/2026 02:17 PM", "2026-10-14T12:23:38+00:00", "3", T.WITHIN),     # Fri -> Wed: exactly 3 = within
    ("08/10/2026 02:17 PM", "2026-10-14T12:23:38+00:00", "4", T.AGED),       # Thu -> Wed: older than 3
    ("13/10/2026 11:59 PM", "2026-10-13T23:30:00+00:00", "1", T.WITHIN),     # 23:59 Tue UK; 00:30 Wed UK (BST)
])
def test_case_age_boundaries_in_business_days_and_uk_time(end, snap, gap, age):
    t = T.case_timing(end, snap, T.Holidays())
    assert (t["gap"], t["age"]) == (gap, age)


def test_case_age_alone_never_decides_the_result_only_a_mismatch_does():
    """The age matrix: Incorrect needs a MATCH false (or a rule not satisfied); age only names the discrepancy type."""
    sys.path.insert(0, str(PROJECT / "3_copilot_sender"))
    import copilot_case_sender as S
    young = T.case_timing("12/10/2026 02:17 PM", SNAP, T.Holidays())
    exact = T.case_timing("09/10/2026 02:17 PM", SNAP, T.Holidays())
    old = T.case_timing("08/10/2026 02:17 PM", SNAP, T.Holidays())
    no_date = T.case_timing("", SNAP, T.Holidays())
    bad_date = T.case_timing("31/02/2026", SNAP, T.Holidays())
    assert (young["age"], exact["age"], old["age"]) == (T.WITHIN, T.WITHIN, T.AGED)
    assert no_date["age"] == bad_date["age"] == T.NOT_PROVIDED and bad_date["notes"]
    rule = [{"RULE": "Signed", "STATUS": "Satisfied", "EVIDENCE": "e", "NOTE": "ok"}]

    def answer(timing, verdict, match, kind):
        rows = [comparison("Signatory", match)]
        just = (kind + ": x. " + (T.AGED_SENTENCE if kind == T.AGED_CASE else "")) if kind != "None" else "x"
        obj = {"COPILOT_RESULT": verdict, "DISCREPANCY_TYPE": kind, "HEADLINE": "h", "CASE_END_DATETIME": timing["case_end"],
               "REGISTER_SNAPSHOT_DATETIME": timing["snapshot"], "CLIENT_REQUEST": "c", "JUSTIFICATION": just,
               "A_B_COMPARISONS": rows, "RULE_CHECKS": rule}
        return S.validate_result(obj, {**timing, "rules_file": True})

    for timing in (young, exact, old, no_date, bad_date):            # an exact match is Correct at any age
        assert answer(timing, "Correct", True, "None") == []
        assert any("Incorrect needs a MATCH false" in p for p in answer(timing, "Incorrect", True, "None"))
    assert answer(young, "Incorrect", False, T.PROCESSOR_ERROR) == []         # a real mismatch stays Incorrect
    assert answer(old, "Incorrect", False, T.AGED_CASE) == []
    assert answer(no_date, "Incorrect", False, T.PROCESSOR_ERROR) == []
    assert any("must be 'Possible discrepancy due to aged case'" in p for p in answer(old, "Incorrect", False, T.PROCESSOR_ERROR))
    assert any("must be None" in p for p in answer(old, "Correct", True, T.AGED_CASE))   # no aged label without a mismatch
