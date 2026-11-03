"""Tests for the daily pipeline: shared contract, coordinator, Part 1 / Part 2 logic, Part 3's live feed, and one whole
run with real processes (fake parts standing in for the browsers).  Synthetic data only.

Run like test_sender.py (files are written ONLY under SENDER_TEST_ROOT):

    set SENDER_TEST_ROOT=<a folder on approved storage>
    <PYTHON_EXECUTABLE> -B -m pytest -p no:cacheprovider tests

What is real here: CSV parsing, plan, hand-over (folder moves, manifests), control signals, Part 3's queue/attachment
order with the real Runner, Part 2's Access-Denied / expired-sign-in decisions, the coordinator's processes and report.
What is faked: System 1, System 2 and Copilot pages.  Real-browser behaviour is NOT proven by this file.
"""
import asyncio
import collections
import importlib.util
import json
import os
import shutil
import sys
import textwrap
import threading
import types
import uuid
from pathlib import Path

import pytest

import test_sender as T                              # its fakes (FakeBrowser, FakeTab ...) and the loaded sender (T.S)
from test_sender import env, env2                    # noqa: F401  (pytest fixtures)

S = T.S
ROOT = T.ROOT
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from shared import pipeline as P                     # noqa: E402


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, PROJECT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


H = load("handover", "orchestrator/handover.py")
P1 = load("system1_documents", "1_system1_documents/system1_documents.py")


def hexid(n):
    return f"{n:032x}"


@pytest.fixture
def base():
    b = Path(ROOT) / f"p-{uuid.uuid4().hex[:8]}"
    b.mkdir(parents=True)
    yield b
    shutil.rmtree(b, ignore_errors=True)


def write_csv(path, rows, header=("instrcution id", "Client Account Number", "Client Number", "Process Status", "Sub Worktype")):
    lines = [",".join(header)] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", "utf-8-sig")
    return path


def plan_row(paths, iid, end_date):
    """Give a planned case its CSV End Date (as read_case_list would)."""
    plan = P.read_plan(paths)
    for c in plan["cases"]:
        if c["iid"] == iid:
            c["end_date"] = end_date
    P.write_json(paths.plan, plan)


def make_paths(base):
    paths = P.Paths.new_run(base / "runtime", base / "cases").create()
    return paths, P.Control(paths)


def guidance(base, *types_):
    g = base / "guidance"
    g.mkdir(exist_ok=True)
    for t in types_:
        (g / f"{t}.md").write_text(f"instructions for {t}", "utf-8")
    contract = g / "result_contract.md"
    contract.write_text("contract", "utf-8")
    return g, contract


def fake_download(paths, iid, account, sub="Change of Address", docs=("b form.pdf", "a letter.pdf")):
    """What Part 1 leaves behind for a finished case."""
    folder = paths.work / P.folder_name(iid, account)
    folder.mkdir(parents=True)
    for d in docs:
        (folder / d).write_bytes(b"%PDF-1.4 synthetic " + d.encode())
    (folder / "Audit Trail.pdf").write_bytes(b"%PDF-1.4 audit")
    P.write_json(paths.downloads / f"{iid}.json", {"iid": iid, "account": account, "folder": folder.name,
                                                  "sub_worktype": sub, "documents": list(docs), "audit": "Audit Trail.pdf",
                                                  "warnings": []})


def fake_snapshot(paths, account, day=None):
    P.write_json(paths.snapshot(account, day), {"Account Summary": {"metadata": {"requestedAccount": account}}})


# ---------------------------------------------------------------------------------------------- the daily CSV

def test_only_ended_rows_are_reviewed_and_every_other_row_is_listed_with_its_reason(base):
    csv = write_csv(base / "daily.csv", [
        (hexid(1), "ab 123456", "C1", "Ended", "Change of Address"),
        (hexid(2), "AB223456", "C2", "Voided", "Change of Address"),
        (hexid(3), "AB323456", "C3", "", "Change of Address"),
        ("not-an-id", "AB423456", "C4", "Ended", "X"),
        (hexid(1), "AB123456", "C1", "Ended", "Change of Address"),
        (hexid(5), "", "C5", "Ended", "X"),
        (hexid(6).upper(), "AB623456", "C6", "ENDED", "Bank details"),
    ])
    cases, skipped = P.read_case_list(csv)
    assert [c["iid"] for c in cases] == [hexid(1), hexid(6)]
    assert cases[0]["account"] == "AB123456" and cases[0]["sub_worktype_csv"] == "Change of Address"
    reasons = {s["row_number"]: s["reason"] for s in skipped}
    assert reasons == {3: "Process Status is Voided", 4: "Process Status is blank",
                       5: "not a valid instruction ID (32 hex characters)",
                       6: "the same instruction ID is on an earlier row", 7: "no Client Account Number"}


def test_a_csv_without_a_needed_column_is_refused_with_the_column_named(base):
    csv = write_csv(base / "daily.csv", [(hexid(1), "AB1", "Ended")], header=("instruction id", "Client Account Number", "Status"))
    with pytest.raises(P.CsvError, match="Process Status, Sub Worktype"):
        P.read_case_list(csv)


def test_semicolon_csv_and_the_corrected_column_spelling_are_accepted(base):
    p = base / "daily.csv"
    p.write_text("Instruction ID;Client Account Number;Process Status;Sub Worktype\n"
                 f"{hexid(9)};AB999999;Ended;Change of Address\n", "utf-8")
    cases, skipped = P.read_case_list(p)
    assert [c["iid"] for c in cases] == [hexid(9)] and not skipped


def test_guidance_file_is_found_by_its_sub_worktype_loosely_and_never_the_contract(base):
    g, contract = guidance(base, "Change of Address", "Bank-Details")
    assert P.guidance_for("change of  address", g).name == "Change of Address.md"
    assert P.guidance_for("Bank Details", g).name == "Bank-Details.md"
    assert P.guidance_for("Result Contract", g, exclude=[contract]) is None
    assert P.guidance_for("Unknown type", g) is None


# ---------------------------------------------------------------------------------------------- control signals

def test_signals_check_answers_pause_resume_stop_and_events(base):
    paths, control = make_paths(base)
    seq = control.request_check()
    control.answer_check("part1", seq, False, "no System 1 tab")
    assert control.check_answer("part1", seq)["problem"] == "no System 1 tab"
    assert control.check_answer("part1", seq + 1) is None
    p = control.pause("part2", "expired")
    assert control.pause("part2", "again") == p           # one pause at a time
    assert control.paused()["reason"] == "expired"
    control.resume(p)
    assert control.paused() is None and control.pause("part2", "later") == p + 1
    control.stop("part2", "Access Denied")
    control.stop("coordinator", "other")
    assert control.stopped()["reason"] == "Access Denied"  # the first reason is the one shown
    control.event("part1", "one")
    with open(paths.sub("events") / "part1.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"part": "part1", "text": "half')         # a line still being written
    assert [e["text"] for e in control.new_events()] == ["one"]
    with open(paths.sub("events") / "part1.jsonl", "a", encoding="utf-8") as fh:
        fh.write(' line"}\n')
    assert [e["text"] for e in control.new_events()] == ["half line"]


# ---------------------------------------------------------------------------------------------- plan + hand-over

def test_plan_marks_sent_complete_foreign_and_new_cases(base):
    paths, _ = make_paths(base)
    g, contract = guidance(base, "Change of Address")
    cases = [{"iid": hexid(i), "account": f"AB{i}00000", "client_number": "", "status": "Ended", "row": {}, "row_number": i,
              "sub_worktype_csv": "Change of Address" if i != 4 else "Mystery"} for i in range(1, 5)]
    (paths.cases_root / f"{hexid(2)} - AB200000").mkdir()             # complete from an earlier run
    P.write_json(paths.cases / f"{hexid(2)}.json", {"folder": f"{hexid(2)} - AB200000"})
    (paths.cases_root / f"{hexid(3)} - old").mkdir()                  # made by something else
    plan = H.build_plan(cases, [], paths, {hexid(1)}, g, contract)
    assert [c["needs"] for c in plan["cases"]] == ["done", "send", "held", "download"]
    assert plan["no_guidance"] == ["Mystery"] and plan["parts"] == {"part1": True, "part2": True}


def plan_for(paths, *specs):
    cases = [{"iid": iid, "account": acc, "client_number": "", "status": "Ended", "row": {}, "row_number": n,
              "sub_worktype_csv": "Change of Address", "needs": "download", "note": ""} for n, (iid, acc) in enumerate(specs, 2)]
    plan = {"run": paths.run.name, "day": P.today(), "cases": cases, "skipped": [], "parts": {"part1": True, "part2": True}}
    P.write_json(paths.plan, plan)
    return plan


def test_a_case_is_handed_over_only_when_documents_and_snapshot_are_both_there(base):
    paths, control = make_paths(base)
    g, contract = guidance(base, "Change of Address")
    plan = plan_for(paths, (hexid(1), "AB100000"))
    hand = H.Handover(paths, control, plan, g, contract)
    fake_download(paths, hexid(1), "AB100000")
    assert hand.step() is False and not any(paths.cases_root.glob(hexid(1) + "*"))   # no snapshot yet
    fake_snapshot(paths, "AB100000")
    assert hand.step() is False                              # handed, but Part 1 is not finished: feed stays open
    dest = paths.cases_root / f"{hexid(1)} - AB100000"
    assert sorted(p.name for p in dest.iterdir()) == ["AB100000" + P.SNAPSHOT_SUFFIX, "Audit Trail.pdf", "a letter.pdf", "b form.pdf"]
    assert not (paths.work / dest.name).exists()
    m = P.read_json(paths.cases / f"{hexid(1)}.json")
    assert m["order"] == ["AB100000" + P.SNAPSHOT_SUFFIX, "Audit Trail.pdf", "b form.pdf", "a letter.pdf"]
    assert Path(m["guidance"]).name == "Change of Address.md"
    control.finished("part1")
    assert hand.step() is True and control.feed_closed()


def test_cases_without_snapshot_guidance_or_download_are_held_with_a_plain_reason(base):
    paths, control = make_paths(base)
    g, contract = guidance(base, "Change of Address")
    plan = plan_for(paths, (hexid(1), "AB100000"), (hexid(2), "AB200000"), (hexid(3), "AB300000"))
    hand = H.Handover(paths, control, plan, g, contract)
    fake_download(paths, hexid(1), "AB100000")                          # Part 2 failed this account
    fake_download(paths, hexid(2), "AB200000", sub="Mystery")           # no instructions file
    fake_snapshot(paths, "AB200000")
    P.write_json(paths.sub("status") / "part2-failed.json", {"AB100000": "RuntimeError: HTTP 500"})
    hand.step()
    assert set(control.held()) == {hexid(2)}                          # Part 2 still running: case 1 waits
    control.finished("part2")
    control.finished("part1")                                         # case 3 was never downloaded
    assert hand.step() is True
    held = control.held()
    assert "no System 2 snapshot (RuntimeError: HTTP 500)" in held[hexid(1)]["reason"]
    assert "Mystery" in held[hexid(2)]["reason"]
    assert "not downloaded" in held[hexid(3)]["reason"]
    assert not any(p.name.startswith(hexid(1)) for p in paths.cases_root.iterdir())


def test_a_stop_hands_nothing_over_and_leaves_the_case_in_work(base):
    paths, control = make_paths(base)
    g, contract = guidance(base, "Change of Address")
    plan = plan_for(paths, (hexid(1), "AB100000"))
    fake_download(paths, hexid(1), "AB100000")
    fake_snapshot(paths, "AB100000")
    control.stop("part2", "Access Denied")
    assert H.Handover(paths, control, plan, g, contract).step() is True
    assert (paths.work / f"{hexid(1)} - AB100000").is_dir() and control.feed_closed()


def test_a_crashed_part_counts_as_finished(base):
    paths, control = make_paths(base)
    g, contract = guidance(base, "Change of Address")
    plan = plan_for(paths, (hexid(1), "AB100000"))
    hand = H.Handover(paths, control, plan, g, contract, alive=lambda part: False)
    assert hand.step() is True and "not downloaded" in control.held()[hexid(1)]["reason"]


def test_the_daily_report_has_one_line_per_csv_row(base):
    paths, control = make_paths(base)
    plan = plan_for(paths, (hexid(1), "AB100000"), (hexid(2), "AB200000"), (hexid(3), "AB300000"))
    plan["skipped"] = [{"row_number": 9, "iid": hexid(9), "account": "AB9", "client_number": "", "status": "Voided",
                        "sub_worktype_csv": "x", "reason": "Process Status is Voided"}]
    P.write_json(paths.sub("status") / "part3-results.json",
                 {hexid(1): {"state": "result_incorrect", "reason": "2 differences", "left_out_files": 3}})
    control.hold(hexid(2), "no System 2 snapshot", "coordinator")
    rows = H.daily_report(paths, plan, control)
    assert [(r["instruction_id"], r["outcome"]) for r in rows] == [
        (hexid(1), "sent - Copilot: Incorrect"), (hexid(2), "held"), (hexid(3), "not reached - run ended"),
        (hexid(9), "skipped")]
    assert rows[0]["left_out_files"] == 3
    out = H.write_report(rows, base / "reports", "r1")
    assert out.read_text("utf-8-sig").splitlines()[0].startswith("row,instruction_id")
    assert P.workload_line(rows) == "Reviewed 1 of 3 'Ended' case(s) (4 CSV row(s), 1 skipped); 2 need you"


def test_the_pipeline_report_shows_what_system1_and_system2_saw(base):
    paths, control = make_paths(base)
    plan = plan_for(paths, (hexid(1), "AB100000"), (hexid(2), "AB200000"))
    P.write_json(paths.diagnostics("part1") / f"{hexid(1)}.json", {
        "iid": hexid(1), "status": "FAILED", "failure": "Downloaded 0 of 1", "sub_worktype": "Bank Details", "tries": [
            {"attempt": 1, "status": "FAILED", "captures": [f"{hexid(1)}-attempt1-failed.png"], "events": [
                {"at": "2026-11-02T12:00:00", "event": "document-result", "name": "a.pdf", "failure": "the document window opened",
                 "directRoute": "the viewer address answered HTTP 403", "attempts": [{"address": "https://h/x?id=…", "http": 403}]}]}]})
    P.write_json(paths.diagnostics("part2") / "accounts.json", {"accounts": {"AB100000": {"state": "reused (made earlier today)"}}})
    control.event("part1", "case downloaded")
    paths.captures.mkdir(parents=True)
    (paths.captures / f"{hexid(1)}-attempt1-failed.png").write_bytes(b"png")
    rows = H.daily_report(paths, plan, control)
    out = H.pipeline_report(paths, plan, rows, None)
    text = out.read_text("utf-8")
    assert out.parent == paths.logs and out.name == f"pipeline-report-{paths.run.name}.md"
    for part in ("Reviewed 0 of 2", "**FAILED**", "a.pdf: NOT DOWNLOADED - the document window opened",
                 "direct route: the viewer address answered HTTP 403", '"http": 403', "reused (made earlier today)",
                 "case downloaded", f"- {hexid(1)}-attempt1-failed.png", f"- {hexid(2)}: no record (not reached)"):
        assert part in text, part


# ---------------------------------------------------------------------------------------------- Part 3: the live feed

class FeedWorld:
    """A real PipelineFeed over a T.Env, plus the coordinator's Handover, for Runner tests."""

    def __init__(self, env, n_cases, completion="submission_only"):
        self.env = env
        g, _ = guidance(env.base, "Change of Address")
        self.guidance = g
        env.cfg.result_contract_path = str(env.contract)
        self.paths, self.control = P.Paths.new_run(env.runtime, env.cases_root).create(), None
        self.control = P.Control(self.paths)
        self.specs = [(hexid(i), f"AB{i}00000") for i in range(1, n_cases + 1)]
        self.plan = plan_for(self.paths, *self.specs)
        self.hand = H.Handover(self.paths, self.control, self.plan, g, env.contract)
        ctx = types.SimpleNamespace(cfg=env.cfg, policy=env.policy, red=env.red, rt=env.rt, log=env.log)
        self.feed = S.PipelineFeed(self.paths.run, ctx, S.local_salt(env.rt))

    def finish_case(self, i):
        iid, acc = self.specs[i]
        fake_download(self.paths, iid, acc)
        fake_snapshot(self.paths, acc)
        self.hand.step()

    def runner(self):
        r = self.env.runner()
        r.feed = self.feed
        return r


def test_cases_arriving_during_the_run_are_sent_each_with_its_own_instructions_in_order(env2):
    env = env2                               # answers collected: the result contract is attached too
    w = FeedWorld(env, 2)
    w.finish_case(0)
    r = w.runner()
    first = w.feed.poll()

    async def later():                       # case 2 is finished by Part 1 while case 1 is being sent
        while not env.world.sends:
            await asyncio.sleep(0.01)
        w.finish_case(1)
        w.control.finished("part1")
        w.hand.step()

    async def go():
        t = asyncio.ensure_future(later())
        res = await r.run(collections.deque(first))
        await t
        return res
    results = T.run(go())
    assert sorted(s for s, _ in results.values()) == ["result_correct", "result_correct"]
    names = [c[1] for c in env.world.attach_calls]
    snap = "AB100000" + P.SNAPSHOT_SUFFIX
    assert names[0] == ["daily_review_instructions.md", "Change of Address.md", "result_contract.md", snap, "Audit Trail.pdf",
                        "b form.pdf", "a letter.pdf"]
    assert names[1][3] == "AB200000" + P.SNAPSHOT_SUFFIX
    assert w.feed.done


def test_over_the_limit_the_last_documents_are_left_out_never_the_snapshot_or_audit_trail(env):
    env.cfg.max_attachments = 4
    w = FeedWorld(env, 1)
    w.finish_case(0)
    w.control.finished("part1")
    w.hand.step()
    r = w.runner()
    T.run(r.run(collections.deque(w.feed.poll())))
    sent = env.world.attach_calls[0][1]          # send-only mode: no result contract, so 4 = 2 instructions + 2 files
    assert sent == ["daily_review_instructions.md", "Change of Address.md", "AB100000" + P.SNAPSHOT_SUFFIX, "Audit Trail.pdf"]
    assert r.partial


def test_a_stop_sends_nothing_new_but_still_collects_the_answer_already_sent(env2):
    env = env2
    w = FeedWorld(env, 2)
    w.finish_case(0)
    w.finish_case(1)
    env.world.behaviors = [{"gen_polls": 5}]                   # case 1's answer takes a few polls
    r = w.runner()
    sent = r.mark_sent

    def sent_then_stop(case, state):                          # System 2 says Access Denied right after case 1 went out
        sent(case, state)
        w.control.stop("part2", "System 2 refused access")
    r.mark_sent = sent_then_stop
    results = T.run(r.run(collections.deque(w.feed.poll())))  # both cases complete at the start
    states = sorted(s for s, _ in results.values())
    assert len(env.world.sends) == 1
    assert "result_correct" in states and "not_sent_stopped" in states


def test_confirm_ready_reports_what_is_missing_and_starts_only_when_every_part_is_ready(env, capsys):
    w = FeedWorld(env, 1)
    stdin = T.FakeStdin(["", ""])
    answers = iter([(False, "2 System 1 tabs are open - close all but one"), (True, "")])

    async def parts():                       # Part 1 and Part 2 answering each check
        done = 0
        while not w.control.is_go():
            seq = w.control.check_seq()
            if seq > done:
                ok, problem = next(answers)
                w.control.answer_check("part1", seq, ok, problem)
                w.control.answer_check("part2", seq, True, "")
                done = seq
            await asyncio.sleep(0.01)

    async def go():
        t = asyncio.ensure_future(parts())
        await w.feed.confirm_ready(stdin, env.log, T.FakeStop())
        await t
    T.run(go())
    out = capsys.readouterr().out
    assert "close all but one" in out and w.control.is_go()


def test_a_pause_is_shown_and_your_enter_resumes_it(env):
    w = FeedWorld(env, 1)
    seq = w.control.pause("part2", "System 2 sign-in expired")
    w.control.event("part1", "case downloaded")

    async def go():
        t = asyncio.ensure_future(w.feed.relay(env.log, T.FakeStdin([""])))
        for _ in range(100):
            if w.control.resumed() >= seq:
                break
            await asyncio.sleep(0.02)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    T.run(go())
    assert w.control.resumed() == seq
    assert any("case downloaded" in line for line in env.out) and any("sign-in expired" in line for line in env.out)


def test_discovery_never_treats_the_work_folder_as_a_case(env):
    (env.cases_root / ".work" / f"{hexid(1)} - AB1").mkdir(parents=True)
    env.make_case(2)
    cases, other, excluded = env.cases()
    assert [c.id for c in cases] == [hexid(2)] and excluded == 1 and not other


# ---------------------------------------------------------------------------------------------- Part 1 logic (no browser)

class P1Fakes:
    """System 1 page steps replaced by scripted ones; the case logic between them is the real one."""

    def __init__(self, mod, monkeypatch, account="AB100000", sub="Change of Address", fail_audit=0, on_docs=None):
        self.calls = collections.Counter()

        async def page_back(*a, **k):
            return "page"

        async def search_open(context, page, iid, events, check_sub=None):
            reason = check_sub(sub) if check_sub else ""
            if reason:                                  # the real one holds BEFORE clicking View
                raise mod.CaseHeld(reason)
            return page, sub, "Overview"

        async def verified(context, page, iid, events, timeout=45):
            return page, {"accountNumber": account, "instructionId": iid}

        async def detect(page):
            return "Overview"

        async def section(context, page, name, events):
            return page

        async def documents(context, page, folder, events):
            for n in ("b form.pdf", "a letter.pdf"):
                (folder / n).write_bytes(b"%PDF")
            if on_docs:
                on_docs()
            return [{"source_text": "View", "saved_file": "b form.pdf"}, {"source_text": "View", "saved_file": "a letter.pdf"}]

        async def audit(context, page, folder, events):
            self.calls["audit"] += 1
            if self.calls["audit"] <= fail_audit:
                raise RuntimeError("Audit Trail click produced no file")
            (folder / "Audit Trail.pdf").write_bytes(b"%PDF")
            return folder / "Audit Trail.pdf", "direct"
        for name, fn in (("wait_inbox", page_back), ("search_open", search_open), ("verified_overview_identity", verified),
                         ("detect_open_section", detect), ("section", section), ("download_documents", documents),
                         ("download_audit", audit), ("open_or_recover_inbox", page_back),
                         ("ensure_clean_inbox_search", page_back)):
            monkeypatch.setattr(mod, name, fn)

        async def back(context, page, events):
            return page
        monkeypatch.setattr(mod, "return_to_my_inbox", back)


def p1_setup(base):
    paths, control = make_paths(base)
    P1.configure(types.SimpleNamespace(system1_url="https://system1.example.invalid/app/inbox", system1_brand_text="Brand",
                                       system1_brand_html="", cases_root=str(paths.cases_root)))
    return paths, control


def test_part1_downloads_into_work_and_reports_the_files_in_order(base, monkeypatch):
    paths, control = p1_setup(base)
    P1Fakes(P1, monkeypatch)
    case = {"iid": hexid(1), "account": "ab 100000", "sub_worktype_csv": "Change of Address"}
    _, result = T.run(P1.process_instruction(None, "page", case, 1, 1, control))
    assert result["status"] == "SUCCESS" and result["documents"] == ["b form.pdf", "a letter.pdf"]
    assert result["audit"] == "Audit Trail.pdf"
    assert (paths.work / f"{hexid(1)} - AB100000" / "Audit Trail.pdf").is_file()


def test_part1_holds_a_case_whose_account_differs_from_the_csv_without_retrying(base, monkeypatch):
    paths, control = p1_setup(base)
    fakes = P1Fakes(P1, monkeypatch, account="ZZ999999")
    case = {"iid": hexid(1), "account": "AB100000", "sub_worktype_csv": "x"}
    _, result = T.run(P1.process_instruction(None, "page", case, 1, 1, control))
    assert result["status"] == "HELD" and "different Client Account Number" in result["failure"]
    assert result["attempt"] == 1 and fakes.calls["audit"] == 0
    assert not any(paths.work.iterdir())


def test_part1_retries_a_failure_once_starting_the_case_folder_clean(base, monkeypatch):
    paths, control = p1_setup(base)
    P1Fakes(P1, monkeypatch, fail_audit=1)
    case = {"iid": hexid(1), "account": "AB100000", "sub_worktype_csv": "x"}
    _, result = T.run(P1.process_instruction(None, "page", case, 1, 1, control))
    assert result["status"] == "SUCCESS" and result["attempt"] == 2
    assert sorted(p.name for p in (paths.work / f"{hexid(1)} - AB100000").iterdir()) == \
        ["Audit Trail.pdf", "a letter.pdf", "b form.pdf"]                 # no "(2)" duplicates from the first try


def test_part1_stops_between_documents_and_audit_trail_when_the_run_stops(base, monkeypatch):
    paths, control = p1_setup(base)
    fakes = P1Fakes(P1, monkeypatch, on_docs=lambda: control.stop("part2", "Access Denied"))
    case = {"iid": hexid(1), "account": "AB100000", "sub_worktype_csv": "x"}
    _, result = T.run(P1.process_instruction(None, "page", case, 1, 1, control))
    assert result["status"] == "STOPPED" and fakes.calls["audit"] == 0


def test_part1_holds_a_case_without_guidance_before_anything_is_downloaded(base, monkeypatch):
    paths, control = p1_setup(base)
    g = base / "guidance"
    g.mkdir()
    (g / "Bank Details.md").write_text("x")
    P1.configure(types.SimpleNamespace(system1_url="https://system1.example.invalid/app/inbox", system1_brand_text="Brand",
                                       system1_brand_html="", cases_root=str(paths.cases_root), guidance_dir=str(g),
                                       result_contract_path=""))
    fakes = P1Fakes(P1, monkeypatch, sub="Unlisted Type")
    case = {"iid": hexid(1), "account": "AB100000", "sub_worktype_csv": "Unlisted Type"}
    _, result = T.run(P1.process_instruction(None, "page", case, 1, 1, control))
    assert result["status"] == "HELD" and result["attempt"] == 1 and fakes.calls["audit"] == 0
    assert "no instructions file for Sub Worktype 'Unlisted Type'" in result["failure"] and "not downloaded" in result["failure"]
    assert not paths.work.exists() or not any(paths.work.iterdir())
    fakes = P1Fakes(P1, monkeypatch, sub="Bank Details")                       # a type WITH guidance goes on as before
    _, result = T.run(P1.process_instruction(None, "page", dict(case, sub_worktype_csv="x"), 1, 1, control))
    assert result["status"] == "SUCCESS" and "events" in result["tries"][0]


class Resp:
    def __init__(self, body: bytes, status=200, ctype="application/pdf", disp=""):
        self.status, self.ok, self._body = status, 200 <= status < 300, body
        self.headers = {"content-type": ctype, **({"content-disposition": disp} if disp else {})}

    async def body(self):
        return self._body


class Ctx:
    """A browser context whose signed-in GET answers from a table (address -> Resp)."""
    def __init__(self, table):
        self.table, self.asked = table, []
        ctx = self

        class Request:
            async def get(self, url, timeout=0):
                ctx.asked.append(url)
                return ctx.table.get(url) or Resp(b"not found", 404, "text/plain")
        self.request = Request()


def test_part1_direct_route_reads_the_file_inside_a_viewer_page_and_records_every_request(base):
    paths, _ = p1_setup(base)
    folder = base / "f"
    folder.mkdir()
    o = "https://system1.example.invalid"
    ctx = Ctx({o + "/app/view?documentEntityId=1&token=SECRET": Resp(b"<!doctype html><iframe src='/files/1/content'></iframe>", ctype="text/html"),
               o + "/files/1/content": Resp(b"PK\x03\x04 word", ctype="application/octet-stream",
                                            disp='attachment; filename="Letter.docx"')})
    item = {"documentName": "Letter", "viewerUrl": "/app/view?documentEntityId=1&token=SECRET", "pairing": "id"}
    saved, how = T.run(P1.fast_direct_document(ctx, item, folder, asyncio.Semaphore(1)))
    assert saved.name == "Letter.docx" and saved.read_bytes() == b"PK\x03\x04 word" and how == "direct-file-inside-viewer-page"
    a = item["attempts"]
    assert [x["looksLike"] for x in a] == ["html", "office/zip"] and a[0]["http"] == 200
    assert "SECRET" not in json.dumps(a) and a[0]["address"].endswith("?documentEntityId=…&token=…")
    item = {"documentName": "x.pdf", "viewerUrl": o + "/app/view?documentEntityId=2", "pairing": "position"}
    saved, how = T.run(P1.fast_direct_document(ctx, item, folder, asyncio.Semaphore(1)))
    assert saved is None and "HTTP 404" in how and item["attempts"][0]["http"] == 404
    saved, how = T.run(P1.fast_direct_document(ctx, {"documentName": "y.pdf", "pairing": "none"}, folder, asyncio.Semaphore(1)))
    assert saved is None and "no viewer address" in how and "pairing: none" in how


class Popup:
    """A document window: its address, what its page shows, and whether it has its own toolbar Download button."""
    def __init__(self, url, shown=(), toolbar=False):
        self.url, self.shown, self.toolbar, self.frames, self.clicked = url, list(shown), toolbar, [None], []

    async def wait_for_load_state(self, *a, **k):
        pass

    async def evaluate(self, script, *a):
        if script == P1.TOOLBAR_DOWNLOAD_JS:
            return {"found": self.toolbar, "controls": [{"tag": "a", "name": "Terms (a link inside the document)"}]}
        return list(self.shown)

    def locator(self, sel):
        self.clicked.append(sel)
        raise AssertionError("nothing may be clicked in this test")

    def on(self, event, fn):
        pass

    def is_closed(self):
        return getattr(self, "closed", False)

    async def close(self):
        self.closed = True


class Download:
    def __init__(self, data, name):
        self.data, self.suggested_filename = data, name

    async def save_as(self, path):
        Path(path).write_bytes(self.data)


class S1Page:
    """System 1's case page: its Received Documents rows (what document_rows reads) and what a View click does."""
    def __init__(self, ctx, rows, on_view):
        self.ctx, self.rows, self.on_view, self.marked = ctx, rows, on_view, None
        self.listeners, self.context = collections.defaultdict(list), type("NoCursor", (), {})()   # no visual cursor here

    async def evaluate(self, script, arg=None):
        if "data-s4-document-view" in script:       # mark_document_view
            self.marked = arg
            return True
        return [dict(r) for r in self.rows]          # document_rows

    def on(self, event, fn):
        self.listeners[event].append(fn)

    def remove_listener(self, event, fn):
        self.listeners[event].remove(fn)

    def fire(self, event, value):
        for fn in list(self.listeners[event]):
            fn(value)

    def locator(self, sel):
        page = self

        class L:
            def __init__(self):
                self.page = page

            async def click(self, **k):
                page.on_view(page, page.marked)
        return L()


def test_part1_whole_document_step_direct_window_and_straight_download(base):
    """The real download_documents end to end (only the browser is scripted): this is the step that crashed on the VDI."""
    paths, _ = p1_setup(base)
    folder = base / "f"
    folder.mkdir()
    o = "https://system1.example.invalid"
    ctx = Ctx({o + "/view/a": Resp(b"%PDF-a"), o + "/view/b": Resp(b"<html>viewer, nothing inside</html>", ctype="text/html"),
               o + "/files/b.pdf": Resp(b"%PDF-b")})
    windows = []

    def on_view(page, row):
        if row["documentId"] == "id-b":              # a document window showing the PDF (Edge's viewer)
            win = Popup(o + "/files/b.pdf")
            windows.append(win)
            ctx.pages.append(win)
            page.fire("popup", win)
        else:                                       # a Word file: downloads straight away
            page.fire("download", Download(b"PK\x03\x04", "c.docx"))
    rows = [{"index": 0, "documentName": "A.pdf", "documentId": "id-a", "viewerUrl": o + "/view/a", "pairing": "id", "viewerUrlsOnPage": 2},
            {"index": 1, "documentName": "B.pdf", "documentId": "id-b", "viewerUrl": o + "/view/b", "pairing": "id", "viewerUrlsOnPage": 2},
            {"index": 2, "documentName": "C.docx", "documentId": "id-c", "pairing": "none", "viewerUrlsOnPage": 2}]
    page = S1Page(ctx, rows, on_view)
    ctx.pages = [page]
    events = []
    out = T.run(P1.download_documents(ctx, page, folder, events))
    assert [r["saved_file"] for r in out] == ["A.pdf", "B.pdf", "C.docx"]
    assert [r["status"] for r in out] == ["direct-viewer-download", "file-at-document-window-address", "ui-view-downloaded-directly"]
    results = [e for e in events if e["event"] == "document-result"]
    assert [e["name"] for e in results] == ["A.pdf", "B.pdf", "C.docx"] and results[0]["directRoute"] == "ok"
    assert "page" in results[1]["directRoute"] and "no viewer address" in results[2]["directRoute"]
    assert windows and all(w.closed for w in windows) and not any(w.clicked for w in windows)
    assert not page.listeners["popup"] and not page.listeners["download"]          # listeners removed again


def test_part1_a_program_error_is_not_retried_and_keeps_its_trace(base, monkeypatch):
    paths, control = p1_setup(base)
    fakes = P1Fakes(P1, monkeypatch)

    async def broken(context, page, folder, events):
        raise TypeError("event() got multiple values for argument 'name'")
    monkeypatch.setattr(P1, "download_documents", broken)
    case = {"iid": hexid(1), "account": "AB100000", "sub_worktype_csv": "x"}
    _, result = T.run(P1.process_instruction(None, "page", case, 1, 1, control))
    assert result["status"] == "FAILED" and result["attempt"] == 1 and len(result["tries"]) == 1 and fakes.calls["audit"] == 0
    assert result["failure"].startswith("PROGRAM ERROR (not retried) - TypeError")
    assert "Traceback" in result["tries"][0]["trace"] and "test_pipeline.py" in result["tries"][0]["trace"]


def test_part1_stops_when_the_same_program_error_hits_a_second_case(base, monkeypatch):
    paths, control = p1_setup(base)
    bug = "PROGRAM ERROR (not retried) - TypeError: boom"
    outcome = {hexid(1): bug, hexid(2): "no instructions file for Sub Worktype 'X'", hexid(3): bug}

    async def fake(context, page, case, i, n, control):
        return page, {"status": "FAILED" if outcome[case["iid"]] == bug else "HELD", "failure": outcome[case["iid"]],
                      "program_error": outcome[case["iid"]] == bug, "tries": []}
    monkeypatch.setattr(P1, "process_instruction", fake)
    said, counts = [], {"done": 0, "held": 0}
    todo = [{"iid": hexid(n), "sub_worktype_csv": "x"} for n in range(1, 6)]
    T.run(P1.run_cases(None, "page", todo, paths, control, lambda text, *a, **k: said.append(text), counts))
    held = control.held()
    assert counts == {"done": 0, "held": 5} and set(held) == {hexid(n) for n in range(1, 6)}
    assert "not reached - Part 1 stopped on a program error" in held[hexid(4)]["reason"]
    assert any(s.startswith("PART 1 STOPPED") and "2 case(s) not reached" in s for s in said)
    assert not any(hexid(4) in s and "downloading" in s for s in said)


class InboxPage:
    """My Inbox after a search: each evaluate of the state script returns the next scripted state (the last one stays)."""
    def __init__(self, states, url="https://system1.example.invalid/app/inbox"):
        self.states, self.url, self.clicked = list(states), url, []

    def locator(self, sel):
        class Field:
            async def fill(self, v):
                self.v = v

            async def input_value(self):
                return self.v

            async def press(self, key):
                pass
        return Field()

    async def evaluate(self, script, arg=None):
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if script == P1.SINGLE_VIEW_JS:
            return {"count": st["views"], "subWorkType": "Bank Details" if st["views"] == 1 else ""}
        return {"text": "", "loading": False, "password": False, "filters": ["My tasks", "Active", "Waiting"], **st}

    async def eval_on_selector(self, sel, script):
        self.clicked.append(sel)


def search(monkeypatch, page):
    async def same(*a, **k):
        return page
    monkeypatch.setattr(P1, "ensure_clean_inbox_search", same)
    monkeypatch.setattr(P1, "live", same)
    monkeypatch.setattr(P1, "SEARCH_WAIT", 0.01)
    monkeypatch.setattr(P1, "SEARCH_SETTLE", 1.0)
    monkeypatch.setattr(P1, "SEARCH_STABLE", 0.3)
    events = []
    return events


def test_my_inbox_saying_empty_is_held_with_its_filters_and_never_clicked(base, monkeypatch):
    p1_setup(base)
    empty = {"views": 0, "text": "Tasks Your inbox is empty. There are no tasks available. You can start one of the following processes:"}
    page = InboxPage([{"views": 0}, {"views": 0}, {"views": 0, "loading": True}, empty])   # nothing, loading, then the label
    events = search(monkeypatch, page)
    with pytest.raises(P1.CaseHeld, match="not in My Inbox under the current filters") as held:
        T.run(P1.search_open(None, page, hexid(6), events))
    assert "My tasks, Active, Waiting" in str(held.value) and not page.clicked
    assert [e["state"] for e in events if e["event"] == "search-state"] == ["none", "loading", "empty"]


def test_a_single_view_that_appears_late_is_used_and_other_states_are_named(base, monkeypatch):
    p1_setup(base)
    page = InboxPage([{"views": 0}, {"views": 0, "loading": True}, {"views": 1}])
    events = search(monkeypatch, page)
    with pytest.raises(P1.CaseHeld, match="no instructions"):                 # stops just before the click
        T.run(P1.search_open(None, page, hexid(6), events, check_sub=lambda s: "no instructions for " + s))
    assert any(e["event"] == "single-view-found-after-fixed-wait" and e["subWorkType"] == "Bank Details" for e in events)
    page = InboxPage([{"views": 3}])
    with pytest.raises(P1.CaseHeld, match="showed 3 results"):
        T.run(P1.search_open(None, page, hexid(6), search(monkeypatch, page)))
    page = InboxPage([{"views": 0, "loading": True}])                           # never settles: a failure (retried once)
    with pytest.raises(RuntimeError, match="no View and no 'inbox is empty' label") as err:
        T.run(P1.search_open(None, page, hexid(6), search(monkeypatch, page)))
    assert not isinstance(err.value, P1.CaseHeld) and "page state: loading" in str(err.value)
    page = InboxPage([{"views": 0, "password": True}])
    with pytest.raises(RuntimeError, match="page state: signin"):
        T.run(P1.search_open(None, page, hexid(6), search(monkeypatch, page)))


def test_every_report_and_capture_opens_with_the_reviewer_note():
    assert "never copy" in P.REVIEWER_NOTE and "access codes" in P.REVIEWER_NOTE
    assert P.html_note("<html></html>").startswith("<!-- FOR THE REVIEWING COPILOT") and "--" not in P.html_note("")[4:].split("-->")[0]
    assert T.S.reviewer_note().startswith("> FOR THE REVIEWING COPILOT")


def test_part1_document_window_is_read_without_clicking_inside_the_document(base):
    paths, _ = p1_setup(base)
    folder = base / "f"
    folder.mkdir()
    o = "https://system1.example.invalid"
    ctx = Ctx({o + "/doc/7.pdf": Resp(b"%PDF-1.7"), o + "/blob/8": Resp(b"\x89PNG..", ctype="image/png")})
    win = Popup(o + "/doc/7.pdf")                                           # Edge's PDF viewer: the window shows the PDF's address
    saved, how = T.run(P1.read_document_popup(ctx, win, folder, "Form.pdf", [], []))
    assert saved.name == "Form.pdf" and how == "file-at-document-window-address" and not win.clicked
    win = Popup(o + "/viewer/8", shown=[o + "/blob/8"])                    # an image shown inside a viewer page
    ctx.table[o + "/viewer/8"] = Resp(b"<html><a href='/elsewhere'>link</a></html>", ctype="text/html")
    saved, how = T.run(P1.read_document_popup(ctx, win, folder, "scan", [], []))
    assert saved.name == "scan.png" and how == "file-shown-in-document-window" and not win.clicked
    win, tries = Popup(o + "/viewer/9"), []
    ctx.table[o + "/viewer/9"] = Resp(b"<html>document with links</html>", ctype="text/html")
    saved, how = T.run(P1.read_document_popup(ctx, win, folder, "z.pdf", tries, []))
    assert saved is None and "neither its address" in how and not win.clicked   # no toolbar button: links are never tried
    assert tries[-1]["documentWindowToolbar"]["found"] is False and o + "/elsewhere" not in ctx.asked


def test_strip_query_keeps_parameter_names_only():
    assert P.strip_query("https://h.example/a/b?code=SECRET&state=x#frag") == "https://h.example/a/b?code=…&state=…"
    assert P.strip_query("https://h.example/a") == "https://h.example/a"
    assert "SECRET" not in P.strip_queries("GET https://h.example/cb?token=SECRET failed (see https://h.example/x?y=SECRET2)")


def test_system1_sub_worktype_wins_over_the_csv_and_the_difference_is_reported():
    assert P1.choose_sub_worktype("Bank Details", "bank details") == ("Bank Details", [])
    sub, warn = P1.choose_sub_worktype("Bank Details", "Change of Address")
    assert sub == "Bank Details" and "System 1's is used" in warn[0]
    sub, warn = P1.choose_sub_worktype(P.UNKNOWN_SUB, "Change of Address")
    assert sub == "Change of Address" and "no Sub WorkType" in warn[0]


def test_part1_sign_in_check_wants_exactly_one_system1_tab_showing_my_inbox(base):
    p1_setup(base)

    class Page:
        def __init__(self, url, text="My Inbox"):
            self.url, self.text = url, text

        def is_closed(self):
            return False

        def locator(self, _):
            page = self

            class L:
                async def inner_text(self, timeout=0):
                    return page.text
            return L()
    s1, s2 = "https://system1.example.invalid/app/case/x", "https://system2.example.invalid/home"
    check = lambda pages: T.run(P1.check_system1(types.SimpleNamespace(pages=pages)))
    assert check([Page(s2)])[0] is False and "no System 1 tab" in check([Page(s2)])[1]
    assert "close all but one" in check([Page(s1), Page(s1), Page(s2)])[1]
    assert "My Inbox" in check([Page(s1, "Sign in")])[1]
    assert check([Page(s1), Page(s2)]) == (True, "")


# ---------------------------------------------------------------------------------------------- Part 2 logic (no browser)

@pytest.fixture
def p2():
    pytest.importorskip("bs4")
    pytest.importorskip("requests")
    mod = load("system2_snapshots", "2_system2_snapshots/system2_snapshots.py")
    mod.load_libs()
    mod.configure(types.SimpleNamespace(system2_url="https://system2.example.invalid/home", systems_cdp_port=9223,
                                        system2_account_page="https://system2.example.invalid/account?id={account}",
                                        system2_party_marker="partyprofile",
                                        system2_party_links_page="https://system2.example.invalid/links?party={party}"))
    return mod


def page(mod, html, status=200, url="https://system2.example.invalid/x"):
    return types.SimpleNamespace(status_code=status, url=url), mod.BeautifulSoup(html, "html.parser")


def test_part2_tells_access_denied_from_an_expired_sign_in_from_a_normal_page(p2):
    ok = "<html><title>Account</title><body><a>Logout</a><p>Status: Not authorised</p></body></html>"
    p2.judge(*page(p2, ok))                                                   # a field saying 'Not authorised' is fine
    with pytest.raises(p2.AccessDenied):
        p2.judge(*page(p2, "<title>Error</title><body>Logout</body>", status=403))
    with pytest.raises(p2.AccessDenied):
        p2.judge(*page(p2, "<title>Access Denied</title><body><a>Logout</a></body>"))
    with pytest.raises(p2.SessionExpired):
        p2.judge(*page(p2, "<title>Sign in</title><body>Enter your password</body>", url="https://login.example.invalid/"))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        p2.judge(*page(p2, "<body>Logout</body>", status=500))


def test_part2_keeps_the_page_system2_answered_with_when_an_account_fails(p2, base, monkeypatch):
    html = "<html><title>Access Denied</title><body>Request https://system2.example.invalid/cb?ticket=SECRET refused</body></html>"
    r, s = page(p2, html, status=403, url="https://system2.example.invalid/account?id=AB1&sso=SECRET")
    r.text, r.apparent_encoding = html, "utf-8"
    monkeypatch.setattr(p2, "GATE", types.SimpleNamespace(check=lambda: None))
    monkeypatch.setattr(p2, "session", lambda: types.SimpleNamespace(get=lambda *a, **k: r))
    monkeypatch.setattr(p2, "BeautifulSoup", lambda text, parser: s)
    with pytest.raises(p2.AccessDenied) as err:
        p2.soup_get("https://system2.example.invalid/account?id=AB1")
    monkeypatch.setattr(p2, "CAPTURES", base / "captures")
    names = p2.capture("AB1", err.value)
    assert names == ["system2-AB1.txt", "system2-AB1.html"]
    text = (base / "captures" / "system2-AB1.txt").read_text("utf-8")
    assert "HTTP 403" in text and "Access Denied" in text and "SECRET" not in text and "?id=…&sso=…" in text
    assert "SECRET" not in (base / "captures" / "system2-AB1.html").read_text("utf-8")
    assert p2.capture("AB2", RuntimeError("no page kept")) == []


class FakeControl:
    def __init__(self):
        self.stop_rec, self.events, self.pause_seq, self.resumed_seq = None, [], 0, 0

    def stopped(self):
        return self.stop_rec

    def stop(self, by, reason):
        self.stop_rec = self.stop_rec or {"by": by, "reason": reason}

    def event(self, part, text, level="minimal"):
        self.events.append(text)

    def pause(self, by, reason):
        self.pause_seq += 1
        return self.pause_seq

    def resumed(self):
        return self.resumed_seq


def test_part2_access_denied_twice_stops_the_whole_run_and_every_other_request(p2):
    control = FakeControl()
    p2.GATE = p2.Gate(control, None)
    calls = collections.Counter()

    def denied(a):
        calls[a] += 1
        raise p2.AccessDenied("System 2 refused access (HTTP 403)")
    with pytest.raises(p2.AccessDenied):
        p2.with_retry(denied, "AB1")
    assert calls["AB1"] == 2                                  # one retry only
    assert control.stop_rec["by"] == "part2" and "not hammered" in control.stop_rec["reason"]
    with pytest.raises(p2.RunStopped):
        p2.GATE.check()                                       # every other worker stops at its next request


def test_part2_an_expired_sign_in_waits_for_your_enter_then_continues_with_fresh_cookies(p2, monkeypatch):
    control = FakeControl()
    p2.GATE = p2.Gate(control, "sender")
    monkeypatch.setattr(p2, "auth", lambda sender: ([{"name": "c", "value": "new"}], "UA"))
    state = {"n": 0}

    def flaky(a):
        state["n"] += 1
        if state["n"] == 1:
            raise p2.SessionExpired("System 2 sign-in expired")
        return "ok"
    threading.Timer(0.3, lambda: setattr(control, "resumed_seq", 1)).start()   # you press Enter
    gen = p2.COOKIE_GEN
    assert p2.with_retry(flaky, "AB1") == "ok"
    assert control.pause_seq == 1 and p2.COOKIE_GEN == gen + 1 and p2.COOKIES[0]["value"] == "new"


def test_part2_builds_every_page_address_from_the_pages_pasted_in_setup(p2, monkeypatch):
    asked = []
    html = ("<html><body><a>Logout</a><div class='account-details'><h1>Acct</h1></div>"
            "<div class='panel panel-default'><div class='panel-heading'>Interested Parties</div><div class='panel-body'>"
            "<table><tr><th>Full Name</th></tr><tr><td><a href='/x/partyprofile?InterestedPartyid=P 7'>A B</a></td></tr>"
            "<tr><td><a href='/x/other?InterestedPartyid=9'>C</a></td></tr></table></div></div></body></html>")
    monkeypatch.setattr(p2, "soup_get", lambda url, params=None: (asked.append(url) or page(p2, html)))
    d = p2.fetch_account("AB 1/2", [])
    assert asked == ["https://system2.example.invalid/account?id=AB%201%2F2"]
    parties = d["_interestedPartyLinks"]
    assert [x["interestedPartyId"] for x in parties] == ["P 7"]            # only links with the pasted marker
    assert p2.link_url(parties[0]) == "https://system2.example.invalid/links?party=P%207"


def test_part2_reads_only_system2_cookies_from_the_systems_edge(p2):
    reply = {"cookies": [{"name": "a", "value": "1", "domain": ".example.invalid"},
                         {"name": "b", "value": "2", "domain": "system2.example.invalid"},
                         {"name": "c", "value": "3", "domain": "copilot.cloud.microsoft"}]}
    fake = types.SimpleNamespace(cdp_http=lambda port, path, t: {"webSocketDebuggerUrl": "ws://x", "User-Agent": "Edg/1"},
                                 endpoint_valid=lambda payload, port: "ws://127.0.0.1:9223/devtools/browser/1",
                                 cdp_call=lambda ws, port, method, params, timeout, target: reply)
    cookies, ua = p2.auth(fake)
    assert [c["name"] for c in cookies] == ["a", "b"] and ua == "Edg/1"
    fake.cdp_call = lambda *a, **k: {"cookies": [{"name": "c", "domain": "other.invalid"}]}
    with pytest.raises(p2.SessionExpired, match="sign in to System 2"):
        p2.auth(fake)


# ---------------------------------------------------------------------------------------------- one whole run, real processes

FAKE_PART = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, {project!r})
from shared import pipeline as P
part, run = {part!r}, Path(sys.argv[sys.argv.index("--run-dir") + 1])
cfg = json.loads(Path({config!r}).read_text("utf-8"))
paths = P.Paths(cfg["runtime_root"], cfg["cases_root"], run); control = P.Control(paths); plan = P.read_plan(paths)
answered = 0
while not control.is_go():
    seq = control.check_seq()
    if seq > answered:
        control.answer_check(part, seq, True, ""); answered = seq
    time.sleep(0.05)
for c in [c for c in plan["cases"] if c["needs"] == "download"]:
    if part == "part1":
        if c["account"] == "AB300000":
            control.hold(c["iid"], "System 1: View not found", "part1"); continue
        folder = paths.work / P.folder_name(c["iid"], c["account"]); folder.mkdir(parents=True)
        (folder / "doc.pdf").write_bytes(b"%PDF"); (folder / "Audit Trail.pdf").write_bytes(b"%PDF")
        P.write_json(paths.downloads / (c["iid"] + ".json"), dict(iid=c["iid"], account=c["account"], folder=folder.name,
                     sub_worktype=c["sub_worktype_csv"], documents=["doc.pdf"], audit="Audit Trail.pdf", warnings=[]))
        control.event("part1", c["iid"] + " downloaded"); time.sleep(0.2)
    else:
        P.write_json(paths.snapshot(c["account"], plan["day"]), dict(account=c["account"]))
control.finished(part)
'''

FAKE_SENDER = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, {project!r})
from shared import pipeline as P
run = Path(sys.argv[sys.argv.index("--pipeline") + 1])
cfg = json.loads(Path({config!r}).read_text("utf-8"))
paths = P.Paths(cfg["runtime_root"], cfg["cases_root"], run); control = P.Control(paths); plan = P.read_plan(paths)
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
            results[iid] = dict(state="result_correct", reason="", left_out_files=0)
    if closed:
        break
    time.sleep(0.1)
P.write_json(paths.sub("status") / "part3-results.json", results)
'''


def test_one_whole_run_coordinates_the_three_parts_and_reports_every_csv_row(base, monkeypatch, capsys):
    cases_root, runtime = base / "cases", base / "runtime"
    cases_root.mkdir()
    runtime.mkdir()
    g, contract = guidance(base, "Change of Address")
    msg = base / "base_message.md"
    msg.write_text("Please review. {{CASE_END_DATETIME}} {{REGISTER_SNAPSHOT_DATETIME}} {{BUSINESS_DAYS_GAP}} "
                   "{{CASE_AGE_STATEMENT}}", "utf-8")
    daily = base / "daily_review_instructions.md"
    daily.write_text("daily instructions", "utf-8")
    config = base / "sender_config.json"
    config.write_text(json.dumps({"cases_root": str(cases_root), "runtime_root": str(runtime), "base_message_path": str(msg),
                                  "daily_instructions_path": str(daily),
                                  "python_executable": sys.executable, "edge_executable": sys.executable,
                                  "approved_root": str(base), "guidance_dir": str(g), "result_contract_path": str(contract),
                                  "system1_url": "https://system1.example.invalid/app/inbox", "system1_brand_text": "Brand",
                                  "system2_url": "https://system2.example.invalid/home", "console_detail": "full",
                                  "system2_account_page": "https://system2.example.invalid/account?id={account}",
                                  "system2_party_marker": "partyprofile",
                                  "system2_party_links_page": "https://system2.example.invalid/links?party={party}",
                                  "system2_detail_pages": {n: f"https://system2.example.invalid/detail{i}?id={{account}}"
                                                           for i, n in enumerate(P.DETAIL_SECTIONS)}}), "utf-8")
    (cases_root / "Copilot review results.xlsx").write_bytes(b"older run")   # the coordinator sets it aside here
    csv = write_csv(base / "daily.csv", [(hexid(1), "AB100000", "C1", "Ended", "Change of Address"),
                                         (hexid(2), "AB200000", "C2", "Voided", "Change of Address"),
                                         (hexid(3), "AB300000", "C3", "Ended", "Change of Address"),
                                         (hexid(4), "AB400000", "C4", "Ended", "Change of Address")])
    fakes = {}
    for name, src, part in (("part1", FAKE_PART, "part1"), ("part2", FAKE_PART, "part2"), ("sender", FAKE_SENDER, "")):
        fakes[name] = base / f"fake_{name}.py"
        fakes[name].write_text(textwrap.dedent(src.format(project=str(PROJECT), config=str(config), part=part)), "utf-8")
    saved_env, saved_tmp, saved_path = dict(os.environ), __import__("tempfile").tempdir, list(sys.path)
    monkeypatch.setenv("SENDER_CONFIG", str(config))
    R = load("run_pipeline", "orchestrator/run_pipeline.py")
    sender = sys.modules["copilot_case_sender"]
    monkeypatch.setattr(sender, "ensure_dependencies", lambda *a: None)
    monkeypatch.setattr(sender, "cdp_http", lambda *a, **k: None)     # no real Edge: the pointer sweep finds none
    monkeypatch.setattr(R, "open_systems_edge", lambda cfg, rt: "started (fake)")
    monkeypatch.setattr(R, "PART_SCRIPTS", {"part1": fakes["part1"], "part2": fakes["part2"]})
    monkeypatch.setattr(R, "SENDER_SCRIPT", fakes["sender"])
    try:
        code = R.main(["--csv", str(csv)])
    finally:
        sender.WRITE_GUARD["roots"] = None
        os.environ.clear()
        os.environ.update(saved_env)
        __import__("tempfile").tempdir, sys.path[:] = saved_tmp, saved_path
        sys.dont_write_bytecode, sys.pycache_prefix = False, None
    out = capsys.readouterr().out
    assert code == 0, out
    report = next((runtime / "reports").glob("daily-report-*.csv")).read_text("utf-8-sig").splitlines()
    by_id = {line.split(",")[1]: line for line in report[1:]}
    assert "sent - Copilot: Correct" in by_id[hexid(1)] and "sent - Copilot: Correct" in by_id[hexid(4)]
    assert "skipped" in by_id[hexid(2)] and "Voided" in by_id[hexid(2)]
    assert "held" in by_id[hexid(3)] and "View not found" in by_id[hexid(3)]
    assert sorted(p.name for p in cases_root.iterdir() if p.is_dir() and p.name != ".work") == \
        [f"{hexid(1)} - AB100000", f"{hexid(4)} - AB400000"]
    older = [p.name for p in cases_root.iterdir() if p.is_file()]     # the results workbook lives in CASES_ROOT
    assert len(older) == 1 and older[0].startswith("Copilot review results - older ")
    assert not list((runtime / "reports").glob("*.xlsx"))              # never in RUNTIME_ROOT
    assert "SKIPPED - 1 row(s)" in out and "DAILY REPORT" in out
    assert "Visual pointer: the systems Edge did not answer" in out      # the sweep ran once the parts had ended


# ---------------------------------------------------------------------------------------------- case timing (End Date)

def test_the_csv_end_date_column_is_read_and_is_optional(base):
    with_end = write_csv(base / "a.csv", [(hexid(1), "AB100000", "C1", "Ended", "Change of Address", "46308.48958"),
                                          (hexid(2), "AB200000", "C2", "Ended", "Change of Address", "")],
                         header=("instruction id", "Client Account Number", "Client Number", "Process Status", "Sub Worktype",
                                 "End  date"))
    cases, _ = P.read_case_list(with_end)
    assert [c["end_date"] for c in cases] == ["46308.48958", ""]
    without = write_csv(base / "b.csv", [(hexid(1), "AB100000", "C1", "Ended", "Change of Address")])
    cases, _ = P.read_case_list(without)
    assert cases[0]["end_date"] == ""


def test_a_pipeline_case_gets_its_timing_in_the_message_its_answer_checked_and_a_workbook_row(env2):
    WB = sys.modules.get("shared.review_workbook") or __import__("shared.review_workbook", fromlist=["x"])
    env = env2
    w = FeedWorld(env, 1)
    iid, acc = w.specs[0]
    plan_row(w.paths, iid, "12/10/2026 02:17 PM")
    w.feed = S.PipelineFeed(w.paths.run, types.SimpleNamespace(cfg=env.cfg, policy=env.policy, red=env.red, rt=env.rt,
                                                                log=env.log), S.local_salt(env.rt))
    fake_download(w.paths, iid, acc)
    P.write_json(w.paths.snapshot(acc), {"Account Summary": {"metadata": {"requestedAccount": acc,
                                                                          "retrievedUtc": "2026-10-14T12:23:38.123456+00:00"}}})
    w.control.finished("part1")
    w.hand.step()
    good = T.answer_obj("Incorrect", end="12/10/2026 02:17 PM", snap="2026-10-14T12:23:38+00:00")
    wrong_end = {**good, "CASE_END_DATETIME": "Not provided"}
    env.world.behaviors = [{"answers": ["Working\n- compared\n" + json.dumps(wrong_end), json.dumps(good)]}]
    r = w.runner()
    r.workbook = WB.ReviewWorkbook(env.cases_root)
    results = T.run(r.run(collections.deque(w.feed.poll())))
    message = env.world.sends[0][2]
    assert "{{" not in message and "12/10/2026 02:17 PM" in message and "2026-10-14T12:23:38+00:00" in message
    assert "Case completed within the last 3 business days" in message and " 2:" in message      # Mon -> Wed = 2
    (state, note), = results.values()
    assert state == "result_incorrect" and "Potential processor error" in note and "CHECK" not in note
    tab = list(env.world.tabs.values())[0]
    assert len(tab.followups) == 1 and "must be copied" in tab.followups[0]     # the wrong copy was sent back once
    saved = json.loads((env.rt.results / f"{iid}.json").read_text("utf-8"))
    assert list(saved["result"]) == list(S.RESULT_KEYS)
    book = env.cases_root / WB.NAME
    import zipfile
    with zipfile.ZipFile(book) as z:
        sheet = z.read("xl/worksheets/sheet2.xml").decode("utf-8")
    assert iid in sheet and "No match" in sheet and "Case completed within the last 3 business days" in sheet
    assert r.workbook.close() == book


def test_a_case_without_an_end_date_is_sent_with_not_provided_and_flagged(env2):
    env = env2
    w = FeedWorld(env, 1)
    w.finish_case(0)
    w.control.finished("part1")
    w.hand.step()
    r = w.runner()
    results = T.run(r.run(collections.deque(w.feed.poll())))
    (state, note), = results.values()
    assert state == "result_correct" and "CHECK: no End Date in the CSV" in note
    assert env.world.sends[0][2].count("Not provided") == 4


# ---------------------------------------------------------------------------------------------- the purple pointer

S1_URL, S2_URL = "https://system1.example.invalid/app/inbox", "https://system2.example.invalid/home"
VC = sys.modules["studio_visual_cursor"]             # loaded by Part 1


class PointerFrame:
    """One document.  `pointer` = token of the pointer drawn in it (None = none).  `hang` = 'install' / 'remove' /
    'all': that call never answers (a frozen or busy tab)."""

    def __init__(self, pointer=None, hang=""):
        self.pointer, self.hang, self.page = pointer, hang, None

    async def evaluate(self, script, arg=None):
        if script == VC.INSTALL:
            if self.hang in ("install", "all"):
                await asyncio.sleep(3600)
            self.pointer = arg["token"]
        elif script == VC.REMOVE:
            if self.hang in ("remove", "all"):
                await asyncio.sleep(3600)
            found, self.pointer = int(self.pointer is not None), None
            return found


class PointerPage:
    def __init__(self, url, *frames):
        self.url, self.frames, self.handlers, self.closed = url, list(frames) or [PointerFrame()], {}, False
        for f in self.frames:
            f.page = self

    def on(self, name, fn):
        self.handlers.setdefault(name, []).append(fn)

    def remove_listener(self, name, fn):
        self.handlers[name].remove(fn)

    def is_closed(self):
        return self.closed

    def set_default_timeout(self, ms):
        pass

    async def evaluate(self, script, arg=None):
        return await self.frames[0].evaluate(script, arg)


class PointerContext:
    def __init__(self, *pages):
        self.pages, self.handlers = list(pages), []

    def on(self, name, fn):
        self.handlers.append(fn)

    def remove_listener(self, name, fn):
        self.handlers.remove(fn)


def system1_only(page):
    return page.url.startswith("https://system1.example.invalid/")


def test_the_pointer_is_drawn_on_system1_tabs_only_and_removed_from_every_tab_at_the_end():
    s1 = PointerPage(S1_URL)
    s2 = PointerPage(S2_URL, PointerFrame(pointer="left-by-an-earlier-run"))
    ctx = PointerContext(s1, s2)

    async def go():
        cur = await VC.VisualCursor.start(ctx, VC.CursorConfig(), only=system1_only)
        drawn = (s1.frames[0].pointer, s2.frames[0].pointer)
        await cur.move_to(s2, 5, 5)                          # a move on System 2 draws nothing there
        after_move = s2.frames[0].pointer
        first = await cur.close()
        second = await cur.close()                            # called twice: nothing new, no error
        return cur, drawn, after_move, first, second
    cur, drawn, after_move, first, second = asyncio.run(go())
    assert drawn == (cur._token, None) and after_move is None    # never on System 2; the old one there was removed
    assert s1.frames[0].pointer is None and s2.frames[0].pointer is None
    assert first == {"removed": 1, "absent": 1, "unreachable": 0} and second == first
    assert not ctx.handlers and not any(s1.handlers.values()) and not any(s2.handlers.values())   # no listener left


def test_a_system1_tab_that_goes_elsewhere_loses_the_pointer_at_once():
    s1 = PointerPage(S1_URL)
    ctx = PointerContext(s1)

    async def go():
        cur = await VC.VisualCursor.start(ctx, VC.CursorConfig(), only=system1_only)
        assert s1.frames[0].pointer == cur._token
        s1.url = S2_URL
        for fn in list(s1.handlers["framenavigated"]):
            fn(s1.frames[0])
        for _ in range(5):
            await asyncio.sleep(0)
        gone = s1.frames[0].pointer
        await cur.close()
        return gone
    assert asyncio.run(go()) is None


def test_a_tab_that_does_not_answer_cannot_hold_the_pointer_start_or_clean_up(monkeypatch):
    monkeypatch.setattr(VC, "STOP_SECONDS", 0.05)
    ok, stuck = PointerPage(S1_URL), PointerPage(S1_URL, PointerFrame(hang="all"))
    ctx = PointerContext(ok, stuck)

    async def go():
        cur = await asyncio.wait_for(VC.VisualCursor.start(ctx, VC.CursorConfig(), only=system1_only), 2)
        return await asyncio.wait_for(cur.close(), 2)
    assert asyncio.run(go()) == {"removed": 1, "absent": 0, "unreachable": 1}
    assert ok.frames[0].pointer is None


def test_a_switched_off_pointer_still_removes_one_left_by_an_earlier_run():
    s1 = PointerPage(S1_URL, PointerFrame(pointer="earlier"))

    async def go():
        cur = await VC.VisualCursor.start(PointerContext(s1), VC.CursorConfig(enabled=False), only=system1_only)
        return await cur.close()
    assert asyncio.run(go()) == {"removed": 1, "absent": 0, "unreachable": 0} and s1.frames[0].pointer is None


@pytest.mark.parametrize("outcome", ["finished", "stopped before go", "system1 failed", "pointer clean-up hangs"])
def test_part1_removes_the_pointer_from_both_tabs_on_every_way_out(base, monkeypatch, outcome):
    s1, s2 = PointerPage(S1_URL), PointerPage(S2_URL, PointerFrame(pointer="earlier"))
    ctx = PointerContext(s1, s2)
    paths = P.Paths.new_run(base / "runtime", base / "cases").create()
    cfg = types.SimpleNamespace(runtime_root=str(base / "runtime"), cases_root=str(base / "cases"), node_executable="",
                                systems_cdp_port=9223)
    stopped = []

    class PW:
        async def stop(self):
            stopped.append(True)

    async def start_driver(*a, **k):
        return PW(), "fake", 0.0

    async def connect(pw, sender, port):
        return types.SimpleNamespace(contexts=[ctx])

    async def go_signal(context, control):
        return outcome != "stopped before go"

    async def inbox(context, page=None):
        return s1

    async def cases(*a):
        if outcome == "system1 failed":
            raise RuntimeError("System 1 page changed")
    sender = types.SimpleNamespace(load_config=lambda: cfg, validate_config=lambda c: (None, []),
                                   bootstrap_storage=lambda c, p: None, load_playwright=lambda: (None,),
                                   start_driver=start_driver)
    monkeypatch.setitem(sys.modules, "copilot_case_sender", sender)     # what Part 1 imports inside run_pipeline
    monkeypatch.setattr(P1, "configure", lambda c: None)
    monkeypatch.setattr(P1, "ORIGIN", "https://system1.example.invalid")
    monkeypatch.setattr(P1, "connect_systems_edge", connect)
    monkeypatch.setattr(P1, "wait_for_go", go_signal)
    monkeypatch.setattr(P1, "open_or_recover_inbox", inbox)
    monkeypatch.setattr(P1, "run_cases", cases)
    monkeypatch.setattr(P1.P, "read_plan", lambda p: {"cases": []})
    if outcome == "pointer clean-up hangs":
        monkeypatch.setattr(P1, "CURSOR_CLOSE_SECONDS", 0.05)
        s1.frames[0].hang = "remove"
    if outcome == "system1 failed":
        with pytest.raises(RuntimeError, match="page changed"):
            asyncio.run(P1.run_pipeline(paths.run))
    else:
        asyncio.run(P1.run_pipeline(paths.run))
    events = (paths.sub("events") / "part1.jsonl").read_text("utf-8")
    assert stopped and s2.frames[0].pointer is None               # System 2: the old pointer is gone, never redrawn
    if outcome == "pointer clean-up hangs":
        assert "coordinator sweeps the tabs at the end" in events   # said so; the coordinator's sweep follows
    else:
        assert s1.frames[0].pointer is None and "visual pointer: removed from 1 page(s), none on 1, 0 did not answer" in events   # System 2 was cleaned at the start


def test_the_coordinator_sweeps_every_systems_edge_tab_only_after_part1_has_ended(monkeypatch):
    R = sys.modules.get("run_pipeline") or load("run_pipeline", "orchestrator/run_pipeline.py")
    tabs = [{"type": "page", "url": S1_URL, "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/1"},
            {"type": "page", "url": S2_URL, "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/2"},
            {"type": "page", "url": "edge://newtab/", "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/3"},
            {"type": "service_worker", "url": S1_URL, "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/4"}]
    pointers = {"1": 1, "2": 1}
    evaluated = []

    def page_eval(ws, port, expression, timeout):
        n = ws.rsplit("/", 1)[1]
        evaluated.append((n, port, expression))
        found, pointers[n] = pointers.get(n, 0), 0
        return found
    monkeypatch.setattr(R.sender, "cdp_http", lambda port, path, timeout=1.5: tabs if path == "/json/list" else None)
    monkeypatch.setattr(R.sender, "cdp_page_eval", page_eval)
    events = []
    control = types.SimpleNamespace(event=lambda part, text: events.append(text))
    cfg = types.SimpleNamespace(systems_cdp_port=9223)
    running = {"part1": types.SimpleNamespace(poll=lambda: None)}
    assert "still finishing" in R.sweep_pointer(cfg, running, control) and not evaluated   # never under Part 1's feet
    ended = {"part1": types.SimpleNamespace(poll=lambda: 1), "part2": types.SimpleNamespace(poll=lambda: 0)}
    assert R.sweep_pointer(cfg, ended, control) == "Visual pointer: removed from 2 tab(s) of the systems Edge, none on 0"
    assert sorted(n for n, _, _ in evaluated) == ["1", "2"] and {e for _, _, e in evaluated} == {VC.SWEEP}
    assert R.sweep_pointer(cfg, ended, control).endswith("removed from 0 tab(s) of the systems Edge, none on 2")  # twice
    monkeypatch.setattr(R.sender, "cdp_page_eval", lambda *a: None)
    assert R.sweep_pointer(cfg, ended, control).endswith("none on 0, 2 did not answer")
    monkeypatch.setattr(R.sender, "cdp_http", lambda *a, **k: None)
    assert "did not answer - no tab was swept" in R.sweep_pointer(cfg, ended, control) and len(events) == 5


DOM_TEST = r"""
const {JSDOM} = require(JSDOM_PATH);
const out = {};
const dom = new JSDOM('<!doctype html><body><p id="keep">page</p><iframe></iframe></body>', {runScripts: 'outside-only'});
const w = dom.window;
w.matchMedia = () => ({matches: true});
w.Element.prototype.animate = () => ({finished: Promise.resolve(), cancel(){}});
const install = w.eval('(' + INSTALL + ')'), remove = w.eval('(' + REMOVE + ')');
install(CFG);
out.drawn = w.document.querySelectorAll('[data-studio-visual-cursor]').length;
install({...CFG, token: 'run-2'});
out.afterSecondRun = w.document.querySelectorAll('[data-studio-visual-cursor]').length;
out.token = w.__studio_visual_cursor_v1__.token;
w.document.body.appendChild(w.document.createElement('div')).setAttribute('data-studio-visual-cursor', '');
out.removed = w.eval(SWEEP);
out.left = w.document.querySelectorAll('[data-studio-visual-cursor]').length + (w.__studio_visual_cursor_v1__ ? 1 : 0);
out.again = w.eval(SWEEP);
out.pageKept = !!w.document.getElementById('keep');
install(CFG);
w.dispatchEvent(new w.Event('pagehide'));
out.afterPagehide = w.document.querySelectorAll('[data-studio-visual-cursor]').length + (w.__studio_visual_cursor_v1__ ? 1 : 0);
out.removeFalse = remove(false);
console.log(JSON.stringify(out));
"""


def test_the_in_page_scripts_draw_replace_sweep_and_self_remove_in_a_real_dom(base):
    """The in-page scripts themselves, in a DOM (jsdom under node): another run's pointer is replaced, the sweep
    removes every pointer and nothing else, twice is safe, and leaving the page removes it.  Skipped unless
    JSDOM_NODE_MODULES names a node_modules folder holding jsdom (local development only, never on the VDI)."""
    import subprocess
    mods = os.environ.get("JSDOM_NODE_MODULES", "")
    if not (shutil.which("node") and mods and (Path(mods) / "jsdom").is_dir()):
        pytest.skip("set JSDOM_NODE_MODULES to a node_modules folder with jsdom")
    cfg = {**VC.asdict(VC.CursorConfig()), "token": "run-1", "report_prefix": "__p:", "initial_x": 24, "initial_y": 24}
    values = {"JSDOM_PATH": str(Path(mods) / "jsdom"), "INSTALL": VC.INSTALL, "REMOVE": VC.REMOVE, "SWEEP": VC.SWEEP,
              "CFG": cfg}
    script = base / "dom_test.js"
    script.write_text("".join(f"const {k} = {json.dumps(v)};\n" for k, v in values.items()) + DOM_TEST, "utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out == {"drawn": 1, "afterSecondRun": 1, "token": "run-2", "removed": 2, "left": 0, "again": 0,
                   "pageKept": True, "afterPagehide": 0, "removeFalse": 0}


# ---------------------------------------------------------------------------------------------- Part 2: the party profile
# A synthetic Interested Party profile in the shape System 2 sends it (no script run): the contact rows are built by
# the page's setup functions (shape from the sanitised sample, System_2_Claude_Handover.md + Copilot's answer); only
# headers and placeholders are rows.  Every value is made up.

import base64 as _b64

PNG = "data:image/png;base64," + _b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 24).decode()

SETUP_SCRIPT = """
var LastEmailId = 0;
$(document).ready(function () {
    hidePartyForm();
    AddExistingCustomerInterestedPartyEmails();
    AddExistingCustomerInterestedPartyPhoneNumbers();
    AddExistingCustomerInterestedPartyAddresses();
});
function AddExistingCustomerInterestedPartyEmails() {
    var newEmailContact;
    var newEmailTemplate = tmpl("NewEmailTemplate");
%(emails)s
}
function AddExistingCustomerInterestedPartyPhoneNumbers() {
    var newEmailContact;
    var newEmailTemplate = tmpl("NewPhoneTemplate");
%(phones)s
}
function AddExistingCustomerInterestedPartyAddresses() {
    var newEmailContact;
    var newEmailTemplate = tmpl("AddressessTemplate");
%(addresses)s
}
"""


def js_email(kind, address, entry="ID_EMAIL_A"):
    return ('    LastEmailId++;\n    newEmailContact = newEmailTemplate({\n        Id: ++LastEmailId,\n'
            f'        EmailType: "{kind}",\n        EmailAddress: "{address}",\n        EmailEntry: "{entry}"\n    }});\n'
            "    $('#EmailContacts tr:last').after(newEmailContact);\n")


def js_phone(kind, area, number):
    return ('    LastEmailId++;\n    newEmailContact = newEmailTemplate({\n        Id: ++LastEmailId,\n'
            f'        PhoneCountry: "999",\n        PhoneDbCountryId: "ID_COUNTRY_A",\n        PhoneType: "{kind}",\n'
            f'        PhoneAreaCode: "{area}",\n        PhoneNumber: "{number}",\n        PhoneEntry: "ID_PHONE"\n    }});\n'
            "    $('#PhoneContacts tr:last').after(newEmailContact);\n")


def js_address(line1, line2):
    return ('    LastEmailId++;\n    newEmailContact = newEmailTemplate({\n        Id: ++LastEmailId,\n'
            '        AddressType: "ADDRESS_TYPE_A",\n        AddressTypeId: "ID_ADDRESS_TYPE_A",\n'
            '        AddressCountry: "COUNTRY_A",\n        AddressDbCountryId: "ID_COUNTRY_A",\n'
            f'        AddressLine1: "{line1}",\n        AddressLine2: "{line2}",\n        AddressLine3: "",\n'
            '        AddressLine4: "",\n        AddressLine5: "",\n        AddressLine6: "",\n'
            '        Postcode: "POSTCODE_A",\n        AddressEntry: "ID_ADDRESS_A"\n    });'
            " $('#addressList tr:last').after(newEmailContact);\n")


def compliance_html(code, name, status, result, comment):
    opts = lambda chosen, words: "".join(f'<option value="{w}"{" selected=\'selected\'" if w == chosen else ""}>{w}</option>'
                                         for w in words)
    return (f"<h3>{name}</h3>"
            f'<div class="form-group"><label class="control-label">{name} Check Status</label>'
            f'<select id="entryForm{code}CheckStatus">{opts(status, ["STATUS_A", "STATUS_B"])}</select></div>'
            f'<div class="form-group"><label class="control-label">{name} Check Result</label>'
            f'<select id="entryForm{code}CheckResult"><option value=""></option>{opts(result, ["RESULT_A", "RESULT_B"])}</select></div>'
            # the page labels every comment box 'Financial Sanctions Check Comment'
            f'<div class="form-group"><label class="control-label">Financial Sanctions Check Comment</label>'
            f'<textarea id="entryForm{code}CheckComment">{comment}</textarea></div>')


def profile(emails=None, phones=None, addresses=None, script=True, rows=None, signature=PNG, tables=True, extra_script=""):
    rows = rows or {}
    emails = [js_email("EMAIL_TYPE_A", "x@example.com")] if emails is None else emails
    phones = [js_phone("PHONE_TYPE_A", "1234", "000001"), js_phone("PHONE_TYPE_B", "0123", "000002")] if phones is None else phones
    addresses = [js_address("ADDRESS_LINE_A", "ADDRESS_LINE_B")] if addresses is None else addresses
    setup = SETUP_SCRIPT % {"emails": "".join(emails), "phones": "".join(phones), "addresses": "".join(addresses)}
    address_head = "".join(f'<td class="TableHead">{h}</td>' for h in
                           ["Address Type", "Country"] + [f"Address Line {n}" for n in range(1, 7)] + ["Postcode", "Edit"])
    contact_tables = (
        '<h3>Email</h3><table id="EmailContacts"><tbody><tr><th>Email Type</th><th>Email Address</th><th>Action</th></tr>'
        '<tr id="NoEmail"><td colspan="3">Click Add New to add an email address</td></tr>' + rows.get("email", "") + '</tbody></table>'
        '<h3>Phone numbers</h3><table id="PhoneContacts"><tbody><tr><th>Phone Type</th><th>Country</th><th>Area Code</th>'
        '<th>Number</th><th>Action</th></tr><tr id="NoPhone" class="TableDataRow"><td colspan="5">No phone numbers</td></tr>'
        + rows.get("phone", "") + '</tbody></table>'
        f'<h3>Address</h3><table id="addressList"><tbody><tr>{address_head}</tr>'
        '<tr id="NoAddress" class="TableDataRow"><td colspan="4">No addresses</td></tr>' + rows.get("address", "") + "</tbody></table>"
    ) if tables else ""
    return (
        "<html><head><title>Interested Party</title></head><body><a>Logout</a><div id='interestPartyContainer'>"
        "<h3>Personal Details</h3>"
        '<div class="form-group"><label class="control-label">Title</label><select id="entryFormTitle"><option value="">'
        '</option><option value="T1">TITLE_A</option><option value="T2" selected="selected">TITLE_B</option></select></div>'
        '<div class="form-group"><label class="control-label">First Name</label><input id="entryFormFirstName" type="text" value="PERSON_A"></div>'
        '<div class="form-group"><label class="control-label">Middle Name</label><input id="entryFormMiddleName" type="text" value=""></div>'
        '<div class="form-group"><label class="control-label">Date of Birth</label><input id="entryFormDateOfBirth" type="text" value="01/01/1900" readonly></div>'
        '<div class="form-group"><label class="control-label">Marketing Opt In</label><input id="MarketingOptIn" type="checkbox" checked="checked"></div>'
        '<div class="form-group"><label class="control-label">Deceased</label><input id="IsDeceased" type="checkbox"></div>'
        '<div class="form-group"><label class="control-label">Wrap Provider</label><span>COMPANY_A</span><input type="hidden" id="customerAcd" value="ID_9"></div>'
        '<div id="EmailDialog" class="modal" style="display:none"><div class="form-group"><label>Email Address</label>'
        '<input id="dlgEmailAddress" value=""></div><div class="form-group"><label>Confirm Email Address</label>'
        '<input id="dlgConfirmEmailAddress" value=""></div></div>'
        + contact_tables
        + compliance_html("AML", "AML", "STATUS_A", "RESULT_A", "AML_NOTE")
        + compliance_html("PEP", "PEP", "STATUS_B", None, "")
        + compliance_html("FS", "Financial Sanctions", "STATUS_A", None, "FS_NOTE")
        + "<h3>Signature</h3>" + (f'<img id="imgSignature" src="{signature}">' if signature is not None else "")
        + '<div id="UploadSignatureDialog" class="modal"><input type="file"><img id="imgUploadedSignature" src="data:image/png;base64,AAAA"></div>'
        "</div>"
        '<script type="text/html" id="NewEmailTemplate"><tr id="EmailRow{%=o.Id%}" class="TableDataRow"><td id="rowEditEmailType{%=o.Id%}">{%=o.EmailType%}</td></tr></script>'
        + (f"<script>{setup}</script>" if script else "") + (f"<script>{extra_script}</script>" if extra_script else "")
        + "</body></html>")


def party_of(p2, monkeypatch, html):
    monkeypatch.setattr(p2, "soup_get", lambda url, params=None: page(p2, html, url="https://system2.example.invalid/p"))
    return p2.fetch_party({"interestedPartyId": "ID_A", "nameFromAccount": "PERSON_A", "url": "https://system2.example.invalid/p"})


def values(section):
    return [{k: v["value"] for k, v in row.items()} for row in section["rows"]]


def test_part2_reads_every_contact_record_from_the_page_setup_script_without_running_it(p2, monkeypatch):
    party = party_of(p2, monkeypatch, profile())
    emails, phones, addresses = party["Emails"], party["Phone numbers"], party["Addresses"]
    assert values(emails) == [{"Email Type": "EMAIL_TYPE_A", "Email Address": "x@example.com"}]
    assert values(phones) == [{"Phone Type": "PHONE_TYPE_A", "Country": "999", "Area Code": "1234", "Number": "000001"},
                              {"Phone Type": "PHONE_TYPE_B", "Country": "999", "Area Code": "0123", "Number": "000002"}]
    assert values(addresses) == [{"Address Type": "ADDRESS_TYPE_A", "Country": "COUNTRY_A", "Address Line 1": "ADDRESS_LINE_A",
                                  "Address Line 2": "ADDRESS_LINE_B", "Address Line 3": "", "Address Line 4": "",
                                  "Address Line 5": "", "Address Line 6": "", "Postcode": "POSTCODE_A"}]
    assert addresses["columns"] == ["Address Type", "Country"] + [f"Address Line {n}" for n in range(1, 7)] + ["Postcode"]
    assert phones["columns"] == ["Phone Type", "Country", "Area Code", "Number"]          # td.TableHead / th, no Action
    for sec, n in ((emails, 1), (phones, 2), (addresses, 1)):
        c = sec["completeness"]
        assert sec["captureStatus"] == "Captured" and c["outcome"] == "Extracted successfully"
        assert c["source"].startswith("page setup script") and c["recordsDetected"] == c["recordsWritten"] == n
        assert c["placeholdersExcluded"] == 1 and c["tableRows"] == 0 and c["setupScriptRecords"] == n
    text = json.dumps(party)
    assert "ID_COUNTRY_A" not in text and "ID_EMAIL_A" not in text and "ID_ADDRESS_TYPE_A" not in text  # hidden refs left out


def test_part2_finished_rows_are_read_by_id_prefix_with_any_suffix_and_matched_with_the_script(p2, monkeypatch):
    rows = {"email": '<tr id="EmailRow7" class="TableDataRow"><td id="rowEditEmailType7">EMAIL_TYPE_A</td>'
                     '<td id="rowEditEmailAddress7">x@example.com<input type="hidden" id="tableEmailEntry7" value="ID_EMAIL_A"></td>'
                     '<td><a>Edit</a></td></tr>',
            "phone": "".join(f'<tr id="PhoneRow{n}" class="TableDataRow"><td id="PhoneType{n}">{t}</td>'
                             f'<td><span id="rowEditPhoneCountry{n}">999</span><input type="hidden" id="rowEditPhoneDbCountryId{n}" value="ID_COUNTRY_A"></td>'
                             f'<td id="rowEditPhoneAreaCode{n}">{a}</td><td id="rowEditPhoneNumber{n}">{num}</td><td>Edit</td></tr>'
                             for n, t, a, num in ((12, "PHONE_TYPE_A", "1234", "000001"), (40, "PHONE_TYPE_B", "0123", "000002"))),
            "address": ('<tr id="AddressRow3" class="TableDataRow"><td><span id="rowEditAddressType3">ADDRESS_TYPE_A</span>'
                        '<input type="hidden" id="rowEditAddressTypeId3" value="ID_ADDRESS_TYPE_A"></td>'
                        '<td id="rowEditAddressCountry3">COUNTRY_A</td><td id="rowEditAddressLine13">ADDRESS_LINE_A</td>'
                        '<td id="rowEditAddressLine23">ADDRESS_LINE_B</td><td id="rowEditAddressLine33"></td><td id="rowEditAddressLine43"></td>'
                        '<td id="rowEditAddressLine53"></td><td id="rowEditAddressLine63"></td>'
                        '<td id="rowEditAddressPostcode3">POSTCODE_A</td><td>Edit</td></tr>')}
    party = party_of(p2, monkeypatch, profile(rows=rows))
    assert [r["Area Code"] for r in values(party["Phone numbers"])] == ["1234", "0123"]     # suffixes 12, 40: not 1, 2
    assert values(party["Addresses"])[0]["Address Type"] == "ADDRESS_TYPE_A"              # never the hidden TypeId
    assert values(party["Addresses"])[0]["Address Line 3"] == "" and values(party["Addresses"])[0]["Postcode"] == "POSTCODE_A"
    for key in ("Emails", "Phone numbers", "Addresses"):
        c = party[key]["completeness"]
        assert party[key]["captureStatus"] == "Captured" and c["source"] == "table rows (same as the page setup script)"
        assert c["tableRows"] == c["setupScriptRecords"] == c["recordsWritten"]


def test_part2_an_empty_table_is_blank_only_when_the_setup_script_says_so(p2, monkeypatch):
    empty = party_of(p2, monkeypatch, profile(emails=[], phones=[], addresses=[]))
    for key in ("Emails", "Phone numbers", "Addresses"):
        assert empty[key]["captureStatus"].startswith("Blank on Register") and empty[key]["rows"] == []
        assert empty[key]["completeness"]["outcome"] == "Confirmed empty"
    no_script = party_of(p2, monkeypatch, profile(script=False))           # only the placeholder: NOT proof of no records
    for key in ("Emails", "Phone numbers", "Addresses"):
        assert no_script[key]["captureStatus"].startswith("Not captured") and no_script[key]["rows"] == []
        assert no_script[key]["completeness"]["outcome"] == "Extraction failed"
    no_tables = party_of(p2, monkeypatch, profile(script=False, tables=False))
    assert no_tables["Emails"]["captureStatus"].startswith("Not available in Register snapshot")
    assert no_tables["Emails"]["completeness"]["outcome"] == "Not found"


def test_part2_one_unreadable_section_is_not_captured_while_the_others_are_read(p2, monkeypatch):
    odd = [js_email("EMAIL_TYPE_A", "x@example.com").replace('EmailType: "EMAIL_TYPE_A"', "EmailType: typeFor(3)")]
    party = party_of(p2, monkeypatch, profile(emails=odd))
    assert party["Emails"]["captureStatus"].startswith("Not captured (the records could not be read")
    assert party["Emails"]["completeness"]["outcome"] == "Extraction failed" and party["Emails"]["rows"] == []
    assert len(party["Phone numbers"]["rows"]) == 2 and party["Addresses"]["captureStatus"] == "Captured"


def test_part2_many_records_keep_their_order_and_identical_records_are_not_merged(p2, monkeypatch):
    emails = [js_email("EMAIL_TYPE_A", "a@example.com"), js_email("EMAIL_TYPE_B", "b@example.com"),
              js_email("EMAIL_TYPE_B", "b@example.com"), js_email("EMAIL_TYPE_C", "O&#39;Neill@example.com")]
    party = party_of(p2, monkeypatch, profile(emails=emails))
    assert [r["Email Address"] for r in values(party["Emails"])] == ["a@example.com", "b@example.com", "b@example.com",
                                                                   "O'Neill@example.com"]
    assert party["Emails"]["completeness"]["recordsWritten"] == 4
    assert any("HTML character references" in n for n in party["Emails"]["completeness"]["notes"])


def test_part2_table_rows_that_disagree_with_the_script_are_not_captured_and_both_kept(p2, monkeypatch):
    rows = {"email": '<tr id="EmailRow9" class="TableDataRow"><td id="rowEditEmailType9">EMAIL_TYPE_Z</td>'
                     '<td id="rowEditEmailAddress9">z@example.com</td><td>Edit</td></tr>'}
    party = party_of(p2, monkeypatch, profile(rows=rows))
    e = party["Emails"]
    assert e["captureStatus"].startswith("Not captured (the page's table rows and its setup script disagree")
    assert e["completeness"]["outcome"] == "Discrepancy" and values(e) == [{"Email Type": "EMAIL_TYPE_Z", "Email Address": "z@example.com"}]
    assert e["setupScriptRows"][0]["Email Address"]["value"] == "x@example.com"


def test_part2_personal_details_keep_selected_values_ticks_blanks_and_leave_dialogs_out(p2, monkeypatch):
    pd = party_of(p2, monkeypatch, profile())["Personal Details"]
    assert pd["Title"] == "TITLE_B" and pd["First Name"] == "PERSON_A" and pd["Middle Name"] == ""      # blank stays blank
    assert pd["Marketing Opt In"] is True and pd["Deceased"] is False
    assert pd["Wrap Provider"] == "COMPANY_A"                                 # displayed text, not the hidden input
    assert "Email Address" not in pd and "Confirm Email Address" not in pd    # the hidden dialog's blank defaults
    c = pd["controls"]
    assert c["Title"]["selectedLabel"] == "TITLE_B" and c["Title"]["selectedValue"] == "T2" and c["Title"]["selectedMarked"]
    assert "TITLE_A" not in json.dumps(c["Title"])                            # the option list is not the person's data
    assert c["Date of Birth"]["readOnly"] is True and c["Deceased"] == {"label": "Deceased", "control": "checkbox",
                                                                         "id": "IsDeceased", "checked": False}
    assert pd["completeness"] == {"outcome": "Extracted successfully", "sectionFound": True, "fieldsRead": 7,
                                  "dialogFieldsLeftOut": 2}


def test_part2_compliance_fields_are_keyed_by_control_not_by_their_repeated_label(p2, monkeypatch):
    sc = party_of(p2, monkeypatch, profile())["Security checks"]
    aml, pep, fs = sc["AML"], sc["PEP"], sc["Financial Sanctions"]
    assert (aml["AML Check Status"], aml["AML Check Result"], aml["AML Check Comment"]) == ("STATUS_A", "RESULT_A", "AML_NOTE")
    assert (pep["PEP Check Status"], pep["PEP Check Result"], pep["PEP Check Comment"]) == ("STATUS_B", "", "")
    assert fs["Financial Sanctions Check Comment"] == "FS_NOTE" and fs["Financial Sanctions Check Result"] == ""
    assert "Financial Sanctions Check Comment" not in aml and "Financial Sanctions Check Comment" not in pep
    assert aml["controls"]["AML Check Comment"]["label"] == "Financial Sanctions Check Comment"   # kept as evidence
    assert pep["controls"]["PEP Check Result"]["selectedMarked"] is False                     # nothing chosen: not inferred
    assert pep["controls"]["PEP Check Status"]["selectedLabel"] == "STATUS_B"
    assert aml["completeness"]["controlsMissing"] == []


def test_part2_signature_is_reported_present_absent_or_unreadable_and_never_copied(p2, monkeypatch):
    good = party_of(p2, monkeypatch, profile())["Security checks"]
    assert good["Signature"] == "Signature present in the profile"
    assert good["Signature capture"] == {"savedSignature": "present", "uploadPreviewLeftOut": True, "imageData": "readable PNG image"}
    assert PNG.split(",")[1][:20] not in json.dumps(good)
    gone = party_of(p2, monkeypatch, profile(signature=None))["Security checks"]
    assert gone["Signature"] == "No signature found in the profile" and gone["Signature capture"]["savedSignature"] == "absent"
    bad = party_of(p2, monkeypatch, profile(signature="data:image/png;base64,bm90IGFuIGltYWdl"))["Security checks"]
    assert bad["Signature"].endswith("its image could not be read")
    assert bad["Signature capture"]["imageData"] == "unreadable (not a known image format)"


def test_part2_a_record_dropped_while_writing_is_caught_and_the_file_never_reused(p2, monkeypatch, base):
    party = party_of(p2, monkeypatch, profile())
    target = base / ("AB1" + P.SNAPSHOT_SUFFIX)
    details = {n: {"captureStatus": "Captured", "data": {}, "completeness": {"outcome": "Extracted successfully", "sectionFound": True}}
               for n in P.DETAIL_SECTIONS}
    p2.write_result(target, "AB1", {"metadata": {"requestedAccount": "AB1"}}, [party], [], details)
    back = json.loads(target.read_text("utf-8"))["Interested Parties linked to the account"][0]
    assert [len(back[k]["rows"]) for k in ("Emails", "Phone numbers", "Addresses")] == [1, 2, 1]
    real = P.write_json

    def lossy(path, obj):
        copy = json.loads(json.dumps(obj))
        copy["Interested Parties linked to the account"][0]["Phone numbers"]["rows"].pop()
        real(path, copy)
    monkeypatch.setattr(p2.P, "write_json", lossy)
    with pytest.raises(p2.SnapshotIncomplete, match="holds 3 contact record"):
        p2.write_result(target, "AB1", {"metadata": {"requestedAccount": "AB1"}}, [party], [], details)
    assert not target.exists() and (base / (target.stem + ".incomplete.json")).exists()   # set aside, never reused


def test_part2_the_interested_party_link_tab_is_reported_inspected_or_not(p2, monkeypatch):
    d = {"_interestedPartyLinks": [
        {"interestedPartyId": "ID_A", "nameFromAccount": "PERSON_A", "url": "https://system2.example.invalid/partyprofile?InterestedPartyid=ID_A"},
        {"interestedPartyId": "ID_B", "nameFromAccount": "PERSON_B", "url": "https://system2.example.invalid/partyprofile?InterestedPartyid=ID_B"}]}
    a_url, b_url = (x["url"] for x in d["_interestedPartyLinks"])
    a_links, b_links = p2.link_url(d["_interestedPartyLinks"][0]), p2.link_url(d["_interestedPartyLinks"][1])
    pr = {a_url: {"interestedPartyId": "ID_A", "Emails": {"rows": []}}}
    pe = {b_url: "ReadTimeout: party page did not answer"}
    lr = {a_links: {"interestedPartyId": "ID_A"}}
    le = {b_links: "ReadTimeout: link page did not answer"}
    parties, links, missing = p2.assemble(d, pr, pe, lr, le)
    assert parties[0]["Interested Party Link tab"].startswith("Inspected")
    assert parties[1]["Interested Party Link tab"] == "Not inspected - the link page could not be read (ReadTimeout: link page did not answer)"
    assert parties[1]["metadata"]["retrievalError"].startswith("ReadTimeout") and missing == 2
    assert "Interested Party Link tab" not in pr[a_url]                     # the shared page record is not changed
    html = "<html><body><a>Logout</a><table id='CustomerInterestedParties'><tr><th>Customer</th><th>Role</th><th>x</th><th>y</th><th>Status</th></tr></table></body></html>"
    monkeypatch.setattr(p2, "soup_get", lambda url, params=None: page(p2, html))
    cov = p2.fetch_links(d["_interestedPartyLinks"][0])["coverage"]
    assert cov == {"Customer Interested Parties": "table found, 0 link(s) read",
                   "Account Interested Parties": "table not found on the page - nothing read from it"}
