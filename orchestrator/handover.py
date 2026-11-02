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
import os
import shutil
import uuid
from pathlib import Path

from shared import pipeline as P


# ---------------------------------------------------------------------------------------------- the plan

def build_plan(cases: list, skipped: list, paths: P.Paths, sent: set, guidance_dir, contract) -> dict:
    """What each CSV row needs this run: 'download' (Parts 1 + 2, then sent), 'send' (complete from an earlier run),
    'done' (sent in an earlier run) or 'held' (cannot be done safely - with the reason)."""
    try:
        existing = {e.name[:32].lower(): e.name for e in os.scandir(paths.cases_root)
                    if e.is_dir() and P.ID_RE.match(e.name[:32].lower())}
    except OSError:
        existing = {}
    out = []
    for c in cases:
        iid, item = c["iid"], dict(c)
        handed = P.read_json(paths.cases / f"{iid}.json")
        if iid in sent:
            item.update(needs="done", note="sent in an earlier run")
        elif handed and (paths.cases_root / handed["folder"]).is_dir():
            item.update(needs="send", note="complete from an earlier run - only sent now")
        elif iid in existing:
            item.update(needs="held", note="a folder for this instruction ID already exists in CASES_ROOT and was not "
                                           "made by this pipeline - move it out, then run again")
        else:
            item.update(needs="download", note="")
        out.append(item)
    downloads = [c for c in out if c["needs"] == "download"]
    no_guidance = sorted({c["sub_worktype_csv"] for c in downloads
                          if not P.guidance_for(c["sub_worktype_csv"], guidance_dir, exclude=[contract])})
    return {"run": paths.run.name, "day": P.today(), "created": P.now(), "cases": out, "skipped": skipped,
            "parts": {"part1": bool(downloads), "part2": bool(downloads)}, "no_guidance": no_guidance}


# ---------------------------------------------------------------------------------------------- the hand-over

class Handover:
    """Called every half second by the coordinator while the run goes on (`step`).  `alive(part)` says whether that
    part's process is still running (a part that crashed counts as finished)."""

    def __init__(self, paths: P.Paths, control: P.Control, plan: dict, guidance_dir, contract, alive=lambda part: True):
        self.paths, self.control, self.plan = paths, control, plan
        self.guidance_dir, self.contract, self.alive = guidance_dir, contract, alive
        self.todo = [c for c in plan["cases"] if c.get("needs") == "download"]
        self.day = plan.get("day")
        self.done, self.handed = set(), []

    def over(self, part: str) -> bool:
        return bool(self.control.is_finished(part)) or not self.alive(part)

    def _hold(self, iid: str, reason: str) -> None:
        self.control.hold(iid, reason, "coordinator")
        self.control.event("coordinator", f"{iid} HELD: {reason}", iid=iid)
        self.done.add(iid)

    def step(self) -> bool:
        """Hand over every case that became complete; True once the feed is closed (nothing more will come)."""
        if self.control.feed_closed():
            return True
        if self.control.stopped():                 # no hand-over while stopping: unfinished cases stay in .work
            self.control.close_feed()
            return True
        held = self.control.held()
        failed = P.read_json(self.paths.sub("status") / "part2-failed.json", {}) or {}
        p1_over, p2_over = self.over("part1"), self.over("part2")
        for c in self.todo:
            iid = c["iid"]
            if iid in self.done:
                continue
            if iid in held:                        # Part 1 could not download it (reason already recorded)
                self.done.add(iid)
                continue
            m = P.read_json(self.paths.downloads / f"{iid}.json")
            if not m or not (self.paths.work / m["folder"]).is_dir():
                if p1_over:
                    self._hold(iid, "System 1 documents were not downloaded (Part 1 ended before this case)")
                continue
            guidance = P.guidance_for(m["sub_worktype"], self.guidance_dir, exclude=[self.contract])
            if guidance is None:
                self._hold(iid, f"no instructions file for Sub Worktype '{m['sub_worktype']}' in the guidance folder")
                continue
            snap = self.paths.snapshot(c["account"], self.day)
            if not snap.is_file():
                if p2_over:
                    self._hold(iid, "no System 2 snapshot (" + failed.get(c["account"], "not retrieved") + ")")
                continue
            self._hand_over(c, m, guidance, snap)
        if p1_over and all(c["iid"] in self.done for c in self.todo):
            self.control.close_feed()
            return True
        return False

    def _hand_over(self, c: dict, m: dict, guidance: Path, snap: Path) -> None:
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
            "guidance": str(guidance), "order": order, "warnings": m.get("warnings", []), "handed": P.now()})
        self.done.add(iid)
        self.handed.append(iid)
        self.control.event("coordinator", f"{iid} complete ({len(order)} files) - handed to Copilot", iid=iid)


# ---------------------------------------------------------------------------------------------- the daily report

OUTCOMES = {
    "result_correct": "sent - Copilot: Correct", "result_incorrect": "sent - Copilot: Incorrect",
    "result_inconclusive": "sent - Copilot: Inconclusive", "submitted": "sent (answer not collected)",
    "submitted_partial": "sent with files left out", "result_invalid": "sent - answer not in the required format",
    "response_timeout": "sent - no answer in time", "submission_uncertain": "UNCERTAIN send - check the Copilot chat",
    "preflight_blocked": "not sent - blocked", "failed_exhausted": "not sent - failed after all tries",
    "not_sent_stopped": "not sent - run stopped", "skipped_already_sent": "already sent earlier",
}
REPORT_COLUMNS = ["row", "instruction_id", "client_account_number", "client_number", "process_status", "sub_worktype",
                  "outcome", "detail", "left_out_files"]


def daily_report(paths: P.Paths, plan: dict, control: P.Control) -> list:
    """One row per CSV row, in CSV order: what happened to it this run and why."""
    held = control.held()
    sent = P.read_json(paths.sub("status") / "part3-results.json", {}) or {}
    rows = []
    for c in plan["skipped"]:
        rows.append({"row": c["row_number"], "instruction_id": c["iid"], "client_account_number": c["account"],
                     "client_number": c["client_number"], "process_status": c["status"],
                     "sub_worktype": c["sub_worktype_csv"], "outcome": "skipped", "detail": c["reason"], "left_out_files": ""})
    for c in plan["cases"]:
        iid = c["iid"]
        m = P.read_json(paths.downloads / f"{iid}.json") or {}
        row = {"row": c["row_number"], "instruction_id": iid, "client_account_number": c["account"],
               "client_number": c["client_number"], "process_status": c["status"],
               "sub_worktype": m.get("sub_worktype") or c["sub_worktype_csv"], "outcome": "", "detail": "",
               "left_out_files": ""}
        if c["needs"] in ("done", "held"):
            row.update(outcome="already sent earlier" if c["needs"] == "done" else "held", detail=c["note"])
        elif iid in sent:
            r = sent[iid]
            row.update(outcome=OUTCOMES.get(r["state"], r["state"]), detail=r.get("reason", ""),
                       left_out_files=r.get("left_out_files") or "")
        elif iid in held:
            row.update(outcome="held", detail=held[iid]["reason"])
        elif m:
            row.update(outcome="not sent - run ended", detail="downloaded, not handed over or not sent yet")
        else:
            row.update(outcome="not reached - run ended", detail="")
        if m.get("warnings"):
            row["detail"] = "; ".join(filter(None, [row["detail"], *m["warnings"]]))
        rows.append(row)
    rows.sort(key=lambda r: r["row"])
    return rows


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
