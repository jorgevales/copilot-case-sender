#!/usr/bin/env python3
"""COORDINATOR - the daily review's MENU and runs (double-click Run.cmd).

  MENU   1 Full run      System 1 + System 2 together; each case goes to System 3 the moment it is complete
         2 System 1 only (documents)        3 System 2 only (snapshots)        4 System 1 + System 2 (no review)
         5 System 3 only (send the cases already downloaded and captured)
         6 Setup         7 Check
  (System 1 / System 2 show the names you gave them in setup; System 3 = Mission Control, or Copilot / Azure.)

  Every run:
  1. Asks for the BAM Report (.xlsx; the newest in your BAM Reports folder is offered) and lists the rows excluded
     automatically (Voided ...).
  2. Asks which cases (the 10 run choices, shared/run_scope.py).
  3. Warns about cases already done before (the already-done log, shared/done_log.py) and asks once:
     Enter = run them all again, 1 = skip them, 2 = show the list first.
  4. Opens the systems Edge when System 1 or 2 is in the run, starts them as background programs (they wait for go).
  5. Starts System 3 in this console (Mission Control, the Copilot sender or the Azure reviewer) - or, when System 3 is
     not in the run, Mission Control's console only (the sign-in step and progress; nothing is sent).
  6. Meanwhile hands each complete case over (snapshot copied in, folder moved from .work into CASES_ROOT), so case 1
     is sent while System 1 downloads case 2.
  7. At the end: the daily report (one line per row), the already-done log in Excel, the pipeline report.

Nothing here touches a page: the browser work is in the parts.  shared/pipeline.py describes the files they share.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

HERE = Path(os.path.abspath(__file__)).parent      # never .resolve(): it turns a mapped drive (S:) into its UNC form
PROJECT = HERE.parent
for _p in (PROJECT, PROJECT / "3_copilot_sender", PROJECT / "1_system1_documents", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import copilot_case_sender as sender               # noqa: E402  config, storage rules, Edge helpers (Part 3's, reused)
from shared import pipeline as P                   # noqa: E402
from shared import review_workbook as WB            # noqa: E402
from shared import bam_report as BAM                # noqa: E402
from shared import run_scope as RS                  # noqa: E402
from shared import done_log as DL                   # noqa: E402
import handover as H                               # noqa: E402
import studio_visual_cursor as VC                  # noqa: E402  only its clean-up script (stdlib module, no Playwright)

PART_SCRIPTS = {"part1": PROJECT / "1_system1_documents" / "system1_documents.py",
                "part2": PROJECT / "2_system2_snapshots" / "system2_snapshots.py"}
SENDER_SCRIPT = PROJECT / "3_copilot_sender" / "copilot_case_sender.py"
AZURE_SCRIPT = PROJECT / "3_azure_reviewer" / "azure_case_reviewer.py"
MISSION_SCRIPT = PROJECT / "3_mission_control" / "mission_control.py"
EDGE_SECONDS = 30                                   # the systems Edge's debugging endpoint should answer by then
STOP_WAIT_SECONDS = 600                            # after System 3 ends: how long Part 1 / Part 2 may take to finish
MENU = [("full", "Full run - {s1} + {s2} together; each complete case goes to {rv} at once"),
        ("s1", "{s1} only - download the case documents"),
        ("s2", "{s2} only - capture the account snapshots"),
        ("s12", "{s1} + {s2} - documents and snapshots, no review"),
        ("s3", "{rv} only - send the cases already downloaded and captured"),
        ("setup", "Setup - names, pages, folders, {rv}"),
        ("check", "Check - test the settings (nothing is sent)")]


def names_of(cfg) -> dict:
    n = DL.default_names(cfg)
    n["system3"] = P.REVIEWER_NAMES.get(reviewer_of(cfg), "System 3").replace("the Azure", "Azure")
    return n


def part_names(cfg) -> dict:
    n = names_of(cfg)
    return {"part1": f"{n['system1']} (documents)", "part2": f"{n['system2']} (snapshots)"}


def pipeline_problems(cfg, systems: str = "123") -> list:
    out = []
    keys = []
    if "1" in systems:
        keys += [("system1_url", "System 1 inbox page address"), ("system1_brand_text", "System 1 brand text pattern")]
    if "2" in systems:
        keys += [("system2_url", "System 2 signed-in page address"),
                 ("system2_account_page", "System 2 account summary page address"),
                 ("system2_party_marker", "System 2 party profile link text"),
                 ("system2_party_links_page", "System 2 party links page address"),
                 ("system2_customer_summary_page", "System 2 customer summary page address")]
    reviewer = reviewer_of(cfg)
    if H.needs_guidance(reviewer) and ("1" in systems or "3" in systems):
        keys.append(("guidance_dir", "guidance folder (one .md per Sub Worktype)"))
    for key, what in keys:
        if not getattr(cfg, key, ""):
            out.append(f"{what} is not set")
    for key in ("system1_url", "system2_url", "system2_account_page", "system2_party_links_page", "system2_customer_summary_page"):
        if key not in dict(keys):
            continue
        u = urlparse(getattr(cfg, key, "") or "")
        if getattr(cfg, key, "") and (u.scheme not in ("https", "http") or not u.netloc):
            out.append(f"{key} is not a web address")
    if "2" in systems:
        for key, slot in (("system2_account_page", "{account}"), ("system2_party_links_page", "{party}"),
                          ("system2_customer_summary_page", "{customer}")):
            if getattr(cfg, key, "") and slot not in getattr(cfg, key):
                out.append(f"{key} must contain {slot} where the value goes")
        customer = urlparse(getattr(cfg, "system2_customer_summary_page", "") or "")
        home = urlparse(getattr(cfg, "system2_url", "") or "")
        if customer.netloc and home.netloc and customer.netloc.lower() != home.netloc.lower():
            out.append("system2_customer_summary_page must be on the same host as system2_url (the System 2 sign-in)")
        pages = getattr(cfg, "system2_detail_pages", None) or {}
        for name in P.DETAIL_SECTIONS:              # every account-detail page is mandatory for a complete snapshot
            page = pages.get(name, "") if isinstance(pages, dict) else ""
            u = urlparse(page or "")
            if not page:
                out.append(f"System 2 account-detail page '{name}' is not set (run setup)")
            elif u.scheme not in ("https", "http") or not u.netloc:
                out.append(f"System 2 account-detail page '{name}' is not a web address")
            elif "{account}" not in page:
                out.append(f"System 2 account-detail page '{name}' must contain {{account}} where the account number goes")
    if "3" in systems:
        azure = reviewer == "azure"
        if (azure or (reviewer == "copilot" and cfg.completion_mode == "json_result")) and not cfg.result_contract_path:
            out.append("the result contract file is not set" + (" (the Azure reviewer always needs it)" if azure else ""))
        if azure and not AZURE_SCRIPT.is_file():
            out.append("reviewer is 'azure' but 3_azure_reviewer\\azure_case_reviewer.py is missing from this folder")
        if reviewer == "mission_control" and not MISSION_SCRIPT.is_file():
            out.append("3_mission_control\\mission_control.py is missing from this folder")
        if reviewer == "copilot" and int(cfg.systems_cdp_port) == int(cfg.cdp_port):
            out.append("systems_cdp_port must differ from cdp_port (System 1/2 and Copilot run in two separate Edges)")
    return out


def reviewer_of(cfg) -> str:
    """System 3 of this setup: 'mission_control', 'copilot' or 'azure'."""
    who = str(getattr(cfg, "reviewer", "") or "copilot").strip().lower()
    return who if who in P.REVIEWERS else "copilot"


def ledger_path(cfg, rt) -> Path:
    """The 'never twice' list of Copilot (sent_instructions.txt) or of the Azure reviewer."""
    return rt.root / P.AZURE_LEDGER if reviewer_of(cfg) == "azure" else rt.ledger


# ---------------------------------------------------------------------------------------------- the questions

def ask_line(prompt: str, default: str = "") -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return default


def menu(cfg) -> str:
    n = names_of(cfg)
    words = {"s1": n["system1"], "s2": n["system2"], "rv": n["system3"]}
    while True:
        print("\n" + "=" * 96 + "\nDAILY REVIEW - MENU\n" + "=" * 96)
        for i, (_, text) in enumerate(MENU, 1):
            print(f"  {i}  {text.format(**words)}")
        print("  0  Exit")
        answer = ask_line("Your choice: ", "0")
        if answer == "0":
            return ""
        if answer.isdigit() and 1 <= int(answer) <= len(MENU):
            return MENU[int(answer) - 1][0]
        print("  Please type a number from the menu.")


def reports_folder(cfg) -> Path:
    return Path(getattr(cfg, "bam_reports_dir", "") or PROJECT / "BAM Reports")


def ask_report(cfg) -> Path:
    """The newest .xlsx in your BAM Reports folder is offered; Enter takes it, or paste / drag another file."""
    folder = reports_folder(cfg)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    while True:
        newest = BAM.newest_report(folder)
        if newest:
            answer = sender.clean_path(ask_line(f"\nBAM Report: {newest.name}  (from {folder})\n"
                                                "  Press Enter to use it, or paste / drag another .xlsx here: "))
            path = Path(answer) if answer else newest
        else:
            answer = sender.clean_path(ask_line(f"\nNo BAM Report in {folder} yet.\n"
                                                "  Drop it there and press Enter, or paste / drag the .xlsx here: "))
            if not answer:
                if not sys.stdin or sys.stdin.closed:
                    raise sender.SenderError("the BAM Report is needed (no console input)")
                continue
            path = Path(answer)
        if path.is_file() and path.suffix.lower() == ".xlsx":
            return path
        print("  That is not an .xlsx file that can be found - try again.")


def print_excluded(excluded: list) -> None:
    if not excluded:
        print("Automatically excluded: none (every row has Process Status 'Ended').")
        return
    print(f"\nAUTOMATICALLY EXCLUDED - {len(excluded)} row(s) are NOT part of any run:")
    for s in excluded:
        print(f"  row {s['row_number']:>4}  {s['iid'] or '(no RequestId)':<32}  {s['reason']}")
    print()


def ask_again(done: dict, systems: str, reviewer: str, names: dict, answer=None) -> str:
    """Cases already done before: 'again' (run them all again, the default) or 'skip'."""
    per = {}
    for iid, by in done.items():
        for s, when in by.items():
            per.setdefault(s, []).append(when)
    words = {"system1": "documents downloaded by", "system2": "snapshots captured by", "system3": "sent to"}
    print(f"\nALREADY DONE BEFORE - {len(done)} of the chosen cases:")
    for s in ("system1", "system2", "system3"):
        if s in per:
            print(f"  - {len(per[s])} {words[s]} {names[s]} (last on {max(per[s])[:10]})")
    if answer:
        return "skip" if answer.lower() in ("no", "skip", "1") else "again"
    while True:
        choice = ask_line("  Enter = run them all again,  1 = skip them,  2 = show the list first: ")
        if choice == "2":
            for iid, by in sorted(done.items()):
                print(f"    {iid}  " + ", ".join(f"{names[s]} {when[:16].replace('T', ' ')}" for s, when in sorted(by.items())))
            continue
        if choice == "1":
            return "skip"
        if choice == "":
            return "again"
        print("  Please press Enter, or type 1 or 2.")


def print_plan(plan: dict, names: dict) -> None:
    counts = {}
    for c in plan["cases"]:
        counts[c["needs"]] = counts.get(c["needs"], 0) + 1
    sys_ = plan.get("systems", "123")
    work = "to retrieve" + (" and send" if plan.get("send") else "")
    print(f"PLAN  {counts.get('download', 0)} {work}, {counts.get('send', 0)} complete already (send only), "
          f"{counts.get('done', 0)} nothing to do, {counts.get('held', 0)} held"
          + (f"  |  {plan.get('out_of_scope', 0)} other case(s) of the report are not in this run's choice"
             if plan.get("out_of_scope") else ""))
    print("  Systems in this run: " + " + ".join(names[f"system{s}"] for s in sys_))
    for c in plan["cases"]:
        if c["needs"] == "held":
            print(f"  HELD  {c['iid']}  {c['note']}")
    if plan["no_guidance"]:
        print("  WARNING: no instructions file in the guidance folder yet for Sub Worktype(s): "
              + ", ".join(f"'{s}'" for s in plan["no_guidance"])
              + "\n           those cases are held before anything is downloaded, unless System 1 shows a Sub Worktype"
                " that has one.")


# ---------------------------------------------------------------------------------------------- the systems Edge

def open_systems_edge(cfg, rt, systems: str = "12") -> str:
    """Join the systems Edge if it is already open on its port, else start it with its own remembered profile
    (RUNTIME_ROOT\\profile-systems, never the system drive).  Missing System 1 / System 2 tabs are opened once."""
    port = int(cfg.systems_cdp_port)
    urls = [u for s, u in (("1", cfg.system1_url), ("2", cfg.system2_url)) if s in systems]
    payload = sender.cdp_http(port, "/json/version", 0.75)
    if payload:
        if not sender.endpoint_valid(payload, port):
            raise sender.SenderError(f"port {port} is in use by something that is not an Edge debugging endpoint")
        pages = [t.get("url", "") for t in sender.cdp_http(port, "/json/list", 1.5) or [] if t.get("type") == "page"]
        for url in urls:
            u = urlparse(url)
            if not any(p.startswith(f"{u.scheme}://{u.netloc}") for p in pages):
                sender.cdp_http(port, "/json/new?" + quote(url, safe=":/?&=%#"), 3, method="PUT")
        return "joined (already open)"
    profile = rt.root / "profile-systems"
    prefs = profile / "Default" / "Preferences"
    if not prefs.exists():                          # downloads on S:, never a prompt
        prefs.parent.mkdir(parents=True, exist_ok=True)
        d = str(rt.root / "downloads-systems")
        sender.write_atomic(prefs, json.dumps({"download": {"default_directory": d, "prompt_for_download": False},
                                               "savefile": {"default_directory": d}}))
    # Loopback only.  The three background switches keep System 1 at full speed while another window is in front
    # (Proposed until confirmed on the VDI).
    flags = [cfg.edge_executable, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={profile}", f"--disk-cache-dir={rt.root / 'cache-systems'}",
             f"--crash-dumps-dir={rt.crashes}", "--no-first-run", "--no-default-browser-check",
             "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
             "--disable-renderer-backgrounding", "--new-window"] + urls
    err = open(rt.logs / "edge-systems-stderr.log", "ab")
    quiet = None
    if os.name == "nt":                             # shown without taking the keyboard from your app
        quiet = subprocess.STARTUPINFO()
        quiet.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        quiet.wShowWindow = 4                       # SW_SHOWNOACTIVATE
    subprocess.Popen(flags, cwd=str(rt.work), stdin=subprocess.DEVNULL, stdout=err, stderr=err, startupinfo=quiet,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    end = time.monotonic() + EDGE_SECONDS
    while time.monotonic() < end:
        if sender.endpoint_valid(sender.cdp_http(port, "/json/version", 0.5), port):
            return "started"
        time.sleep(0.1)
    raise sender.SenderError(f"the systems Edge did not open its debugging port {port} within {EDGE_SECONDS}s")


# ---------------------------------------------------------------------------------------------- the run

def start_part(part: str, paths: P.Paths, rt):
    """A background program: no console window, no Ctrl+C from this console (stopped through the stop signal)."""
    log = open(paths.sub("logs") / f"{part}.log", "ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen([sys.executable, "-B", "-E", "-s", str(PART_SCRIPTS[part]), "--run-dir", str(paths.run)],
                            cwd=str(rt.work), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=flags)


def sweep_pointer(cfg, procs: dict, control: P.Control) -> str:
    """End of the run: remove Part 1's purple pointer from every tab of the systems Edge - one clean-up script per tab
    over its own loopback DevTools socket (no click, no reload, no tab closed or brought to the front).  Part 1 removes
    its own pointer when it ends; this also covers a Part 1 that crashed or was stopped, a tab its clean-up could not
    reach, and a pointer left by an earlier run.  Never while Part 1 still runs (its pointer then shows real work).
    Safe to repeat: a tab without a pointer is left as it is.  Returns the line it printed."""
    p1 = procs.get("part1")
    if p1 is not None and p1.poll() is None:
        line = "Visual pointer: Part 1 is still finishing - it removes its own pointer when it ends"
    else:
        port = int(cfg.systems_cdp_port)
        tabs = sender.cdp_http(port, "/json/list", 1.5)
        if not isinstance(tabs, list):
            line = "Visual pointer: the systems Edge did not answer - no tab was swept"
        else:
            removed = clean = failed = 0
            for t in tabs:
                if t.get("type") != "page" or not str(t.get("url", "")).startswith(("http://", "https://")):
                    continue
                found = sender.cdp_page_eval(str(t.get("webSocketDebuggerUrl", "")), port, VC.SWEEP, 3.0)
                if found is None:
                    failed += 1
                elif found:
                    removed += 1
                else:
                    clean += 1
            line = (f"Visual pointer: removed from {removed} tab(s) of the systems Edge, none on {clean}"
                    + (f", {failed} did not answer" if failed else ""))
    print(line, flush=True)
    control.event("coordinator", line)
    return line


def third_script(reviewer: str, systems: str) -> Path:
    """System 3 of this run; Mission Control's console (sending nothing) when System 3 is not in the run."""
    if "3" not in systems or reviewer == "mission_control":
        return MISSION_SCRIPT
    return AZURE_SCRIPT if reviewer == "azure" else SENDER_SCRIPT


def run_sender(paths: P.Paths, detail, script: Path) -> int:
    """System 3 in THIS console (its questions and the Enter).  Ctrl+C belongs to it: the first press stops it safely."""
    cmd = [sys.executable, "-B", "-E", "-s", str(script), "run", "--pipeline", str(paths.run)]
    proc = subprocess.Popen(cmd + (["--detail", detail] if detail else []))
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue                                # System 3 handles it; this window just waits for it


def run_mode(cfg, mode: str, args) -> int:
    systems = P.MODES[mode]
    names = names_of(cfg)
    reviewer = reviewer_of(cfg)
    problems = pipeline_problems(cfg, systems)
    if problems:
        print("Not set up for this run yet - choose Setup in the menu (or 'Run.cmd setup'):\n  - " + "\n  - ".join(problems))
        return 2
    ctx = sender.context(cfg)                       # validates paths, storage rules (nothing on the system drive)
    if "1" in systems or "2" in systems or ("3" in systems and reviewer == "copilot"):
        sender.ensure_dependencies(cfg, ctx.rt, ctx.log)
    if "3" in systems and (reviewer == "azure" or (reviewer == "copilot" and cfg.completion_mode == "json_result")):
        try:                                        # the older results workbook is renamed (open in Excel = wait)
            WB.make_room(Path(cfg.cases_root), name=WB.name_for(reviewer))
        except RuntimeError as exc:
            raise sender.SenderError(str(exc)) from None
    report = Path(sender.clean_path(args.report)) if args.report else ask_report(cfg)
    cases, excluded, warnings = P.read_report(report)
    if str(report) != cfg.last_csv_path:
        cfg.last_csv_path = str(report)
        try:                                        # a convenience only
            sender.save_config(cfg)
        except (sender.StorageError, sender.PersistenceError):
            pass
    print(f"\nBAM Report '{report.name}': {len(cases) + len(excluded)} row(s) - {len(cases)} can be reviewed.")
    for w in warnings:
        print(f"  NOTE: {w}")
    print_excluded(excluded)
    scope = RS.from_text(args.scope, cases, excluded) if args.scope else RS.choose(cases, excluded)
    print(f"Run choice: {scope.label} - {len(scope.cases)} case(s).")
    for note in scope.notes:
        print(f"  NOTE: {note}")
    log = DL.DoneLog(cfg.runtime_root, "coordinator")
    watched = "12" + ("3" if reviewer == "mission_control" else "")
    before = log.done_before([c["iid"] for c in scope.cases],
                             [f"system{s}" for s in systems if s in watched])
    rerun, skip = set(), set()
    if before:
        if ask_again(before, systems, reviewer, names, args.again) == "again":
            rerun = set(before)
            print(f"  {len(rerun)} case(s) will be run again.")
        else:
            skip = set(before)
            print(f"  {len(skip)} case(s) skipped.")
    sent = set(sender.Ledger(ledger_path(cfg, ctx.rt), cfg.persist_retries).read()) if reviewer != "mission_control" else set()
    paths = P.Paths.new_run(cfg.runtime_root, cfg.cases_root).create()
    special = [cfg.result_contract_path, cfg.daily_instructions_path]     # never taken for a Sub Worktype's file
    plan = H.build_plan(scope.cases, excluded, paths, sent, cfg.guidance_dir, special, reviewer=reviewer,
                        systems=systems, rerun=rerun, skip=skip,
                        extra={"bam": report.name, "scope": scope.label, "out_of_scope": len(cases) - len(scope.cases),
                               "rerun": sorted(rerun), "mode": mode})
    for line in H.set_aside_for_rerun(paths, plan):
        print(line)
    P.write_json(paths.plan, plan)
    print_plan(plan, names)
    control = P.Control(paths)
    try:                                            # the pipeline report shows only this run's systems Edge lines
        edge_offset = (paths.logs / "edge-systems-stderr.log").stat().st_size
    except OSError:
        edge_offset = 0
    if not any(c["needs"] in ("download", "send") for c in plan["cases"]):
        print("\nNothing to do in this run.")
        return finish(paths, plan, control, 0, cfg=cfg, log=log)
    procs = {}
    if plan["parts"]["part1"] or plan["parts"]["part2"]:
        shown = " and ".join(names[f"system{s}"] for s in "12" if plan["parts"][f"part{s}"])
        used = "".join(s for s in "12" if plan["parts"][f"part{s}"])
        print(f"\nSystems Edge: {open_systems_edge(cfg, ctx.rt, used)} on port {cfg.systems_cdp_port} - sign in to "
              f"{shown} there" + (f" (keep ONE {names['system1']} tab)." if plan["parts"]["part1"] else "."))
        for part in ("part1", "part2"):
            if plan["parts"][part]:
                procs[part] = start_part(part, paths, ctx.rt)
    hand = H.Handover(paths, control, plan, cfg.guidance_dir, special,
                      alive=lambda part: part in procs and procs[part].poll() is None, done_log=log)
    quit_ = threading.Event()

    def coordinate():
        while not quit_.is_set():
            try:
                if hand.step():
                    return
            except Exception as exc:             # the hand-over never crashes the run silently
                control.event("coordinator", f"hand-over problem: {type(exc).__name__}: {exc}")
            quit_.wait(0.5)
    worker = threading.Thread(target=coordinate, daemon=True)
    worker.start()
    who = names["system3"] if "3" in systems else "the run's console"
    print(f"\nStarting {who} ...\n", flush=True)
    code = run_sender(paths, args.detail, third_script(reviewer, systems))
    if code != 0 or not control.feed_closed():
        control.stop("coordinator", f"{who} ended (code {code})")
    quit_.set()
    worker.join(5)
    t0 = time.monotonic()
    pn = part_names(cfg)
    while any(p.poll() is None for p in procs.values()) and time.monotonic() - t0 < STOP_WAIT_SECONDS:
        busy = [pn[k] for k, p in procs.items() if p.poll() is None]
        print(f"Waiting for {', '.join(busy)} to finish its current step ...", flush=True)
        for _ in range(20):
            if all(p.poll() is not None for p in procs.values()):
                break
            time.sleep(0.5)
    try:                                            # what was made after the hand-over stopped is logged too
        for c in hand.todo:
            hand._note_done(c, P.read_json(paths.downloads / f"{c['iid']}.json"))
    except Exception:                               # noqa: BLE001 - the log never decides the run
        pass
    if procs:                                       # the systems Edge was used: no pointer may stay in its tabs
        sweep_pointer(cfg, procs, control)
    return finish(paths, plan, control, code, edge_offset, cfg=cfg, log=log)


def finish(paths: P.Paths, plan: dict, control: P.Control, code: int, edge_offset=None, cfg=None, log=None) -> int:
    """The daily report: on screen (what needs you) and as a CSV beside the run; the already-done log in Excel; and the
    pipeline report (what System 1 and System 2 saw) in RUNTIME_ROOT\\logs, for the reviewing Copilot."""
    rows = H.daily_report(paths, plan, control)
    report = H.write_report(rows, paths.runtime_root / "reports", paths.run.name)
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print("\n" + "=" * 96 + "\nDAILY REPORT  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print("  " + P.workload_line(rows))
    if plan.get("out_of_scope"):
        print(f"  {plan['out_of_scope']} other case(s) of the BAM Report were not in this run's choice")
    excluded = [r for r in rows if r["outcome"] == P.EXCLUDED]
    if excluded:
        print(f"  AUTOMATICALLY EXCLUDED: {len(excluded)} row(s)")
        for r in excluded:
            print(f"    row {r['row']:>4}  {r['instruction_id'] or '(no RequestId)':<32}  {r['detail']}")
    for r in rows:
        if P.needs_you(r):
            print(f"  row {r['row']:>4}  {r['instruction_id']:<32}  {r['outcome']}  {r['detail']}")
    print(f"Full report (every row): {report}")
    book = (P.read_json(paths.sub("status") / "workbook.json", {}) or {}).get("path")
    if book:
        who = "Azure" if plan.get("reviewer") == "azure" else "Copilot"
        print(f"{who} review results (Excel, every valid answer of this run): {book}")
    if log is not None and cfg is not None:
        path, main = log.write_xlsx(cfg.cases_root, names_of(cfg))
        if path:
            print(f"Already-done log (Excel): {path}" + ("" if main else
                  f"  ('{DL.XLSX_NAME}' is open in Excel - this copy was written instead)"))
    detail = H.pipeline_report(paths, plan, rows, edge_offset)
    if detail:
        print(f"What System 1 / System 2 saw (for review): {detail}")
    print("=" * 96, flush=True)
    return code


def check(cfg) -> None:
    """Settings + Edge/driver checks of the sender, and Mission Control's when it is System 3."""
    subprocess.call([sys.executable, "-B", "-E", "-s", str(SENDER_SCRIPT), "check"])
    if reviewer_of(cfg) == "mission_control":
        subprocess.call([sys.executable, "-B", "-E", "-s", str(MISSION_SCRIPT), "check"])
    elif reviewer_of(cfg) == "azure":
        subprocess.call([sys.executable, "-B", "-E", "-s", str(AZURE_SCRIPT), "check"])


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Daily review: the menu, or one run (--mode).")
    ap.add_argument("command", nargs="?", choices=["menu", "run"], help="(the default: the menu)")
    ap.add_argument("--mode", choices=sorted(P.MODES), help="full, s1, s2, s12 or s3 (skips the menu)")
    ap.add_argument("--report", help="the BAM Report .xlsx (else asked)")
    ap.add_argument("--csv", dest="report", help=argparse.SUPPRESS)          # the older name of --report
    ap.add_argument("--scope", help="the run choice in one line, e.g. all, first:5, type:Change of Address")
    ap.add_argument("--again", choices=("yes", "no"), help="cases done before: run again (yes) or skip (no)")
    ap.add_argument("--detail", choices=("minimal", "full"), help="console detail for this run")
    args = ap.parse_args(argv)
    try:
        if not sender.config_path().exists():
            print("No configuration yet - starting setup first.\n")
            if sender.setup_command():
                return 2
        mode = args.mode
        while not mode:
            choice = menu(sender.load_config())
            if not choice:
                return 0
            if choice == "setup":
                sender.setup_command()
                continue
            if choice == "check":
                check(sender.load_config())
                continue
            mode = choice
        return run_mode(sender.load_config(), mode, args)
    except (P.CsvError, RS.ScopeError) as exc:
        print(f"BAM REPORT PROBLEM: {exc}." if isinstance(exc, P.CsvError) else f"RUN CHOICE PROBLEM: {exc}.")
        return 2
    except sender.StorageError as exc:
        print(f"STORAGE PROBLEM: {exc}. Nothing was written elsewhere.")
        return 3
    except sender.SenderError as exc:
        print(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        print("Aborted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
