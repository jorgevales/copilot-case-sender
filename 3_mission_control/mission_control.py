"""Mission Control - System 3 of the daily review: each complete case is sent to the company's Mission Control
workflow (GPT agents and sub-agents that produce the review).  FIRST VERSION: the exact trigger structure the workflow
needs is not known yet, so the package below is a starting point, made to be changed in one place (`case_package`).

  run --pipeline <run dir>   started by orchestrator/run_pipeline.py.  It is also the run's console when System 3 is
                             not part of the run (System 1 only / System 2 only / both): then it only shows the
                             progress, answers the sign-in step and sends nothing (plan 'send' = false).
  check                      the settings, and whether the endpoint answers at all (nothing is sent)

When (2026-11-26, user's rule): NOTHING is sent while System 1 / System 2 are still working.  Once every case of the run
is settled (the coordinator has written the run's document map and closed the feed), the map is sent FIRST, then the
cases one per send in the BAM Report's order (`send_order`: THE place to change to send related cases together).
A map that could not be made or was not accepted (after 3 tries) = no case is sent this run.
  POST <mission_control_url>   multipart/form-data
      the map:  part 'map'     JSON: shared/document_map.py's map (how_to_read, groups, instructions, documents)
      a case:   part 'case'    JSON: the case's BAM Report details, its Sub Worktype, its Received Documents table
                               ('documents_table'), what the map says about it ('related': group, related instructions
                               with their shared Document IDs) and the list of its files (name, kind, size, sha256), in
                               the order snapshot, Audit Trail, documents table, documents
                part 'files'   every file of the case folder, in that order
  The key (setting mission_control_auth = key) goes in the header named by mission_control_key_header.

Not connected yet (mission_control_url empty): nothing leaves the computer; the package's case.json is saved in
RUNTIME_ROOT\\mission-control\\<run>\\<id>\\ and the case is reported as 'prepared - Mission Control not connected yet'.
That copy is also kept for every real send (what exactly was sent).

Outcomes (daily report + the already-done log):
  sent        Mission Control answered 2xx (its reference, if it gives one, is recorded)
  uncertain   no answer in time after the case was sent - it may have arrived: check Mission Control before sending
              it again (never re-sent automatically)
  failed      refused (4xx) or unreachable after 3 tries - the run goes on with the next case
  prepared    not connected yet: saved only
A refused key (401/403) stops new sends for the rest of the run (every following case would fail the same way).
Nothing here reads the Sub Worktype instructions files: Mission Control's agents already have them.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures
import hashlib
import json
import os
import socket
import sys
import time
import types
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(os.path.abspath(__file__)).parent      # never .resolve(): it turns a mapped drive (S:) into its UNC form
PROJECT = HERE.parent
for p in (str(PROJECT / "3_copilot_sender"), str(PROJECT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import copilot_case_sender as S                     # noqa: E402  settings, storage rules, hand-over feed, log
from shared import pipeline as P                    # noqa: E402
from shared import done_log as DL                   # noqa: E402
from shared import document_map as DM               # noqa: E402

PACKAGE_VERSION = 2                                 # 2: documents table + related instructions; the map goes first
OUTBOX = "mission-control"                          # RUNTIME_ROOT\mission-control\<run>\<id>\case.json (+ map.json)
MAP_OUTBOX = "map.json"                             # RUNTIME_ROOT\mission-control\<run>\map.json: the map as sent
TRIES = 3
BACKOFF = (5, 15)
MAX_PACKAGE_MB = 300                                # one case bigger than this is not sent (reported as failed)
EXTRA_PHASES = {"mission-control", "mc-sent", "mc-prepared", "mc-failed", "mc-uncertain", "safe-stop", "mc-map",
                "mc-waiting"}
STATES = {"sent": "mc_sent", "prepared": "mc_prepared", "failed": "mc_failed", "uncertain": "mc_uncertain"}


class KeyRefused(Exception):
    """Mission Control refused the sign-in (401/403): no further case can be sent this run."""


def names_of(cfg) -> dict:
    return DL.default_names(cfg)


# ---------------------------------------------------------------------------------------------- the package

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def case_files(folder: Path, order: list, snapshot_suffix: str = P.SNAPSHOT_SUFFIX) -> list:
    """Every file of the case folder: first in the hand-over's order (snapshot, Audit Trail, documents), then any
    other file by name.  Each as {name, kind, path}."""
    present = {p.name: p for p in sorted(folder.iterdir(), key=lambda p: p.name.casefold()) if p.is_file()
               and not p.name.startswith((".", "~$"))}
    names = [n for n in order if n in present] + [n for n in present if n not in order]
    out = []
    for n in names:
        kind = ("account snapshot" if n.endswith(snapshot_suffix) else
                "documents table" if n == P.DOCUMENTS_TABLE_FILE else
                "audit trail" if n.casefold().startswith("audit trail") else "document")
        out.append({"name": n, "kind": kind, "path": present[n]})
    return out


def case_package(row: dict, handed: dict, folder: Path, table=None, related=None) -> tuple:
    """(the 'case' JSON, the files to send).  THE place to change when Mission Control's trigger structure is known.
    `table` = the case's Received Documents records, `related` = what the run's map says about it."""
    files = case_files(folder, list(handed.get("order") or []))
    meta = {
        "package_version": PACKAGE_VERSION,
        "request_id": row.get("iid", ""),
        "client_account_number": row.get("account", ""),
        "client_name": row.get("client_name", ""),
        "client_number": row.get("client_number", ""),
        "fund": row.get("fund", ""),
        "fund_type": row.get("fund_type", ""),
        "process_status": row.get("status", ""),
        "sub_worktype": handed.get("sub_worktype") or row.get("sub_worktype_csv", ""),
        "sub_worktype_in_report": row.get("sub_worktype_csv", ""),
        "received_date": row.get("received_date", ""),
        "end_date": row.get("end_date", ""),
        "source": row.get("case_source", ""),
        "email_subject": row.get("email_subject", ""),
        "warnings": list(handed.get("warnings") or []),
        "documents_table": list(table or []),
        "related": related or {"group": None, "related_instructions": [], "document_ids": []},
        "files": [{"name": f["name"], "kind": f["kind"], "size": f["path"].stat().st_size, "sha256": sha256(f["path"])}
                  for f in files],
        "prepared_at": P.now(),
    }
    return meta, files


def multipart(meta: dict, files: list) -> tuple:
    """-> (body bytes, content type)."""
    boundary = "----dailyreview" + uuid.uuid4().hex
    out = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"case\"\r\n"
           "Content-Type: application/json; charset=utf-8\r\n\r\n".encode(),
           json.dumps(meta, ensure_ascii=False).encode("utf-8"), b"\r\n"]
    for f in files:
        safe = f["name"].replace('"', "'").replace("\r", " ").replace("\n", " ")
        out += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{safe}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"), f["path"].read_bytes(), b"\r\n"]
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------------------------- the send

def reference_of(body: bytes) -> str:
    """Mission Control's own reference for the case, when its answer is JSON with an id-like field."""
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if isinstance(obj, dict):
        for k in ("id", "runId", "run_id", "workflowRunId", "reference", "jobId", "job_id", "requestId"):
            if obj.get(k):
                return str(obj[k])[:120]
    return ""


def post(cfg, body: bytes, ctype: str, opener=urllib.request.urlopen) -> tuple:
    """One POST -> (http status, answer body).  Raises urllib errors / timeouts as they come."""
    headers = {"Content-Type": ctype, "Accept": "application/json"}
    if cfg.mission_control_auth == "key":
        headers[cfg.mission_control_key_header.strip()] = cfg.mission_control_key
    req = urllib.request.Request(cfg.mission_control_url, data=body, headers=headers, method="POST")
    with opener(req, timeout=int(cfg.mission_control_timeout)) as resp:
        return resp.status, resp.read(65536)


def map_multipart(doc_map: dict) -> tuple:
    """The run's document map as its own send: one part 'map' (JSON).  -> (body bytes, content type)."""
    boundary = "----dailyreview" + uuid.uuid4().hex
    body = b"".join([f"--{boundary}\r\nContent-Disposition: form-data; name=\"map\"; filename=\"document_map.json\"\r\n"
                     "Content-Type: application/json; charset=utf-8\r\n\r\n".encode(),
                     json.dumps(doc_map, ensure_ascii=False).encode("utf-8"), f"\r\n--{boundary}--\r\n".encode()])
    return body, f"multipart/form-data; boundary={boundary}"


def send_case(cfg, meta: dict, files: list, opener=urllib.request.urlopen, sleep=time.sleep) -> tuple:
    """-> (outcome, plain detail).  outcome: sent / failed / uncertain.  Raises KeyRefused on 401/403."""
    size = sum(f["path"].stat().st_size for f in files)
    if size > MAX_PACKAGE_MB * 1024 * 1024:
        return "failed", f"the case's files are {size // (1024 * 1024)} MB - more than {MAX_PACKAGE_MB} MB, not sent"
    body, ctype = multipart(meta, files)
    return send_body(cfg, body, ctype, opener, sleep, "case")


def send_body(cfg, body: bytes, ctype: str, opener=urllib.request.urlopen, sleep=time.sleep, what="case") -> tuple:
    """One package (a case or the map), up to TRIES times -> (outcome, plain detail).  Raises KeyRefused on 401/403."""
    last = ""
    for n in range(1, TRIES + 1):
        try:
            status, answer = post(cfg, body, ctype, opener)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise KeyRefused(f"Mission Control refused the sign-in (HTTP {exc.code}) - check the key in setup")
            if exc.code in (408, 429) or exc.code >= 500:
                last = f"Mission Control was busy or had a problem (HTTP {exc.code})"
            else:
                return "failed", f"Mission Control refused this {what} (HTTP {exc.code})"
        except (socket.timeout, TimeoutError):
            return "uncertain", (f"no answer within {cfg.mission_control_timeout} s after sending - the {what} may have "
                                 "arrived: check Mission Control before sending it again")
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                return "uncertain", (f"no answer within {cfg.mission_control_timeout} s - the {what} may have arrived: "
                                     "check Mission Control before sending it again")
            last = "Mission Control could not be reached (network)"
        except OSError:
            last = "Mission Control could not be reached (connection)"
        else:
            if 200 <= status < 300:
                ref = reference_of(answer)
                return "sent", f"accepted (HTTP {status})" + (f", reference {ref}" if ref else "")
            return "failed", f"unexpected answer from Mission Control (HTTP {status})"
        if n < TRIES:
            sleep(BACKOFF[min(n - 1, len(BACKOFF) - 1)])
    return "failed", f"{last} - tried {TRIES} times"


def save_package(out: Path, meta: dict, outcome: str = "", detail: str = "") -> Path:
    out.mkdir(parents=True, exist_ok=True)
    P.write_json(out / "case.json", {**meta, "outcome": outcome, "detail": detail})
    return out


def send_map(cfg, paths: P.Paths, opener=urllib.request.urlopen, sleep=time.sleep) -> tuple:
    """The run's document map, before any case -> (ok, plain detail, the map or None).  ok only when Mission Control
    accepted it (2xx) or, not connected yet, when it was prepared.  Raises KeyRefused."""
    status = DM.read_status(paths) or {}
    doc_map = DM.read_map(paths) if status.get("ok") else None
    if not doc_map:
        return False, "the run's document map could not be made (" + (status.get("error") or "no map file") + ")", None
    out = Path(cfg.runtime_root) / OUTBOX / paths.run.name
    out.mkdir(parents=True, exist_ok=True)
    if not cfg.mission_control_url:
        P.write_json(out / MAP_OUTBOX, {**doc_map, "outcome": "prepared"})
        return True, "Mission Control not connected yet - the document map was prepared and saved, not sent", doc_map
    body, ctype = map_multipart(doc_map)
    try:
        outcome, detail = send_body(cfg, body, ctype, opener, sleep, "map")
    except KeyRefused as exc:
        P.write_json(out / MAP_OUTBOX, {**doc_map, "outcome": "failed", "detail": str(exc)})
        raise
    P.write_json(out / MAP_OUTBOX, {**doc_map, "outcome": outcome, "detail": detail})
    return outcome == "sent", detail, doc_map


def send_order(cases: list, plan_order: list, doc_map=None) -> list:
    """THE place that decides how the cases go after the map.  Now: one case per send, in the BAM Report's order.
    (To send related cases together later: group them here by doc_map['instructions'][id]['group'].)"""
    rank = {iid: n for n, iid in enumerate(plan_order)}
    return sorted(cases, key=lambda c: rank.get(c.id, len(rank)))


def process(cfg, feed, case, opener=urllib.request.urlopen, sleep=time.sleep) -> tuple:
    """One case end to end -> (outcome, detail).  Raises KeyRefused."""
    row = feed.rows.get(case.id) or {}
    handed = P.read_json(feed.paths.cases / f"{case.id}.json") or {}
    table = (P.read_json(feed.paths.downloads / f"{case.id}.json") or {}).get("documents_table")
    meta, files = case_package(row, handed, case.path, table, DM.related_of(getattr(feed, "doc_map", None), case.id))
    out = Path(cfg.runtime_root) / OUTBOX / feed.paths.run.name / case.id
    if not cfg.mission_control_url:
        save_package(out, meta, "prepared", "Mission Control not connected yet - saved, not sent")
        return "prepared", "Mission Control not connected yet - the case was prepared and saved, not sent"
    try:
        outcome, detail = send_case(cfg, meta, files, opener, sleep)
    except KeyRefused as exc:
        save_package(out, meta, "failed", str(exc))
        raise
    save_package(out, meta, outcome, detail)
    return outcome, detail


# ---------------------------------------------------------------------------------------------- run --pipeline

class MissionFeed(S.PipelineFeed):
    """The sender's hand-over feed; only the sign-in banner differs (no Copilot window, your system names)."""

    names = {"part1": "System 1", "part2": "System 2"}
    third = "  - Mission Control: no sign-in needed here\n"
    no_terminal_wait = float(os.environ.get("DAILY_REVIEW_NO_TERMINAL_WAIT") or 10)   # no console: check every 10 s

    async def confirm_ready(self, stdin, log, stop) -> None:
        if not self.parts:
            self.control.go()
            return
        names = self.names
        S.say("\n" + "=" * 96 + "\nSIGN IN, THEN PRESS ENTER HERE\n"
              "  - Systems Edge window: " + " and ".join(names[p] for p in self.parts)
              + (f" (keep exactly ONE {names['part1']} tab, showing My Inbox)" if "part1" in self.parts else "") + "\n"
              + self.third + "=" * 96)
        while True:
            line = await stdin.line()
            if line is None:
                await stop.sleep(self.no_terminal_wait)
            stop.check()
            seq = await asyncio.to_thread(self.control.request_check)
            S.say("Checking " + " and ".join(names[p] for p in self.parts) + " ...")
            answers, t0 = {}, time.monotonic()
            while len(answers) < len(self.parts) and time.monotonic() - t0 < S.CHECK_SECONDS:
                for part in self.parts:
                    if part not in answers:
                        a = await asyncio.to_thread(self.control.check_answer, part, seq)
                        if a is None and await asyncio.to_thread(self.control.is_finished, part):
                            a = {"ok": False, "problem": f"{names[part]} part has stopped (see its log in the run folder)"}
                        if a:
                            answers[part] = a
                await stop.sleep(0.5)
            problems = [f"{names[p]}: " + (answers[p]["problem"] if p in answers else "no answer yet (still starting?)")
                        for p in self.parts if not answers.get(p, {}).get("ok")]
            if not problems:
                await asyncio.to_thread(self.control.go)
                log("pipeline", note="everything is signed in - the run starts now")
                return
            S.say("Not ready yet:\n  - " + "\n  - ".join(problems) + "\nFix this in Edge, then press Enter again.")


async def pipeline_async(ctx, run_dir: Path, opener=urllib.request.urlopen) -> int:
    cfg, log = ctx.cfg, ctx.log
    stop = S.Stopper()
    feed = MissionFeed(run_dir, ctx, S.local_salt(ctx.rt))
    feed.doc_map = None
    names = names_of(cfg)
    feed.names = {"part1": names["system1"], "part2": names["system2"]}
    sending = bool(feed.plan.get("send", True))
    if not sending:
        feed.third = "  - Mission Control: not part of this run (nothing is sent)\n"
    elif not cfg.mission_control_url:
        feed.third = "  - Mission Control: NOT CONNECTED YET - each case is prepared and saved, not sent\n"
    done = DL.DoneLog(cfg.runtime_root, "mission-control")
    results, counts = {}, collections.Counter()
    refused, code = "", 0
    terminal = S.StdinLines()
    relay = asyncio.ensure_future(feed.relay(log, terminal))
    pool = concurrent.futures.ThreadPoolExecutor(max(1, int(cfg.mission_control_parallel)))
    loop = asyncio.get_running_loop()

    def record(case, outcome, detail):
        results[case.key] = (STATES[outcome], detail)
        counts[outcome] += 1
        log("mc-" + outcome, case.alias, note=detail)
        row = feed.rows.get(case.id) or {}
        try:
            done.add("system3", case.id, outcome, row.get("account", ""), detail, feed.paths.run.name,
                     feed.sub_of.get(case.key) or row.get("sub_worktype_csv", ""))
        except OSError as exc:
            log("WARNING", case.alias, note=f"the already-done log could not be written: {S.safe_exc(ctx.red, exc)}")

    try:
        await feed.confirm_ready(terminal, log, stop)
        queue, running = [], {}
        map_ok, no_map, told = False, "", False       # the map goes first: no case before it was accepted
        while True:
            for case in await asyncio.to_thread(feed.poll):
                if not sending:
                    continue                                # System 1 / 2 only: the case stays ready in CASES_ROOT
                if case.blocked:
                    feed.control.hold(case.id, case.blocked, "part3")
                    log("held", case.alias, note=case.blocked)
                else:
                    queue.append(case)
            stopping = bool(stop.is_set or feed.stopping() or refused)
            if queue and not map_ok and not no_map and not stopping:
                if not feed.closed:                         # System 1 / System 2 still working on other cases
                    if not told:
                        told = True
                        log("mc-waiting", note=f"{len(queue)} case(s) ready - nothing is sent until every case of the "
                                               "run is settled; then the document map goes first")
                else:
                    try:
                        map_ok, detail, feed.doc_map = await loop.run_in_executor(pool, send_map, cfg, feed.paths, opener)
                    except KeyRefused as exc:
                        refused, code, map_ok, detail = str(exc), 3, False, str(exc)
                        feed.control.stop("part3", str(exc))
                    except Exception as exc:                # noqa: BLE001 - reported, the cases are not sent
                        map_ok, detail = False, f"could not be sent: {S.safe_exc(ctx.red, exc)}"
                    log("mc-map", note=("document map: " + detail) if map_ok else f"document map NOT sent: {detail} - "
                        "no case is sent this run")
                    if map_ok:
                        queue = send_order(queue, feed.wanted, feed.doc_map)
                    else:
                        no_map = "not sent - the document map did not reach Mission Control first: " + detail
                    stopping = bool(stopping or refused)
            if no_map:
                for case in queue:
                    record(case, "failed", no_map)
                queue.clear()
            while queue and map_ok and not stopping and len(running) < pool._max_workers:
                case = queue.pop(0)
                log("mission-control", case.alias, note="sending to Mission Control" if cfg.mission_control_url
                    else "preparing the package (not connected yet)")
                running[loop.run_in_executor(pool, process, cfg, feed, case, opener)] = case
            if stopping:
                reason = (refused and "not sent - " + refused) or \
                         (stop.is_set and "run stopped by you before this case was sent") or feed.stop_reason()
                for case in queue:
                    results[case.key] = ("not_sent_stopped", reason)
                queue.clear()
            if not running:
                if not queue and (feed.done or stopping):
                    break
                try:
                    await stop.sleep(1.0)
                except S.Stopped:
                    pass
                continue
            finished, _ = await asyncio.wait(list(running), timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            for fut in finished:
                case = running.pop(fut)
                try:
                    outcome, detail = fut.result()
                except KeyRefused as exc:
                    if not refused:
                        refused, code = str(exc), 3
                        feed.control.stop("part3", str(exc))
                        log("safe-stop", case.alias, note=f"{exc} - no further case is sent this run")
                    record(case, "failed", str(exc))
                    continue
                except Exception as exc:                    # noqa: BLE001 - one case never stops the others
                    outcome, detail = "failed", f"could not be prepared or sent: {S.safe_exc(ctx.red, exc)}"
                record(case, outcome, detail)
    except S.Stopped:
        log("stopped", note="stopped by you: no new cases were sent")
    finally:
        relay.cancel()
        pool.shutdown(wait=True)
        try:
            feed.write_results(results, types.SimpleNamespace(cases={}))
        except Exception as exc:                            # noqa: BLE001 - the daily report still lists every row
            log("WARNING", note=f"part3-results could not be written: {S.safe_exc(ctx.red, exc)}")
        if sending:
            S.say("\nMISSION CONTROL  " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no case sent"))
    return code


def run_command(args) -> int:
    cfg = S.load_config()
    if getattr(args, "detail", None):
        cfg.console_detail = args.detail
    ctx = S.context(cfg)                                # validates; storage rules + write guard, like the sender
    S.MINIMAL_PHASES.update(EXTRA_PHASES | {"held"})
    S.CONSOLE["log"] = ctx.log
    return asyncio.run(pipeline_async(ctx, Path(args.pipeline)))


def check_command(args, opener=urllib.request.urlopen) -> int:
    """The settings, and (when connected) whether the endpoint answers at all - nothing is sent."""
    cfg = S.load_config()
    problems = S.mission_control_problems(cfg)
    if problems:
        print("Mission Control settings - fix in 'Run.cmd setup':\n  - " + "\n  - ".join(problems))
        return 2
    if not cfg.mission_control_url:
        print("Mission Control: not connected yet (no endpoint address) - cases are prepared and saved, not sent.")
        return 0
    try:
        with opener(urllib.request.Request(cfg.mission_control_url, method="HEAD"), timeout=15) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code                                   # any HTTP answer means the address is reachable
    except (urllib.error.URLError, OSError):
        print("Mission Control: the endpoint could not be reached from this computer.")
        return 3
    print(f"Mission Control: the endpoint answers (HTTP {status}). Nothing was sent.")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Mission Control - System 3 of the daily review.")
    sub = ap.add_subparsers(dest="command")
    r = sub.add_parser("run", help="the daily pipeline's System 3 (started by the coordinator)")
    r.add_argument("--pipeline", metavar="RUN_DIR", required=True)
    r.add_argument("--detail", choices=("minimal", "full"))
    sub.add_parser("check", help="settings + whether the endpoint answers (nothing is sent)")
    args = ap.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "check":
            return check_command(args)
        ap.print_help()
        return 2
    except S.StorageError as exc:
        print(f"STORAGE PROBLEM: {exc}. Nothing was written elsewhere.")
        return 3
    except S.SenderError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
