#!/usr/bin/env python3
"""COORDINATOR - the daily review in one double-click (Run.cmd run).

  1. Asks for the daily CSV, lists the rows that are skipped (Process Status other than Ended) and plans the run.
  2. Opens the systems Edge (System 1 + System 2, port systems_cdp_port, its own remembered profile).
  3. Starts Part 1 (System 1 documents) and Part 2 (System 2 snapshots) as background programs - they wait for go.
  4. Starts Part 3 (the Copilot sender) in this console: moving-cursor question, Copilot Edge, model choice, then
     "sign in everywhere and press Enter" - every part checks its own sign-in, then everything starts together.
  5. Meanwhile hands each complete case over (snapshot copied in, folder moved from .work into CASES_ROOT), so Copilot
     works on case 1 while System 1 downloads case 2.
  6. At the end: the daily report - one line per CSV row (sent + verdict, held + why, skipped + why).

Nothing here touches a page: the browser work is in the three parts.  shared/pipeline.py describes the files they share.
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
import handover as H                               # noqa: E402
import studio_visual_cursor as VC                  # noqa: E402  only its clean-up script (stdlib module, no Playwright)

PART_SCRIPTS = {"part1": PROJECT / "1_system1_documents" / "system1_documents.py",
                "part2": PROJECT / "2_system2_snapshots" / "system2_snapshots.py"}
SENDER_SCRIPT = PROJECT / "3_copilot_sender" / "copilot_case_sender.py"
PART_NAMES = {"part1": "Part 1 (System 1 documents)", "part2": "Part 2 (System 2 snapshots)"}
EDGE_SECONDS = 30                                   # the systems Edge's debugging endpoint should answer by then
STOP_WAIT_SECONDS = 600                            # after the sender ends: how long Part 1 / Part 2 may take to finish


def pipeline_problems(cfg) -> list:
    out = []
    for key, what in (("system1_url", "System 1 inbox page address"), ("system1_brand_text", "System 1 brand text pattern"),
                      ("system2_url", "System 2 signed-in page address"),
                      ("system2_account_page", "System 2 account summary page address"),
                      ("system2_party_marker", "System 2 party profile link text"),
                      ("system2_party_links_page", "System 2 party links page address"),
                      ("guidance_dir", "guidance folder (one .md per Sub Worktype)")):
        if not getattr(cfg, key, ""):
            out.append(f"{what} is not set")
    for key in ("system1_url", "system2_url", "system2_account_page", "system2_party_links_page"):
        u = urlparse(getattr(cfg, key, "") or "")
        if getattr(cfg, key, "") and (u.scheme not in ("https", "http") or not u.netloc):
            out.append(f"{key} is not a web address")
    for key, slot in (("system2_account_page", "{account}"), ("system2_party_links_page", "{party}")):
        if getattr(cfg, key, "") and slot not in getattr(cfg, key):
            out.append(f"{key} must contain {slot} where the value goes")
    pages = getattr(cfg, "system2_detail_pages", None) or {}
    for name in P.DETAIL_SECTIONS:                  # every account-detail page is mandatory for a complete snapshot
        page = pages.get(name, "") if isinstance(pages, dict) else ""
        u = urlparse(page or "")
        if not page:
            out.append(f"System 2 account-detail page '{name}' is not set (run setup)")
        elif u.scheme not in ("https", "http") or not u.netloc:
            out.append(f"System 2 account-detail page '{name}' is not a web address")
        elif "{account}" not in page:
            out.append(f"System 2 account-detail page '{name}' must contain {{account}} where the account number goes")
    if cfg.completion_mode == "json_result" and not cfg.result_contract_path:
        out.append("the result contract file is not set")
    if int(cfg.systems_cdp_port) == int(cfg.cdp_port):
        out.append("systems_cdp_port must differ from cdp_port (System 1/2 and Copilot run in two separate Edges)")
    return out


def ask_csv(cfg) -> Path:
    while True:
        shown = f" [{cfg.last_csv_path}]" if cfg.last_csv_path else ""
        try:
            answer = sender.clean_path(input(f"Daily CSV (paste its path){shown}: "))
        except EOFError:
            raise sender.SenderError("the daily CSV path is needed (no console input)") from None
        path = Path(answer or cfg.last_csv_path)
        if path.is_file():
            return path
        print("  That file was not found - try again.")


def print_skipped(skipped: list) -> None:
    if not skipped:
        print("Skipped rows: none (every row has Process Status 'Ended').")
        return
    print(f"\nSKIPPED - {len(skipped)} row(s) will NOT be reviewed:")
    for s in skipped:
        print(f"  row {s['row_number']:>4}  {s['iid'] or '(no ID)':<32}  {s['reason']}")
    print()


def print_plan(plan: dict) -> None:
    counts = {}
    for c in plan["cases"]:
        counts[c["needs"]] = counts.get(c["needs"], 0) + 1
    print(f"PLAN  {counts.get('download', 0)} to download and send, {counts.get('send', 0)} complete already (send only), "
          f"{counts.get('done', 0)} already sent earlier, {counts.get('held', 0)} held")
    for c in plan["cases"]:
        if c["needs"] == "held":
            print(f"  HELD  {c['iid']}  {c['note']}")
    if plan["no_guidance"]:
        print("  WARNING: no instructions file in the guidance folder yet for Sub Worktype(s): "
              + ", ".join(f"'{s}'" for s in plan["no_guidance"])
              + "\n           those cases are held before anything is downloaded, unless System 1 shows a Sub Worktype"
                " that has one.")


# ---------------------------------------------------------------------------------------------- the systems Edge

def open_systems_edge(cfg, rt) -> str:
    """Join the systems Edge if it is already open on its port, else start it with its own remembered profile
    (RUNTIME_ROOT\\profile-systems, never the system drive).  Missing System 1 / System 2 tabs are opened once."""
    port = int(cfg.systems_cdp_port)
    payload = sender.cdp_http(port, "/json/version", 0.75)
    if payload:
        if not sender.endpoint_valid(payload, port):
            raise sender.SenderError(f"port {port} is in use by something that is not an Edge debugging endpoint")
        pages = [t.get("url", "") for t in sender.cdp_http(port, "/json/list", 1.5) or [] if t.get("type") == "page"]
        for url in (cfg.system1_url, cfg.system2_url):
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
    # Loopback only.  The three background switches keep System 1 at full speed while Copilot's window is in front
    # (Proposed until confirmed on the VDI).
    flags = [cfg.edge_executable, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={profile}", f"--disk-cache-dir={rt.root / 'cache-systems'}",
             f"--crash-dumps-dir={rt.crashes}", "--no-first-run", "--no-default-browser-check",
             "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
             "--disable-renderer-backgrounding", "--new-window", cfg.system1_url, cfg.system2_url]
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


def run_sender(paths: P.Paths, detail) -> int:
    """Part 3 in THIS console (its questions and the Enter).  Ctrl+C belongs to it: the first press stops it safely."""
    cmd = [sys.executable, "-B", "-E", "-s", str(SENDER_SCRIPT), "run", "--pipeline", str(paths.run)]
    proc = subprocess.Popen(cmd + (["--detail", detail] if detail else []))
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue                                # the sender handles it; this window just waits for it


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Daily review: System 1 documents + System 2 snapshots + Copilot, together.")
    ap.add_argument("command", nargs="?", choices=["run"], help="(the default)")
    ap.add_argument("--detail", choices=("minimal", "full"), help="console detail for this run")
    ap.add_argument("--csv", help="the daily CSV (else asked)")
    args = ap.parse_args(argv)
    try:
        if not sender.config_path().exists():
            print("No configuration yet - starting setup first.\n")
            if sender.setup_command():
                return 2
        cfg = sender.load_config()
        problems = pipeline_problems(cfg)
        if problems:
            print("The daily pipeline is not set up yet - run 'Run.cmd setup':\n  - " + "\n  - ".join(problems))
            return 2
        ctx = sender.context(cfg)                   # validates paths, storage rules (nothing on the system drive)
        sender.ensure_dependencies(cfg, ctx.rt, ctx.log)
        if cfg.completion_mode == "json_result":    # before anything starts: the older results workbook is renamed
            try:                                    # (open in Excel = wait for Enter, then try again)
                WB.make_room(Path(cfg.cases_root))   # the workbook lives beside the case folders
            except RuntimeError as exc:
                raise sender.SenderError(str(exc)) from None
        csv_path = Path(sender.clean_path(args.csv)) if args.csv else ask_csv(cfg)
        cases, skipped = P.read_case_list(csv_path)
        if str(csv_path) != cfg.last_csv_path:
            cfg.last_csv_path = str(csv_path)
            try:                                    # a convenience only: offered as the default next time
                sender.save_config(cfg)
            except (sender.StorageError, sender.PersistenceError):
                pass
        print(f"\nCSV: {len(cases) + len(skipped)} row(s) - {len(cases)} with Process Status 'Ended'.")
        print_skipped(skipped)
        if cases and not any(c["end_date"] for c in cases):
            print("NOTE: no End Date in this CSV - every case is sent with case timing 'Not provided' (flagged CHECK).\n")
        sent = set(sender.Ledger(ctx.rt.ledger, cfg.persist_retries).read())
        paths = P.Paths.new_run(cfg.runtime_root, cfg.cases_root).create()
        special = [cfg.result_contract_path, cfg.daily_instructions_path]     # never taken for a Sub Worktype's file
        plan = H.build_plan(cases, skipped, paths, sent, cfg.guidance_dir, special)
        P.write_json(paths.plan, plan)
        print_plan(plan)
        control = P.Control(paths)
        try:                                        # the pipeline report shows only this run's systems Edge lines
            edge_offset = (paths.logs / "edge-systems-stderr.log").stat().st_size
        except OSError:
            edge_offset = 0
        if not any(c["needs"] in ("download", "send") for c in plan["cases"]):
            print("\nNothing to do in this run.")
            return finish(paths, plan, control, 0)
        procs = {}
        if plan["parts"]["part1"] or plan["parts"]["part2"]:
            print(f"\nSystems Edge: {open_systems_edge(cfg, ctx.rt)} on port {cfg.systems_cdp_port} - sign in to "
                  "System 1 and System 2 there (keep ONE System 1 tab).")
            for part in ("part1", "part2"):
                if plan["parts"][part]:
                    procs[part] = start_part(part, paths, ctx.rt)
        else:
            control.close_feed()                    # nothing to download: only the complete cases are sent
        hand = H.Handover(paths, control, plan, cfg.guidance_dir, special,
                          alive=lambda part: part in procs and procs[part].poll() is None)
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
        print("\nStarting the Copilot sender (Part 3) ...\n", flush=True)
        code = run_sender(paths, args.detail)
        if code != 0 or not control.feed_closed():
            control.stop("coordinator", f"the Copilot sender ended (code {code})")
        quit_.set()
        worker.join(5)
        t0 = time.monotonic()
        while any(p.poll() is None for p in procs.values()) and time.monotonic() - t0 < STOP_WAIT_SECONDS:
            busy = [PART_NAMES[k] for k, p in procs.items() if p.poll() is None]
            print(f"Waiting for {', '.join(busy)} to finish its current step ...", flush=True)
            for _ in range(20):
                if all(p.poll() is not None for p in procs.values()):
                    break
                time.sleep(0.5)
        if procs:                                   # the systems Edge was used: no pointer may stay in its tabs
            sweep_pointer(cfg, procs, control)
        return finish(paths, plan, control, code, edge_offset)
    except P.CsvError as exc:
        print(f"CSV PROBLEM: {exc}.")
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


def finish(paths: P.Paths, plan: dict, control: P.Control, code: int, edge_offset=None) -> int:
    """The daily report: on screen (what needs you) and as a CSV beside the run; and the pipeline report (what System 1
    and System 2 saw) in RUNTIME_ROOT\\logs beside the sender's run report, for the reviewing Copilot."""
    rows = H.daily_report(paths, plan, control)
    report = H.write_report(rows, paths.runtime_root / "reports", paths.run.name)
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print("\n" + "=" * 96 + "\nDAILY REPORT  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print("  " + P.workload_line(rows))
    for r in rows:
        if P.needs_you(r):
            print(f"  row {r['row']:>4}  {r['instruction_id']:<32}  {r['outcome']}  {r['detail']}")
    print(f"Full report (every CSV row): {report}")
    book = (P.read_json(paths.sub("status") / "workbook.json", {}) or {}).get("path")
    if book:
        print(f"Copilot review results (Excel, every valid answer of this run): {book}")
    detail = H.pipeline_report(paths, plan, rows, edge_offset)
    if detail:
        print(f"What System 1 / System 2 saw (for review): {detail}")
    print("=" * 96, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
