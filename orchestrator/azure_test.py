#!/usr/bin/env python3
"""AZURE TEST - Run.cmd azure-test: the Azure reviewer on made-up cases, scored against the pass bar (laptop only).

  1. tests/azure_synthetic/generate.py (its own laptop-only Python, tests/azure_synthetic/.synthetic-venv) makes the
     made-up Studio report, case folders, Sub Worktype rules files and the expected answers.  Same seed = the very same
     files, so Document Intelligence's results are reused from the cache and a re-run costs no Azure pages.
  2. 3_azure_reviewer/azure_case_reviewer.py review - the real reviewer, on your Azure (the settings of 'Run.cmd setup').
  3. tests/azure_synthetic/score.py - the accuracy report against the pass bar: no trap case Correct, at least 95% of
     the handwritten fields read right, every field read below 0.80 confidence flagged.

Everything is written to RUNTIME_ROOT\\azure-test\\<time>\\ (never into this folder).  Made-up data only: never run it on
real cases.  Exit code: 0 = pass bar met, 1 = missed, 2 = could not run.
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(os.path.abspath(__file__)).parent      # never .resolve(): it turns a mapped drive (S:) into its UNC form
PROJECT = HERE.parent
for _p in (PROJECT, PROJECT / "3_copilot_sender"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import copilot_case_sender as sender               # noqa: E402  settings (load_config, azure_problems)

SYNTHETIC = PROJECT / "tests" / "azure_synthetic"
SYNTHETIC_PY = SYNTHETIC / ".synthetic-venv" / "Scripts" / "python.exe"
GENERATE, SCORE = SYNTHETIC / "generate.py", SYNTHETIC / "score.py"
ENGINE = PROJECT / "3_azure_reviewer" / "azure_case_reviewer.py"
CREATE_VENV = ("Create it once on the laptop (made-up data only, never on the VDI):\n"
               "  python -m venv tests\\azure_synthetic\\.synthetic-venv\n"
               "  tests\\azure_synthetic\\.synthetic-venv\\Scripts\\python.exe -m pip install -r "
               "tests\\azure_synthetic\\requirements-synthetic.txt\n"
               "  tests\\azure_synthetic\\.synthetic-venv\\Scripts\\python.exe tests\\azure_synthetic\\download.py")


def problems(cfg) -> list:
    """What stops the test before anything runs (plain words, never an endpoint or a key)."""
    out = []
    if not str(getattr(cfg, "runtime_root", "") or "").strip():
        out.append("RUNTIME_ROOT is not set - run 'Run.cmd setup'")
    azure = sender.azure_problems(cfg)
    if azure:
        out.append("the Azure settings are not complete - run 'Run.cmd setup' and choose the azure reviewer:\n      - "
                   + "\n      - ".join(azure))
    for path, what in ((ENGINE, "the Azure reviewer (3_azure_reviewer\\azure_case_reviewer.py)"),
                       (GENERATE, "the test data generator (tests\\azure_synthetic\\generate.py)"),
                       (SCORE, "the accuracy scorer (tests\\azure_synthetic\\score.py)")):
        if not path.is_file():
            out.append(f"{what} is missing from this folder")
    if not SYNTHETIC_PY.is_file():
        out.append("the laptop-only test Python (tests\\azure_synthetic\\.synthetic-venv) is not there.\n    " + CREATE_VENV)
    return out


def steps(out: Path, args) -> list:
    """The three commands, in order: (title, command)."""
    gen = [str(SYNTHETIC_PY), "-B", "-E", "-s", str(GENERATE), "--out", str(out)]
    if args.seed is not None:
        gen += ["--seed", str(args.seed)]
    review = [sys.executable, "-B", "-E", "-s", str(ENGINE), "review", "--csv", str(out / "studio.csv"),
              "--cases-root", str(out / "cases"), "--guidance", str(out / "guidance"), "--out", str(out / "results")]
    if args.case_model:
        review += ["--case-model", args.case_model]
    if args.limit:
        review += ["--limit", str(args.limit)]
    score = [sys.executable, "-B", "-E", "-s", str(SCORE), "--expected", str(out / "expected.json"),
             "--results", str(out / "results"), "--out", str(out / "accuracy-report.md")]
    return [("1/3 making the made-up cases", gen), ("2/3 Azure reviews them", review),
            ("3/3 scoring against the pass bar", score)]


def main(argv=None, run=subprocess.run) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="The Azure reviewer on made-up cases, scored against the pass bar.")
    ap.add_argument("command", nargs="?", choices=["azure-test"], help="(Run.cmd passes it)")
    ap.add_argument("--case-model", help="deployment name for the case review of this test only (e.g. a second model)")
    ap.add_argument("--limit", type=int, help="review only the first N made-up cases")
    ap.add_argument("--seed", type=int, help="made-up data seed (default: the generator's own; same seed = same files)")
    args = ap.parse_args(argv)
    try:
        cfg = sender.load_config()
    except sender.SenderError as exc:
        print(f"ERROR: {exc}")
        return 2
    found = problems(cfg)
    if found:
        print("AZURE TEST cannot start:\n  - " + "\n  - ".join(found))
        return 2
    out = Path(cfg.runtime_root) / "azure-test" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"AZURE TEST - made-up cases only.  Everything goes to: {out}\n"
          "Document Intelligence's free tier reads 500 pages a month; an unchanged re-run reuses its cache.\n", flush=True)
    code = 0
    for title, cmd in steps(out, args):
        print(f"--- {title} ...", flush=True)
        try:                                        # cwd = the fresh test folder: no stray module is picked up
            code = run(cmd, cwd=str(out), stdin=subprocess.DEVNULL).returncode
        except OSError as exc:
            print(f"    could not start: {type(exc).__name__}")
            return 2
        if title.startswith("1/3") and code != 0:
            print(f"    the generator ended with code {code} - nothing was sent to Azure.")
            return 2
        if title.startswith("2/3") and code != 0:   # a safe stop still leaves the answers it got: score those
            print(f"    the reviewer ended with code {code} - scoring the answers it wrote.")
    report = out / "accuracy-report.md"
    print(f"\nAccuracy report: {report}" if report.is_file() else "\nNo accuracy report was written.")
    print("PASS BAR MET" if code == 0 else "PASS BAR MISSED - Azure is not ready for the VDI (see the report)"
          if code == 1 else f"The scorer ended with code {code}.")
    return code if code in (0, 1) else 2


if __name__ == "__main__":
    sys.exit(main())
