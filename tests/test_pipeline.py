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
    assert names[0] == ["Change of Address.md", "result_contract.md", snap, "Audit Trail.pdf", "b form.pdf", "a letter.pdf"]
    assert names[1][2] == "AB200000" + P.SNAPSHOT_SUFFIX
    assert w.feed.done


def test_over_the_limit_the_last_documents_are_left_out_never_the_snapshot_or_audit_trail(env):
    env.cfg.max_attachments = 4
    w = FeedWorld(env, 1)
    w.finish_case(0)
    w.control.finished("part1")
    w.hand.step()
    r = w.runner()
    T.run(r.run(collections.deque(w.feed.poll())))
    sent = env.world.attach_calls[0][1]          # send-only mode: no result contract, so 4 = instructions + 3 files
    assert sent == ["Change of Address.md", "AB100000" + P.SNAPSHOT_SUFFIX, "Audit Trail.pdf", "b form.pdf"]
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
    msg.write_text("Please review.", "utf-8")
    config = base / "sender_config.json"
    config.write_text(json.dumps({"cases_root": str(cases_root), "runtime_root": str(runtime), "base_message_path": str(msg),
                                  "python_executable": sys.executable, "edge_executable": sys.executable,
                                  "approved_root": str(base), "guidance_dir": str(g), "result_contract_path": str(contract),
                                  "system1_url": "https://system1.example.invalid/app/inbox", "system1_brand_text": "Brand",
                                  "system2_url": "https://system2.example.invalid/home", "console_detail": "full",
                                  "system2_account_page": "https://system2.example.invalid/account?id={account}",
                                  "system2_party_marker": "partyprofile",
                                  "system2_party_links_page": "https://system2.example.invalid/links?party={party}"}), "utf-8")
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
    assert sorted(p.name for p in cases_root.iterdir() if p.name != ".work") == \
        [f"{hexid(1)} - AB100000", f"{hexid(4)} - AB400000"]
    assert "SKIPPED - 1 row(s)" in out and "DAILY REPORT" in out
