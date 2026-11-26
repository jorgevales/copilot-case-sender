"""Tests for the Azure reviewer's wiring into the daily pipeline (no Azure call, no browser; synthetic data only):
the BAM Studio report (Request Id, case source), the case source line of a case's message, the plan / hand-over /
daily report with reviewer = azure, the results workbook's Azure tabs, setup rules, Run.cmd check / azure-test.

Run like the other tests (files are written ONLY under SENDER_TEST_ROOT):

    set SENDER_TEST_ROOT=<a folder on approved storage>
    <PYTHON_EXECUTABLE> -B -m pytest -p no:cacheprovider tests
"""
import collections
import datetime
import io
import json
import os
import sys
import textwrap
import types
import zipfile
from pathlib import Path

import pytest

import test_sender as T                              # the loaded sender (T.S) and its fakes
import bam_helpers as BH                             # synthetic BAM Reports
import test_pipeline as TP                           # its helpers (base fixture, CSV writer, fake parts, FeedWorld)
from test_sender import env, env2                    # noqa: F401  (pytest fixtures)
from test_pipeline import base                       # noqa: F401  (pytest fixture)

S, P, H = T.S, TP.P, TP.H
hexid, write_csv = TP.hexid, TP.write_csv
from shared import review_workbook as W              # noqa: E402

STUDIO = ("Request Id", "Client Account Number", "Client Number", "Process Status", "Sub Worktype", "End Date",
          "Case Source", "Team")


# ---------------------------------------------------------------------------------------------- the Studio report

def test_the_studio_report_is_read_with_request_id_and_its_case_source(base):
    csv = write_csv(base / "studio.csv", [(hexid(1), "AB100000", "C1", "Ended", "Change of Address", "", "Email", "T1"),
                                          (hexid(2), "AB200000", "C2", "Ended", "Change of Address", "", "", "T1"),
                                          (hexid(3), "AB300000", "C3", "Voided", "Change of Address", "", "Post", "T1")],
                    header=STUDIO)
    cases, skipped = P.read_case_list(csv)
    assert [(c["iid"], c["case_source"]) for c in cases] == [(hexid(1), "Email"), (hexid(2), "")]
    assert skipped[0]["case_source"] == "Post" and skipped[0]["reason"] == "Process Status is Voided"


def test_the_old_id_spellings_still_work_and_the_first_source_column_is_the_case_source(base):
    old = write_csv(base / "old.csv", [(hexid(1), "AB100000", "C1", "Ended", "Change of Address")])
    cases, _ = P.read_case_list(old)
    assert cases[0]["iid"] == hexid(1) and cases[0]["case_source"] == ""          # no source column: optional
    two = write_csv(base / "two.csv", [(hexid(1), "AB100000", "Ended", "X", " Fax  in ", "Email")],
                    header=("Instruction ID", "Client Account Number", "Process Status", "Sub Worktype",
                            "Request  source", "Other Source"))
    cases, _ = P.read_case_list(two)
    assert cases[0]["case_source"] == "Fax in"                                      # first match, spaces tidied
    with pytest.raises(P.CsvError, match="Request Id"):
        P.read_case_list(write_csv(base / "none.csv", [("x", "AB1", "Ended", "X")],
                                   header=("Case", "Client Account Number", "Process Status", "Sub Worktype")))


def test_the_case_source_line_goes_under_the_case_timing_or_first(base):
    msg = "Review.\n\n- Case end: 1\n- Case age statement: aged\n\nThese values apply."
    out = P.with_case_source(msg, "Email")
    lines = out.split("\n")
    assert lines[3] == "- Case age statement: aged" and lines[4] == P.case_source_line("Email")
    assert lines[4] == "- Case source (where the customer request came in, from the Studio report): Email"
    plain = P.with_case_source("Review this case.", "  ")
    assert plain.startswith("- Case source") and plain.split("\n")[0].endswith(": not stated") and "Review this case." in plain


def test_a_copilot_pipeline_message_carries_the_case_source(env2):
    w = TP.FeedWorld(env2, 1)
    plan = P.read_plan(w.paths)
    plan["cases"][0]["case_source"] = "Email"
    P.write_json(w.paths.plan, plan)
    w.feed = S.PipelineFeed(w.paths.run, types.SimpleNamespace(cfg=env2.cfg, policy=env2.policy, red=env2.red,
                                                                rt=env2.rt, log=env2.log), S.local_salt(env2.rt))
    w.finish_case(0)
    w.control.finished("part1")
    w.hand.step()
    r = w.runner()
    T.run(r.run(collections.deque(w.feed.poll())))
    message = env2.world.sends[0][2]
    assert P.case_source_line("Email") in message and "Please review this synthetic case" in message
    case = w.feed.cases[0]
    assert w.feed.info(case)["case_source"] == "Email" and w.feed.case_source(case) == "Email"


# ---------------------------------------------------------------------------------------------- plan, hand-over, report

def test_the_plan_records_the_reviewer_and_the_hand_over_the_case_source(base):
    paths, control = TP.make_paths(base)
    g, contract = TP.guidance(base, "Change of Address")
    cases = [{"iid": hexid(1), "account": "AB100000", "client_number": "", "status": "Ended", "row": {},
              "row_number": 2, "sub_worktype_csv": "Change of Address", "end_date": "", "case_source": "Post"}]
    plan = H.build_plan(cases, [], paths, set(), g, contract, reviewer="azure")
    assert plan["reviewer"] == "azure" and plan["cases"][0]["case_source"] == "Post"
    assert H.build_plan(cases, [], paths, set(), g, contract)["reviewer"] == "copilot"
    P.write_json(paths.plan, plan)
    hand = H.Handover(paths, control, plan, g, contract)
    TP.fake_download(paths, hexid(1), "AB100000")
    TP.fake_snapshot(paths, "AB100000")
    control.finished("part1")
    hand.step()
    assert P.read_json(paths.cases / f"{hexid(1)}.json")["case_source"] == "Post"
    assert any("handed to the Azure reviewer" in e["text"] for e in control.new_events())


def test_the_daily_report_words_azure_outcomes_and_uses_only_this_runs_azure_files(base):
    paths, control = TP.make_paths(base)
    rows = [{"iid": hexid(i), "account": f"AB{i}00000", "client_number": "", "status": "Ended", "row": {},
             "row_number": i + 1, "sub_worktype_csv": "Change of Address", "needs": "download", "note": "",
             "case_source": "Email" if i == 1 else ""} for i in (1, 2, 3, 4)]
    rows.append({**rows[0], "iid": hexid(5), "row_number": 6, "needs": "done", "note": "sent in an earlier run"})
    stale = paths.runtime_root / P.AZURE_RESULTS / f"{hexid(3)}.json"
    P.write_json(stale, {"iid": hexid(3), "state": "result_correct"})              # an earlier run's answer
    old = (datetime.datetime.now() - datetime.timedelta(days=1)).timestamp()
    os.utime(stale, (old, old))
    plan = {"run": paths.run.name, "day": P.today(), "created": P.now(), "reviewer": "azure", "cases": rows,
            "skipped": [], "parts": {}}
    P.write_json(paths.sub("status") / "part3-results.json", {hexid(1): {"state": "result_incorrect", "reason": "r1"}})
    P.write_json(paths.runtime_root / P.AZURE_RESULTS / f"{hexid(2)}.json", {"iid": hexid(2), "state": "result_correct",
                                                                             "reason": "fallback"})
    control.hold(hexid(4), "a .msg file cannot be read", "part3")
    report = {r["instruction_id"]: r for r in P.daily_report(paths, plan, control)}
    assert report[hexid(1)]["outcome"] == "reviewed - Azure: Incorrect" and report[hexid(1)]["case_source"] == "Email"
    assert report[hexid(2)]["outcome"] == "reviewed - Azure: Correct" and report[hexid(2)]["detail"] == "fallback"
    assert report[hexid(2)]["case_source"] == P.CASE_SOURCE_NOT_STATED
    assert report[hexid(3)]["outcome"] == "not reached - run ended"                # the stale file is not this run's
    assert report[hexid(4)]["outcome"] == "held" and ".msg" in report[hexid(4)]["detail"]
    assert report[hexid(5)]["outcome"] == "already reviewed earlier"
    assert P.workload_line(list(report.values())) == "Reviewed 2 of 5 'Ended' case(s) (5 row(s), 0 excluded); 2 need you"
    assert list(P.REPORT_COLUMNS).index("case_source") == list(P.REPORT_COLUMNS).index("sub_worktype") + 1


# ---------------------------------------------------------------------------------------------- the workbook

AZURE_RESULT = {"REVIEW_RESULT": "Inconclusive", "DISCREPANCY_TYPE": "None", "HEADLINE": "Account number unreadable",
                "CASE_END_DATETIME": "Not provided", "REGISTER_SNAPSHOT_DATETIME": "Not provided",
                "CLIENT_REQUEST": "Change the address", "JUSTIFICATION": "x",
                "A_B_COMPARISONS": [{"ITEM": "Account number", "Customer_expectation": "AB1?", "CUSTOMER_SOURCE": "D1",
                                     "System_data": "AB100000", "SYSTEM_SOURCE": "Account Summary", "MATCH": None,
                                     "EXPLANATION": "read with low confidence", "FIELD_IDS": ["D1.account_number"]}],
                "RULE_CHECKS": [],
                "DOCUMENTS": [{"DOC_ID": "D1", "FILE": "form.pdf", "KIND": "form", "FORM": "change-address-form",
                               "PAGES": 4, "PAGES_READ": 4}],
                "EXTRACTED_FIELDS": [{"FIELD_ID": "D1.account_number", "DOC_ID": "D1", "FORM": "change-address-form",
                                      "FIELD": "account_number", "LABEL": "Account number", "VALUE": "AB1?",
                                      "HANDWRITTEN": True, "CONFIDENCE": 0.41, "LOW_CONFIDENCE": True,
                                      "CITATIONS": ["D1.p1.l5"]},
                                     {"FIELD_ID": "D1.postcode", "DOC_ID": "D1", "FORM": "change-address-form",
                                      "FIELD": "postcode", "LABEL": "Postcode", "VALUE": "AB1 2CD",
                                      "HANDWRITTEN": True, "CONFIDENCE": 0.97, "LOW_CONFIDENCE": False,
                                      "CITATIONS": ["D1.p1.l9"]}]}
INFO = {"iid": "a" * 32, "account": "AB100000", "sub_worktype": "Change of Address", "client_number": "C1",
        "gap": "", "age": "", "case_source": "Email"}


def cells(sheet):
    return [[text for text, _ in row] for row in sheet.rows]


def test_the_azure_workbook_has_the_copilot_tabs_headed_azure_plus_documents_and_fields():
    sheets = W.build([(INFO, AZURE_RESULT)])
    assert [s.name for s in sheets] == list(W.AZURE_SHEETS)
    cases = cells(sheets[1])
    head = cases[0]
    assert "Azure result" in head and "Copilot result" not in head and len(head) == len(W.CASE_WIDTHS)
    row = dict(zip(head, cases[1]))
    assert row["Azure result"] == "Inconclusive" and row["Case source"] == "Email"
    assert "Azure result" in cells(sheets[2])[0] and "Azure result" in cells(sheets[3])[0]
    assert cells(sheets[3])[1][-1] == "Fields read: D1.account_number"
    docs, fields = cells(sheets[4]), cells(sheets[5])
    assert docs[1][:5] == ["a" * 32, "D1", "form.pdf", "form", "change-address-form"]
    f = {r[1]: dict(zip(fields[0], r)) for r in fields[1:]}
    assert f["D1.account_number"]["Confidence"] == "0.41" and f["D1.account_number"]["Low confidence (could not compare)"] == "Yes"
    assert sheets[5].rows[1][0][1][1] == W.YELLOW and sheets[5].rows[2][0][1][1] == ""      # low confidence in yellow
    assert ["Azure review results", cells(sheets[0])[0][1]] == cells(sheets[0])[0]
    data = W.xlsx_bytes([(INFO, AZURE_RESULT)])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "xl/worksheets/sheet6.xml" in z.namelist()
        assert 'name="Extracted fields"' in z.read("xl/workbook.xml").decode("utf-8")


def test_the_copilot_workbook_keeps_its_four_tabs_and_heading_with_the_case_source_column():
    copilot = {k: v for k, v in AZURE_RESULT.items() if k not in ("REVIEW_RESULT", "DOCUMENTS", "EXTRACTED_FIELDS")}
    sheets = W.build([(INFO, {**copilot, "COPILOT_RESULT": "Correct"})])
    assert [s.name for s in sheets] == list(W.SHEETS) and cells(sheets[1])[0] == W.CASE_HEAD
    assert W.CASE_HEAD.index("Case source") == W.CASE_HEAD.index("Client Number") + 1
    assert cells(sheets[0])[0][0] == "Copilot review results"


def test_the_azure_workbook_has_its_own_file_and_is_set_aside_alone(base):
    (base / W.NAME).write_bytes(b"copilot")
    (base / W.AZURE_NAME).write_bytes(b"azure")
    moved = W.make_room(base, wait=lambda p: None, say=lambda t: None, name=W.name_for("azure"))
    assert moved.name.startswith("Azure review results - older ") and (base / W.NAME).read_bytes() == b"copilot"
    book = W.ReviewWorkbook(base, reviewer="azure")
    assert book.add(INFO, AZURE_RESULT) and book.close() == base / W.AZURE_NAME
    with zipfile.ZipFile(base / W.AZURE_NAME) as z:
        assert len([n for n in z.namelist() if n.startswith("xl/worksheets/")]) == 6


# ---------------------------------------------------------------------------------------------- settings and check

def test_the_azure_reviewer_needs_the_result_contract(env):
    cfg = types.SimpleNamespace(**{**vars(env.cfg), "reviewer": "azure", "azure_auth": "signin",
                                   "azure_docintel_endpoint": "https://di.example.invalid",
                                   "azure_openai_endpoint": "https://ai.example.invalid",
                                   "azure_form_model": "form", "azure_review_model": "review",
                                   "completion_mode": "json_result"})
    _, problems = S.validate_config(cfg)
    assert not problems, problems
    cfg.result_contract_path, cfg.completion_mode = "", "submission_only"
    _, problems = S.validate_config(cfg)
    assert any("Azure reviewer needs result_contract_path" in p for p in problems)


def test_check_runs_the_azure_reviewers_own_check(monkeypatch, base, capsys):
    script = base / "azure_case_reviewer.py"
    monkeypatch.setattr(S, "azure_script", lambda: script)
    assert S.azure_check(run=lambda *a, **k: types.SimpleNamespace(returncode=0)) is False      # missing
    script.write_text("pass", "utf-8")
    seen = []

    def fake(cmd, **kw):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=1)
    assert S.azure_check(run=fake) is False and seen[0][-1] == "check" and seen[0][1:4] == ["-B", "-E", "-s"]
    assert S.azure_check(run=lambda *a, **k: types.SimpleNamespace(returncode=0)) is True
    assert "AZURE REVIEWER CHECK" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- the coordinator

FAKE_AZURE = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, {project!r})
from shared import pipeline as P
assert sys.argv[1:3] == ["run", "--pipeline"], sys.argv
run = Path(sys.argv[3])
cfg = json.loads(Path({config!r}).read_text("utf-8"))
paths = P.Paths(cfg["runtime_root"], cfg["cases_root"], run); control = P.Control(paths); plan = P.read_plan(paths)
assert plan["reviewer"] == "azure"
seq = control.request_check()
while not all(control.check_answer(p, seq) for p in ("part1", "part2")):
    time.sleep(0.05)
control.go()
wanted, results = [c["iid"] for c in plan["cases"] if c["needs"] in ("download", "send")], {{}}
while True:
    closed = control.feed_closed()
    for iid in wanted:
        m = P.read_json(paths.cases / (iid + ".json"))
        if m and iid not in results and (paths.cases_root / m["folder"]).is_dir():
            results[iid] = dict(state="result_correct", reason="source " + m.get("case_source", ""), left_out_files=0)
    if closed:
        break
    time.sleep(0.1)
P.write_json(paths.sub("status") / "part3-results.json", results)
'''


def test_a_whole_run_with_the_azure_reviewer(base, monkeypatch, capsys):
    cases_root, runtime = base / "cases", base / "runtime"
    cases_root.mkdir()
    runtime.mkdir()
    g, contract = TP.guidance(base, "Change of Address")
    msg = base / "base_message.md"
    msg.write_text("Please review. {{CASE_END_DATETIME}} {{REGISTER_SNAPSHOT_DATETIME}} {{BUSINESS_DAYS_GAP}} "
                   "{{CASE_AGE_STATEMENT}}", "utf-8")
    daily = base / "daily_review_instructions.md"
    daily.write_text("daily instructions", "utf-8")
    config = base / "sender_config.json"
    config.write_text(json.dumps({
        "cases_root": str(cases_root), "runtime_root": str(runtime), "base_message_path": str(msg),
        "daily_instructions_path": str(daily), "python_executable": sys.executable, "edge_executable": sys.executable,
        "approved_root": str(base), "guidance_dir": str(g), "result_contract_path": str(contract),
        "reviewer": "azure", "azure_auth": "signin", "azure_docintel_endpoint": "https://di.example.invalid",
        "azure_openai_endpoint": "https://ai.example.invalid", "azure_form_model": "form", "azure_review_model": "review",
        "cdp_port": 9223, "systems_cdp_port": 9223,            # the same port is fine: no Copilot Edge with Azure
        "system1_url": "https://system1.example.invalid/app/inbox", "system1_brand_text": "Brand",
        "system2_url": "https://system2.example.invalid/home", "console_detail": "full",
        "system2_account_page": "https://system2.example.invalid/account?id={account}",
        "system2_party_marker": "partyprofile",
        "system2_party_links_page": "https://system2.example.invalid/links?party={party}",
        "system2_customer_summary_page": "https://system2.example.invalid/customer?id={customer}",
        "system2_detail_pages": {n: f"https://system2.example.invalid/detail{i}?id={{account}}"
                                 for i, n in enumerate(P.DETAIL_SECTIONS)}}), "utf-8")
    (cases_root / W.NAME).write_bytes(b"copilot's, left alone")
    (cases_root / W.AZURE_NAME).write_bytes(b"older azure run")
    (runtime / "sent_instructions.txt").write_text(f"{hexid(1)}\tsubmitted\tx\n", "utf-8")   # Copilot's: not Azure's
    (runtime / P.AZURE_LEDGER).write_text(f"{hexid(4)}\tresult_correct\tx\n", "utf-8")
    csv = BH.write_bam(base / "BAM Report.xlsx", [BH.case(hexid(1), "AB100000", client="C1", source="Email"),
                                                  BH.case(hexid(2), "AB200000", "Voided", client="C2", source="Post"),
                                                  BH.case(hexid(4), "AB400000", client="C4", source="Post")])
    fakes = {}
    for name, src, part in (("part1", TP.FAKE_PART, "part1"), ("part2", TP.FAKE_PART, "part2"), ("azure", FAKE_AZURE, "")):
        fakes[name] = base / f"fake_{name}.py"
        fakes[name].write_text(textwrap.dedent(src.format(project=str(TP.PROJECT), config=str(config), part=part)), "utf-8")
    saved_env, saved_tmp, saved_path = dict(os.environ), __import__("tempfile").tempdir, list(sys.path)
    monkeypatch.setenv("SENDER_CONFIG", str(config))
    R = TP.load("run_pipeline", "orchestrator/run_pipeline.py")
    sender = sys.modules["copilot_case_sender"]
    monkeypatch.setattr(sender, "ensure_dependencies", lambda *a: None)
    monkeypatch.setattr(sender, "cdp_http", lambda *a, **k: None)
    monkeypatch.setattr(R, "open_systems_edge", lambda cfg, rt, used="12": "started (fake)")
    monkeypatch.setattr(R, "PART_SCRIPTS", {"part1": fakes["part1"], "part2": fakes["part2"]})
    monkeypatch.setattr(R, "AZURE_SCRIPT", fakes["azure"])
    monkeypatch.setattr(R, "SENDER_SCRIPT", base / "the-copilot-sender-must-not-start.py")
    try:
        code = R.main(["--report", str(csv), "--mode", "full", "--scope", "all"])
    finally:
        sender.WRITE_GUARD["roots"] = None
        os.environ.clear()
        os.environ.update(saved_env)
        __import__("tempfile").tempdir, sys.path[:] = saved_tmp, saved_path
        sys.dont_write_bytecode, sys.pycache_prefix = False, None
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Starting Azure reviewer" in out
    report = next((runtime / "reports").glob("daily-report-*.csv")).read_text("utf-8-sig").splitlines()
    head = report[0].split(",")
    by_id = {dict(zip(head, line.split(",")))["instruction_id"]: dict(zip(head, line.split(","))) for line in report[1:]}
    assert by_id[hexid(1)]["outcome"] == "reviewed - Azure: Correct" and by_id[hexid(1)]["detail"] == "source Email"
    assert by_id[hexid(1)]["case_source"] == "Email" and by_id[hexid(2)]["outcome"] == "excluded automatically"
    assert by_id[hexid(4)]["outcome"] == "already reviewed earlier"           # the Azure ledger, not Copilot's
    files = sorted(p.name for p in cases_root.iterdir() if p.is_file())
    assert W.NAME in files and any(n.startswith("Azure review results - older ") for n in files)
    plan = json.loads(next((runtime / "pipeline" / "runs").glob("*/plan.json")).read_text("utf-8"))
    assert plan["reviewer"] == "azure"


def test_the_pipeline_settings_for_azure_skip_the_copilot_port_and_need_the_contract(monkeypatch, base):
    R = sys.modules.get("run_pipeline") or TP.load("run_pipeline", "orchestrator/run_pipeline.py")
    cfg = types.SimpleNamespace(**{**S.DEFAULTS, "guidance_dir": "g", "system1_url": "https://a.invalid",
                                   "system1_brand_text": "b", "system2_url": "https://b.invalid",
                                   "system2_account_page": "https://b.invalid/{account}", "system2_party_marker": "p",
                                   "system2_party_links_page": "https://b.invalid/{party}",
                                   "system2_customer_summary_page": "https://b.invalid/{customer}",
                                   "system2_detail_pages": {n: "https://b.invalid/{account}" for n in P.DETAIL_SECTIONS},
                                   "cdp_port": 9223, "systems_cdp_port": 9223, "result_contract_path": "",
                                   "completion_mode": "submission_only"})
    cfg.reviewer = "copilot"
    assert any("systems_cdp_port" in p for p in R.pipeline_problems(cfg))          # Copilot: two Edges
    cfg.reviewer = "azure"
    monkeypatch.setattr(R, "AZURE_SCRIPT", base / "missing.py")
    found = R.pipeline_problems(cfg)
    assert not any("systems_cdp_port" in p for p in found)
    assert any("result contract" in p for p in found) and any("is missing" in p for p in found)
    assert R.ledger_path(cfg, types.SimpleNamespace(root=base, ledger=base / "sent.txt")) == base / P.AZURE_LEDGER


# ---------------------------------------------------------------------------------------------- Run.cmd azure-test

def test_run_cmd_routes_azure_test_and_check():
    text = (TP.PROJECT / "Run.cmd").read_text("utf-8")
    assert 'if /i "%~1"=="azure-test" set "TARGET=%~dp0orchestrator\\azure_test.py"' in text
    assert all(ord(c) >= 32 or c in "\r\n\t" for c in text)


@pytest.fixture
def azure_test(base, monkeypatch):
    AT = TP.load("azure_test", "orchestrator/azure_test.py")
    files = {}
    for name in ("python.exe", "generate.py", "score.py", "azure_case_reviewer.py"):
        files[name] = base / "tools" / name
        files[name].parent.mkdir(exist_ok=True)
        files[name].write_text("x", "utf-8")
    monkeypatch.setattr(AT, "SYNTHETIC_PY", files["python.exe"])
    monkeypatch.setattr(AT, "GENERATE", files["generate.py"])
    monkeypatch.setattr(AT, "SCORE", files["score.py"])
    monkeypatch.setattr(AT, "ENGINE", files["azure_case_reviewer.py"])
    cfg = types.SimpleNamespace(**{**S.DEFAULTS, "runtime_root": str(base / "runtime"), "azure_auth": "signin",
                                   "azure_docintel_endpoint": "https://di.example.invalid",
                                   "azure_openai_endpoint": "https://ai.example.invalid",
                                   "azure_form_model": "form", "azure_review_model": "review"})
    monkeypatch.setattr(AT.sender, "load_config", lambda *a, **k: cfg)
    return types.SimpleNamespace(AT=AT, files=files, cfg=cfg, base=base)


def test_azure_test_runs_generate_review_score_and_reports_the_bar(azure_test, capsys):
    t, calls = azure_test, []

    def fake(cmd, cwd=None, **kw):
        calls.append((cmd, cwd))
        if cmd[4] == str(t.files["score.py"]):
            Path(cmd[cmd.index("--out") + 1]).write_text("report", "utf-8")
            return types.SimpleNamespace(returncode=1)
        return types.SimpleNamespace(returncode=3 if cmd[4] == str(t.files["azure_case_reviewer.py"]) else 0)
    code = t.AT.main(["azure-test", "--case-model", "case-review-astra", "--limit", "2"], run=fake)
    out = capsys.readouterr().out
    assert code == 1 and "PASS BAR MISSED" in out and "not ready for the VDI" in out
    gen, review, score = (c for c, _ in calls)
    out_dir = Path(gen[gen.index("--out") + 1])
    assert gen[0] == str(t.files["python.exe"]) and out_dir.parent == t.base / "runtime" / "azure-test"
    assert review[4:6] == [str(t.files["azure_case_reviewer.py"]), "review"] and review[1:4] == ["-B", "-E", "-s"]
    assert review[review.index("--csv") + 1] == str(out_dir / "studio.csv")
    assert review[review.index("--case-model") + 1] == "case-review-astra" and review[review.index("--limit") + 1] == "2"
    assert score[score.index("--expected") + 1] == str(out_dir / "expected.json")
    assert all(cwd == str(out_dir) for _, cwd in calls) and "scoring the answers it wrote" in out


def test_azure_test_stops_before_azure_when_the_generator_fails_or_pieces_are_missing(azure_test, capsys):
    t, calls = azure_test, []
    code = t.AT.main([], run=lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=2))
    assert code == 2 and len(calls) == 1 and "nothing was sent to Azure" in capsys.readouterr().out
    t.files["python.exe"].unlink()
    t.cfg.azure_openai_endpoint = "http://not-https.invalid"
    calls.clear()
    assert t.AT.main([], run=lambda cmd, **kw: calls.append(cmd)) == 2 and not calls
    out = capsys.readouterr().out
    assert ".synthetic-venv" in out and "azure_openai_endpoint must be an https:// address" in out
    assert "not-https" not in out                                                    # never an endpoint in the text
