"""Azure case reviewer - Part 3 of the daily review without Copilot: Azure Document Intelligence reads every page,
an Azure language model fills each recognised form and reviews the case.  Sits beside 3_copilot_sender (kept as the
fallback); the setting `reviewer: azure` makes the coordinator start this instead of the sender.

  run --pipeline <run dir>   started by orchestrator/run_pipeline.py (same hand-over, sign-in step and console)
  review --csv <studio.csv> --cases-root <dir> --guidance <dir> --out <dir> [--case-model X] [--limit N]
                             standalone (azure-test): one <instruction id>.json per case in --out
  check                      settings + a live call to Document Intelligence and to each model deployment

Settings come from sender_config.json (Run.cmd setup).  Never prints a key, a token or an endpoint.
Held cases go to the coordinator's held list, never to part3-results.json.  A safe stop (credentials, quota, wrong
endpoint/deployment) stops the whole run; cases already under review finish.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures
import datetime
import fnmatch
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

HERE = Path(os.path.abspath(__file__)).parent      # never .resolve(): it turns a mapped drive (S:) into its UNC form
PROJECT = HERE.parent
for p in (str(PROJECT / "3_copilot_sender"), str(PROJECT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import copilot_case_sender as S                     # noqa: E402  settings, storage rules, hand-over feed, ledger, log
from shared import pipeline as P                    # noqa: E402
from shared import timing as TM                     # noqa: E402
from shared import review_workbook as WB            # noqa: E402
import azure_client as AZ                           # noqa: E402
import reviewer as RV                               # noqa: E402

FORM_MAPS = Path(os.environ.get("AZURE_FORM_MAPS") or HERE / "form_maps.json")   # env: tests only
LEDGER_NAME = getattr(P, "AZURE_LEDGER", "azure_reviewed_instructions.txt")
RESULTS_DIR = getattr(P, "AZURE_RESULTS", "results-azure")
VALID = ("result_correct", "result_incorrect", "result_inconclusive")
EXTRA_PHASES = {"held", "safe-stop", "slow-down", "review", "azure"}


class AzureLedger(S.Ledger):
    HEADER = ("# Instruction IDs already reviewed by the Azure reviewer (valid results only). They are never reviewed\n"
              "# again. To review one again, delete its line and save. Format: id<TAB>state<TAB>time\n")


def ledger_path(cfg) -> Path:
    return Path(cfg.runtime_root) / LEDGER_NAME


def _text(path, fallback: Path) -> str:
    for p in (path, fallback):
        if p and Path(p).is_file():
            return Path(p).read_text("utf-8-sig", errors="replace")
    return ""


def add_deps(cfg) -> None:
    """`requests` on the VDI lives in the dependency folder (installed by setup), like for Part 2."""
    deps = Path(cfg.dependency_directory) if getattr(cfg, "dependency_directory", "") else \
        (Path(cfg.runtime_root) / "deps" if getattr(cfg, "runtime_root", "") else None)
    if deps and deps.is_dir():
        d = S.drive_letter_spelling(str(deps))
        if d not in sys.path:
            sys.path.insert(0, d)


def build(cfg, case_model: str | None = None, stop=None, on_wait=None) -> RV.Reviewer:
    auth = AZ.Auth(cfg)
    http = AZ.Http(auth, stop=stop, on_wait=on_wait)
    di = AZ.DocIntel(cfg.azure_docintel_endpoint, http, Path(cfg.runtime_root) / "cache" / "azure-di",
                     free=cfg.azure_docintel_tier == "free")
    chat = AZ.Chat(cfg.azure_openai_endpoint, http, getattr(cfg, "azure_openai_api_version", "") or "")
    holidays = S.holidays_of(cfg)
    ignore = [pat.lower() for pat in (cfg.ignore_names or [])]
    return RV.Reviewer(
        di=di, chat=chat, forms=RV.load_forms(FORM_MAPS), form_model=cfg.azure_form_model,
        case_model=case_model or cfg.azure_review_model, threshold=float(cfg.azure_low_confidence),
        daily=_text(cfg.daily_instructions_path, PROJECT / "daily_review_instructions.md"),
        contract=_text(cfg.result_contract_path, PROJECT / "result_contract.md"),
        base=_text(cfg.base_message_path, PROJECT / "daily_review_base_message.md"),
        validate=S.validate_result,
        timing=lambda end, retrieved, from_csv: TM.case_timing(end, retrieved, holidays, from_csv=from_csv),
        discrepancy_words=S.DISCREPANCY_WORDS, snapshot_suffix=P.SNAPSHOT_SUFFIX,
        free_tier=cfg.azure_docintel_tier == "free",
        ignore=lambda name: any(fnmatch.fnmatch(name.lower(), pat) for pat in ignore),
        stop=lambda: False, fixups=int(cfg.fixup_requests))


def public(rec: dict) -> dict:
    """The per-case result file: everything but the raw model turns and program traces."""
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def write_record(folder: Path, rec: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{rec['iid']}.json"
    S.write_atomic(path, json.dumps(public(rec), ensure_ascii=False, indent=1))
    return path


def stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def headline(rec: dict) -> str:
    r = rec.get("result") or {}
    return r.get("HEADLINE") or rec.get("reason") or ""


# ---------------------------------------------------------------------------------------------- report files

def responses_md(records: list, rv: RV.Reviewer, started: float, label=lambda rec: rec["iid"]) -> str:
    """azure-responses-<time>.md: every model answer of the run, per case, with what was read."""
    di, chat = rv.di, rv.chat
    out = [S.reviewer_note(), "", f"# Azure reviewer responses ({datetime.datetime.now():%Y-%m-%d %H:%M})", "",
           f"- Models: form filling `{rv.form_model}`, case review `{rv.case_model}`",
           f"- Document Intelligence pages analysed now: {di.pages_used} (from cache: {di.cached_pages}); "
           f"model calls: {chat.usage['calls']}, tokens in/out: {chat.usage['prompt_tokens']}/"
           f"{chat.usage['completion_tokens']}; run time {time.monotonic() - started:.0f} s",
           f"- Low-confidence mark: {rv.threshold:.2f}", ""]
    for rec in records:
        out += [f"## {label(rec)} - {rec['state']}", ""]
        if rec.get("reason"):
            out += [f"Reason / notes: {rec['reason']}", ""]
        for n in rec.get("notes") or []:
            out.append(f"- {n}")
        if rec.get("DOCUMENTS"):
            out += ["", "| doc | file | kind | form | pages (expected) | handwritten lines |", "|---|---|---|---|---|---|"]
            for d in rec["DOCUMENTS"]:
                out.append(f"| {d['DOC_ID']} | {d['FILE']} | {d['KIND']} | {d.get('FORM', '')} | {d['PAGES']}"
                           f"{' (' + str(d['PAGES_EXPECTED']) + ')' if d.get('PAGES_EXPECTED') else ''} | "
                           f"{d.get('HANDWRITTEN_LINES', 0)} |")
        low = [f for f in rec.get("EXTRACTED_FIELDS") or [] if f["LOW_CONFIDENCE"]]
        if rec.get("EXTRACTED_FIELDS"):
            out += ["", f"Extracted fields: {len(rec['EXTRACTED_FIELDS'])}, low confidence: {len(low)}"]
            out += [f"- {f['FIELD_ID']} = '{f['VALUE']}' ({f['CONFIDENCE']:.2f}): {f['REASON']}" for f in low]
        for t in rec.get("_turns") or []:
            out += ["", f"### {t['kind']} {t.get('doc', '')} ({t.get('model', '')})"]
            if t.get("problems"):
                out += ["Problems found by the program: " + "; ".join(t["problems"])]
            out += ["```json", t.get("reply") or "(no answer)", "```"]
        if rec.get("_trace"):
            out += ["", "### program error", "```", rec["_trace"], "```"]
        out.append("")
    return "\n".join(out)


def run_report_md(cfg, records: list, rv: RV.Reviewer, started: float, code: int, safe: str, argv: list,
                  red=lambda s: s, label=lambda rec: rec["iid"]) -> str:
    settings = S.public_settings(cfg)
    for k in ("azure_docintel_endpoint", "azure_openai_endpoint"):
        settings[k] = "set" if settings.get(k) else "not set"
    settings = {k: v for k, v in settings.items() if k.startswith("az") or k in ("reviewer", "fixup_requests")}
    counts = collections.Counter(r["state"] for r in records)
    out = [S.reviewer_note(), "", f"# Azure reviewer run report ({datetime.datetime.now():%Y-%m-%d %H:%M})", "",
           f"- Command: {red(' '.join(argv))}", f"- Exit code: {code}" + (f" - SAFE STOP: {red(safe)}" if safe else ""),
           f"- Run time: {time.monotonic() - started:.0f} s",
           f"- Counts: {', '.join(f'{k}={v}' for k, v in sorted(counts.items())) or 'no cases'}",
           f"- Document Intelligence pages analysed now: {rv.di.pages_used}, from cache: {rv.di.cached_pages}",
           f"- Model calls: {rv.chat.usage['calls']}, tokens in/out: {rv.chat.usage['prompt_tokens']}/"
           f"{rv.chat.usage['completion_tokens']}", "", "## Settings (keys and endpoints never shown)", "",
           "```json", red(json.dumps(settings, indent=1)), "```", "", "## Cases", "",
           "| case | state | result | documents | fields (low) | reason |", "|---|---|---|---|---|---|"]
    for r in records:
        f = r.get("EXTRACTED_FIELDS") or []
        out.append(f"| {label(r)} | {r['state']} | {(r.get('result') or {}).get('REVIEW_RESULT', '')} | "
                   f"{len(r.get('DOCUMENTS') or [])} | {len(f)} ({sum(1 for x in f if x['LOW_CONFIDENCE'])}) | "
                   f"{red(r.get('reason', '')).replace('|', '/')} |")
    traces = [(label(r), r["_trace"]) for r in records if r.get("_trace")]
    if traces:
        out += ["", "## Program errors", ""]
        for who, tr in traces:
            out += [f"### {who}", "```", red(tr), "```"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------- check

def check_command(args) -> int:
    """Proves the exact production calls: Document Intelligence analyze (same model + api-version) on a built-in
    1-page PDF (costs 1 page) and each deployment with the same strict JSON schema answer format."""
    cfg = S.load_config()
    ok = True

    def line(name, good, note=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'ok' if good else 'FAIL'}] {name}{(' - ' + note) if note else ''}", flush=True)

    print("AZURE REVIEWER CHECK (keys and endpoints are never shown)")
    print(f"  [info] reviewer setting: {cfg.reviewer}" + ("" if cfg.reviewer == "azure" else
          " - Run.cmd run uses Copilot; set reviewer to azure in setup to use this reviewer"))
    problems = S.azure_problems(cfg)
    line("Azure settings complete", not problems, "; ".join(problems))
    forms = RV.load_forms(FORM_MAPS)
    line("form maps (3_azure_reviewer/form_maps.json)", bool(forms), f"{len(forms)} form(s)" if forms else
         "missing or unreadable - every document would be read as a supporting document")
    if problems:
        return 1
    add_deps(cfg)
    try:
        rv = build(cfg)
    except ImportError:
        line("Python package 'requests'", False, "not installed - run Run.cmd setup")
        return 1
    http = rv.di.http
    mode = "key (keys typed in setup)" if http.auth.mode == "key" else "sign-in (az login, no key on disk)"
    try:
        http.auth.headers("di")
        line("Azure authentication", True, mode)
    except AZ.AzureError as exc:
        line("Azure authentication", False, f"{mode}: {exc}")
        return 1
    try:
        rv.di.info()
        line("Document Intelligence reachable", True, f"tier {cfg.azure_docintel_tier}")
        try:
            text = rv.di.check_analyze()
            line("Document Intelligence analyze (prebuilt-layout, 1 test page)", "AZURE REVIEWER CHECK" in text,
                 f"api-version {AZ.DI_API}; read: '{text[:40]}'")
        except AZ.AzureError as exc:
            line("Document Intelligence analyze (prebuilt-layout, 1 test page)", False, str(exc))
    except AZ.AzureError as exc:
        line("Document Intelligence reachable", False, str(exc))
    for role, name in (("form model", cfg.azure_form_model), ("case model", cfg.azure_review_model)):
        try:
            reported = rv.chat.ping(name)
            line(f"{role} '{name}' answers in the strict JSON schema", True, f"model {reported}")
        except AZ.AzureError as exc:
            line(f"{role} '{name}' answers in the strict JSON schema", False, str(exc))
    print(f"  [info] language model address style: {rv.chat.style_used}")
    print(f"  [info] secure connection (TLS): " + ("Windows certificate store (company proxy certificates)"
                                                  if http.tls == "windows" else "standard"))
    print("  All checks passed." if ok else "  Fix the FAIL lines above (Run.cmd setup), then check again.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------------------------- review (standalone)

def find_folder(cases_root: Path, iid: str, account: str):
    exact = Path(cases_root) / P.folder_name(iid, account)
    if exact.is_dir():
        return exact
    try:
        for p in sorted(Path(cases_root).iterdir(), key=lambda p: p.name.casefold()):
            if p.is_dir() and p.name.lower().startswith(iid.lower()):
                return p
    except OSError:
        pass
    return None


def review_command(args) -> int:
    started = time.monotonic()
    cfg = S.load_config()
    problems = S.azure_problems(cfg) + ([] if cfg.runtime_root else ["runtime_root is not set"])
    if problems:
        print("The Azure settings are not complete - run 'Run.cmd setup':\n  - " + "\n  - ".join(problems))
        return 2
    add_deps(cfg)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        rows, skipped = P.read_case_list(args.csv)
    except P.CsvError as exc:
        print(f"ERROR: {exc}")
        return 2
    if args.limit:
        rows = rows[:args.limit]
    guidance = Path(args.guidance or cfg.guidance_dir or PROJECT / "guidance")
    halt = threading.Event()
    rv = build(cfg, args.case_model,
               on_wait=lambda what, s: print(f"  slow-down: {what} - Azure asked to wait {s:.0f} s", flush=True))
    excl = [p for p in (cfg.daily_instructions_path, cfg.result_contract_path) if p]
    inputs, records = [], []
    for row in rows:
        folder = find_folder(Path(args.cases_root), row["iid"], row["account"])
        ci = RV.CaseInput(iid=row["iid"], folder=folder or Path(args.cases_root), account=row["account"],
                          client_number=row.get("client_number", ""), sub_worktype=row.get("sub_worktype_csv", ""),
                          case_source=row.get("case_source", ""), end_date=row.get("end_date", ""), from_csv=True,
                          rules=P.guidance_for(row.get("sub_worktype_csv", ""), guidance, exclude=excl))
        if folder is None:
            rec = {"iid": row["iid"], "account": row["account"], "case_source": ci.case_source, "state": "held",
                   "reason": "case folder not found in the cases root", "reviewer": "azure", "result": None,
                   "DOCUMENTS": [], "EXTRACTED_FIELDS": [], "notes": []}
            records.append(rec)
            write_record(out, rec)
            continue
        inputs.append(ci)
    print(f"Reviewing {len(inputs)} case(s) with Azure ({cfg.azure_parallel_cases} at a time; case review model "
          f"'{rv.case_model}'); {len(skipped)} CSV row(s) skipped.", flush=True)
    safe = ""
    lock = threading.Lock()

    def one(ci):
        nonlocal safe
        if halt.is_set():
            return {"iid": ci.iid, "account": ci.account, "case_source": ci.case_source, "state": "not_sent_stopped",
                    "reason": "run stopped (safe stop) before this case was reviewed: " + safe, "reviewer": "azure",
                    "result": None, "DOCUMENTS": [], "EXTRACTED_FIELDS": [], "notes": []}
        try:
            return rv.review(ci)
        except AZ.SafeStop as exc:
            with lock:
                safe = safe or str(exc)
            halt.set()
            return exc.record

    try:
        with concurrent.futures.ThreadPoolExecutor(max(1, int(cfg.azure_parallel_cases))) as pool:
            for rec in pool.map(one, inputs):
                records.append(rec)
                write_record(out, rec)
                print(f"  {rec['iid']}  {rec['state']:<20} {headline(rec)[:110]}", flush=True)
    finally:
        code = 3 if safe else 0
        t = stamp()
        S.write_atomic(out / f"azure-responses-{t}.md", responses_md(records, rv, started))
        S.write_atomic(out / f"azure-run-report-{t}.md",
                       run_report_md(cfg, records, rv, started, code, safe, sys.argv[1:]))
    counts = collections.Counter(r["state"] for r in records)
    print("SUMMARY  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Document Intelligence pages analysed now: {rv.di.pages_used} (from cache: {rv.di.cached_pages})")
    if safe:
        print(f"SAFE STOP: {safe}")
    print(f"Results: {out}")
    return code


# ---------------------------------------------------------------------------------------------- run --pipeline

class AzureFeed(S.PipelineFeed):
    """The sender's hand-over feed, unchanged except the sign-in banner (no Copilot window with this reviewer)."""

    async def confirm_ready(self, stdin, log, stop) -> None:
        if not self.parts:
            self.control.go()
            return
        names = {"part1": "System 1", "part2": "System 2"}
        missing = self.plan.get("no_guidance") or []
        S.say("\n" + "=" * 96 + "\nSIGN IN EVERYWHERE, THEN PRESS ENTER HERE\n"
              "  - Systems Edge window: " + " and ".join(names[p] for p in self.parts)
              + (" (keep exactly ONE System 1 tab, showing My Inbox)" if "part1" in self.parts else "") + "\n"
              "  - Azure reviewer: reachable (already checked - no Copilot window is used)\n"
              + ("  - NOTE: no guidance file for Sub Worktype(s) " + ", ".join(f"'{s}'" for s in missing)
                 + " - those cases are held before anything is downloaded\n" if missing else "") + "=" * 96)
        while True:
            line = await stdin.line()
            if line is None:
                await stop.sleep(10)
            stop.check()
            seq = await asyncio.to_thread(self.control.request_check)
            S.say("Checking " + " and ".join(names[p] for p in self.parts) + " ...")
            answers, t0 = {}, time.monotonic()
            while len(answers) < len(self.parts) and time.monotonic() - t0 < S.CHECK_SECONDS:
                for part in self.parts:
                    if part not in answers:
                        a = await asyncio.to_thread(self.control.check_answer, part, seq)
                        if a is None and await asyncio.to_thread(self.control.is_finished, part):
                            a = {"ok": False, "problem": f"Part {part[-1]} has stopped (see its log in the run folder)"}
                        if a:
                            answers[part] = a
                await stop.sleep(0.5)
            problems = [f"{names[p]}: " + (answers[p]["problem"] if p in answers else "no answer yet (still starting?)")
                        for p in self.parts if not answers.get(p, {}).get("ok")]
            if not problems:
                await asyncio.to_thread(self.control.go)
                log("pipeline", note="everything is signed in - documents, snapshots and Azure reviews start now")
                return
            S.say("Not ready yet:\n  - " + "\n  - ".join(problems) + "\nFix this in Edge, then press Enter again.")


def case_input(feed, case) -> RV.CaseInput:
    row = feed.rows.get(case.id) or {}
    m = P.read_json(feed.paths.cases / f"{case.id}.json") or {}
    return RV.CaseInput(iid=case.id, folder=case.path, account=row.get("account", ""),
                        client_number=row.get("client_number", ""),
                        sub_worktype=feed.sub_of.get(case.key) or m.get("sub_worktype") or row.get("sub_worktype_csv", ""),
                        case_source=(feed.case_source(case) if hasattr(feed, "case_source") else row.get("case_source", "")),
                        rules=Path(m["guidance"]) if m.get("guidance") else None,
                        end_date=row.get("end_date", ""), from_csv=True, order=list(m.get("order") or []),
                        alias=case.alias)


def preflight(rv: RV.Reviewer, cfg) -> str:
    """'' = Azure answers (sign-in, Document Intelligence, each deployment); else the reason to stop safely."""
    try:
        rv.di.info()
        for name in dict.fromkeys([cfg.azure_form_model, rv.case_model]):
            rv.chat.ping(name)
    except AZ.AzureError as exc:
        return str(exc)
    return ""


async def pipeline_async(ctx, run_dir: Path) -> int:
    cfg, log, started = ctx.cfg, ctx.log, time.monotonic()
    stop = S.Stopper()
    feed = AzureFeed(run_dir, ctx, S.local_salt(ctx.rt))
    ledger = AzureLedger(ledger_path(cfg), cfg.persist_retries)
    listed = ledger.read()
    out = Path(cfg.runtime_root) / RESULTS_DIR
    rv = build(cfg, on_wait=lambda what, s: log("slow-down", note=f"{what}: Azure asked to wait {s:.0f} s"))
    try:
        workbook = WB.ReviewWorkbook(Path(cfg.cases_root), reviewer="azure")
    except TypeError:                                 # an older shared/review_workbook.py
        workbook = WB.ReviewWorkbook(Path(cfg.cases_root))
    results, records, by_iid = {}, [], {}
    safe, code = "", 0
    terminal = S.StdinLines()
    relay = asyncio.ensure_future(feed.relay(log, terminal))
    pool = concurrent.futures.ThreadPoolExecutor(max(1, int(cfg.azure_parallel_cases)))
    loop = asyncio.get_running_loop()

    def finish(case, rec):
        rec["alias"] = case.alias
        records.append(rec)
        by_iid[rec["iid"]] = case
        write_record(out, rec)
        st = rec["state"]
        if st == "held":
            feed.control.hold(case.id, rec["reason"], "part3")
            log("held", case.alias, note=rec["reason"])
            return
        results[case.key] = (st, rec.get("reason", ""))
        if st in VALID:
            ledger.add(case.id, st)
            listed[case.id] = st
            log(st, case.alias, note=headline(rec))
            info = {**feed.info(case), "case_source": rec.get("case_source", "")}
            row = {**rec["result"], "DOCUMENTS": rec["DOCUMENTS"], "EXTRACTED_FIELDS": rec["EXTRACTED_FIELDS"]}
            if not workbook.add(info, row):
                log("WARNING", case.alias, note=f"'{workbook.path.name}' is open in Excel - it is written again after "
                                                 "the next answer (or a time-stamped copy at the end)")
        else:
            log("result_invalid" if st == "result_invalid" else "WARNING", case.alias, note=f"{st}: {rec['reason']}")

    try:
        problem = await asyncio.to_thread(preflight, rv, cfg)
        if problem:
            safe, code = problem, 3
            feed.control.stop("part3", "Azure reviewer: " + problem)
            log("safe-stop", note=f"Azure is not usable: {problem} - nothing was sent; Parts 1 and 2 are told to stop")
        else:
            log("azure", note=f"Azure answers (form filling '{rv.form_model}', case review '{rv.case_model}', "
                              f"{cfg.azure_parallel_cases} case(s) at a time)")
            await feed.confirm_ready(terminal, log, stop)
            queue, running = [], {}
            n = max(1, int(cfg.azure_parallel_cases))
            while True:
                for case in await asyncio.to_thread(feed.poll):
                    if case.id in listed:
                        log("queue", case.alias, note="already reviewed earlier (Azure ledger) - not reviewed again")
                    elif case.blocked:
                        feed.control.hold(case.id, case.blocked, "part3")
                        log("held", case.alias, note=case.blocked)
                    else:
                        queue.append(case)
                stopping = bool(stop.is_set or feed.stopping() or safe)
                while queue and not stopping and len(running) < n:
                    case = queue.pop(0)
                    log("review", case.alias, note="reading the documents with Azure")
                    running[loop.run_in_executor(pool, rv.review, case_input(feed, case))] = case
                if stopping:
                    reason = (safe and "run stopped (safe stop) before this case was reviewed: " + safe) or \
                             (stop.is_set and "run stopped by you before this case was reviewed") or feed.stop_reason()
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
                done, _ = await asyncio.wait(list(running), timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    case = running.pop(fut)
                    try:
                        rec = fut.result()
                    except AZ.SafeStop as exc:
                        rec = exc.record
                        if not safe:
                            safe, code = str(exc), 3
                            feed.control.stop("part3", "Azure reviewer: " + safe)
                            log("safe-stop", case.alias, note=f"{safe} - no new reviews; Parts 1 and 2 are told to stop")
                    finish(case, rec)
    except S.Stopped:
        log("stopped", note="stopped by you: no new reviews were started")
    finally:
        relay.cancel()
        pool.shutdown(wait=True)
        try:
            feed.write_results(results, types.SimpleNamespace(cases={}))
        except Exception as exc:                          # noqa: BLE001 - the daily report still lists every row
            log("WARNING", note=f"part3-results could not be written: {S.safe_exc(ctx.red, exc)}")
        try:
            path = workbook.close()
            if path:
                P.write_json(feed.paths.sub("status") / "workbook.json", {"path": str(path)})
                S.say(f"Azure review results (Excel): {path}")
        except Exception as exc:                          # noqa: BLE001
            log("WARNING", note=f"the results workbook could not be written: {S.safe_exc(ctx.red, exc)}")
        t = stamp()
        label = (lambda rec: rec.get("alias") or rec["iid"])
        try:
            S.write_atomic(ctx.rt.logs / f"azure-responses-{t}.md", responses_md(records, rv, started, label))
            report = ctx.rt.logs / f"azure-run-report-{t}.md"
            S.write_atomic(report, run_report_md(cfg, records, rv, started, code, safe, sys.argv[1:], ctx.red, label))
            S.say(f"Azure run report: {report}")
        except Exception as exc:                          # noqa: BLE001
            log("WARNING", note=f"the Azure reports could not be written: {S.safe_exc(ctx.red, exc)}")
        counts = collections.Counter(r["state"] for r in records)
        S.say("\nSUMMARY  " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no case reviewed")
              + f"  |  Document Intelligence pages: {rv.di.pages_used} new, {rv.di.cached_pages} from cache")
        try:
            rows = P.daily_report(feed.paths, feed.plan, feed.control)
            S.say("WHOLE WORKLOAD  " + P.workload_line(rows))
        except Exception:                                 # noqa: BLE001 - the coordinator's daily report lists every row
            pass
    return code


def run_command(args) -> int:
    cfg = S.load_config()
    if getattr(args, "detail", None):
        cfg.console_detail = args.detail
    ctx = S.context(cfg)                                # validates; storage rules + write guard, like the sender
    S.MINIMAL_PHASES.update(EXTRA_PHASES)
    S.CONSOLE["log"] = ctx.log
    return asyncio.run(pipeline_async(ctx, Path(args.pipeline)))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Azure case reviewer (Document Intelligence + a language model).")
    sub = ap.add_subparsers(dest="command")
    r = sub.add_parser("run", help="the daily pipeline's Part 3 (started by the coordinator)")
    r.add_argument("--pipeline", metavar="RUN_DIR", required=True)
    r.add_argument("--detail", choices=("minimal", "full"))
    v = sub.add_parser("review", help="review the case folders of a Studio CSV (azure-test)")
    v.add_argument("--csv", required=True)
    v.add_argument("--cases-root", required=True)
    v.add_argument("--guidance")
    v.add_argument("--out", required=True)
    v.add_argument("--case-model", help="case-review deployment for this run only")
    v.add_argument("--limit", type=int, default=0)
    sub.add_parser("check", help="settings + a live call to each Azure service")
    args = ap.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "review":
            return review_command(args)
        if args.command == "check":
            return check_command(args)
        ap.print_help()
        return 2
    except S.StorageError as exc:
        print(f"STORAGE PROBLEM: {exc}. Nothing was written elsewhere.")
        return 3
    except S.PersistenceError as exc:
        print(f"STATE COULD NOT BE SAVED: {exc}.")
        return 3
    except S.SenderError as exc:
        print(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        print("Aborted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
