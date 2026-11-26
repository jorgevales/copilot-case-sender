"""Coordinator logic without any browser: the plan of a run, the hand-over of finished cases, the daily report.

The hand-over is the only step that moves a case into CASES_ROOT, and only when it is complete:
  Part 1 finished it (RUNTIME_ROOT\\pipeline\\downloads\\<id>.json, folder in CASES_ROOT\\.work)
  + its Sub Worktype has an instructions file in the guidance folder
  + Part 2 saved today's snapshot of its account
  -> the snapshot is copied into the folder, the folder is renamed into CASES_ROOT (one step, same share)
  -> RUNTIME_ROOT\\pipeline\\cases\\<id>.json tells Part 3 which instructions to attach and in which order.
A case that cannot be completed is held with a plain reason (shown in the daily report), never sent half-done.
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import shutil
import uuid
from pathlib import Path

from shared import pipeline as P


# ---------------------------------------------------------------------------------------------- the plan

def _special(contract) -> list:
    """Files in the guidance folder that are never a Sub Worktype's instructions (contract, daily instructions)."""
    return [p for p in (contract if isinstance(contract, (list, tuple)) else [contract]) if p]


def needs_guidance(reviewer: str) -> bool:
    """Copilot and Azure get a Sub Worktype instructions file with every case; Mission Control's agents have them."""
    return reviewer != "mission_control"


def build_plan(cases: list, skipped: list, paths: P.Paths, sent: set, guidance_dir, contract,
               reviewer: str = "copilot", systems: str = "123", rerun=(), skip=(), extra=None) -> dict:
    """What each case needs this run: 'download' (Parts 1 / 2 work on it, then it is handed over), 'send' (complete
    from an earlier run), 'done' (nothing to do: sent / reviewed earlier - `sent` is the 'never twice' list of Copilot or
    Azure - or already complete when System 3 is not in the run, or skipped by your choice) or 'held' (cannot be done
    safely - with the reason).

    `systems` ('1', '2', '3' in any combination, see P.MODES) is the run's choice.  `rerun` = cases you chose to run
    again although they were done before: what an earlier run left (case folder, downloads, today's snapshot) is
    moved aside by `set_aside_for_rerun` before the parts start, so they work on it from scratch.  `skip` = cases you
    chose NOT to run again.  `extra` is copied into the plan (BAM Report name, run choice, out-of-scope count)."""
    try:
        existing = {e.name[:32].lower(): e.name for e in os.scandir(paths.cases_root)
                    if e.is_dir() and P.ID_RE.match(e.name[:32].lower())}
    except OSError:
        existing = {}
    retrieve, send = "1" in systems or "2" in systems, "3" in systems
    rerun, skip = set(rerun), set(skip)
    out = []
    for c in cases:
        iid, item = c["iid"], dict(c)
        handed = P.read_json(paths.cases / f"{iid}.json")
        complete = bool(handed and (paths.cases_root / handed["folder"]).is_dir())
        m = P.read_json(paths.downloads / f"{iid}.json")
        has_docs = bool(m and (paths.work / m.get("folder", "")).is_dir())
        if iid in skip:
            item.update(needs="done", note="done before - not run again (your choice)",
                        word="already done - skipped by your choice")
        elif send and iid in sent and reviewer != "mission_control":
            item.update(needs="done", note=f"sent in an earlier run - {P.REVIEWER_NAMES.get(reviewer, reviewer)} "
                                           "never gets the same case twice")
        elif complete and iid in rerun and retrieve:
            item.update(needs="download", note="run again (your choice)", redo=True)
        elif complete and send:
            item.update(needs="send", note="complete from an earlier run - only sent now")
        elif complete:
            item.update(needs="done", note="documents and snapshot already complete - ready for System 3",
                        word="already complete")
        elif iid in existing:
            item.update(needs="held", note="a folder for this instruction ID already exists in CASES_ROOT and was not "
                                           "made by this pipeline - move it out, then run again")
        elif "1" not in systems and not has_docs and send and "2" not in systems:
            item.update(needs="held", note="its documents are not downloaded yet - run System 1 first")
        else:
            item.update(needs="download", note="", redo=bool(iid in rerun and has_docs and "1" in systems))
        out.append(item)
    downloads = [c for c in out if c["needs"] == "download"]
    no_guidance = sorted({c["sub_worktype_csv"] for c in downloads
                          if not P.guidance_for(c["sub_worktype_csv"], guidance_dir, exclude=_special(contract))}) \
        if needs_guidance(reviewer) else []
    return {"run": paths.run.name, "day": P.today(), "created": P.now(), "reviewer": reviewer, "cases": out,
            "skipped": skipped, "systems": systems, "send": send, **(extra or {}),
            "parts": {"part1": bool(downloads) and "1" in systems, "part2": bool(downloads) and "2" in systems},
            "no_guidance": no_guidance}


def set_aside_for_rerun(paths: P.Paths, plan: dict) -> list:
    """Cases you chose to run again: their earlier case folder, hand-over and download records and (when System 2 is
    in the run) today's snapshot of their account are MOVED to RUNTIME_ROOT\\pipeline\\older\\<run>\\ - never deleted.
    Returns plain lines for the screen (a move that fails holds the case: it is never half re-run)."""
    lines, moved_snaps = [], set()
    systems = plan.get("systems", "123")
    for c in plan["cases"]:
        if c.get("needs") != "download" or c["iid"] not in set(plan.get("rerun") or []):
            continue
        iid, dest = c["iid"], paths.older / c["iid"]
        try:
            dest.mkdir(parents=True, exist_ok=True)
            handed = P.read_json(paths.cases / f"{iid}.json") or {}
            m = P.read_json(paths.downloads / f"{iid}.json") or {}
            done = paths.cases_root / handed["folder"] if handed.get("folder") else None
            if done is not None and done.is_dir():
                if "1" in systems:                     # documents downloaded again: the old folder is kept aside
                    shutil.move(str(done), str(dest / done.name))
                else:                                  # only the snapshot is redone: the documents go back to .work
                    os.rename(done, paths.work / done.name)
            if (paths.cases / f"{iid}.json").is_file():
                shutil.move(str(paths.cases / f"{iid}.json"), str(dest / "handed.json"))
            if "1" in systems:
                if m.get("folder") and (paths.work / m["folder"]).is_dir():
                    shutil.move(str(paths.work / m["folder"]), str(dest / m["folder"]))
                if (paths.downloads / f"{iid}.json").is_file():
                    shutil.move(str(paths.downloads / f"{iid}.json"), str(dest / "downloads.json"))
            snap = paths.snapshot(c["account"], plan.get("day"))
            if "2" in systems and snap.is_file() and c["account"] not in moved_snaps:
                shutil.move(str(snap), str(dest / snap.name))
                moved_snaps.add(c["account"])
        except OSError as exc:
            c.update(needs="held", note=f"could not move the earlier files aside to run it again ({type(exc).__name__})")
            lines.append(f"  {iid}: NOT run again - its earlier files could not be moved aside")
    return lines


# ---------------------------------------------------------------------------------------------- the hand-over

class Handover:
    """Called every half second by the coordinator while the run goes on (`step`).  `alive(part)` says whether that
    part's process is still running (a part that crashed counts as finished)."""

    def __init__(self, paths: P.Paths, control: P.Control, plan: dict, guidance_dir, contract, alive=lambda part: True,
                 done_log=None):
        self.paths, self.control, self.plan = paths, control, plan
        self.guidance_dir, self.contract, self.alive = guidance_dir, contract, alive
        self.todo = [c for c in plan["cases"] if c.get("needs") == "download"]
        self.day = plan.get("day")
        self.systems = plan.get("systems", "123")
        self.rules = needs_guidance(plan.get("reviewer", "copilot"))
        self.done, self.handed, self.waiting = set(), [], {}
        self.log, self.logged = done_log, set()           # the already-done log: System 1 / System 2 lines

    def over(self, part: str) -> bool:
        return bool(self.control.is_finished(part)) or not self.alive(part)

    def _hold(self, iid: str, reason: str) -> None:
        self.control.hold(iid, reason, "coordinator")
        self.control.event("coordinator", f"{iid} HELD: {reason}", iid=iid)
        self.done.add(iid)

    def _wait(self, iid: str, reason: str) -> None:
        """Not complete yet, by the run's choice (e.g. System 1 only): no problem, it is finished in a later run."""
        self.waiting[iid] = reason
        P.write_json(self.paths.sub("status") / "waiting.json", self.waiting)
        self.control.event("coordinator", f"{iid} not complete yet: {reason}", "detail", iid=iid)
        self.done.add(iid)

    def _note_done(self, c: dict, m) -> None:
        """One already-done line per case and system, when this run's System 1 / System 2 made it (not reused)."""
        if self.log is None:
            return
        since = self.plan.get("created", "")
        if "1" in self.systems and m and (c["iid"], "system1") not in self.logged and \
                str(m.get("finished") or self._made(self.paths.downloads / f"{c['iid']}.json")) >= since:
            self._log_line(c, "system1", "downloaded", f"{len(m.get('documents') or [])} document(s) + Audit Trail",
                           m.get("sub_worktype", ""))
        snap = self.paths.snapshot(c["account"], self.day)
        if "2" in self.systems and (c["iid"], "system2") not in self.logged and snap.is_file():
            if self._made(snap) >= since:
                self._log_line(c, "system2", "captured", "", (m or {}).get("sub_worktype", "") or c.get("sub_worktype_csv", ""))

    @staticmethod
    def _made(path: Path) -> str:
        """When a file was last written, as UTC ISO text ('' when it cannot be read)."""
        try:
            return datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat()
        except OSError:
            return ""

    def _log_line(self, c, system, outcome, detail, sub) -> None:
        try:
            self.log.add(system, c["iid"], outcome, c["account"], detail, self.paths.run.name, sub)
            self.logged.add((c["iid"], system))
        except OSError as exc:
            self.control.event("coordinator", f"WARNING: the already-done log could not be written ({type(exc).__name__})")
            self.logged.add((c["iid"], system))           # said once, not every half second

    def step(self) -> bool:
        """Hand over every case that became complete; True once the feed is closed (nothing more will come)."""
        if self.control.feed_closed():
            return True
        if self.control.stopped():                 # no hand-over while stopping: unfinished cases stay in .work
            for c in self.todo:                    # what was made before the stop is still logged
                self._note_done(c, P.read_json(self.paths.downloads / f"{c['iid']}.json"))
            self.control.close_feed()
            return True
        held = self.control.held()
        failed = P.read_json(self.paths.sub("status") / "part2-failed.json", {}) or {}
        p1_over, p2_over = self.over("part1"), self.over("part2")
        s1, s2 = "1" in self.systems, "2" in self.systems
        for c in self.todo:
            iid = c["iid"]
            if iid in self.done:
                continue
            m = P.read_json(self.paths.downloads / f"{iid}.json")
            self._note_done(c, m)
            if iid in held:                        # Part 1 could not download it (reason already recorded)
                self.done.add(iid)
                continue
            if not m or not (self.paths.work / m["folder"]).is_dir():
                if s1 and p1_over:
                    self._hold(iid, "System 1 documents were not downloaded (Part 1 ended before this case)")
                elif not s1 and (p2_over or not s2):
                    self._wait(iid, "its documents are not downloaded yet (run System 1)")
                continue
            guidance = None
            if self.rules:
                guidance = P.guidance_for(m["sub_worktype"], self.guidance_dir, exclude=_special(self.contract))
                if guidance is None:
                    self._hold(iid, f"no instructions file for Sub Worktype '{m['sub_worktype']}' in the guidance folder")
                    continue
            snap = self.paths.snapshot(c["account"], self.day) if s2 else self.paths.latest_snapshot(c["account"])
            if snap is None or not snap.is_file():
                if s2 and p2_over:
                    self._hold(iid, "no System 2 snapshot (" + failed.get(c["account"], "not retrieved") + ")")
                elif not s2:
                    self._wait(iid, "documents downloaded - no account snapshot yet (run System 2)")
                continue
            self._hand_over(c, m, guidance, snap)
        if p1_over and all(c["iid"] in self.done for c in self.todo):
            self.control.close_feed()
            return True
        return False

    def _hand_over(self, c: dict, m: dict, guidance: Path | None, snap: Path) -> None:
        iid = c["iid"]
        work, dest = self.paths.work / m["folder"], self.paths.cases_root / m["folder"]
        if dest.exists():
            self._hold(iid, "a folder with this name already exists in CASES_ROOT")
            return
        try:
            tmp = work / f".{snap.name}.tmp-{uuid.uuid4().hex[:8]}"
            shutil.copyfile(snap, tmp)
            os.replace(tmp, work / snap.name)
            os.rename(work, dest)                  # the case appears in CASES_ROOT complete, in one step
        except OSError as exc:
            self._hold(iid, f"the case folder could not be moved into CASES_ROOT ({type(exc).__name__})")
            return
        order = [snap.name] + ([m["audit"]] if m.get("audit") else []) + list(m.get("documents") or [])
        P.write_json(self.paths.cases / f"{iid}.json", {
            "iid": iid, "account": c["account"], "folder": m["folder"], "sub_worktype": m["sub_worktype"],
            "guidance": str(guidance) if guidance else "", "order": order, "warnings": m.get("warnings", []),
            "case_source": c.get("case_source", ""), "snapshot_day": snap.parent.name, "handed": P.now()})
        self.done.add(iid)
        self.handed.append(iid)
        who = P.REVIEWER_NAMES.get(self.plan.get("reviewer", "copilot"), "System 3") if self.plan.get("send", True) \
            else "CASES_ROOT (ready for System 3)"
        self.control.event("coordinator", f"{iid} complete ({len(order)} files) - handed to {who}", iid=iid)


# ---------------------------------------------------------------------------------------------- the daily report

# The rows themselves are built in shared/pipeline.py: the sender shows the same whole-workload summary at its end.
OUTCOMES, REPORT_COLUMNS, daily_report = P.OUTCOMES, P.REPORT_COLUMNS, P.daily_report


def write_report(rows: list, folder: Path, run_name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"daily-report-{run_name}.csv"
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:      # utf-8-sig: opens cleanly in Excel
        w = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------------------------- the pipeline report

PIPELINE_GUIDE = """\
What System 1 (documents, Part 1) and System 2 (account snapshots, Part 2) did in ONE daily run, for a reviewing LLM that
cannot see the VDI. Read it with the sender's run-report-*.md and copilot-responses-*.md of the same time (same folder).
Part 1, per case: the search (how many View buttons), the Sub Worktype it read, every Received Document with the way it was
downloaded - 'direct-*' = fetched through its viewer address with the System 1 sign-in (no click); 'file-at-document-window-
address' / 'file-shown-in-document-window' / 'download-started-by-document-window' / 'document-window-toolbar-download' = the
View fallback (one click on View, then nothing inside the document is clicked) - and, for every request, its HTTP status,
content type, size and what the body looked like (pdf, office/zip, html = a viewer page, never saved as the document).
'pairing' says how a viewer address was matched to its row: id (the address names the document ID), position (page order,
only when no address names an ID and the counts match) or none. Screenshots / page copies taken at a failure are listed
by file name and lie in captures\\<run>\\ beside this file. Web addresses are cut before '?' (parameter names kept)."""


def _tail(path: Path, lines: int = 200) -> list:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return []
    return [P.strip_queries(x) for x in text.splitlines()[-lines:]]


def _document_lines(events: list) -> list:
    out = []
    for e in events:
        name = e.get("event")
        if name == "document-rows-resolved":
            out.append(f"    - {e.get('count')} Received Document row(s); viewer addresses on the page: {e.get('viewerAddressesOnPage')}")
            out += [f"      - {d.get('name') or '(no name)'} | id {d.get('id') or '-'} | pairing {d.get('pairing') or '-'} | "
                    f"{d.get('viewerAddress') or '(no viewer address)'}" for d in e.get("documents") or []]
        elif name == "document-result":
            how = e.get("method") or f"NOT DOWNLOADED - {e.get('failure')}"
            out.append(f"    - {e.get('name') or e.get('documentId') or '?'}: {how}"
                       + (f" (direct route: {e['directRoute']})" if e.get("directRoute") not in (None, "ok") else ""))
            for t in e.get("attempts") or []:
                out.append("      - " + json.dumps(t, ensure_ascii=False, default=str)[:600])
            for c in e.get("captures") or []:
                out.append(f"      - capture: {c}")
            if e.get("viewerPageCopy"):
                out.append(f"      - viewer page copy: {e['viewerPageCopy']}")
        elif name in ("search-result-not-single", "search-state", "single-view-found-after-fixed-wait", "held-before-opening", "run-failed",
                      "identity-verified", "audit-trail-downloaded", "document-ui-fallback-started", "all-received-documents-downloaded"):
            extra = {k: v for k, v in e.items() if k not in ("at", "event")}
            out.append(f"    - {e.get('at', '')[11:19]} {name} {json.dumps(extra, ensure_ascii=False, default=str)[:400]}")
    return out


def pipeline_report(paths: P.Paths, plan: dict, rows: list, edge_offset=None) -> Path | None:
    """RUNTIME_ROOT\\logs\\pipeline-report-<run>.md - never raises (a report must not change a run)."""
    try:
        out = [f"# Pipeline report - run {paths.run.name}", "", "> " + P.REVIEWER_NOTE, "", PIPELINE_GUIDE, "", "## 1. Whole workload",
               f"- {P.workload_line(rows)}", "",
               f"- Part 3 reviewer: {plan.get('reviewer') or 'copilot'}", "",
               "| row | instruction ID | account | status | Sub Worktype | case source | outcome | detail |",
               "|---|---|---|---|---|---|---|---|"]
        out += [f"| {r['row']} | {r['instruction_id']} | {r['client_account_number']} | {r['process_status']} | {r['sub_worktype']} | "
                f"{r.get('case_source', '')} | {r['outcome']} | {str(r['detail']).replace('|', '/')} |" for r in rows]
        if plan.get("no_guidance"):
            out += ["", "- Sub Worktypes of the CSV with no guidance file at the start: " + ", ".join(plan["no_guidance"])]
        out += ["", "## 2. Part 1 - System 1 documents, per case"]
        for c in plan["cases"]:
            d = P.read_json(paths.diagnostics("part1") / f"{c['iid']}.json")
            if not d:
                out.append(f"- {c['iid']}: {'not handled by Part 1 this run (' + c['needs'] + ')' if c['needs'] != 'download' else 'no record (not reached)'}")
                continue
            out.append(f"- {c['iid']} (CSV row {c['row_number']}): **{d.get('status')}** - Sub Worktype System 1 "
                       f"'{d.get('sub_worktype')}' / CSV '{d.get('csv_sub_worktype')}' - {d.get('files_downloaded') or 0} file(s) "
                       f"in {d.get('elapsed_seconds')}s" + (f" - {d['failure']}" if d.get("failure") else ""))
            for t in d.get("tries") or []:
                out.append(f"  - attempt {t.get('attempt')}: {t.get('status')}" + (f" - {t['failure']}" if t.get("failure") else ""))
                out += [f"    - capture: {x}" for x in t.get("captures") or []]
                out += _document_lines(t.get("events") or [])
                if t.get("trace"):
                    out += ["    - error trace (file:line of every step):", "```"] + P.strip_queries(t["trace"]).splitlines() + ["```"]
        snaps = P.read_json(paths.diagnostics("part2") / "accounts.json") or {}
        out += ["", "## 3. Part 2 - System 2 snapshots, per account" + (f" (DENIED: {snaps['denied']})" if snaps.get("denied") else "")]
        out += [f"- {a}: " + json.dumps(v, ensure_ascii=False, default=str)[:600] for a, v in (snaps.get("accounts") or {}).items()] \
            or ["- no record (Part 2 did not run or ended before writing it)"]
        out += ["", "## 4. Progress lines of every part (as shown on the console)", "```"]
        for part in P.PARTS:
            for line in _tail(paths.sub("events") / f"{part}.jsonl", 400):
                try:
                    e = json.loads(line)
                    out.append(f"{e.get('at', '')[11:19]} {part:<11} {e.get('text', '')}")
                except ValueError:
                    pass
        out += ["```", "", "## 5. Screenshots and page copies of this run (folder captures\\" + paths.run.name + ")"]
        try:
            shots = sorted(f.name for f in paths.captures.iterdir())
        except OSError:
            shots = []
        out += [f"- {n}" for n in shots] or ["- none (nothing failed in System 1 / System 2)"]
        for part in ("part1", "part2"):
            out += ["", f"## {6 if part == 'part1' else 7}. {part}.log - last lines", "```"] + _tail(paths.sub("logs") / f"{part}.log") + ["```"]
        if edge_offset is not None:
            try:
                with open(paths.logs / "edge-systems-stderr.log", "rb") as fh:
                    fh.seek(edge_offset)
                    edge = [P.strip_queries(x) for x in fh.read(2_000_000).decode("utf-8", "replace").splitlines() if x.strip()][-300:]
            except OSError:
                edge = []
            out += ["", "## 8. Systems Edge messages during this run (addresses cut before '?')", "```"] + (edge or ["(none)"]) + ["```"]
        path = paths.logs / f"pipeline-report-{paths.run.name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        tmp.write_text("\n".join(out), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception as exc:                          # noqa: BLE001 - diagnostics only
        print(f"(pipeline report could not be written: {type(exc).__name__}: {exc})", flush=True)
        return None
