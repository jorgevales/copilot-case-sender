"""Tests for the November 2026 update: the BAM Report (.xlsx) input, the 10 run choices, the already-done log, the
menu's run modes (System 1 only / System 2 only / both / System 3 only), running cases again, and System 3 =
Mission Control (a local fake endpoint).  Synthetic data only; files only under SENDER_TEST_ROOT.

    set SENDER_TEST_ROOT=<a folder on approved storage>
    <PYTHON_EXECUTABLE> -B -m pytest -p no:cacheprovider tests
"""
import csv
import datetime
import email.parser
import email.policy
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import types
import urllib.error
import zipfile
from pathlib import Path

import pytest

import bam_helpers as BH
import test_pipeline as TP                           # its helpers (base fixture, fake parts, plan/hand-over helpers)
from test_pipeline import base                       # noqa: F401  (pytest fixture)

S = TP.S
P = TP.P
H = TP.H
hexid = TP.hexid
from shared import bam_report as BAM                  # noqa: E402
from shared import run_scope as RS                    # noqa: E402
from shared import done_log as DL                     # noqa: E402
MC = TP.load("mission_control", "3_mission_control/mission_control.py")


# ---------------------------------------------------------------------------------------------- the BAM Report

def test_the_report_is_read_from_row_5_of_the_first_sheet_with_its_dates_and_extras(base):
    path = BH.write_bam(base / "bam.xlsx", [
        BH.case(hexid(1), "ab 100000", client="C1", fund="Fund A", source="Email", subject="Change of address please"),
        BH.case(hexid(2), "AB200000", "Voided"),
        BH.case(hexid(3), "AB300000", source="", subject=""),
        BH.case("not-an-id", "AB400000"),
        BH.case(hexid(1).upper(), "AB100000"),
        BH.case(hexid(6), ""),
    ])
    cases, excluded, warnings = P.read_report(path)
    assert [c["iid"] for c in cases] == [hexid(1), hexid(3)] and not warnings
    one = cases[0]
    assert one["account"] == "AB100000" and one["client_number"] == "C1" and one["client_name"] == "Synthetic Client One"
    assert one["fund"] == "Fund A" and one["fund_type"] == "Income" and one["case_source"] == "Email"
    assert one["email_subject"] == "Change of address please" and one["sub_worktype_csv"] == "Change of Address"
    assert one["received_date"] == "20/11/2026 09:15 AM" and one["end_date"] == "25/11/2026 12:23 PM"
    assert one["row_number"] == 6 and cases[1]["case_source"] == ""
    reasons = {e["row_number"]: e["reason"] for e in excluded}
    assert reasons == {7: "Process Status is Voided", 9: "not a valid RequestId (32 characters)",
                       10: "the same RequestId is on an earlier row", 11: "no Client Account Number"}


def test_the_end_date_text_is_what_the_case_timing_reads(base):
    from shared import timing as TM
    path = BH.write_bam(base / "bam.xlsx", [BH.case(hexid(1), end=datetime.datetime(2026, 10, 13, 11, 45))])
    cases, _, _ = P.read_report(path)
    assert TM.parse_end(cases[0]["end_date"]) == datetime.datetime(2026, 10, 13, 11, 45)


def test_columns_are_found_by_name_and_another_count_is_only_a_warning(base):
    heads = ["Sub Worktype", "Process Status", "RequestId", "Client Account Number"]     # moved, and only 4
    path = BH.write_bam(base / "bam.xlsx", [BH.case(hexid(1))], headers=heads)
    cases, _, warnings = P.read_report(path)
    assert [c["iid"] for c in cases] == [hexid(1)] and cases[0]["received_date"] == ""
    assert "4 columns instead of the usual 31" in warnings[0] and "Received Date" in warnings[1]


def test_a_missing_needed_header_stops_with_its_name_and_row_5_is_mentioned(base):
    heads = [h for h in BH.HEADERS if h != "Process Status"]
    with pytest.raises(P.CsvError, match=r"row 5 .*Process Status"):
        P.read_report(BH.write_bam(base / "bam.xlsx", [BH.case(hexid(1))], headers=heads))
    with pytest.raises(P.CsvError, match="RequestId"):        # headers not in row 5 (one title row too many)
        P.read_report(BH.write_bam(base / "b2.xlsx", [BH.case(hexid(1))], title_rows=5))


def test_only_an_xlsx_is_accepted_and_a_broken_one_is_named(base):
    csv = base / "report.csv"
    csv.write_text("RequestId\n", "utf-8")
    with pytest.raises(P.CsvError, match=r"\.xlsx"):
        P.read_report(csv)
    bad = base / "bad.xlsx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(P.CsvError, match="not a readable .xlsx"):
        P.read_report(bad)


def test_shared_strings_and_sparse_cells_are_read(base):
    """A report saved by Excel keeps text in sharedStrings.xml and leaves empty cells out."""
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             '<row r="5"><c r="A5" t="s"><v>0</v></c><c r="N5" t="s"><v>1</v></c><c r="V5" t="s"><v>2</v></c>'
             '<c r="AA5" t="s"><v>3</v></c></row>'
             f'<row r="6"><c r="A6" t="s"><v>4</v></c><c r="N6"><v>12345</v></c><c r="V6" t="s"><v>5</v></c>'
             '<c r="AA6" t="s"><v>6</v></c></row></sheetData></worksheet>')
    strings = ["RequestId", "Client Account Number", "Process Status", "Sub Worktype", hexid(7), "Ended", "Bank Details"]
    sst = ('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    path = base / "excel.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   '<sheet name="R" sheetId="1" r:id="rId9"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="rId9" Type="x" Target="/xl/worksheets/data.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/data.xml", sheet)
    cases, _, _ = P.read_report(path)
    assert cases[0]["iid"] == hexid(7) and cases[0]["account"] == "12345" and cases[0]["sub_worktype_csv"] == "Bank Details"


def test_the_newest_report_in_the_folder_is_offered_and_excel_lock_files_are_ignored(base):
    folder = base / "BAM Reports"
    folder.mkdir()
    old = BH.write_bam(folder / "old.xlsx", [])
    new = BH.write_bam(folder / "new.xlsx", [])
    os.utime(old, (1_700_000_000, 1_700_000_000))
    (folder / "~$new.xlsx").write_bytes(b"lock")
    assert BAM.newest_report(folder) == new
    assert BAM.newest_report(base / "missing") is None


# ---------------------------------------------------------------------------------------------- the run choices

def scope_cases():
    rows = [BH.case(hexid(1), "AB1", client="C1", sub="Change of Address", fund="Fund A", source="Email",
                    received=datetime.datetime(2026, 11, 1, 9)),
            BH.case(hexid(2), "AB2", client="C2", sub="Bank Details", fund="Fund B", source="Post",
                    received=datetime.datetime(2026, 11, 10, 9)),
            BH.case(hexid(3), "AB1", client="C1", sub="bank details", fund="Fund A", source="",
                    received=datetime.datetime(2026, 11, 20, 9)),
            BH.case(hexid(4), "AB4", client="C4", sub="Change of Address", fund="Fund C", source="Email",
                    received=datetime.datetime(2026, 12, 1, 9))]
    return rows


@pytest.fixture
def cases(base):
    return P.read_report(BH.write_bam(base / "bam.xlsx", scope_cases() + [BH.case(hexid(9), "AB9", "Voided")]))[:2]


def ids(scope):
    return [c["iid"][-1] for c in scope.cases]


def test_every_one_line_run_choice(cases):
    got, excluded = cases
    assert ids(RS.from_text("all", got)) == ["1", "2", "3", "4"]
    assert ids(RS.from_text(f"one:{hexid(2)}", got)) == ["2"]
    assert ids(RS.from_text("first:2", got)) == ["1", "2"]
    assert ids(RS.from_text(f"ids:{hexid(4)}, {hexid(1)}", got)) == ["1", "4"]           # report order kept
    assert ids(RS.from_text("client:C1", got)) == ["1", "3"]
    assert ids(RS.from_text("account:ab 1,AB4", got)) == ["1", "3", "4"]
    assert ids(RS.from_text("type:Bank Details", got)) == ["2", "3"]                     # case and spaces ignored
    assert ids(RS.from_text("fund:fund a", got)) == ["1", "3"]
    assert ids(RS.from_text("source:email", got)) == ["1", "4"]
    assert ids(RS.from_text("source:blank", got)) == ["3"]
    s = RS.from_text("received:05/11/2026-20/11/2026", got)
    assert ids(s) == ["2", "3"] and s.label == "Cases received between 05/11/2026 and 20/11/2026"
    pasted = RS.from_text(f"ids:{hexid(1)},{hexid(9)},{hexid(8)}", got, excluded)
    assert ids(pasted) == ["1"] and any("Voided" in n for n in pasted.notes) and any("not in this BAM" in n for n in pasted.notes)
    with pytest.raises(RS.ScopeError):
        RS.from_text("client:nobody", got)
    with pytest.raises(RS.ScopeError):
        RS.from_text("first:x", got)


def answers(*lines):
    it = iter(lines)
    return lambda prompt="": next(it)


def test_the_console_choices_ask_plain_questions_and_try_again_after_a_mistake(cases):
    got, excluded = cases
    said = []
    assert ids(RS.choose(got, excluded, answers(""), said.append)) == ["1", "2", "3", "4"]          # Enter = every case
    assert ids(RS.choose(got, excluded, answers("1", ""), said.append)) == ["1"]                    # Enter = first case
    assert ids(RS.choose(got, excluded, answers("2", "3"), said.append)) == ["1", "2", "3"]
    assert ids(RS.choose(got, excluded, answers("4", hexid(2), hexid(4), ""), said.append)) == ["2", "4"]
    assert ids(RS.choose(got, excluded, answers("7", "1"), said.append)) == ["2", "3"]              # numbered list
    assert any("Bank Details" in s and "(2 cases)" in s for s in said)
    assert ids(RS.choose(got, excluded, answers("5", "C4"), said.append)) == ["4"]
    assert any("Synthetic Client One" in s for s in said)                                          # name beside number
    assert ids(RS.choose(got, excluded, answers("10", "01/11/2026", "02/11/2026"), said.append)) == ["1"]
    said.clear()
    assert ids(RS.choose(got, excluded, answers("99", "5", "nobody", "9", "2"), said.append)) == ["2"]
    assert any("type a number" in s for s in said) and any("try again" in s for s in said)


# ---------------------------------------------------------------------------------------------- the already-done log

def test_the_log_counts_real_work_only_and_is_written_for_excel(base):
    log = DL.DoneLog(base / "runtime", "coordinator")
    mc = DL.DoneLog(base / "runtime", "mission-control")
    log.add("system1", hexid(1), "downloaded", "AB1", "2 document(s)", "run-a", "Change of Address")
    log.add("system2", hexid(1), "captured", "AB1", "", "run-a")
    mc.add("system3", hexid(2), "prepared", "AB2", "not connected", "run-a")
    mc.add("system3", hexid(3), "uncertain", "AB3", "no answer", "run-a")
    (log.folder / "coordinator.jsonl").open("a", encoding="utf-8").write('{"half a line')
    before = log.done_before([hexid(1), hexid(2), hexid(3)], ["system1", "system2", "system3"])
    assert set(before) == {hexid(1), hexid(3)} and set(before[hexid(1)]) == {"system1", "system2"}
    assert log.done_before([hexid(1)], ["system3"]) == {}
    cases_root = base / "cases"
    cases_root.mkdir()
    path, main = log.write_xlsx(cases_root, {"system1": "Studio", "system2": "Register", "system3": "Mission Control"})
    assert main and path.name == DL.XLSX_NAME
    with zipfile.ZipFile(path) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Studio" in sheet and "Register" in sheet and "send uncertain" in sheet and hexid(3) in sheet


def test_an_open_log_workbook_gets_a_time_stamped_copy(base, monkeypatch):
    log = DL.DoneLog(base / "runtime", "coordinator")
    log.add("system1", hexid(1), "downloaded")
    real = os.replace

    def locked(src, dst):
        if Path(dst).name == DL.XLSX_NAME:
            raise PermissionError("open in Excel")
        return real(src, dst)
    monkeypatch.setattr(DL.os, "replace", locked)
    path, main = log.write_xlsx(base, DL.default_names())
    assert not main and path.name.startswith("Already sent log - ")


# ---------------------------------------------------------------------------------------------- plan + hand-over by mode

def mk_cases(*specs):
    return [{"iid": iid, "account": acc, "client_number": "", "status": "Ended", "row": {}, "row_number": n,
             "sub_worktype_csv": "No Instructions Type"} for n, (iid, acc) in enumerate(specs, 6)]


def test_the_plan_follows_the_chosen_systems(base):
    paths, _ = TP.make_paths(base)
    g, contract = TP.guidance(base, "Change of Address")
    c = mk_cases((hexid(1), "AB1"), (hexid(2), "AB2"), (hexid(3), "AB3"))
    TP.fake_download(paths, hexid(2), "AB2")                           # documents already in .work
    (paths.cases_root / f"{hexid(3)} - AB3").mkdir()                   # complete from an earlier run
    P.write_json(paths.cases / f"{hexid(3)}.json", {"folder": f"{hexid(3)} - AB3"})
    need = lambda plan: [x["needs"] for x in plan["cases"]]
    full = H.build_plan(c, [], paths, set(), g, contract, reviewer="mission_control", systems="123")
    assert need(full) == ["download", "download", "send"] and full["no_guidance"] == []     # Mission Control: no files
    assert full["parts"] == {"part1": True, "part2": True} and full["send"] is True
    s1 = H.build_plan(c, [], paths, set(), g, contract, reviewer="mission_control", systems="1")
    assert need(s1) == ["download", "download", "done"] and s1["parts"] == {"part1": True, "part2": False}
    assert s1["send"] is False and s1["cases"][2]["word"] == "already complete"
    s3 = H.build_plan(c, [], paths, set(), g, contract, reviewer="mission_control", systems="3")
    assert need(s3) == ["held", "download", "send"] and s3["parts"] == {"part1": False, "part2": False}
    assert "run System 1 first" in s3["cases"][0]["note"]
    skip = H.build_plan(c, [], paths, set(), g, contract, reviewer="mission_control", systems="3", skip={hexid(3)})
    assert need(skip) == ["held", "download", "done"]
    copilot = H.build_plan(c, [], paths, {hexid(3)}, g, contract, reviewer="copilot", systems="3", rerun={hexid(3)})
    assert need(copilot)[2] == "done" and "never gets the same case twice" in copilot["cases"][2]["note"]
    assert H.build_plan(c, [], paths, set(), g, contract, reviewer="copilot")["no_guidance"] == ["No Instructions Type"]


def test_system1_only_leaves_cases_waiting_and_system3_only_uses_the_latest_snapshot(base):
    paths, control = TP.make_paths(base)
    plan = TP.plan_for(paths, (hexid(1), "AB1"), (hexid(2), "AB2"))
    plan.update(reviewer="mission_control", systems="1", send=False)
    P.write_json(paths.plan, plan)
    TP.fake_download(paths, hexid(1), "AB1")
    hand = H.Handover(paths, control, plan, "", [], alive=lambda part: part == "part1")
    assert hand.step() is False                                       # Part 1 still on case 2
    assert P.read_json(paths.sub("status") / "waiting.json") == {hexid(1): "documents downloaded - no account snapshot yet (run System 2)"}
    TP.fake_download(paths, hexid(2), "AB2")
    TP.fake_snapshot(paths, "AB2", "2026-11-01")                      # an older day's snapshot is enough without System 2
    control.finished("part1")
    hand.alive = lambda part: False
    assert hand.step() is True
    assert (paths.cases_root / f"{hexid(2)} - AB2" / "AB2_Complete_Account_Profile.json").is_file()
    handed = P.read_json(paths.cases / f"{hexid(2)}.json")
    assert handed["guidance"] == "" and handed["snapshot_day"] == "2026-11-01"      # no instructions file needed
    rows = {r["instruction_id"]: r for r in P.daily_report(paths, plan, control)}
    assert rows[hexid(1)]["outcome"] == P.WAITING and rows[hexid(2)]["outcome"] == P.READY
    assert not any(P.needs_you(r) for r in rows.values())


def test_system2_only_waits_for_its_snapshots_and_never_needs_documents(base):
    paths, control = TP.make_paths(base)
    plan = TP.plan_for(paths, (hexid(1), "AB1"), (hexid(2), "AB2"))
    plan.update(reviewer="mission_control", systems="2", send=False)
    TP.fake_download(paths, hexid(2), "AB2")
    log = DL.DoneLog(paths.runtime_root, "coordinator")
    plan["created"] = P.now()
    hand = H.Handover(paths, control, plan, "", [], alive=lambda part: part == "part2", done_log=log)
    assert hand.step() is False
    TP.fake_snapshot(paths, "AB1")
    TP.fake_snapshot(paths, "AB2")
    control.finished("part2")
    assert hand.step() is True
    assert P.read_json(paths.sub("status") / "waiting.json") == {hexid(1): "its documents are not downloaded yet (run System 1)"}
    assert (paths.cases_root / f"{hexid(2)} - AB2").is_dir()
    assert set(log.done_before([hexid(1), hexid(2)], ["system2"])) == {hexid(1), hexid(2)}


def test_running_a_case_again_moves_the_earlier_files_aside_never_deletes_them(base):
    paths, _ = TP.make_paths(base)
    g, contract = TP.guidance(base)
    TP.fake_download(paths, hexid(1), "AB1")
    TP.fake_snapshot(paths, "AB1")
    hand_dir = paths.cases_root / f"{hexid(1)} - AB1"
    os.rename(paths.work / f"{hexid(1)} - AB1", hand_dir)
    P.write_json(paths.cases / f"{hexid(1)}.json", {"folder": hand_dir.name})
    c = mk_cases((hexid(1), "AB1"))
    plan = H.build_plan(c, [], paths, set(), g, contract, reviewer="mission_control", systems="123",
                        rerun={hexid(1)}, extra={"rerun": [hexid(1)]})
    assert plan["cases"][0]["needs"] == "download"
    assert H.set_aside_for_rerun(paths, plan) == []
    older = paths.older / hexid(1)
    assert (older / hand_dir.name / "a letter.pdf").is_file() and (older / "handed.json").is_file()
    assert (older / "downloads.json").is_file() and (older / "AB1_Complete_Account_Profile.json").is_file()
    assert not hand_dir.exists() and not paths.snapshot("AB1").exists()


def test_redoing_only_the_snapshot_puts_the_documents_back_to_work(base):
    paths, _ = TP.make_paths(base)
    TP.fake_download(paths, hexid(1), "AB1")
    TP.fake_snapshot(paths, "AB1")
    hand_dir = paths.cases_root / f"{hexid(1)} - AB1"
    os.rename(paths.work / hand_dir.name, hand_dir)
    P.write_json(paths.cases / f"{hexid(1)}.json", {"folder": hand_dir.name})
    plan = H.build_plan(mk_cases((hexid(1), "AB1")), [], paths, set(), "", [], reviewer="mission_control",
                        systems="2", rerun={hexid(1)}, extra={"rerun": [hexid(1)]})
    H.set_aside_for_rerun(paths, plan)
    assert (paths.work / hand_dir.name / "a letter.pdf").is_file() and (paths.downloads / f"{hexid(1)}.json").is_file()
    assert not paths.snapshot("AB1").exists() and not (paths.cases / f"{hexid(1)}.json").exists()


# ---------------------------------------------------------------------------------------------- Mission Control

class Resp:
    def __init__(self, status, body=b""):
        self.status, self.body = status, body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def mc_cfg(**kw):
    return types.SimpleNamespace(**{**S.DEFAULTS, "mission_control_url": "https://mc.example.invalid/trigger",
                                    "mission_control_auth": "key", "mission_control_key": "SECRET-MC",
                                    "mission_control_key_header": "api-key", **kw})


def one_case(base):
    folder = base / f"{hexid(1)} - AB1"
    folder.mkdir()
    for n in ("z doc.pdf", "Audit Trail.pdf", "AB1_Complete_Account_Profile.json", "extra.txt"):
        (folder / n).write_bytes(b"synthetic " + n.encode())
    row = {"iid": hexid(1), "account": "AB1", "client_name": "Synthetic", "client_number": "C1", "fund": "F",
           "fund_type": "T", "status": "Ended", "sub_worktype_csv": "change of address", "received_date": "20/11/2026 09:15 AM",
           "end_date": "25/11/2026 12:23 PM", "case_source": "Email", "email_subject": "Hello"}
    handed = {"order": ["AB1_Complete_Account_Profile.json", "Audit Trail.pdf", "z doc.pdf"], "sub_worktype": "Change of Address"}
    return row, handed, folder


def test_the_package_has_the_bam_details_and_every_file_in_order(base):
    row, handed, folder = one_case(base)
    meta, files = MC.case_package(row, handed, folder)
    assert [f["name"] for f in files] == ["AB1_Complete_Account_Profile.json", "Audit Trail.pdf", "z doc.pdf", "extra.txt"]
    assert [f["kind"] for f in meta["files"]] == ["account snapshot", "audit trail", "document", "document"]
    assert meta["sub_worktype"] == "Change of Address" and meta["sub_worktype_in_report"] == "change of address"
    assert meta["received_date"] == "20/11/2026 09:15 AM" and meta["email_subject"] == "Hello"
    assert len(meta["files"][0]["sha256"]) == 64


def test_a_send_is_retried_when_busy_and_its_reference_is_kept(base):
    row, handed, folder = one_case(base)
    meta, files = MC.case_package(row, handed, folder)
    seen, answers_ = [], [urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO()), Resp(202, b'{"runId": "R-77"}')]

    def opener(req, timeout):
        seen.append(req)
        a = answers_.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    outcome, detail = MC.send_case(mc_cfg(), meta, files, opener, sleep=lambda s: None)
    assert outcome == "sent" and "reference R-77" in detail and len(seen) == 2
    assert seen[0].get_header("Api-key") == "SECRET-MC" and seen[0].get_method() == "POST"
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + seen[0].get_header("Content-type").encode() + b"\r\n\r\n" + seen[0].data)
    parts = list(msg.iter_parts())
    assert json.loads(parts[0].get_content())["request_id"] == hexid(1)
    assert [p.get_filename() for p in parts[1:]] == [f["name"] for f in files]


@pytest.mark.parametrize("error, outcome, words", [
    (urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO()), "failed", "refused this case (HTTP 400)"),
    (socket.timeout("slow"), "uncertain", "may have arrived"),
    (urllib.error.URLError(ConnectionRefusedError()), "failed", "could not be reached"),
])
def test_refused_slow_and_unreachable_sends_are_named_plainly(base, error, outcome, words):
    row, handed, folder = one_case(base)
    meta, files = MC.case_package(row, handed, folder)

    def opener(req, timeout):
        raise error
    got, detail = MC.send_case(mc_cfg(), meta, files, opener, sleep=lambda s: None)
    assert got == outcome and words in detail and "SECRET" not in detail


def test_a_refused_key_stops_the_sending(base):
    row, handed, folder = one_case(base)
    meta, files = MC.case_package(row, handed, folder)

    def opener(req, timeout):
        raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO())
    with pytest.raises(MC.KeyRefused, match="check the key"):
        MC.send_case(mc_cfg(), meta, files, opener, sleep=lambda s: None)


def test_not_connected_yet_prepares_and_saves_the_package_only(base):
    row, handed, folder = one_case(base)
    paths, _ = TP.make_paths(base)
    P.write_json(paths.cases / f"{hexid(1)}.json", handed)
    feed = types.SimpleNamespace(rows={hexid(1): row}, paths=paths)
    case = types.SimpleNamespace(id=hexid(1), path=folder)
    cfg = mc_cfg(mission_control_url="", runtime_root=str(paths.runtime_root))
    outcome, detail = MC.process(cfg, feed, case, opener=lambda *a, **k: pytest.fail("nothing may be sent"))
    saved = P.read_json(paths.runtime_root / MC.OUTBOX / paths.run.name / hexid(1) / "case.json")
    assert outcome == "prepared" and "not connected yet" in detail and saved["outcome"] == "prepared"


def test_the_mission_control_settings_are_checked_without_showing_the_key():
    assert S.mission_control_problems(mc_cfg()) == []
    assert S.mission_control_problems(mc_cfg(mission_control_url="")) == []          # not connected yet is fine
    bad = S.mission_control_problems(mc_cfg(mission_control_url="http://mc.example.invalid", mission_control_key=""))
    assert any("https://" in p for p in bad) and any("mission_control_key is not set" in p for p in bad)
    assert S.mission_control_problems(mc_cfg(mission_control_url="http://127.0.0.1:9/x")) == []   # a local test endpoint
    assert all("SECRET" not in p for p in bad)


# ---------------------------------------------------------------------------------------------- whole runs

class Trigger(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
        parts = list(msg.iter_parts())
        Trigger.got.append((json.loads(parts[0].get_content()), [p.get_filename() for p in parts[1:]],
                            self.headers.get("api-key")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"runId": f"RUN-{len(Trigger.got)}"}).encode())

    def log_message(self, *a):
        pass


def whole_run(base, monkeypatch, capsys, mode, cases, extra=None, argv_extra=()):
    cases_root, runtime = base / "cases", base / "runtime"
    cases_root.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)
    msg = base / "base_message.md"
    msg.write_text("Please review. {{CASE_END_DATETIME}} {{REGISTER_SNAPSHOT_DATETIME}} {{BUSINESS_DAYS_GAP}} "
                   "{{CASE_AGE_STATEMENT}}", "utf-8")
    daily = base / "daily_review_instructions.md"
    daily.write_text("daily instructions", "utf-8")
    config = base / "sender_config.json"
    config.write_text(json.dumps({
        "cases_root": str(cases_root), "runtime_root": str(runtime), "base_message_path": str(msg),
        "daily_instructions_path": str(daily), "python_executable": sys.executable, "edge_executable": sys.executable,
        "approved_root": str(base), "guidance_dir": str(base), "reviewer": "mission_control",
        "completion_mode": "submission_only", "system1_name": "Studio Test", "system2_name": "Register Test",
        "system1_url": "https://system1.example.invalid/app/inbox", "system1_brand_text": "Brand",
        "system2_url": "https://system2.example.invalid/home", "console_detail": "full",
        "system2_account_page": "https://system2.example.invalid/account?id={account}", "system2_party_marker": "p",
        "system2_party_links_page": "https://system2.example.invalid/links?party={party}",
        "system2_customer_summary_page": "https://system2.example.invalid/customer?id={customer}",
        "system2_detail_pages": {n: f"https://system2.example.invalid/detail{i}?id={{account}}"
                                 for i, n in enumerate(P.DETAIL_SECTIONS)}, **(extra or {})}), "utf-8")
    report = BH.write_bam(base / "BAM Report.xlsx", cases)
    fakes = {}
    for name in ("part1", "part2"):
        fakes[name] = base / f"fake_{name}.py"
        fakes[name].write_text(textwrap.dedent(TP.FAKE_PART.format(project=str(TP.PROJECT), config=str(config), part=name)),
                               "utf-8")
    saved_env, saved_tmp, saved_path = dict(os.environ), __import__("tempfile").tempdir, list(sys.path)
    monkeypatch.setenv("SENDER_CONFIG", str(config))
    monkeypatch.setenv("DAILY_REVIEW_NO_TERMINAL_WAIT", "0.2")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    R = TP.load("run_pipeline", "orchestrator/run_pipeline.py")
    sender = sys.modules["copilot_case_sender"]
    monkeypatch.setattr(sender, "ensure_dependencies", lambda *a: None)
    monkeypatch.setattr(sender, "cdp_http", lambda *a, **k: None)
    monkeypatch.setattr(R, "open_systems_edge", lambda cfg, rt, used="12": "started (fake)")
    monkeypatch.setattr(R, "PART_SCRIPTS", {"part1": fakes["part1"], "part2": fakes["part2"]})
    monkeypatch.setattr(R, "run_sender", lambda paths, detail, script: subprocess.call(
        [sys.executable, "-B", str(script), "run", "--pipeline", str(paths.run)], stdin=subprocess.DEVNULL))
    try:
        code = R.main(["--report", str(report), "--mode", mode, *argv_extra])
    finally:
        sender.WRITE_GUARD["roots"] = None
        os.environ.clear()
        os.environ.update(saved_env)
        __import__("tempfile").tempdir, sys.path[:] = saved_tmp, saved_path
        sys.dont_write_bytecode, sys.pycache_prefix = False, None
    out = capsys.readouterr().out
    rows = {}
    for f in sorted((runtime / "reports").glob("daily-report-*.csv")):
        rows = {r["instruction_id"]: r for r in csv.DictReader(io.StringIO(f.read_text("utf-8-sig")))}
    return code, out, rows, runtime, cases_root


@pytest.fixture
def trigger():
    Trigger.got = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trigger)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/trigger"
    server.shutdown()


def test_a_full_run_sends_each_complete_case_to_mission_control_and_logs_everything(base, monkeypatch, capsys, trigger):
    cases = [BH.case(hexid(1), "AB100000", sub="No Instructions File Needed"),
             BH.case(hexid(2), "AB200000", "Voided"),
             BH.case(hexid(3), "AB300000"),                                   # the fake System 1 cannot find it
             BH.case(hexid(4), "AB400000", source="Post")]
    extra = {"mission_control_url": trigger, "mission_control_auth": "key", "mission_control_key": "SECRET-MC"}
    code, out, rows, runtime, cases_root = whole_run(base, monkeypatch, capsys, "full", cases, extra,
                                                     ["--scope", "all"])
    assert code == 0, out
    sent = {m["request_id"]: (m, names, key) for m, names, key in Trigger.got}
    assert set(sent) == {hexid(1), hexid(4)}
    meta, names, key = sent[hexid(4)]
    assert key == "SECRET-MC" and meta["source"] == "Post" and names[0] == "AB400000_Complete_Account_Profile.json"
    assert rows[hexid(1)]["outcome"] == "sent to Mission Control" and "RUN-" in rows[hexid(1)]["detail"]
    assert rows[hexid(2)]["outcome"] == "excluded automatically" and "Voided" in rows[hexid(2)]["detail"]
    assert rows[hexid(3)]["outcome"] == "held"
    assert "AUTOMATICALLY EXCLUDED - 1 row(s)" in out and "Studio Test" in out and "SECRET" not in out
    log = DL.DoneLog(runtime, "x")
    done = log.done_before([hexid(1), hexid(4)], ["system1", "system2", "system3"])
    assert set(done[hexid(1)]) == {"system1", "system2", "system3"}
    assert (cases_root / DL.XLSX_NAME).is_file()

    # The same cases again: warned, and '--again no' skips them (Enter would run them all again).
    Trigger.got = []
    code, out, rows, *_ = whole_run(base, monkeypatch, capsys, "s3", cases, extra, ["--scope", "all", "--again", "no"])
    assert code == 0, out
    assert "ALREADY DONE BEFORE - 2 of the chosen cases" in out and "sent to Mission Control" in out
    assert Trigger.got == [] and rows[hexid(1)]["outcome"] == "already done - skipped by your choice"

    # System 3 only, run again: the complete cases are sent once more.
    code, out, rows, *_ = whole_run(base, monkeypatch, capsys, "s3", cases, extra, ["--scope", f"one:{hexid(4)}",
                                                                                     "--again", "yes"])
    assert code == 0, out
    assert [m["request_id"] for m, *_ in Trigger.got] == [hexid(4)]


def test_systems_1_and_2_only_send_nothing_and_leave_the_cases_ready(base, monkeypatch, capsys, trigger):
    cases = [BH.case(hexid(1), "AB100000"), BH.case(hexid(4), "AB400000")]
    code, out, rows, runtime, cases_root = whole_run(base, monkeypatch, capsys, "s12", cases,
                                                     {"mission_control_url": trigger}, ["--scope", "all"])
    assert code == 0, out
    assert Trigger.got == []
    assert rows[hexid(1)]["outcome"] == P.READY and (cases_root / f"{hexid(1)} - AB100000").is_dir()
    assert not list((runtime / MC.OUTBOX).glob("*/*/case.json"))       # not even prepared


def test_not_connected_yet_prepares_every_case(base, monkeypatch, capsys):
    cases = [BH.case(hexid(1), "AB100000")]
    code, out, rows, runtime, _ = whole_run(base, monkeypatch, capsys, "full", cases, {}, ["--scope", "all"])
    assert code == 0, out
    assert rows[hexid(1)]["outcome"] == "prepared - Mission Control not connected yet"
    assert list((runtime / MC.OUTBOX).glob(f"*/{hexid(1)}/case.json"))


def test_the_menu_shows_your_system_names_and_exits_cleanly(base, monkeypatch, capsys):
    config = base / "sender_config.json"
    config.write_text(json.dumps({"system1_name": "Studio Test", "system2_name": "Register Test",
                                  "reviewer": "mission_control"}), "utf-8")
    monkeypatch.setenv("SENDER_CONFIG", str(config))
    R = TP.load("run_pipeline", "orchestrator/run_pipeline.py")
    replies = iter(["9", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    assert R.main([]) == 0
    out = capsys.readouterr().out
    assert "1  Full run - Studio Test + Register Test together; each complete case goes to Mission Control at once" in out
    assert "2  Studio Test only" in out and "5  Mission Control only" in out and "Please type a number" in out