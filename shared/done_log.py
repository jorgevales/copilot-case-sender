"""The 'already done' log: every case System 1 downloaded, System 2 captured or System 3 sent, kept on the VDI.

  RUNTIME_ROOT\\already_done_log\\<writer>.jsonl   the record itself (one line per event; never edited, only added to).
                                                 One file per writer (the coordinator, Mission Control), so two
                                                 programs never write into the same file.
  CASES_ROOT\\Already sent log.xlsx               the same lines for you, rebuilt at the end of every run
                                                 (open in Excel = a time-stamped copy is written beside it instead).

A run that includes cases already in the log WARNS you and asks once (Enter = run them all again); nothing here ever
blocks a case by itself.  Copilot and Azure keep their own 'never send twice' lists; this log only informs.
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path

from shared import review_workbook as WB

FOLDER = "already_done_log"
XLSX_NAME = "Already sent log.xlsx"
SYSTEMS = ("system1", "system2", "system3")
# Outcomes that count as 'already done' for the warning (a package only saved, never sent, does not).
DONE = {"system1": ("downloaded",), "system2": ("captured",), "system3": ("sent", "uncertain")}
WORDS = {"downloaded": "documents downloaded", "captured": "account snapshot captured", "sent": "sent",
         "uncertain": "send uncertain - check before sending again", "prepared": "prepared, not sent (not connected)",
         "failed": "not sent - failed"}


def default_names(cfg=None) -> dict:
    """The names you gave the systems in setup ('System 1' / 'System 2' when none)."""
    return {"system1": (getattr(cfg, "system1_name", "") or "System 1").strip() or "System 1",
            "system2": (getattr(cfg, "system2_name", "") or "System 2").strip() or "System 2",
            "system3": "Mission Control"}


class DoneLog:
    def __init__(self, runtime_root, writer: str):
        self.folder = Path(runtime_root) / FOLDER
        self.file = self.folder / f"{writer}.jsonl"

    def add(self, system: str, iid: str, outcome: str, account: str = "", detail: str = "", run: str = "",
            sub_worktype: str = "") -> None:
        """One line; a failure to write is raised (the caller says so on screen - the log must not go quiet)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"), "system": system, "iid": iid,
                           "account": account, "sub_worktype": sub_worktype, "outcome": outcome, "detail": detail,
                           "run": run}, ensure_ascii=False)
        with open(self.file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def entries(self) -> list:
        """Every line of every writer, oldest first."""
        out = []
        try:
            files = sorted(self.folder.glob("*.jsonl"))
        except OSError:
            files = []
        for f in files:
            try:
                text = f.read_text("utf-8", errors="replace")
            except OSError:
                continue
            for raw in text.splitlines():
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue                                # a line cut by a crash is skipped, never fatal
                if isinstance(e, dict) and e.get("iid"):
                    out.append(e)
        out.sort(key=lambda e: str(e.get("at", "")))
        return out

    def done_before(self, iids, systems) -> dict:
        """iid -> {system: last time it was done} for the given cases and systems (only real downloads / captures /
        sends count)."""
        want, out = set(iids), {}
        for e in self.entries():
            s = e.get("system")
            if e["iid"] in want and s in systems and e.get("outcome") in DONE.get(s, ()):
                out.setdefault(e["iid"], {})[s] = e.get("at", "")
        return out

    def write_xlsx(self, folder, names: dict) -> tuple:
        """CASES_ROOT\\Already sent log.xlsx from every line -> (path written, True if it is the main file).  The
        file open in Excel -> a time-stamped copy beside it.  Never raises (the log itself is already safe)."""
        sheet = WB.Sheet("Already done", [20, 18, 36, 18, 28, 34, 50, 22])
        sheet.add(["Date and time", "System", "RequestId", "Client Account Number", "Sub Worktype", "What happened",
                   "Detail", "Run"], bold=True, fill="DDEBF7")
        for e in reversed(self.entries()):                  # newest first
            when = str(e.get("at", "")).replace("T", " ")
            sheet.add([when, names.get(e.get("system"), e.get("system", "")), e.get("iid", ""), e.get("account", ""),
                       e.get("sub_worktype", ""), WORDS.get(e.get("outcome"), e.get("outcome", "")),
                       e.get("detail", ""), e.get("run", "")],
                      fill="FCE4D6" if e.get("outcome") in ("uncertain", "failed") else "")
        try:
            data = WB.workbook_bytes([sheet])
            path = Path(folder) / XLSX_NAME
            for candidate in (path, path.with_name(f"{path.stem} - {datetime.datetime.now():%Y-%m-%d %H%M%S}.xlsx")):
                tmp = candidate.with_name(f".{candidate.name}.tmp-{uuid.uuid4().hex[:8]}")
                try:
                    tmp.write_bytes(data)
                    os.replace(tmp, candidate)
                    return candidate, candidate == path
                except OSError:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except Exception:                                   # noqa: BLE001
            pass
        return None, False
