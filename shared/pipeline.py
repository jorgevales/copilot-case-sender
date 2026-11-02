"""The contract between the three parts of the daily review (stdlib only, no browser, no network).

  Part 1  System 1 documents   1_system1_documents/   downloads each case into CASES_ROOT\\.work\\<id> - <ACCOUNT>
  Part 2  System 2 snapshots   2_system2_snapshots/   one JSON snapshot per account, in bulk
  Part 3  Copilot sender       3_copilot_sender/      sends every case that is complete, one fresh chat each
  Coordinator                  orchestrator/          starts the parts, hands finished cases over, writes the daily report

The parts never call each other.  They only read and write the small files described here:

  RUNTIME_ROOT\\pipeline\\
    downloads\\<id>.json         Part 1: every document + Audit Trail of this case is saved in .work (persistent)
    cases\\<id>.json             Coordinator: the case folder is complete in CASES_ROOT, ready for Part 3 (persistent)
    snapshots\\<YYYY-MM-DD>\\     Part 2: <ACCOUNT>_Account_Summary_Interested_Parties_and_Links.json (reused the same day)
    runs\\<run id>\\
      plan.json                 Coordinator: the CSV rows of this run and what each one needs
      control\\                  go / check / stop / pause / resume / feed-closed / <part>-finished
      status\\                   answers to the sign-in check, Part 2's failed accounts
      events\\<part>.jsonl        progress lines Part 3 shows on the one console
      held\\<id>.json             a case that will not be sent this run, and why
      logs\\                      Part 1 / Part 2 console output
      diagnostics\\<part>\\        what each part saw, per case / account (read by the coordinator's pipeline report)
  RUNTIME_ROOT\\logs\\captures\\<run id>\\   screenshots + page copies taken when a case fails or is held

A case reaches CASES_ROOT only when it is complete (documents + Audit Trail + snapshot), in one rename from .work,
so Part 3 never sees a half-downloaded case.
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

WORK_DIR = ".work"                      # inside CASES_ROOT (same share, so the hand-over is one rename)
SNAPSHOT_SUFFIX = "_Account_Summary_Interested_Parties_and_Links.json"
ID_RE = re.compile(r"^[0-9a-f]{32}$")
REVIEWED_STATUS = "ended"               # only this Process Status is reviewed; every other value is skipped and listed
UNKNOWN_SUB = "Unknown Sub WorkType"    # what Part 1 records when System 1 shows no Sub WorkType


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.date.today().isoformat()


def norm_key(text) -> str:
    """Compare names loosely: case, punctuation and spacing ignored ('Change of Address' == 'change-of-address')."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(text or "").casefold()).split())


def norm_account(text) -> str:
    return re.sub(r"\s+", "", str(text or "")).upper()


def write_json(path: Path, obj, tries: int = 5) -> None:
    """Atomic replace through a temp file beside the target (same share), with short retries for a locked file."""
    raw = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(tries):
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(0.15 * (2 ** i))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def strip_query(url) -> str:
    """A web address safe for reports: everything after '?' or '#' (where sign-in codes live) is cut; the parameter
    NAMES stay so the address is still recognisable ('.../view?documentEntityId=…')."""
    text = str(url or "")
    try:
        u = urlsplit(text)
    except ValueError:
        return text.split("?", 1)[0].split("#", 1)[0]
    if not u.scheme and not u.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    names = [k for k, _ in parse_qsl(u.query, keep_blank_values=True)]
    return f"{u.scheme}://{u.netloc}{u.path}" + ("?" + "&".join(f"{k}=…" for k in names) if names else "")


REVIEWER_NOTE = (
    "FOR THE REVIEWING COPILOT: use everything in this file - and the screenshots, page copies and other files of the same "
    "run - to explain exactly what happened, as precisely and usefully as you can. In what you write back, never copy "
    "company or client details: no client or company names, instruction IDs, account or customer numbers, hosts, web "
    "addresses, file paths, user or machine names, access codes, tokens or session values (page copies can contain them, "
    "e.g. in inline scripts). Describe them generically instead (CASE-01, 'the System 1 host', 'an access value in an "
    "inline script'). Sub Worktype names, file types, counts, timings, page states, error texts and settings may stay.")


def html_note(html_text: str) -> str:
    """A saved page copy, opening with REVIEWER_NOTE as an HTML comment (the page itself is unchanged)."""
    return "<!-- " + REVIEWER_NOTE.replace("--", "-") + " -->\n" + str(html_text or "")


URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)\]]+")


def strip_queries(text) -> str:
    """Every web address inside a piece of text (a log line, a page excerpt) cut like strip_query."""
    return URL_IN_TEXT.sub(lambda m: strip_query(m.group(0)), str(text or ""))


# ---------------------------------------------------------------------------------------------- the daily CSV

class CsvError(Exception):
    """The CSV cannot be used at all (message is safe to show)."""


def _norm_head(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def read_case_list(path) -> tuple[list, list]:
    """The daily CSV -> (cases to review, skipped rows).

    Columns (header spelling is loose): instruction id (also the 'instrcution id' spelling), Client Account Number,
    Client Number, Process Status, Sub Worktype.  Only Process Status 'Ended' is reviewed; every other row is returned
    in `skipped` with the reason, so it can be shown clearly - nothing is dropped silently."""
    path = Path(path)
    if not path.is_file():
        raise CsvError("the CSV file was not found")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, dialect=dialect)
        heads = {_norm_head(h): h for h in reader.fieldnames or [] if h}
        col = {"iid": heads.get("instrcutionid") or heads.get("instructionid"),
               "account": heads.get("clientaccountnumber"), "client": heads.get("clientnumber"),
               "status": heads.get("processstatus"), "sub": heads.get("subworktype")}
        missing = [name for key, name in (("iid", "instruction id"), ("account", "Client Account Number"),
                                          ("status", "Process Status"), ("sub", "Sub Worktype")) if not col[key]]
        if missing:
            raise CsvError("the CSV has no column for: " + ", ".join(missing))
        cases, skipped, seen = [], [], set()
        for n, row in enumerate(reader, 2):
            get = lambda key: " ".join(str(row.get(col[key]) or "").split()) if col[key] else ""
            item = {"row_number": n, "iid": get("iid").lower(), "account": norm_account(get("account")),
                    "client_number": get("client"), "status": get("status"), "sub_worktype_csv": get("sub"),
                    "row": {" ".join(str(k).split()): " ".join(str(v or "").split()) for k, v in row.items() if k}}
            if not any(item["row"].values()):
                continue                                    # an empty line
            if not ID_RE.match(item["iid"]):
                skipped.append({**item, "reason": "not a valid instruction ID (32 hex characters)"})
            elif item["status"].casefold() != REVIEWED_STATUS:
                skipped.append({**item, "reason": f"Process Status is {item['status'] or 'blank'}"})
            elif item["iid"] in seen:
                skipped.append({**item, "reason": "the same instruction ID is on an earlier row"})
            elif not item["account"]:
                skipped.append({**item, "reason": "no Client Account Number"})
            else:
                seen.add(item["iid"])
                cases.append(item)
    return cases, skipped


# ---------------------------------------------------------------------------------------------- guidance per Sub Worktype

def guidance_for(sub_worktype: str, guidance_dir, exclude=()) -> Path | None:
    """The instructions file for this Sub Worktype: <guidance_dir>\\<Sub Worktype>.md, matched loosely by name.
    None when there is no such file (the case is then held, never sent with the wrong instructions)."""
    want = norm_key(sub_worktype)
    if not want or not guidance_dir:
        return None
    skip = {os.path.normcase(os.path.abspath(str(p))) for p in exclude if p}
    try:
        files = sorted(Path(guidance_dir).glob("*.md"), key=lambda p: p.name.casefold())
    except OSError:
        return None
    for p in files:
        if norm_key(p.stem) == want and os.path.normcase(os.path.abspath(str(p))) not in skip:
            return p
    return None


# ---------------------------------------------------------------------------------------------- folders

class Paths:
    """Every pipeline file location.  `run_dir` is this run's folder (None = only the persistent parts)."""

    def __init__(self, runtime_root, cases_root, run_dir=None):
        self.runtime_root, self.cases_root = Path(runtime_root), Path(cases_root)
        self.root = self.runtime_root / "pipeline"
        self.downloads = self.root / "downloads"
        self.cases = self.root / "cases"
        self.work = self.cases_root / WORK_DIR
        self.run = Path(run_dir) if run_dir else None

    @classmethod
    def new_run(cls, runtime_root, cases_root):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        return cls(runtime_root, cases_root, Path(runtime_root) / "pipeline" / "runs" / f"{stamp}-{uuid.uuid4().hex[:4]}")

    def snapshots(self, day: str | None = None) -> Path:
        return self.root / "snapshots" / (day or today())

    def snapshot(self, account: str, day: str | None = None) -> Path:
        return self.snapshots(day) / f"{norm_account(account)}{SNAPSHOT_SUFFIX}"

    @property
    def plan(self) -> Path:
        return self.run / "plan.json"

    def sub(self, name: str) -> Path:
        return self.run / name

    @property
    def logs(self) -> Path:
        """RUNTIME_ROOT\\logs: where the sender's run report lives - every file for the reviewing Copilot goes here."""
        return self.runtime_root / "logs"

    @property
    def captures(self) -> Path:
        return self.logs / "captures" / self.run.name

    def diagnostics(self, part: str) -> Path:
        return self.run / "diagnostics" / part

    def create(self) -> "Paths":
        for p in (self.downloads, self.cases, self.work, self.snapshots()):
            p.mkdir(parents=True, exist_ok=True)
        if self.run:
            for name in ("control", "status", "events", "held", "logs", "diagnostics"):
                (self.run / name).mkdir(parents=True, exist_ok=True)
        return self


def folder_name(iid: str, account: str) -> str:
    """The case folder name Part 1 has always used: '<instruction id> - <ACCOUNT>'."""
    return f"{iid} - {account}"


# ---------------------------------------------------------------------------------------------- control files

PARTS = ("part1", "part2", "part3", "coordinator")


class Control:
    """Signals between the parts, one small file each (S: is shared storage: no sockets, no locks needed).

    go        written once everything is signed in (you pressed Enter and every check passed)
    check     {"seq": n} - 'check your sign-in now'; each part answers in status\\<part>-check.json
    stop      {"by", "reason"} - stop safely: no new downloads, no new sends (answers already sent are still collected)
    pause     {"seq", "by", "reason"} - a part needs you (e.g. System 2 sign-in expired); resume {"seq"} after Enter
    feed-closed  no further case will be handed over this run (Part 3 ends once its answers are collected)"""

    def __init__(self, paths: Paths):
        self.p = paths
        self.dir = paths.sub("control")
        self.offsets = {}

    def _f(self, name: str) -> Path:
        return self.dir / name

    # go / stop / feed
    def go(self) -> None:
        write_json(self._f("go.json"), {"at": now()})

    def is_go(self) -> bool:
        return self._f("go.json").exists()

    def stop(self, by: str, reason: str) -> None:
        if not self._f("stop.json").exists():          # the first reason is the one shown
            write_json(self._f("stop.json"), {"by": by, "reason": reason, "at": now()})

    def stopped(self):
        return read_json(self._f("stop.json"))

    def close_feed(self) -> None:
        if not self._f("feed-closed.json").exists():
            write_json(self._f("feed-closed.json"), {"at": now()})

    def feed_closed(self) -> bool:
        return self._f("feed-closed.json").exists()

    # sign-in check
    def request_check(self) -> int:
        seq = (read_json(self._f("check.json"), {}) or {}).get("seq", 0) + 1
        write_json(self._f("check.json"), {"seq": seq, "at": now()})
        return seq

    def check_seq(self) -> int:
        return (read_json(self._f("check.json"), {}) or {}).get("seq", 0)

    def answer_check(self, part: str, seq: int, ok: bool, problem: str = "") -> None:
        write_json(self.p.sub("status") / f"{part}-check.json", {"seq": seq, "ok": bool(ok), "problem": problem, "at": now()})

    def check_answer(self, part: str, seq: int):
        ans = read_json(self.p.sub("status") / f"{part}-check.json")
        return ans if ans and ans.get("seq") == seq else None

    # pause / resume
    def pause(self, by: str, reason: str) -> int:
        cur = read_json(self._f("pause.json"), {}) or {}
        if cur.get("seq", 0) > self.resumed():
            return cur["seq"]                           # already waiting for you
        seq = max(cur.get("seq", 0), self.resumed()) + 1
        write_json(self._f("pause.json"), {"seq": seq, "by": by, "reason": reason, "at": now()})
        return seq

    def paused(self):
        cur = read_json(self._f("pause.json"))
        return cur if cur and cur.get("seq", 0) > self.resumed() else None

    def resume(self, seq: int) -> None:
        write_json(self._f("resume.json"), {"seq": seq, "at": now()})

    def resumed(self) -> int:
        return (read_json(self._f("resume.json"), {}) or {}).get("seq", 0)

    # end of a part
    def finished(self, part: str, **summary) -> None:
        write_json(self._f(f"{part}-finished.json"), {"at": now(), **summary})

    def is_finished(self, part: str):
        return read_json(self._f(f"{part}-finished.json"))

    # held cases
    def hold(self, iid: str, reason: str, by: str) -> None:
        write_json(self.p.sub("held") / f"{iid}.json", {"iid": iid, "reason": reason, "by": by, "at": now()})

    def held(self) -> dict:
        out = {}
        try:
            for f in self.p.sub("held").glob("*.json"):
                rec = read_json(f)
                if rec:
                    out[rec["iid"]] = rec
        except OSError:
            pass
        return out

    # progress lines (one writer per file)
    def event(self, part: str, text: str, level: str = "minimal", iid: str = "") -> None:
        line = json.dumps({"at": now(), "part": part, "level": level, "iid": iid, "text": text}, ensure_ascii=False)
        try:
            with open(self.p.sub("events") / f"{part}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass                                        # progress lines never decide anything

    def new_events(self) -> list:
        """Lines written since the last call (all parts), oldest first per part."""
        out = []
        for part in PARTS:
            f = self.p.sub("events") / f"{part}.jsonl"
            try:
                with open(f, "rb") as fh:
                    fh.seek(self.offsets.get(part, 0))
                    data = fh.read()
            except OSError:
                continue
            end = data.rfind(b"\n") + 1                 # a line still being written is read next time
            self.offsets[part] = self.offsets.get(part, 0) + end
            for raw in data[:end].splitlines():
                try:
                    out.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    pass
        return out


def read_plan(paths: Paths) -> dict:
    plan = read_json(paths.plan)
    if not plan:
        raise FileNotFoundError("this run has no plan.json")
    return plan


# ---------------------------------------------------------------------------------------------- the daily report

OUTCOMES = {
    "result_correct": "sent - Copilot: Correct", "result_incorrect": "sent - Copilot: Incorrect",
    "result_inconclusive": "sent - Copilot: Inconclusive", "submitted": "sent (answer not collected)",
    "submitted_partial": "sent with files left out", "result_invalid": "sent - answer not in the required format",
    "response_timeout": "sent - no answer in time", "submission_uncertain": "UNCERTAIN send - check the Copilot chat",
    "refused_chat_blocked": "sent - Copilot refused and blocked the chat",
    "preflight_blocked": "not sent - blocked", "failed_exhausted": "not sent - failed after all tries",
    "not_sent_stopped": "not sent - run stopped", "skipped_already_sent": "already sent earlier",
}
REPORT_COLUMNS = ["row", "instruction_id", "client_account_number", "client_number", "process_status", "sub_worktype",
                  "outcome", "detail", "left_out_files", "copilot_chat"]
REVIEWED = ("sent - Copilot: Correct", "sent - Copilot: Incorrect", "sent - Copilot: Inconclusive")


def daily_report(paths: Paths, plan: dict, control: Control) -> list:
    """One row per CSV row, in CSV order: what happened to it this run and why."""
    held = control.held()
    sent = read_json(paths.sub("status") / "part3-results.json", {}) or {}
    rows = []
    for c in plan["skipped"]:
        rows.append({"row": c["row_number"], "instruction_id": c["iid"], "client_account_number": c["account"],
                     "client_number": c["client_number"], "process_status": c["status"],
                     "sub_worktype": c["sub_worktype_csv"], "outcome": "skipped", "detail": c["reason"],
                     "left_out_files": "", "copilot_chat": ""})
    for c in plan["cases"]:
        iid = c["iid"]
        m = read_json(paths.downloads / f"{iid}.json") or {}
        row = {"row": c["row_number"], "instruction_id": iid, "client_account_number": c["account"],
               "client_number": c["client_number"], "process_status": c["status"],
               "sub_worktype": m.get("sub_worktype") or c["sub_worktype_csv"], "outcome": "", "detail": "",
               "left_out_files": "", "copilot_chat": ""}
        if c["needs"] in ("done", "held"):
            row.update(outcome="already sent earlier" if c["needs"] == "done" else "held", detail=c["note"])
        elif iid in sent:
            r = sent[iid]
            row.update(outcome=OUTCOMES.get(r["state"], r["state"]), detail=r.get("reason", ""),
                       left_out_files=r.get("left_out_files") or "", copilot_chat=r.get("chat", ""))
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


def needs_you(row: dict) -> bool:
    """A row you have to look at: anything but a clean verdict, an earlier send or a skipped row."""
    return row["outcome"] not in REVIEWED + ("already sent earlier", "skipped") or "CHECK" in str(row.get("detail", ""))


def workload_line(rows: list) -> str:
    """'Reviewed X of Y ...' - the one line that says whether the whole CSV was done."""
    ended = [r for r in rows if r["outcome"] != "skipped"]
    reviewed = sum(1 for r in ended if r["outcome"] in REVIEWED)
    return (f"Reviewed {reviewed} of {len(ended)} 'Ended' case(s) ({len(rows)} CSV row(s), "
            f"{len(rows) - len(ended)} skipped); {sum(1 for r in rows if needs_you(r))} need you")
