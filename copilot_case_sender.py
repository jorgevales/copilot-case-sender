"""Copilot Case Sender - send instruction-ID case folders to Microsoft 365 Copilot, one fresh chat per case.

One file, read top to bottom.  Sections:
  1. Basics          errors, helpers, stop handling, atomic writes, file lock
  2. Path policy     S: <-> UNC identity, containment, junction checks
  3. Config          defaults, validation, interactive setup
  4. Storage         runtime folders, process-local TEMP, write guard, redaction, logging
  5. State           append-only journal, sent-instructions ledger
  6. Cases           discovery, document inventory, attachment plan
  7. Dependencies    first-launch install of Playwright into a private folder
  8. Browser         Edge launch + CDP connect (Playwright connect_over_cdp), Copilot page adapter
  9. Transaction     upload -> verify -> send -> confirm / reconcile, queue and retries
 10. Commands        setup | check | plan | run

Nothing here sends anything unless `run` is used.  Every path printed or logged is replaced by a
symbolic label (CASES_ROOT, RUNTIME_ROOT ...) and every case by a short alias.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import ctypes
import datetime
import fnmatch
import functools
import hashlib
import json
import ntpath
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import types
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------------------------
# 1. BASICS
# --------------------------------------------------------------------------------------------

EXCLUDED_DIR = ".edge-debug-9223"  # browser profile that may sit beside the cases; never case content
CASE_RE = re.compile(r"^(?P<id>[0-9a-fA-F]{32})(?:\s+-\s+(?P<ref>.*))?$")
REQUIREMENTS = ["playwright==1.64.1"]  # pinned; pip resolves its own small dependencies
PIP_WHEEL_JSON = "https://pypi.org/pypi/pip/json"


class SenderError(Exception):
    """Base class; messages must already be safe to show (no paths, no identifiers)."""


class PathError(SenderError): ...
class StorageError(SenderError): ...          # S: / share unavailable: stop, never fall back
class PersistenceError(SenderError): ...      # cannot record state: stop before any further send
class RecoveryRequired(SenderError): ...      # reconnect budget exhausted: durable paused outcome
class ConnectionLost(SenderError): ...        # CDP/target gone (page-level)
class UiError(SenderError): ...               # Copilot page did not behave as expected
class Blocked(SenderError): ...               # deterministic, never retried (config/policy/input)
class AttemptFailed(SenderError): ...         # a retriable, provably-unsent failure
class Stopped(SenderError): ...               # user asked to stop (only raised at safe points)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))  # the Copilot editor appends some


def norm_ws(text) -> str:
    """Whitespace-collapsed text without invisible zero-width characters (for exact-content comparison)."""
    return " ".join(str(text or "").translate(ZERO_WIDTH).split())


def clean_path(text) -> str:
    """Accept pasted paths, including 'Copy as path' output with surrounding quotes."""
    text = str(text or "").strip().strip("\"'\u201c\u201d\u2018\u2019").strip()
    return os.path.expandvars(text)


def io_retry(action, what: str, tries: int = 5):
    """Bounded retries for locked/slow files on network storage; raises PersistenceError when exhausted."""
    for i in range(tries):
        try:
            return action()
        except OSError as exc:
            if i == tries - 1:
                raise PersistenceError(f"{what} failed after {tries} tries ({type(exc).__name__})") from exc
            time.sleep(0.15 * (2 ** i))


def write_atomic(path: Path, data, tries: int = 5) -> None:
    """Replace `path` via a temp file *beside it* (same share, never the system Temp), flushed to disk."""
    raw = data.encode("utf-8") if isinstance(data, str) else data

    def action():
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    io_retry(action, "file replace", tries)


class FileLock:
    """Exclusive byte-range lock on a small file; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                if fh.read(1) == b"":
                    fh.write(b"L")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise SenderError("another instance already controls this runtime (lock is held)") from None
        self.fh = fh

    def release(self) -> None:
        if self.fh:
            try:
                self.fh.close()  # closing drops the OS lock
            finally:
                self.fh = None


class Stopper:
    """Cooperative Ctrl+C: first press = stop at the next safe point, second press = hard abort."""

    def __init__(self):
        self.event = asyncio.Event()
        self.presses = 0
        self.loop = asyncio.get_running_loop()
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, *_):
        self.presses += 1
        if self.presses > 1:
            raise KeyboardInterrupt
        self.loop.call_soon_threadsafe(self.event.set)
        print("Stop requested: finishing the current safe step (Ctrl+C again aborts immediately).", flush=True)

    @property
    def is_set(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        if self.event.is_set():
            raise Stopped("stop requested")

    async def sleep(self, seconds: float) -> None:
        """Interruptible sleep; raises Stopped when stop was requested."""
        if seconds > 0:
            try:
                await asyncio.wait_for(self.event.wait(), seconds)
            except asyncio.TimeoutError:
                pass
        self.check()


async def plain_sleep(seconds: float) -> None:
    """Sleep that ignores stop: used after send_intent, where we must finish confirming."""
    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------------------------
# 2. PATH POLICY  (S: and its UNC backing share are ONE approved storage)
# --------------------------------------------------------------------------------------------

def wnet_universal_name(path: str):
    """Ask Windows which UNC share backs a mapped drive (WNetGetUniversalNameW).

    Returns e.g. r'\\\\server\\share' (the drive root only) or None when the path is not a network
    mapping.  The buffer is resized on ERROR_MORE_DATA; every other error is raised, not swallowed.
    """
    if os.name != "nt":
        return None
    from ctypes import wintypes
    fn = ctypes.WinDLL("mpr", use_last_error=True).WNetGetUniversalNameW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.DWORD
    size = wintypes.DWORD(1024)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        rc = fn(path, 1, buf, ctypes.byref(size))  # 1 = UNIVERSAL_NAME_INFO_LEVEL
        if rc == 0:
            unc = ctypes.cast(buf, ctypes.POINTER(ctypes.c_wchar_p))[0]
            return ntpath.splitdrive(unc)[0] or None
        if rc == 234:  # ERROR_MORE_DATA: `size` now holds the required byte count
            continue
        if rc in (1200, 1201, 1203, 1222, 2250):  # not a redirected drive / not connected
            return None
        raise PathError(f"Windows could not resolve the drive mapping (error {rc})")
    raise PathError("drive-mapping lookup kept asking for a bigger buffer")


class PathPolicy:
    """Central path rules.  `parts()` is the one canonical identity used for discovery,
    persistence and de-duplication; `check()` enforces approved storage.

    Why not Path.resolve() alone: it does not tell us that S:\\x and \\\\server\\share\\x are the
    same storage, and it compares nothing by path *components*.
    """

    def __init__(self, approved_root: str, approved_unc_root: str = "", mapper=wnet_universal_name):
        self.mapper, self._cache = mapper, {}
        drive = ntpath.splitdrive(approved_root)[0]
        verified = self._map(drive) if re.fullmatch(r"[A-Za-z]:", drive) else None
        configured = ntpath.splitdrive(approved_unc_root)[0] if approved_unc_root else ""
        if verified and configured and verified.casefold() != configured.casefold():
            raise PathError("approved_unc_root does not match the mapping Windows reports for the drive")
        self.overrides = {}
        if re.fullmatch(r"[A-Za-z]:", drive) and (verified or configured):
            self.overrides[drive.casefold()] = verified or configured
        self.mapping_source = "verified" if verified else ("configured" if configured else "none")
        self.unc_root = verified or configured or ""
        self.approved = self.parts(approved_root)
        self.roots = {self.approved}   # plus the real spelling (8.3 short names, junction-backed roots ...)
        if len(self.approved) > 1 and os.path.exists(approved_root):
            self.roots.add(self.parts(os.path.realpath(approved_root)))

    def _map(self, drive: str):
        if drive.casefold() not in self._cache:
            self._cache[drive.casefold()] = self.mapper(drive + "\\")
        return self._cache[drive.casefold()]

    def parts(self, path) -> tuple:
        p = str(path)
        if p.startswith("\\\\?\\UNC\\"):
            p = "\\\\" + p[8:]
        elif p.startswith("\\\\?\\"):
            p = p[4:]
        drive, rest = ntpath.splitdrive(p.replace("/", "\\"))
        if not drive or not rest.startswith("\\"):
            raise PathError("path must be absolute (drive letter or UNC)")
        if re.fullmatch(r"[A-Za-z]:", drive):
            drive = self.overrides.get(drive.casefold()) or self._map(drive) or drive
        comps = [c for c in ntpath.normpath(rest).split("\\") if c]  # normpath resolves '..' lexically
        return tuple(x.casefold() for x in [drive.rstrip("\\"), *comps])

    def ident(self, path) -> str:
        return "|".join(self.parts(path))

    def inside(self, path, root=None) -> bool:
        p = self.parts(path)
        return any(p[:len(r)] == tuple(r) for r in ([root] if root is not None else self.roots))  # by components

    def check(self, path, label: str) -> Path:
        """Return the path (original spelling kept) or raise.  Rejects unrelated shares and any
        link/junction whose real target leaves approved storage."""
        p = Path(clean_path(path))
        if not self.inside(p):
            raise PathError(f"{label} is outside the approved storage")
        if os.path.lexists(p) and not self.inside(os.path.realpath(p)):
            raise PathError(f"{label} resolves through a link or junction outside the approved storage")
        return p

    def unc_spelling(self, path):
        """The same location spelled through the UNC share, or None when no mapping is known."""
        drive, rest = ntpath.splitdrive(str(path))
        unc = self.overrides.get(drive.casefold())
        return (unc + rest) if unc else None


def is_reparse(path) -> bool:
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400) or os.path.islink(path)
    except OSError:
        return False


def probe_dir(directory: Path) -> str:
    """Prove a folder is writable AND that replace/read-back work on that storage.  '' = ok."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        a, b = directory / f".probe-{uuid.uuid4().hex[:8]}", directory / f".probe-{uuid.uuid4().hex[:8]}"
        with open(a, "wb") as fh:
            fh.write(b"probe")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(a, b)
        ok = b.read_bytes() == b"probe"
        b.unlink()
        return "" if ok else "read-back mismatch"
    except OSError as exc:
        return type(exc).__name__


# --------------------------------------------------------------------------------------------
# 3. CONFIG
# --------------------------------------------------------------------------------------------

DEFAULTS = {
    "cdp_port": 9333, "result_contract_path": "", "copilot_url": "https://m365.cloud.microsoft/chat", "requested_model": "",
    "recursive_documents": False,
    "completion_mode": "json_result",   # "json_result" = collect + validate Copilot's JSON answer; "submission_only" = V1 (send only)
    "node_executable": "", "dependency_directory": "", "approved_root": "", "approved_unc_root": "",
    "max_attachments": 20,            # a reference-program setting, NOT proof of your tenant's limit
    "over_limit_mode": "send_partial",  # more files than the limit: "send_partial" (instructions + first files, loud
                                        # warning, left-out count) or "block" (send nothing for that case)
    "max_file_mb": 50,
    "allowed_extensions": [".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md",
                           ".csv", ".rtf", ".html", ".htm", ".json", ".xml", ".eml", ".msg",
                           ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"],
    "ignore_names": ["~$*", "desktop.ini", "thumbs.db"],   # reported in the manifest, never silent
    "handshake_timeouts": [3, 6, 12, 20, 30],              # seconds, escalating (from the reference)
    "reconnect_budget_seconds": 180, "endpoint_wait_seconds": 90, "login_wait_seconds": 300,
    "op_timeout": 20, "stable_seconds": 1.0, "poll_ms": 150,
    "upload_timeout": 600, "upload_no_progress": 90, "upload_retry_clicks": 2,
    "send_confirm_seconds": 30, "reconcile_seconds": 45,
    "immediate_retries": 3, "max_attempts": 5,             # a failed case is tried up to 5 times in total
    "warm_tabs": 8, "warm_dwell_seconds": 0.4,             # tabs prepared (and visibly walked through) before the run
    "model_prompt_seconds": 0,                              # 0 = wait for your answer; Enter always takes the default
    "retry_backoff": [2, 5, 15], "persist_retries": 5,
    "console_detail": "minimal",      # "minimal" = queue total, ID being sent + attachments, final status, warnings;
                                      # "full" = every step (uploads, retries, connection details)
    "console_show_ids": True,         # print the real instruction ID on the console (never in the shareable log files)
    "fixup_requests": 3,              # asks for the exact same answer in the right structure, this many times per try
    "answer_overdue_minutes": 20,     # flag a slow answer (never resent); "answer_stable_seconds": text unchanged = finished
    "answer_stable_seconds": 3, "collect_poll_seconds": 2,
    "diagnostics_capture": False,     # screenshots/HTML are SENSITIVE, local-only, off by default
}
REQUIRED = ["cases_root", "instructions_path", "base_message_path", "python_executable",
            "edge_executable", "runtime_root"]
PATH_KEYS = REQUIRED + ["result_contract_path", "node_executable", "dependency_directory", "approved_root", "approved_unc_root"]
HERE = Path(__file__).resolve().parent


def config_path() -> Path:
    return Path(os.environ.get("SENDER_CONFIG") or HERE / "sender_config.json")


def load_config(required: bool = True):
    path = config_path()
    try:
        raw = json.loads(path.read_text("utf-8-sig"))
    except FileNotFoundError:
        if required:
            raise SenderError("no configuration yet - run: setup") from None
        raw = {}
    except (OSError, ValueError):
        raise SenderError("configuration file is unreadable or not valid JSON") from None
    merged = {**DEFAULTS, **raw}
    for key in PATH_KEYS:
        merged[key] = clean_path(merged.get(key, ""))
    return types.SimpleNamespace(**merged)


def save_config(cfg) -> None:
    keep = {k: v for k, v in vars(cfg).items() if k in DEFAULTS or k in REQUIRED}
    write_atomic(config_path(), json.dumps(keep, indent=2))


def exe_candidates(kind: str):
    pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")]
    if kind == "edge":
        return [Path(p) / "Microsoft/Edge/Application/msedge.exe" for p in pf if p]
    found = [os.environ.get("PLAYWRIGHT_NODEJS_PATH", ""), shutil.which("node") or ""]
    return [Path(p) for p in found if p] + [Path(p) / "nodejs/node.exe" for p in pf if p]


def resolve_file(text: str, hint: str = "") -> Path | None:
    """Forgiving path entry: a folder containing `hint`, or a name missing its extension, also works."""
    p = Path(clean_path(text))
    if p.is_file():
        return p
    if p.is_dir() and hint and (p / hint).is_file():
        return p / hint
    if p.is_dir() and hint == "review_guidance.md" and (p / "instructions.md").is_file():  # older name still found
        return p / "instructions.md"
    for ext in (".md", ".exe"):
        if p.with_name(p.name + ext).is_file():
            return p.with_name(p.name + ext)
    return None


def ask(prompt: str, default: str = "", optional: bool = False) -> str:
    suffix = f" [{default}]" if default else (" (Enter to skip)" if optional else "")
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise SenderError("setup needs an interactive console") from None
    return clean_path(answer) if answer else default


def setup_command() -> int:
    """Paste-your-paths setup.  Re-running keeps current values (Enter) so settings can be edited
    without touching source code.  Quotes around pasted paths are removed automatically."""
    cfg = load_config(required=False)
    cur = vars(cfg)
    print("SETUP - paste each path (quotes are fine). Press Enter to keep the value shown.\n")
    script_dir = HERE

    def ask_dir(key, label, create=False, default=""):
        while True:
            value = ask(label, cur.get(key) or default)
            p = Path(value) if value else None
            if p and (p.is_dir() or (create and value)):
                return str(p)
            print("  That folder was not found - try again.")

    def ask_file(key, label, hint="", default=""):
        while True:
            p = resolve_file(ask(label, cur.get(key) or default), hint)
            if p:
                return str(p)
            print("  That file was not found - try again.")

    cur["cases_root"] = ask_dir("cases_root", "Folder that contains the instruction-ID case folders (CASES_ROOT)")
    cur["instructions_path"] = ask_file("instructions_path", "Path of the Review guidance .md file (review_guidance.md)",
                                        "review_guidance.md")
    cur["base_message_path"] = ask_file("base_message_path", "Path of the base_message Markdown file", "base_message.md")
    template = HERE / "result_contract.md"
    value = ask("Path of the Result contract .md - the strict JSON answer format (Enter = do not collect answers)",
                cur.get("result_contract_path") or (str(template) if template.is_file() else ""), True)
    cur["result_contract_path"] = str(resolve_file(value, "result_contract.md") or "") if value else ""
    cur["completion_mode"] = "json_result" if cur["result_contract_path"] else "submission_only"
    cur["python_executable"] = ask_file("python_executable", "Embedded python.exe", "python.exe", sys.executable)
    edge = next((str(p) for p in exe_candidates("edge") if p.is_file()), "")
    cur["edge_executable"] = ask_file("edge_executable", "Installed msedge.exe", "msedge.exe", edge)
    cur["runtime_root"] = ask_dir("runtime_root", "Runtime folder on S: for all tool files (RUNTIME_ROOT; created if missing)",
                                  create=True, default=str(script_dir / "sender_runtime"))
    node = next((str(p) for p in exe_candidates("node") if p.is_file()), "")
    value = ask("Node.js node.exe (optional; Enter = use the copy bundled with Playwright)", cur.get("node_executable") or node, True)
    cur["node_executable"] = str(resolve_file(value, "node.exe") or "") if value else ""
    detail = ask("Console output: 1 = minimal (queue total, ID being sent, attachments, result), 2 = full detail",
                 "2" if cur.get("console_detail") == "full" else "1")
    cur["console_detail"] = "full" if detail.strip() == "2" else "minimal"
    if ask("Change optional settings (port, URL, model, recursion, limits)? y/N", "n").lower().startswith("y"):
        for key in ("cdp_port", "copilot_url", "requested_model", "recursive_documents", "max_attachments", "over_limit_mode", "warm_tabs",
                    "dependency_directory", "approved_root", "approved_unc_root"):
            value = ask(f"  {key}", str(cur.get(key, "")), True)
            default = DEFAULTS[key]
            cur[key] = (value.lower() in ("1", "true", "y", "yes") if isinstance(default, bool)
                        else int(value) if isinstance(default, int) and value.isdigit() else value)
    new = types.SimpleNamespace(**{**DEFAULTS, **cur})
    problems = validate_config(new)[1]
    if problems:
        print("\nNot saved - fix these first:\n  - " + "\n  - ".join(problems))
        return 2
    save_config(new)
    print("\nSaved. Next: 'check' (optional) then 'plan' (dry run) then 'run'.")
    return 0


def validate_config(cfg):
    """Return (policy, problems).  Problems use labels only - never the pasted paths."""
    bad = [f"{k} is missing" for k in REQUIRED if not getattr(cfg, k, "")]
    if bad:
        return None, bad
    problems = []
    try:
        approved = cfg.approved_root or (ntpath.splitdrive(cfg.runtime_root)[0] + "\\")
        policy = PathPolicy(approved, cfg.approved_unc_root)
    except PathError as exc:
        return None, [str(exc)]
    for key, kind in (("cases_root", "dir"), ("runtime_root", "dir"), ("instructions_path", "file"),
                      ("base_message_path", "file"), ("result_contract_path", "file"), ("dependency_directory", "dir")):
        value = getattr(cfg, key)
        if not value:
            continue
        try:
            policy.check(value, key)
        except PathError as exc:
            problems.append(str(exc))
            continue
        if key != "runtime_root" and key != "dependency_directory":
            ok = Path(value).is_dir() if kind == "dir" else Path(value).is_file()
            if not ok:
                problems.append(f"{key} was not found or is not a {kind}")
    for key in ("python_executable", "edge_executable", "node_executable"):
        value = getattr(cfg, key)
        if value and not Path(value).is_file():  # executables are read/execute only: any drive is fine
            problems.append(f"{key} was not found")
    try:  # runtime must never live inside the case parent (every child folder there is a case)
        if policy.inside(cfg.runtime_root, policy.parts(cfg.cases_root)):
            problems.append("runtime_root must be outside CASES_ROOT")
        if policy.inside(cfg.cases_root, policy.parts(cfg.runtime_root)):
            problems.append("CASES_ROOT must be outside runtime_root")
    except PathError as exc:
        problems.append(str(exc))
    if cfg.console_detail not in ("minimal", "full"):
        problems.append("console_detail must be 'minimal' or 'full'")
    if cfg.over_limit_mode not in ("send_partial", "block"):
        problems.append("over_limit_mode must be 'send_partial' or 'block'")
    if cfg.completion_mode not in ("json_result", "submission_only"):
        problems.append("completion_mode must be 'json_result' or 'submission_only'")
    elif cfg.completion_mode == "json_result" and not cfg.result_contract_path:
        problems.append("completion_mode 'json_result' needs result_contract_path (or use 'submission_only')")
    if not problems:
        try:
            load_message(Path(cfg.base_message_path))
        except SenderError as exc:
            problems.append(str(exc))
    return policy, problems


def load_message(path: Path) -> str:
    try:
        text = path.read_bytes().decode("utf-8-sig")  # BOM handled
    except UnicodeDecodeError:
        raise PathError("base_message is not valid UTF-8") from None
    except OSError:
        raise PathError("base_message could not be read") from None
    if not text.strip():
        raise PathError("base_message is empty")
    return text


# --------------------------------------------------------------------------------------------
# 4. STORAGE, REDACTION, LOGGING
# --------------------------------------------------------------------------------------------

class Runtime:
    """Every tool-controlled folder, all under RUNTIME_ROOT."""

    DIRS = ("state", "logs", "results", "staging", "temp", "profile", "cache", "downloads", "crashes",
            "diagnostics", "pycache", "pipcache", "work", "locks", "tools")

    def __init__(self, cfg):
        self.root = Path(cfg.runtime_root)
        for name in self.DIRS:
            setattr(self, name, self.root / name)
        self.deps = Path(cfg.dependency_directory) if cfg.dependency_directory else self.root / "deps"
        self.ledger = self.root / "sent_instructions.txt"   # the file YOU may edit (see README)


WRITE_GUARD = {"roots": None}  # set to the approved parts to arm; None = inactive


def _write_guard_hook(event, args):
    """Audit hook: any Python-level write outside approved storage raises instead of happening."""
    roots = WRITE_GUARD["roots"]
    if not roots:
        return
    path = None
    if event == "open":
        mode, flags = args[1] or "r", args[2] or 0
        if any(c in str(mode) for c in "wax+") or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
            path = args[0]
    elif event in ("os.mkdir", "os.remove", "os.rmdir"):
        path = args[0]
    elif event == "os.rename":
        path = (args[0], args[1])
    elif event == "shutil.copyfile":
        path = args[1]
    if path is None:
        return
    for one in (path if isinstance(path, tuple) else (path,)):
        if isinstance(one, int) or str(one).lower() in ("nul", os.devnull):
            continue
        try:
            inside = WRITE_GUARD["policy"].inside(os.path.abspath(os.fsdecode(one)))
        except Exception:
            inside = False
        if not inside:
            raise StorageError("blocked a write outside the approved storage")


sys.addaudithook(_write_guard_hook)


def bootstrap_storage(cfg, policy: PathPolicy) -> Runtime:
    """Run BEFORE Playwright is imported or any child process starts.  Process-local only:
    USERPROFILE/APPDATA/LOCALAPPDATA are deliberately left alone."""
    rt = Runtime(cfg)
    if (msg := probe_dir(rt.root)):
        raise StorageError(f"runtime storage is not writable ({msg})")
    for name in Runtime.DIRS:
        getattr(rt, name).mkdir(parents=True, exist_ok=True)
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(rt.pycache)  # -B is the primary switch; this is the belt-and-braces redirect
    env = {"TEMP": rt.temp, "TMP": rt.temp, "TMPDIR": rt.temp, "PYTHONPYCACHEPREFIX": rt.pycache,
           "PIP_CACHE_DIR": rt.pipcache, "npm_config_cache": rt.root / "npm-cache",
           "PLAYWRIGHT_BROWSERS_PATH": rt.root / "pw-browsers", "XDG_CACHE_HOME": rt.root / "cache"}
    os.environ.update({k: str(v) for k, v in env.items()})
    os.environ.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"})
    if cfg.node_executable:  # must be set before Playwright starts its driver
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = cfg.node_executable
    tempfile.tempdir = str(rt.temp)
    if os.path.normcase(tempfile.gettempdir()) != os.path.normcase(str(rt.temp)):  # verify, don't assume
        raise StorageError("temporary-file redirection did not take effect")
    if str(rt.deps) not in sys.path:  # embedded Python ignores PYTHONPATH, so add it explicitly
        sys.path.insert(0, str(rt.deps))
    WRITE_GUARD.update(roots=policy.approved, policy=policy)
    return rt


class Redactor:
    """Replace configured paths, case names/IDs and anything path/ID/URL-shaped with safe labels."""

    GENERIC = [(re.compile(r"\\\\[^\s\"'<>|]+"), "<unc-path>"), (re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]*"), "<path>"),
               (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I), "<url>"), (re.compile(r"\b[0-9a-fA-F]{32}\b"), "<id>")]

    def __init__(self):
        self.literals, self._rx = {}, None
        self.ids = {}   # alias -> real instruction ID (console only, when the user chose to see them)

    def add(self, value, label: str) -> None:
        value = str(value or "")
        if len(value) < 3:
            return
        for v in {value, value.replace("\\", "/"), value.replace("\\", "\\\\"), value.rstrip("\\/")}:
            self.literals[v] = label
        self._rx = None

    def __call__(self, text) -> str:
        text = str(text)
        if self.literals:
            if self._rx is None:
                self._rx = re.compile("|".join(re.escape(k) for k in sorted(self.literals, key=len, reverse=True)), re.I)
            lowered = {k.lower(): v for k, v in self.literals.items()}
            text = self._rx.sub(lambda m: lowered.get(m.group(0).lower(), "<redacted>"), text)
        for rx, label in self.GENERIC:
            text = rx.sub(label, text)
        return text


def safe_exc(redact, exc) -> str:
    """Exception text is a disclosure risk (paths, names): redact, first line, bounded."""
    return redact(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")[:240]


def use_colour() -> bool:
    """ANSI colour only on a real console (never in redirected output/log files); honours NO_COLOR."""
    if "NO_COLOR" in os.environ or not sys.stdout.isatty():
        return False
    if os.name == "nt":  # switch on virtual-terminal processing (Windows 10+ consoles)
        try:
            k32 = ctypes.windll.kernel32
            handle, mode = k32.GetStdHandle(-11), ctypes.c_uint32()
            if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            k32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


# What the "minimal" console shows: the queue, the case being sent, its result, and anything the user must act on.
MINIMAL_PHASES = {"queue", "start", "submitted", "submitted_partial", "submission_uncertain", "preflight_blocked",
                  "failed_exhausted", "WARNING", "at-limit", "attempt-failed", "deferred", "released", "paused",
                  "stopped", "login", "session", "model", "reconnect", "recovered", "dependencies", "fixup", "resubmit",
                  "result_correct", "result_incorrect", "result_inconclusive", "result_invalid", "response_timeout"}


class Log:
    """Console progress + structured redacted event log (shareable).  Real values live only in state/.
    The console may additionally show the real instruction ID (console_show_ids); the log file never does."""

    def __init__(self, rt: Runtime | None, redact: Redactor, detail: str = "full", show_ids: bool = False):
        self.redact, self.file, self.detail, self.show_ids = redact, None, detail, show_ids
        if rt:
            self.file = rt.logs / f"events-{datetime.datetime.now():%Y%m%d-%H%M%S}.jsonl"

    def __call__(self, phase: str, alias: str = "", **fields) -> None:
        fields = {k: (self.redact(v) if isinstance(v, str) else v) for k, v in fields.items()}
        line = " ".join(f"{k}={v}" for k, v in fields.items())
        shown = self.redact.ids.get(alias) if self.show_ids else None
        if shown:
            line = f"{line} instruction={shown}".strip()
        text = f"[{datetime.datetime.now():%H:%M:%S}] {alias or '-':<12} {phase:<18} {line}".rstrip()
        shade = {"submitted": "42;97",            # white text on green: sent successfully
                 "submitted_partial": "43;30",    # black on yellow: sent, but some files were left out
                 "WARNING": "43;30", "result_correct": "42;97", "result_incorrect": "41;97",
                 "result_inconclusive": "43;30", "result_invalid": "41;97", "response_timeout": "43;30"}.get(phase)
        if shade and use_colour():
            text = f"\x1b[{shade}m {text} \x1b[0m"
        if self.detail == "full" or phase in MINIMAL_PHASES:
            print(text, flush=True)
        if self.file:
            try:
                with open(self.file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": now(), "phase": phase, "case": alias, **fields}) + "\n")
            except OSError:
                pass  # logging must never decide whether a case is sent; the journal is what matters


def register_redactions(red: Redactor, cfg, policy: PathPolicy | None) -> None:
    labels = {"cases_root": "CASES_ROOT", "runtime_root": "RUNTIME_ROOT", "instructions_path": "REVIEW_GUIDANCE_FILE", "result_contract_path": "RESULT_CONTRACT_FILE",
              "base_message_path": "BASE_MESSAGE_FILE", "python_executable": "PYTHON_EXECUTABLE",
              "edge_executable": "EDGE_EXECUTABLE", "node_executable": "NODE_EXECUTABLE",
              "dependency_directory": "DEPENDENCY_DIR", "approved_root": "APPROVED_ROOT",
              "approved_unc_root": "APPROVED_UNC_ROOT"}
    for key, label in labels.items():
        red.add(getattr(cfg, key, ""), label)
    if policy and policy.unc_root:
        red.add(policy.unc_root, "APPROVED_UNC_ROOT")
    for env in ("USERNAME", "COMPUTERNAME", "USERDOMAIN"):
        red.add(os.environ.get(env, ""), "<" + env.lower() + ">")


# --------------------------------------------------------------------------------------------
# 5. STATE: append-only journal + sent-instructions ledger
# --------------------------------------------------------------------------------------------

TERMINAL_SENT = ("submitted", "submission_uncertain", "fixup_sent", "result_valid", "result_invalid", "response_timeout")


class Journal:
    """Single writer, append-only.  The journal is the truth; state.json is a derived snapshot.

    A crash can leave a half-written last line - replay ignores it.  A retried append reuses its
    sequence number, so replay also ignores duplicates.  If an append cannot be made durable we raise
    PersistenceError and the run stops before any further send.
    """

    def __init__(self, rt: Runtime, retries: int):
        self.path, self.snap, self.retries = rt.state / "journal.jsonl", rt.state / "state.json", retries
        self.lock, self.cases, self.seq = FileLock(rt.state / "writer.lock"), {}, 0
        self.lock.acquire()
        self.replay()

    def replay(self) -> None:
        if not self.path.exists():
            return
        for raw in self.path.read_bytes().splitlines():
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev["seq"] <= self.seq:
                continue
            self.seq = ev["seq"]
            self._apply(ev)

    def _apply(self, ev: dict) -> None:
        rec = self.cases.setdefault(ev["case"], {"history": 0})
        rec.update({k: v for k, v in ev.items() if k not in ("seq", "ts", "case")})
        rec["history"] += 1

    def record(self, case: str, state: str, **fields) -> dict:
        ev = {"seq": self.seq + 1, "ts": now(), "case": case, "state": state, **fields}
        line = (json.dumps(ev) + "\n").encode("utf-8")

        def append():
            with open(self.path, "ab") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        io_retry(append, "journal write", self.retries)
        self.seq = ev["seq"]
        self._apply(ev)
        return ev

    def snapshot(self) -> None:
        try:
            write_atomic(self.snap, json.dumps(self.cases, indent=1))
        except PersistenceError:
            pass  # derived file; the journal already holds every transition

    def close(self) -> None:
        self.snapshot()
        self.lock.release()


class Ledger:
    """Plain text list of instruction IDs already sent (one per line).  You may delete a line to allow
    a manual resend; the tool only ever appends.  Format: id<TAB>state<TAB>timestamp."""

    HEADER = ("# Instruction IDs already sent. The tool never sends an ID listed here again.\n"
              "# To resend one manually, delete its line and save. Format: id<TAB>state<TAB>time\n")

    def __init__(self, path: Path, retries: int):
        self.path, self.retries = path, retries

    def read(self) -> dict:
        out = {}
        try:
            text = self.path.read_text("utf-8")
        except FileNotFoundError:
            return out
        except OSError as exc:
            raise PersistenceError("ledger could not be read") from exc
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                bits = line.split("\t")
                out[bits[0].strip().lower()] = bits[1].strip() if len(bits) > 1 else "submitted"
        return out

    def add(self, instruction_id: str, state: str) -> None:
        def append():
            new = not self.path.exists()
            with open(self.path, "ab") as fh:
                if new:
                    fh.write(self.HEADER.encode())
                fh.write(f"{instruction_id.lower()}\t{state}\t{now()}\n".encode())
                fh.flush()
                os.fsync(fh.fileno())
        io_retry(append, "ledger write", self.retries)


def sync_ledger(journal: Journal, ledger: Ledger, log) -> dict:
    """Reconcile journal and ledger at start.  Returns the ledger contents to use.

    * submitted in journal, ledger line never written (crash between the two) -> write the line.
    * ledger line written then deleted by the user -> the case is *released* for a manual resend.
    """
    listed = ledger.read()
    for key, rec in list(journal.cases.items()):
        iid = rec.get("instruction_id", "").lower()
        if rec.get("state") not in TERMINAL_SENT or not iid or iid in listed:
            continue
        if rec.get("ledger_written"):
            journal.record(key, "queued", released=True, attempts=0, ledger_written=False)
            log("released", rec.get("alias", ""), note="ledger line removed by user: eligible again")
        else:
            ledger.add(iid, "uncertain" if rec["state"] == "submission_uncertain" else ("partial" if rec.get("omitted") else "submitted"))
            journal.record(key, rec["state"], ledger_written=True)
            listed[iid] = rec["state"]
    return listed


# --------------------------------------------------------------------------------------------
# 6. CASES: discovery, inventory, attachment plan
# --------------------------------------------------------------------------------------------

class Case:
    def __init__(self, name: str, path: Path, ident: str, salt: str):
        m = CASE_RE.match(name)
        self.name, self.path, self.ident = name, path, ident
        self.key = name.casefold()
        self.id = m.group("id").lower() if m else ""
        self.ref = (m.group("ref") or "") if m else ""
        self.alias = "case-" + hashlib.sha256((salt + self.key).encode()).hexdigest()[:6]
        self.docs, self.ignored, self.problems, self.blocked = [], [], [], ""


def local_salt(rt: Runtime) -> str:
    path = rt.state / "alias-salt.txt"
    if not path.exists():
        write_atomic(path, uuid.uuid4().hex)
    return path.read_text().strip()


def discover(cfg, policy: PathPolicy, salt: str, red: Redactor):
    """Snapshot the queue: immediate child directories of CASES_ROOT, sorted deterministically.

    A failed directory listing raises StorageError - it is never turned into an empty queue.
    """
    try:
        entries = sorted(os.scandir(cfg.cases_root), key=lambda e: e.name.casefold())
    except OSError as exc:
        raise StorageError("CASES_ROOT could not be listed (storage unavailable?)") from exc
    cases, unrecognised, excluded = [], [], 0
    for entry in entries:
        if entry.name.casefold() == EXCLUDED_DIR.casefold():  # first thing: never touched, even to stat
            excluded += 1
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if not CASE_RE.match(entry.name):
            alias = "other-" + hashlib.sha256((salt + entry.name).encode()).hexdigest()[:6]
            red.add(entry.name, alias)
            unrecognised.append(alias)
            continue
        case = Case(entry.name, Path(entry.path), policy.ident(entry.path), salt)
        red.add(case.name, case.alias)
        red.add(case.id, case.alias)
        red.ids[case.alias] = case.id
        red.add(case.ref, case.alias + "-ref")
        try:
            policy.check(entry.path, "case folder")
        except PathError as exc:
            case.blocked = str(exc)
        cases.append(case)
    by_id = collections.defaultdict(list)
    for c in cases:
        by_id[c.id].append(c)
    for group in by_id.values():
        if len(group) > 1:  # never silently collapse two folders that share an instruction ID
            for c in group:
                c.blocked = c.blocked or f"instruction ID shared by {len(group)} folders"
    return cases, unrecognised, excluded


def _looks_protected(head: bytes, tail: bytes, ext: str) -> bool:
    """Cheap heuristics only (labelled as such in the README)."""
    if ext in (".docx", ".xlsx", ".pptx"):
        return head[:8] == bytes.fromhex("D0CF11E0A1B11AE1")  # OLE container = encrypted OOXML
    if ext == ".pdf":
        return b"/Encrypt" in tail
    return False


def inventory(case: Case, cfg, policy: PathPolicy) -> None:
    """Fill case.docs / ignored / problems.  Never reports a partial inventory as complete."""
    limit = int(cfg.max_file_mb) * 1024 * 1024
    allowed = {e.lower() for e in cfg.allowed_extensions}
    odd = collections.Counter()
    found = []

    def walk(directory: Path, rel: str):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.casefold())
        except OSError as exc:
            raise StorageError("a case folder could not be listed (storage unavailable?)") from exc
        for e in entries:
            relname = f"{rel}{e.name}"
            if e.is_dir(follow_symlinks=False) or (e.is_dir() and is_reparse(e.path)):
                if cfg.recursive_documents:
                    if is_reparse(e.path):
                        odd["link not followed"] += 1
                    else:
                        walk(Path(e.path), relname + "/")
                continue
            found.append((relname, Path(e.path), e))

    walk(case.path, "")
    for rel, path, e in sorted(found, key=lambda t: t[0].casefold()):
        if any(fnmatch.fnmatch(path.name.lower(), pat.lower()) for pat in cfg.ignore_names):
            case.ignored.append(rel)
            continue
        try:
            if is_reparse(path):
                policy.check(path, "document")
                if not policy.inside(os.path.realpath(path), policy.parts(case.path)):
                    raise PathError("link leaves the case folder")
            st = path.stat()
            with open(path, "rb") as fh:
                head = fh.read(4096)
                fh.seek(max(0, st.st_size - 4096))
                tail = fh.read(4096)
        except PathError:
            odd["link outside the case"] += 1
            continue
        except OSError:
            odd["unreadable (locked/permission)"] += 1
            continue
        ext = path.suffix.lower()
        if ext not in allowed:
            odd[f"unsupported type {ext or '(none)'}"] += 1
        elif st.st_size == 0:
            odd["empty file"] += 1
        elif st.st_size > limit:
            odd[f"larger than {cfg.max_file_mb} MB"] += 1
        elif _looks_protected(head, tail, ext):
            odd["password-protected or not a valid Office file"] += 1
        case.docs.append(types.SimpleNamespace(rel=rel, path=path, size=st.st_size, mtime=st.st_mtime_ns, name=path.name))
    case.problems = [f"{n} x {why}" for why, n in sorted(odd.items())]
    if not case.docs and not case.problems:
        case.problems = ["no documents found"]


def build_plan(case: Case, instr, cfg, policy: PathPolicy):
    """Attachment plan -> (plan, left_out).  Instructions exactly once (by canonical identity) + every case
    document.  Over `max_attachments` the behaviour is explicit config: "block" raises Blocked, "send_partial"
    keeps instructions + the first documents (sorted order) and RETURNS what was left out - the caller must
    warn loudly; nothing is ever dropped silently.

    Names in the chip list must be unique, so on a basename clash (case-insensitive) the later entries get a
    deterministic alias `name (2).ext`.  Instructions come first and keep their name.  The plan is stored in
    the journal, so aliases stay reversible.  Source files are never renamed.
    """
    entries, seen_ident = [], set()
    fixed = list(instr) if isinstance(instr, (list, tuple)) else [instr]      # review guidance (+ result contract)
    for doc in fixed + list(case.docs):
        ident = policy.ident(doc.path)
        if ident in seen_ident:
            continue  # the instructions file physically sits inside this case: still attached once
        seen_ident.add(ident)
        entries.append(doc)
    limit, left_out = int(cfg.max_attachments), []
    if len(entries) > limit:
        if cfg.over_limit_mode == "block":
            raise Blocked(f"attachment limit: {len(entries)} files incl. instructions > {limit}")
        entries, left_out = entries[:limit], [d.rel for d in entries[limit:]]
    taken, plan = set(), []
    for doc in entries:
        name, k = doc.name, 1
        while name.casefold() in taken:
            k += 1
            stem, ext = os.path.splitext(doc.name)
            name = f"{stem} ({k}){ext}"
        taken.add(name.casefold())
        plan.append({"rel": doc.rel, "src": str(doc.path), "name": name, "aliased": name != doc.name,
                     "size": doc.size, "mtime": doc.mtime})
    return plan, left_out


def stage_aliases(plan: list, rt: Runtime, attempt_id: str) -> list:
    """Copy ONLY aliased files into a tool-owned staging folder (on RUNTIME_ROOT); everything else is
    uploaded straight from its source path, which is the fast path.  Returns upload paths in order."""
    folder = rt.staging / attempt_id
    paths = []
    for item in plan:
        if not item["aliased"]:
            paths.append(item["src"])
            continue
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".owned-by-case-sender").touch()
        dest = folder / item["name"]
        shutil.copy2(item["src"], dest)
        paths.append(str(dest))
    return paths


def clean_staging(rt: Runtime, attempt_id: str) -> None:
    """Delete only a folder this tool created (marker file present) and only when told it is no longer needed."""
    folder = rt.staging / attempt_id
    if (folder / ".owned-by-case-sender").exists():
        shutil.rmtree(folder, ignore_errors=True)


def files_unchanged(plan: list) -> bool:
    """Detect documents that changed or vanished since the inventory (size + mtime)."""
    for item in plan:
        try:
            st = os.stat(item["src"])
        except OSError:
            return False
        if st.st_size != item["size"] or st.st_mtime_ns != item["mtime"]:
            return False
    return True


def doc_of(path: Path):
    st = path.stat()
    return types.SimpleNamespace(rel="(instructions)", path=path, size=st.st_size, mtime=st.st_mtime_ns, name=path.name)


# --------------------------------------------------------------------------------------------
# 7. DEPENDENCIES  (first launch only; never during the case loop)
# --------------------------------------------------------------------------------------------

def playwright_version():
    try:
        import importlib.metadata as md
        return md.version("playwright")
    except Exception:
        return None


def ensure_dependencies(cfg, rt: Runtime, log) -> None:
    """Install the pinned Playwright into the private dependency folder when missing.
    Uses the configured interpreter; every cache/target/temp stays under RUNTIME_ROOT."""
    want = REQUIREMENTS[0].split("==")[1]
    if playwright_version() == want:
        return
    log("dependencies", note="first launch: installing pinned packages into the private dependency folder")
    py = cfg.python_executable
    child = dict(os.environ)
    child.pop("PYTHONHOME", None)
    probe = subprocess.run([py, "-B", "-s", "-c", "import pip"], capture_output=True, env=child, cwd=rt.work)
    if probe.returncode == 0:
        pip_cmd = [py, "-B", "-s", "-m", "pip"]
    else:  # embedded Python has no pip: run pip straight from its official wheel (checksum verified)
        wheel = rt.tools / "pip.whl"
        if not wheel.exists():
            with urllib.request.urlopen(PIP_WHEEL_JSON, timeout=60) as r:
                meta = json.load(r)
            files = [u for u in meta["urls"] if u["filename"].endswith("py3-none-any.whl") and not u.get("yanked")]
            if len(files) != 1 or not files[0]["url"].startswith("https://files.pythonhosted.org/"):
                raise SenderError("could not identify the official pip wheel")
            with urllib.request.urlopen(files[0]["url"], timeout=120) as r:
                blob = r.read()
            if hashlib.sha256(blob).hexdigest() != files[0]["digests"]["sha256"]:
                raise SenderError("pip wheel checksum mismatch")
            write_atomic(wheel, blob)
        pip_cmd = [py, "-B", "-s", str(wheel) + os.sep + "pip"]
    rt.deps.mkdir(parents=True, exist_ok=True)
    cmd = pip_cmd + ["install", "--target", str(rt.deps), "--cache-dir", str(rt.pipcache), "--only-binary=:all:",
                     "--disable-pip-version-check", "--no-warn-script-location", "--upgrade", *REQUIREMENTS]
    result = subprocess.run(cmd, capture_output=True, text=True, env=child, cwd=rt.work)
    write_atomic(rt.logs / "dependency-install.log", result.stdout + "\n" + result.stderr)
    import importlib
    importlib.invalidate_caches()
    if result.returncode or playwright_version() != want:
        raise SenderError("dependency install failed (details in RUNTIME_ROOT/logs/dependency-install.log; "
                          "compatible wheels may be unavailable for this Python version/architecture)")


def load_playwright():
    from playwright.async_api import async_playwright, Error as PWError, TimeoutError as PWTimeout  # noqa
    return async_playwright, PWError, PWTimeout


# --------------------------------------------------------------------------------------------
# 8. BROWSER: Edge + CDP, and the Copilot page adapter
# --------------------------------------------------------------------------------------------

LOSS_MARKERS = ("target closed", "has been closed", "connection closed", "browser has been closed",
                "disconnected", "target page, context or browser", "session closed", "websocket")


def is_loss(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionLost) or any(m in str(exc).lower() for m in LOSS_MARKERS)


def guarded(fn):
    """Translate browser-gone errors into ConnectionLost; slow renderer replies into UiError."""
    @functools.wraps(fn)
    async def wrapper(self, *a, **k):
        try:
            return await fn(self, *a, **k)
        except (SenderError, asyncio.CancelledError):
            raise
        except asyncio.TimeoutError:
            raise UiError("the page did not answer in time (renderer unresponsive?)") from None
        except Exception as exc:
            if is_loss(exc):
                raise ConnectionLost("browser connection or tab was lost") from exc
            raise UiError(f"{type(exc).__name__} while talking to the page") from exc
    return wrapper


async def cdp_version(port: int, timeout: float = 1.5):
    def fetch():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
                return json.load(r)
        except Exception:
            return None
    return await asyncio.to_thread(fetch)


def endpoint_valid(payload, port: int) -> str:
    """Return the browser WebSocket URL only if it is loopback, on our port, and a browser endpoint."""
    ws = urlparse(str((payload or {}).get("webSocketDebuggerUrl", "")))
    if ws.scheme == "ws" and ws.hostname in ("127.0.0.1", "localhost", "::1") and ws.port == port \
            and ws.path.startswith("/devtools/browser/"):
        return ws.geturl()
    return ""


def port_owned_by_profile(profile: Path, payload) -> bool:
    """Is the endpoint on our port driven by OUR dedicated profile (not someone else's debug session)?

    Strong proof: Edge's DevToolsActivePort file inside the profile, when it writes one.  Some Edge
    builds do not, so otherwise we accept that Edge currently holds the profile's `lockfile` (a second
    process cannot open it).  This is a heuristic: it shows our profile is in use by a running Edge, and
    the port is the one we asked that Edge to use.
    """
    try:
        lines = (profile / "DevToolsActivePort").read_text().splitlines()
        return lines[1].strip() == urlparse(payload["webSocketDebuggerUrl"]).path
    except (IndexError, KeyError):
        return False
    except OSError:
        pass
    try:
        open(profile / "lockfile", "rb").close()
        return False  # openable: no running browser holds this profile
    except PermissionError:
        return True
    except OSError:
        return False


async def page_target_id(page) -> str:
    session = None
    try:
        session = await page.context.new_cdp_session(page)
        return str((await session.send("Target.getTargetInfo")).get("targetInfo", {}).get("targetId", ""))
    finally:
        if session:
            try:
                await session.detach()
            except Exception:
                pass


class EdgeBrowser:
    """Dedicated Edge profile on RUNTIME_ROOT + Playwright connect_over_cdp (never a fresh Chromium)."""

    def __init__(self, cfg, rt, policy, log, stop):
        self.cfg, self.rt, self.policy, self.log, self.stop = cfg, rt, policy, log, stop
        self.lock = FileLock(rt.locks / f"controller-{cfg.cdp_port}.lock")
        self.pw = self.browser = self.context = None
        self.generation = 0
        self.api = None
        self.launched = self.attach_logged = False     # launched: this run started Edge itself (else we joined an open one)

    @property
    def reused(self) -> bool:
        """Joined an Edge that was already open on the port: its prepared Copilot tabs are used, none are added."""
        return not self.launched

    # -- launch -------------------------------------------------------------------------------
    def _variants(self):
        """Which spelling of the profile path Edge gets. Order remembered from the last success."""
        names = ["as_entered"] + (["unc"] if self.policy.unc_spelling(self.rt.profile) else [])
        memo = self.rt.state / "edge-variant.txt"
        if memo.exists() and memo.read_text().strip() in names:
            names.sort(key=lambda n: n != memo.read_text().strip())
        return names

    def _spell(self, path: Path, variant: str) -> str:
        return (self.policy.unc_spelling(path) or str(path)) if variant == "unc" else str(path)

    def _launch(self, variant: str):
        prefs = self.rt.profile / "Default" / "Preferences"
        if not prefs.exists():  # explicit download behaviour, on RUNTIME_ROOT, no prompt
            prefs.parent.mkdir(parents=True, exist_ok=True)
            d = self._spell(self.rt.downloads, variant)
            write_atomic(prefs, json.dumps({"download": {"default_directory": d, "prompt_for_download": False},
                                            "savefile": {"default_directory": d}}))
        # Loopback only. No --remote-allow-origins: Playwright's CDP client needs no browser Origin.
        flags = [self.cfg.edge_executable, f"--remote-debugging-port={self.cfg.cdp_port}",
                 "--remote-debugging-address=127.0.0.1",
                 f"--user-data-dir={self._spell(self.rt.profile, variant)}",
                 f"--disk-cache-dir={self._spell(self.rt.cache, variant)}",
                 f"--crash-dumps-dir={self._spell(self.rt.crashes, variant)}",
                 "--no-first-run", "--no-default-browser-check", "--disable-background-timer-throttling",
                 "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", self.cfg.copilot_url]
        err = open(self.rt.logs / "edge-stderr.log", "ab")
        return subprocess.Popen(flags, env=dict(os.environ), cwd=str(self.rt.work), stdin=subprocess.DEVNULL,
                                stdout=err, stderr=err,
                                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    async def ensure_running(self, launch: bool = True):
        port = self.cfg.cdp_port
        payload = await cdp_version(port)
        if payload:
            if not endpoint_valid(payload, port):
                raise UiError("the debugging endpoint is not a loopback browser endpoint on the expected port")
            if not self.launched and not self.attach_logged:   # an Edge that was open before us (any process): use it as it is
                self.attach_logged = True
                self.log("edge-attach", note="using the Edge session already open on this port"
                         + ("" if port_owned_by_profile(self.rt.profile, payload) else " (started by another process)"))
            return payload
        if not launch:
            return None
        for variant in self._variants():
            self.log("edge-launch", variant=variant)
            proc = self._launch(variant)
            self.launched = True
            deadline = time.monotonic() + self.cfg.endpoint_wait_seconds
            while time.monotonic() < deadline:
                await self.stop.sleep(0.4)
                payload = await cdp_version(port)
                if payload and endpoint_valid(payload, port) and port_owned_by_profile(self.rt.profile, payload):
                    write_atomic(self.rt.state / "edge-variant.txt", variant)
                    return payload
            self.log("edge-launch", note=f"no debugging endpoint with variant {variant} (exit={proc.poll()})")
        raise RecoveryRequired("Edge did not expose its debugging endpoint. If a normal window of the dedicated "
                               "profile is already open, close it and run again.")

    # -- connect ------------------------------------------------------------------------------
    async def _try(self, route: str, timeout: float):
        """One owned connection attempt: cancelled AND awaited if it overruns (never leaked/overlapping)."""
        task = asyncio.ensure_future(self.pw.chromium.connect_over_cdp(route, timeout=timeout * 1000))
        done, _ = await asyncio.wait({task}, timeout=timeout + min(3, timeout))
        if task in done:
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise asyncio.TimeoutError()

    async def connect(self, launch: bool = True):
        budget_end = time.monotonic() + self.cfg.reconnect_budget_seconds
        last = "no attempt"
        for timeout in self.cfg.handshake_timeouts:
            self.stop.check()
            if time.monotonic() > budget_end:
                break
            payload = await self.ensure_running(launch)         # refreshed discovery every cycle
            if payload is None:
                raise ConnectionLost("Edge is not running")
            ws = endpoint_valid(payload, self.cfg.cdp_port)
            # A responsive /json/version does NOT prove the Playwright handshake works: try the browser
            # WebSocket first, then the HTTP discovery URL - strictly one after the other.
            for route in (ws, f"http://127.0.0.1:{self.cfg.cdp_port}"):
                try:
                    self.browser = await self._try(route, timeout)
                except Exception as exc:
                    last = f"{type(exc).__name__} (handshake {timeout}s)"
                    continue
                self.context = self.browser.contexts[0] if self.browser.contexts else None
                if self.context is None:
                    last = "browser exposed no context"
                    await self._drop()
                    continue
                self.generation += 1
                return
            self.log("connect", note=f"retrying: {last}")
        raise RecoveryRequired(f"could not connect within the reconnect budget (last: {last})")

    async def _drop(self):
        if self.browser:
            try:
                await asyncio.wait_for(self.browser.close(), 5)  # for CDP attach this only disconnects
            except Exception:
                pass
        self.browser = self.context = None

    async def start(self, launch: bool = True):
        if not self.lock.fh:
            self.lock.acquire()
        if not self.pw:
            self.api = load_playwright()
            self.pw = await self.api[0]().start()
        await self.connect(launch)

    async def reconnect(self):
        self.log("reconnect", note="re-establishing the browser connection")
        await self._drop()
        await self.connect(launch=True)

    async def close(self):
        """Disconnect only. Edge stays open for inspection; no process is killed."""
        await self._drop()
        if self.pw:
            try:
                await asyncio.wait_for(self.pw.stop(), 10)
            except Exception:
                pass
        self.lock.release()

    # -- tabs ---------------------------------------------------------------------------------
    async def open_tab(self):
        page = await self.context.new_page()
        tab = CopilotTab(page, self.cfg, self.stop)
        await tab.start()
        return tab

    async def open_tabs(self, count: int) -> list:
        """Warm-up: open `count` Copilot tabs at once, then walk through them one by one like a person
        (forward, pointer over the composer) so Copilot fully wakes each of them."""
        tabs = [CopilotTab(await self.context.new_page(), self.cfg, self.stop) for _ in range(count)]
        await asyncio.gather(*(t.navigate() for t in tabs))
        for t in tabs:
            await t.activate()
        return tabs

    async def open_url_tab(self, url: str):
        """Read-only tab on a saved conversation (collecting an answer after a restart)."""
        page = await self.context.new_page()
        tab = CopilotTab(page, self.cfg, self.stop)
        tab.target_id = await asyncio.wait_for(page_target_id(page), self.cfg.op_timeout)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return tab

    async def find_ready_tabs(self) -> list:
        """Every Copilot page of this browser that already shows the chat composer + model button (tabs sign in
        and load at different speeds, so this can grow from one poll to the next)."""
        origin = "{0.scheme}://{0.netloc}".format(urlparse(self.cfg.copilot_url))
        seen, ready = False, []
        for page in list(self.context.pages):
            if page.url.startswith(origin):
                seen = True
                tab = CopilotTab(page, self.cfg, self.stop)
                if await tab.is_ready():
                    ready.append(tab)
        if not seen:  # nothing on Copilot yet (e.g. all tabs closed): open one so sign-in can be seen
            page = await self.context.new_page()
            await page.goto(self.cfg.copilot_url, wait_until="domcontentloaded", timeout=60000)
        return ready

    async def find_ready_tab(self):
        """The first Copilot page that already shows the chat composer + model button."""
        ready = await self.find_ready_tabs()
        return ready[0] if ready else None

    async def find_tab(self, target_id: str):
        """Rebind by EXACT target id - never by tab index or a shared URL."""
        for page in list(self.context.pages):
            try:
                if await page_target_id(page) == target_id:
                    tab = CopilotTab(page, self.cfg, self.stop)
                    tab.target_id = target_id
                    return tab
            except Exception:
                continue
        return None


PLUS_BTN = "button[data-testid='PlusMenuButton']"
ATTACH_BTN = ("#plus-menu-container button[data-testid='PlusMenuButton'], button[data-testid='chat-input-attach-button'], "
              "button[data-test-id='chat-input-attach-button'], button[aria-label='Add'][aria-haspopup='menu']")
EDITORS = ["#m365-chat-editor-target-element", "[data-testid*='chat-editor' i] [contenteditable='true']",
           "[data-test-id*='chat-editor' i] [contenteditable='true']",
           "[role='textbox'][contenteditable='true'][aria-label*='Copilot' i]", "[role='textbox'][aria-label*='Message' i]",
           "textarea[aria-label*='Copilot' i]", "main [contenteditable='true'][role='textbox']"]
SEND_BTN = ('button[aria-label="Send"], button[aria-label^="Send "]:not([aria-label*="stop" i]), '
            'button[type="submit"][aria-label*="send" i]:not([aria-label*="stop" i])')
USER_MSG = '[data-testid="chatQuestion"],.fai-UserMessage,[aria-labelledby^="user-message-"]'
ASSISTANT_MSG = ('[data-testid="copilot-message-reply-div"] [data-testid="markdown-reply"],'
                 '[data-testid="copilot-message-div"] [data-testid="markdown-reply"],.fai-CopilotMessage__content')
UPLOAD_ITEM = re.compile(r"upload", re.I)          # menu entry "Upload images and files" under the + button
READY_JS = ("() => { const vis = e => !!(e && (e.offsetWidth || e.offsetHeight)); const q = s => document.querySelector(s);"
            " return vis(q(" + json.dumps(", ".join(EDITORS)) + ")) && vis(q(\"button[aria-label*='model selector' i], #gptModeSwitcher\")); }")
# First visible menu = modes + provider entries (aria-haspopup); a provider's models open as the 2nd menu.
MENU_JS = r"""idx => {
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim();
  const menus = [...document.querySelectorAll('[role="menu"]')].filter(m => m.offsetWidth || m.getClientRects().length);
  const m = menus[idx]; if (!m) return null;
  return [...m.querySelectorAll('[role="menuitem"],[role="menuitemradio"]')].filter(e => e.closest('[role="menu"]') === m).map(e => {
    const span = [...e.querySelectorAll('span')].find(x => norm(x.textContent) && ![...x.querySelectorAll('span')].some(y => norm(y.textContent)));   // first leaf span = the name
    return {text: norm(e.textContent), title: norm(span ? span.textContent : e.textContent), popup: e.getAttribute('aria-haspopup') || '',
            disabled: e.getAttribute('aria-disabled') === 'true' || e.disabled === true}; }); }"""
MODEL_SWITCHER = ("#gptModeSwitcher, [data-testid*='gptModeSwitcher' i], button[aria-label*='response mode' i], "
                  "button[aria-label*='model selector' i], button[aria-label*='mode selector' i]")

# One browser round trip reads everything the upload check needs (speed: a poll is a single call).
SNAPSHOT_JS = r"""() => {
  const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim();
  const RM = 'button[aria-label^="Remove attachment "]';
  const roots = new Set();
  document.querySelectorAll('[aria-label="Attachments"] > [data-overflow-item="true"], [aria-label="Attachments"] > div[id^="SPO_"]').forEach(e => roots.add(e));
  document.querySelectorAll(RM).forEach(b => roots.add(b.closest('[data-overflow-item="true"], div[id^="SPO_"]') || b));
  const busy = /uploading|processing|attaching|scanning|loading|preparing|transferring|pending|in progress/i;
  const chips = [], active = [];
  roots.forEach(r => {          // textContent (not innerText) so overflow-hidden chips are still counted
    const b = r.matches(RM) ? r : r.querySelector(RM);
    const text = norm(r.textContent);
    chips.push({label: b ? norm(b.getAttribute('aria-label')).slice('Remove attachment '.length) : '', text, title: norm(r.getAttribute('title'))});
    if (busy.test(text)) active.push('chip');
    r.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="spinner" i]').forEach(e => { if (vis(e)) active.push('spinner'); });
  });
  const alertText = () => [...document.querySelectorAll('[role="alert"],[role="dialog"],[aria-live="assertive"],[class*="toast" i],[class*="messagebar" i]')]
      .filter(vis).map(e => norm(e.textContent));
  const alerts = alertText();
  const find = rx => alerts.find(t => rx.test(t)) || '';
  const send = [...document.querySelectorAll('button')].find(b => { const l = norm(b.getAttribute('aria-label')).toLowerCase();
      return vis(b) && (l === 'send' || l.startsWith('send ')) && !l.includes('stop'); });
  const stop = [...document.querySelectorAll('button')].some(b => { const l = norm(b.getAttribute('aria-label')).toLowerCase();
      return vis(b) && (l === 'stop generating' || l === 'stop' || l.startsWith('stop responding')); });
  return {chips, active, sendEnabled: !!(send && !send.disabled && send.getAttribute('aria-disabled') !== 'true'),
          generating: stop,
          error: find(/error occurred while uploading|error while uploading|upload failed|could not upload|try again/i).slice(0, 300),
          limit: find(/maximum(?: of)? \d+|up to \d+ files|\d+ file limit|too many files|remove (?:a|one) file/i).slice(0, 300)};
}"""
# The same chat message matches several of these selectors (wrapper + inner), so only OUTERMOST matches count.
OUTER_JS = "const outer = els => els.filter(e => !els.some(o => o !== e && o.contains(e)));"
SENT_JS = "([needle, userSel, asstSel]) => {" + OUTER_JS + r"""
  const users = outer([...document.querySelectorAll(userSel)]);
  const flat = v => [...String(v || '').toLowerCase()].filter(ch => /[\p{L}\p{N}]/u.test(ch)).join('');
  const match = users.some(u => flat(u.textContent).includes(needle));
  // Sent messages list their files as buttons inside [data-testid="prompt-entity-list"] (names WITHOUT extension).
  const names = [];
  users.forEach(u => u.querySelectorAll('[data-testid="prompt-entity-list"] button[id^="reference-"]')
      .forEach(a => { const t = (a.textContent || '').replace(/\s+/g, ' ').trim(); if (t) names.push(t); }));
  const stop = [...document.querySelectorAll('button')].some(b => /^stop/i.test((b.getAttribute('aria-label') || '').trim()));
  return {users: users.length, match, names, generating: stop, replies: outer([...document.querySelectorAll(asstSel)]).length};
}"""
FRESH_JS = "([userSel, asstSel]) => {" + OUTER_JS + r"""
  return {users: outer([...document.querySelectorAll(userSel)]).length, replies: outer([...document.querySelectorAll(asstSel)]).length,
          chips: document.querySelectorAll('button[aria-label^="Remove attachment "]').length};
}"""
ANSWER_JS = "([userSel, asstSel]) => {" + OUTER_JS + r"""
  const users = outer([...document.querySelectorAll(userSel)]);
  const replies = outer([...document.querySelectorAll(asstSel)]);
  const last = replies[replies.length - 1];     // ONLY the newest assistant reply of this conversation
  const blocks = last ? [...last.querySelectorAll('pre')].map(p => (p.querySelector('code') || p).textContent) : [];
  const stop = [...document.querySelectorAll('button')].some(b => /^stop/i.test((b.getAttribute('aria-label') || '').trim()));
  return {users: users.length, replies: replies.length, text: last ? last.textContent : '', blocks, generating: stop};
}"""
RETRY_JS = r"""() => {   // click "Try again" ONLY inside an upload-error alert (never the response Regenerate/Retry)
  const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  for (const box of document.querySelectorAll('[role="alert"],[aria-live="assertive"],[class*="toast" i],[class*="messagebar" i]')) {
    if (!vis(box) || box.closest('[data-testid="copilot-message-reply-div"],[data-testid="copilot-message-div"]')) continue;
    if (!/upload/i.test(box.textContent || '')) continue;
    const b = [...box.querySelectorAll('button,[role="button"]')].find(x => /try again|retry/i.test(x.textContent || x.getAttribute('aria-label') || ''));
    if (b) { b.click(); return true; }
  }
  return false; }"""


def label_matches(current, entry) -> bool:
    """Does the model button text show this entry?  (exact, or one is the other plus a descriptive suffix)"""
    cur, label = norm_ws(current).casefold(), norm_ws(entry["label"]).casefold()
    return bool(cur) and (cur == label or label.startswith(cur + " ") or cur.startswith(label + " "))


class CopilotTab:
    """Everything that touches one Copilot page.  All other code sees only these small methods, so the
    transaction logic can be tested against a fake tab."""

    def __init__(self, page, cfg, stop):
        self.page, self.cfg, self.stop, self.target_id = page, cfg, stop, ""
        self._chooser = None
        # Registering a file-chooser listener makes Playwright INTERCEPT the chooser: Windows' own file dialog can
        # never open (it used to break the send).  We answer the chooser ourselves with the whole file list.
        page.on("filechooser", self._on_chooser)

    def _on_chooser(self, chooser):
        if self._chooser is not None and not self._chooser.done():
            self._chooser.set_result(chooser)

    async def _ev(self, script, arg=None):
        return await asyncio.wait_for(self.page.evaluate(script, arg) if arg is not None else self.page.evaluate(script),
                                      self.cfg.op_timeout)

    @guarded
    async def start(self):
        self.target_id = await asyncio.wait_for(page_target_id(self.page), self.cfg.op_timeout)
        await self.reset()

    @guarded
    async def reset(self):
        """Back to a brand-new Copilot chat in THIS tab (open the original chat link again)."""
        await self.page.goto(self.cfg.copilot_url, wait_until="domcontentloaded", timeout=60000)
        await self.wait_composer()
        if not await self._is_fresh():
            await self._new_chat()

    @guarded
    async def foreground(self):
        """Bring this tab to the front and wait until the page reports itself visible.  A background tab is
        throttled by the browser: talking to it can time out and a send can silently not happen."""
        await self.page.bring_to_front()
        deadline = time.monotonic() + self.cfg.op_timeout
        while time.monotonic() < deadline:
            if await self._ev("() => document.visibilityState === 'visible'"):
                return
            await asyncio.sleep(0.05)
        raise UiError("the tab could not be brought to the front")

    @guarded
    async def adopt(self):
        """Take over a tab that is already signed in (the login tab): same tab id, fresh chat."""
        self.target_id = await asyncio.wait_for(page_target_id(self.page), self.cfg.op_timeout)
        if not await self._is_fresh():
            await self.reset()

    async def is_ready(self) -> bool:
        """Sign-in proof: the composer AND the model button are both on screen."""
        try:
            return bool(await asyncio.wait_for(self.page.evaluate(READY_JS), 1.5))
        except Exception:
            return False

    @guarded
    async def navigate(self):
        """Start loading Copilot without waiting for it (warm-up opens many tabs at once)."""
        self.target_id = await asyncio.wait_for(page_target_id(self.page), self.cfg.op_timeout)
        await self.page.goto(self.cfg.copilot_url, wait_until="domcontentloaded", timeout=60000)

    @guarded
    async def activate(self):
        """Do what a person would: bring the tab forward, move the pointer over the composer and let Copilot wake up."""
        await self.page.bring_to_front()
        await self.wait_composer()
        box = await (await self._editor()).bounding_box()
        if box:
            await self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=6)
        await asyncio.sleep(self.cfg.warm_dwell_seconds)
        if not await self._is_fresh():
            await self._new_chat()

    async def wait_composer(self):
        deadline = time.monotonic() + self.cfg.login_wait_seconds
        hinted = False
        while time.monotonic() < deadline:
            if await self._editor() is not None:
                return
            if not hinted and time.monotonic() > deadline - self.cfg.login_wait_seconds + 8:
                print("Waiting for the Copilot composer - sign in manually in the dedicated Edge window if asked.", flush=True)
                hinted = True
            await self.stop.sleep(0.4)
        raise UiError("the Copilot composer never appeared (not signed in?)")

    async def _editor(self):
        for sel in EDITORS:
            loc = self.page.locator(sel).first
            if await asyncio.wait_for(loc.count(), self.cfg.op_timeout) and await loc.is_visible():
                return loc
        return None

    async def _is_fresh(self) -> bool:
        s = await self._ev(FRESH_JS, [USER_MSG, ASSISTANT_MSG])
        return s["users"] == 0 and s["replies"] == 0 and s["chips"] == 0

    async def _new_chat(self):
        btn = self.page.locator('[data-testid="new-chat-button"], button:has-text("New chat"), a:has-text("New chat")').first
        if await btn.count():
            await btn.click(timeout=15000)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if await self._is_fresh():
                return
            await self.stop.sleep(0.3)
        raise UiError("a fresh conversation could not be proven")

    @guarded
    async def fresh(self) -> bool:
        return await self._is_fresh()

    @guarded
    async def url(self) -> str:
        return self.page.url

    @guarded
    async def snapshot(self) -> dict:
        return await self._ev(SNAPSHOT_JS)

    async def _assign(self, files: list):
        """Bulk-attach through the + menu -> "Upload images and files" -> intercepted file chooser.
        Only that one menu entry is ever clicked; nothing else on the page (quick-action buttons, other
        menu items) can open Windows' file dialog because the chooser is intercepted anyway."""
        paths = [os.path.abspath(f) for f in files]
        self._chooser = asyncio.get_running_loop().create_future()
        plus = self.page.locator(PLUS_BTN).first
        try:   # early in a page load only a look-alike placeholder "Add" button exists; the real one appears a few seconds later
            await plus.wait_for(state="visible", timeout=15000)
        except Exception:
            plus = self.page.locator(ATTACH_BTN).first          # other Copilot variants
        await plus.click(timeout=10000)
        item = self.page.get_by_role("menuitem", name=UPLOAD_ITEM).first
        deadline = time.monotonic() + self.cfg.op_timeout
        while not self._chooser.done() and time.monotonic() < deadline:
            if await item.count():
                await item.click(timeout=5000)
                break
            await asyncio.sleep(0.05)
        chooser = await asyncio.wait_for(self._chooser, self.cfg.op_timeout)
        await chooser.set_files(paths)   # the whole list at once; success here != upload success

    @guarded
    async def attach(self, paths: list, route: str):
        """route 'bulk': whole list in ONE assignment.  route 'sequential': one file at a time, waiting for
        each chip.  (The reference's Playwright set_input_files route stays quarantined.)"""
        if route == "bulk":
            return await self._assign(paths)
        for i, p in enumerate(paths, 1):
            await self._assign([p])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and len((await self.snapshot())["chips"]) < i:
                await self.stop.sleep(0.15)

    @guarded
    async def set_message(self, text: str):
        ed = await self._editor()
        if ed is None:
            raise UiError("the Copilot message editor was not found")
        await ed.click(timeout=10000)
        await ed.fill(text, timeout=20000)  # fill inserts text only - it cannot press Enter

    @guarded
    async def message_text(self) -> str:
        ed = await self._editor()
        if ed is None:
            return ""
        return await ed.evaluate("e => ('value' in e) ? e.value : (e.innerText || e.textContent || '')")

    async def _menu(self, index: int, expect_open: str = "", not_these=()) -> list:
        """Items of the index-th open menu.  With expect_open, wait until that provider entry is expanded; with
        not_these, ignore a submenu still showing the previous provider's models (it is replaced a moment later)."""
        if expect_open:
            await self.page.wait_for_function(
                "t => [...document.querySelectorAll('[role=menuitem][aria-haspopup][aria-expanded=true]')]"
                ".some(e => e.textContent.includes(t)) && document.querySelectorAll('[role=menu]').length > 1",
                arg=expect_open, timeout=self.cfg.op_timeout * 1000, polling=50)
        deadline = time.monotonic() + self.cfg.op_timeout
        while time.monotonic() < deadline:
            items = await self._ev(MENU_JS, index)
            if items and {m["text"] for m in items} != set(not_these):
                return items
            await asyncio.sleep(0.05)
        raise UiError("the model menu did not open")

    @guarded
    async def discover_models(self) -> list:
        """List what the picker offers: [{'group', 'label', 'text'}].  Modes are plain entries; each provider
        entry (Claude, GPT ...) is hovered to read its models.  The picker is closed again afterwards."""
        await self.page.locator(MODEL_SWITCHER).first.click(timeout=10000)
        top = await self._menu(0)
        found = [{"group": "Modes", "label": m["title"], "text": m["text"], "disabled": m["disabled"]} for m in top if not m["popup"]]
        previous = ()
        for provider in [m for m in top if m["popup"]]:
            await self.page.locator("[role=menuitem][aria-haspopup]", has_text=provider["title"]).first.hover()
            models = await self._menu(1, provider["title"], previous)
            previous = {m["text"] for m in models}
            found += [{"group": provider["title"], "label": m["text"], "text": m["text"], "disabled": m["disabled"]} for m in models]
        await self._close_menus()
        return found

    async def _close_menus(self):
        """Escape closes a submenu first, then the menu: repeat until no menu is left open."""
        for _ in range(4):
            if not await self._ev("() => [...document.querySelectorAll('[role=menu]')].some(m => m.offsetWidth || m.getClientRects().length)"):
                return
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.1)

    @guarded
    async def select_model(self, entry: dict):
        switcher = self.page.locator(MODEL_SWITCHER).first
        if label_matches(await switcher.text_content(), entry):
            return
        await self._close_menus()                 # a menu left open would make the next click close it
        await switcher.click(timeout=10000)
        await self._menu(0)
        index = 0
        if entry["group"] != "Modes":
            await self.page.locator("[role=menuitem][aria-haspopup]", has_text=entry["group"]).first.hover()
            await self._menu(1, entry["group"])
            index = 1
        pattern = re.compile("^" + re.escape(entry["text"]) + "$")
        item = self.page.locator("[role=menu]").nth(index).locator("[role=menuitem],[role=menuitemradio]", has_text=pattern).first
        if await item.get_attribute("aria-disabled") == "true":      # greyed out by Copilot (e.g. usage limit reached)
            await self._close_menus()
            raise RecoveryRequired(f"the chosen model ({entry['label']}) is not available in Copilot right now - run again and pick another one")
        await item.click(timeout=10000)
        deadline = time.monotonic() + self.cfg.op_timeout
        while time.monotonic() < deadline:
            if label_matches(await switcher.text_content(), entry):
                return
            await asyncio.sleep(0.1)
        raise UiError("the model choice could not be confirmed on the page")

    async def ensure_model(self, entry):
        """Cheap when already right (reads the button label); selects otherwise.  None = leave Copilot's default."""
        if entry:
            await self.select_model(entry)

    @guarded
    async def click_upload_retry(self) -> bool:
        return bool(await self._ev(RETRY_JS))

    @guarded
    async def click_send(self):
        # EXACTLY one action; the selector excludes "Stop generating" by construction.
        await self.page.locator(SEND_BTN).first.click(timeout=15000)

    @guarded
    async def evidence(self, needle: str) -> dict:
        return await self._ev(SENT_JS, [needle, USER_MSG, ASSISTANT_MSG])

    @guarded
    async def read_answer(self) -> dict:
        """Newest assistant reply of this chat: text, its code blocks, whether Copilot is still generating."""
        return await self._ev(ANSWER_JS, [USER_MSG, ASSISTANT_MSG])

    @guarded
    async def send_followup(self, text: str):
        """Second message in the SAME chat (the structure fix-up request)."""
        await self.foreground()
        ed = await self._editor()
        if ed is None:
            raise UiError("the Copilot message editor was not found")
        await ed.click(timeout=10000)
        await ed.fill(text, timeout=20000)
        await self.page.locator(SEND_BTN).first.click(timeout=15000)

    @guarded
    async def close(self):
        await self.page.close()

    async def capture(self, base: Path):
        """Local-only diagnostics; sensitive by nature, never part of shareable output."""
        try:
            await self.page.screenshot(path=str(base) + ".png", timeout=10000)
            write_atomic(Path(str(base) + ".html"), await self.page.content())
        except Exception:
            pass


# --------------------------------------------------------------------------------------------
# 9. TRANSACTION, QUEUE AND RETRIES
# --------------------------------------------------------------------------------------------

def resolve_chip(chip: dict, expected: list) -> str:
    """Exact attachment name for a chip.  Prefer the 'Remove attachment <name>' label; otherwise the
    chip text must be exactly an expected name plus only type/size decoration (no substring matching)."""
    if chip.get("label"):
        return chip["label"]
    text = norm_ws(chip.get("title") or chip.get("text"))
    for name in sorted(set(expected), key=len, reverse=True):
        if text.casefold().startswith(name.casefold()):
            rest = text[len(name):]
            if re.fullmatch(r"[\s\W]*(?:[A-Za-z]{2,5})?[\s\d.,]*(?:bytes|[kmg]?b)?\s*", rest, re.I):
                return name
    return text


def needle_of(message: str) -> str:
    """Letters/digits only, so Markdown rendering in the sent bubble can't break the comparison."""
    return "".join(ch for ch in message.lower() if ch.isalnum())[:60]


def judge_upload(snap: dict, expected: list) -> dict:
    """Pure decision on one snapshot (unit-tested).  Never equates count with identity."""
    observed = collections.Counter(resolve_chip(c, expected) for c in snap["chips"])
    want = collections.Counter(expected)
    missing, extra = want - observed, observed - want
    ready = (not missing and not extra and not snap["active"] and not snap["error"] and not snap["limit"]
             and snap["sendEnabled"] and not snap["generating"])
    return {"observed": sum(observed.values()), "missing": sum(missing.values()), "extra": sum(extra.values()),
            "ready": ready, "signature": (sum(observed.values()), len(snap["active"]))}


# --- the answer contract: Copilot must reply with ONE JSON object, written as plain text, shaped like this -----
RESULT_KEYS = ("COPILOT_RESULT", "JUSTIFICATION", "A_B_COMPARISONS")
ROW_KEYS = ("ITEM", "Customer_expectation", "System_data", "MATCH")
RESULT_WORDS = ("Correct", "Incorrect", "Inconclusive")
FIXUP_TEXT = ("Your previous answer did not follow the required result structure. Resend your exact same answer, unchanged "
              "in content, as one JSON object written as plain text on a single line (no code block, no markdown) with "
              "exactly the keys COPILOT_RESULT, JUSTIFICATION and A_B_COMPARISONS, as defined in the attached result contract.")


def validate_result(obj: dict) -> list:
    """Problems with an answer object ([] = valid).  Strict keys, exact verdict words, and the verdict must
    agree with the MATCH flags of the comparison rows."""
    problems = []
    missing, extra = [k for k in RESULT_KEYS if k not in obj], [k for k in obj if k not in RESULT_KEYS]
    if missing:
        problems.append("missing key(s): " + ", ".join(missing))
    if extra:
        problems.append("unexpected key(s): " + ", ".join(extra))
    verdict = obj.get("COPILOT_RESULT")
    if verdict not in RESULT_WORDS:
        problems.append("COPILOT_RESULT must be exactly Correct, Incorrect or Inconclusive")
    if not (isinstance(obj.get("JUSTIFICATION"), str) and obj["JUSTIFICATION"].strip()):
        problems.append("JUSTIFICATION must be non-empty text")
    rows = obj.get("A_B_COMPARISONS")
    if not isinstance(rows, list):
        problems.append("A_B_COMPARISONS must be a list of rows")
        return problems
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != set(ROW_KEYS):
            problems.append(f"A_B_COMPARISONS row {i} must have exactly ITEM, Customer_expectation, System_data, MATCH")
            continue
        if not isinstance(row["MATCH"], bool):
            problems.append(f"row {i}: MATCH must be true or false")
        for k in ("ITEM", "Customer_expectation", "System_data"):
            if isinstance(row[k], bool) or not isinstance(row[k], (str, int, float)):
                problems.append(f"row {i}: {k} must be text")
    if not problems:  # verdict cross-check
        flags = [r["MATCH"] for r in rows]
        if verdict in ("Correct", "Incorrect") and not rows:
            problems.append(f"{verdict} needs at least one A_B_COMPARISONS row")
        elif verdict == "Correct" and not all(flags):
            problems.append("Correct requires every MATCH to be true")
        elif verdict == "Incorrect" and all(flags):
            problems.append("Incorrect requires at least one MATCH to be false")
    return problems


def json_objects(text: str) -> list:
    """Every JSON object inside a piece of text (plain text with or without words around it), in order."""
    decoder, found, i = json.JSONDecoder(), [], 0
    while True:
        i = text.find("{", i)
        if i == -1:
            return found
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = end


def parse_answer(answer: dict):
    """(object or None, problems) from the NEWEST reply.  The result is expected as plain text (a code box is not
    relied on - it sometimes does not render), but JSON inside a code block is read too.  The newest object that
    has COPILOT_RESULT is judged; otherwise the newest object, so a wrong one can be named in the fix-up."""
    body = answer.get("text") or ""
    pool = []
    for block in reversed(answer.get("blocks") or []):
        pool += json_objects(block)[::-1]
    pool += json_objects(body)[::-1]
    for obj in pool:
        if "COPILOT_RESULT" in obj:
            return obj, validate_result(obj)
    if pool:
        return pool[0], validate_result(pool[0])
    looks = "text that looks like JSON but is not valid JSON" if "{" in body else "no JSON object"
    return None, [f"the reply has {looks} (the result must be one JSON object written as plain text)"]


def save_result(rt: Runtime, case, status: str, obj, raw: str, tries: int, fixups: int, omitted: int) -> None:
    """Local result files: <instruction id>.json (full answer) and one row in results.csv."""
    rows = (obj or {}).get("A_B_COMPARISONS") or []
    mismatches = sum(1 for r in rows if isinstance(r, dict) and r.get("MATCH") is False)
    done = now()
    write_atomic(rt.results / f"{case.id}.json", json.dumps(
        {"instruction_id": case.id, "status": status, "tries": tries, "fixups": fixups, "left_out_files": omitted,
         "finished": done, "result": obj, "raw_text": raw}, indent=2))
    line = [case.id, status, (obj or {}).get("COPILOT_RESULT", ""), len(rows), mismatches, tries, fixups, omitted, done]

    def append():
        path = rt.results / "results.csv"
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["instruction_id", "status", "verdict", "rows", "differences", "tries", "fixups", "left_out_files", "finished_utc"])
            w.writerow(line)
            fh.flush()
            os.fsync(fh.fileno())
    io_retry(append, "results.csv write")


class Pending:
    """A submitted case whose answer has not been collected yet (its tab holds the conversation)."""

    def __init__(self, case, tab, generation, fixups=0):
        self.case, self.tab, self.generation, self.fixups = case, tab, generation, fixups
        self.started, self.replies_before = time.monotonic(), 0
        self.last_text, self.stable_since, self.overdue = None, None, False


class Prepared:
    """Result of preparing the next case while the current one uploads (the pipeline)."""

    def __init__(self, case, plan=None, blocked="", tab=None, generation=0, left_out=()):
        self.case, self.plan, self.blocked, self.tab, self.generation = case, plan, blocked, tab, generation
        self.left_out = list(left_out)  # documents NOT sent because of the attachment limit (local detail)


class Runner:
    def __init__(self, cfg, rt, policy, journal, ledger, log, stop, browser, redact, message, instr):
        self.cfg, self.rt, self.policy, self.journal, self.ledger = cfg, rt, policy, journal, ledger
        self.log, self.stop, self.browser, self.redact = log, stop, browser, redact
        self.message, self.instr = message, instr
        self.needle = needle_of(message)
        self.sent_ids = ledger.read()
        self.attempts = collections.Counter()
        self.prefetch = None  # (case key, asyncio.Task)
        self.pool = collections.deque()   # warm, signed-in, fresh tabs: (tab, connection generation)
        self.retry_tab = {}               # case key -> (tab, generation): an unsent tab reset for the next try
        self.model = None                 # chosen model entry (None = Copilot's own default)
        self.pending = {}                 # case key -> Pending: submitted, answer not collected yet
        self.done_tabs = collections.deque()   # tabs whose answer is collected: reset + reused when a new case needs a tab
        self.results = {}                 # case key -> (state, reason) shown in the summary
        self.partial = set()              # cases sent with files left out (never reported as plain success)
        self.fg_lock = asyncio.Lock()     # only ONE party (sender or collector) works in the front tab at a time
        self.queue, self.queue_closed = None, False

    # -- helpers ------------------------------------------------------------------------------
    def rec(self, case, state, **fields):
        return self.journal.record(case.key, state, alias=case.alias, instruction_id=case.id, **fields)

    def storage_alive(self):
        """Called before every new case: stop (never fall back) when S: or its share is gone."""
        try:
            os.listdir(self.cfg.cases_root)
            os.stat(self.rt.state)
        except OSError as exc:
            raise StorageError("approved storage is no longer reachable") from exc

    async def prepare(self, case: Case, want_tab: bool) -> Prepared:
        """Inventory + plan (thread, so the event loop keeps serving the current upload), then a fresh tab."""
        try:
            await asyncio.to_thread(inventory, case, self.cfg, self.policy)
            if case.problems:
                return Prepared(case, blocked="; ".join(case.problems))
            plan, left_out = build_plan(case, self.instr, self.cfg, self.policy)
        except Blocked as exc:
            return Prepared(case, blocked=str(exc))
        tab = None
        if want_tab:
            try:
                tab = self.pool_tab()          # prefetch only takes a warm tab; the attempt opens/recycles one if needed
            except Exception as exc:  # pipeline is an optimisation: failing here only loses the head start
                self.log("prefetch", case.alias, note=safe_exc(self.redact, exc))
        return Prepared(case, plan, tab=tab, generation=self.browser.generation, left_out=left_out)

    def collecting(self) -> bool:
        return self.cfg.completion_mode == "json_result"

    async def recycled_tab(self):
        """A tab whose answer is already collected goes back to a brand-new chat for the next case."""
        while self.done_tabs:
            tab, generation = self.done_tabs.popleft()
            if generation != self.browser.generation:
                continue
            try:
                await tab.reset()
                return tab
            except Exception:
                await self._abandon(tab)
        return None

    def pool_tab(self, key=None):
        """A warm tab (or the case's own reset retry tab) from the current browser connection, else None."""
        entries = [self.retry_tab.pop(key)] if key in self.retry_tab else []
        while not entries and self.pool:
            entries.append(self.pool.popleft())
        for tab, generation in entries:
            if generation == self.browser.generation:   # a reconnect makes older tab objects stale
                return tab
        return None

    def start_prefetch(self, nxt):
        if nxt is not None and (self.prefetch is None):
            self.prefetch = (nxt.key, asyncio.ensure_future(self.prepare(nxt, want_tab=True)))

    async def take_prepared(self, case) -> Prepared:
        if self.prefetch and self.prefetch[0] != case.key:
            await self.discard_prefetch()
        if self.prefetch and self.prefetch[0] == case.key:
            _, task = self.prefetch
            self.prefetch = None
            prep = await task
            if prep.tab and prep.generation != self.browser.generation:
                prep.tab = None  # connection was re-made since: that tab object is stale
            return prep
        return await self.prepare(case, want_tab=False)

    async def discard_prefetch(self):
        if self.prefetch:
            _, task = self.prefetch
            self.prefetch = None
            prep = await asyncio.gather(task, return_exceptions=True)
            if prep and isinstance(prep[0], Prepared) and prep[0].tab:
                await self._abandon(prep[0].tab)

    async def _abandon(self, tab):
        """Close an UNSENT composer tab (only ever called when no send_intent exists for it)."""
        try:
            await tab.close()
        except Exception:
            pass

    # -- one attempt --------------------------------------------------------------------------
    async def attempt(self, case: Case, prep: Prepared, n: int, route: str, nxt) -> str:
        t0, aid = time.monotonic(), uuid.uuid4().hex[:10]
        expected = [p["name"] for p in prep.plan]
        self.rec(case, "preparing", attempt=n, attempt_id=aid, route=route, plan=prep.plan, expected=len(expected),
                 prompt_version=self.prompt_version, omitted=len(prep.left_out), left_out=prep.left_out,
                 contract=self.collecting())
        tab, usable = prep.tab, prep.tab
        prep.tab = None
        intent = reusable = False
        try:
            if not files_unchanged(prep.plan):
                raise AttemptFailed("documents changed while preparing")
            paths = await asyncio.to_thread(stage_aliases, prep.plan, self.rt, aid)
            tab = usable or self.pool_tab(case.key) or await self.recycled_tab() or await self.browser.open_tab()
            self.rec(case, "preparing", target_id=tab.target_id, url=await tab.url())
            await tab.foreground()                        # every tab is foregrounded before we work in it
            pending = getattr(tab, "model_task", None)    # background model selection started right after your choice
            if pending:
                await asyncio.gather(pending, return_exceptions=True)
            fresh, _ = await asyncio.gather(tab.fresh(), tab.ensure_model(self.model))
            if not fresh:
                raise AttemptFailed("conversation was not fresh")
            self.rec(case, "uploading")
            await tab.attach(paths, route)
            self.log("uploading", case.alias, attempt=n, route=route, expected=len(expected))
            self.start_prefetch(nxt)                     # pipeline: next case prepares while this uploads
            await tab.set_message(self.message)          # typing the message overlaps the file transfer
            await self.wait_upload_ready(tab, case, expected)
            await self.pre_send_checks(tab, case, prep.plan)
            self.rec(case, "upload_ready", observed=len(expected))
            self.stop.check()
            # ---- point of no return: intent is durable BEFORE the single send action ----
            self.rec(case, "send_intent", target_id=tab.target_id)
            intent = True
            outcome = await self.send_and_confirm(tab, case, expected)
            partial = outcome == "submitted" and prep.left_out
            self.log("submitted_partial" if partial else outcome, case.alias, attempt=n, route=route,
                     elapsed=round(time.monotonic() - t0, 1))
            if outcome == "submitted" and self.collecting():
                self.watch(case, tab)                    # its answer is collected in the background from now on
            return outcome
        except (Stopped, asyncio.CancelledError):
            if not intent:
                self.rec(case, "cancelled", note="stopped before any send")
            raise
        except ConnectionLost:
            if intent:
                raise
            await self.browser.reconnect()               # may raise RecoveryRequired -> run pauses
            raise AttemptFailed("browser connection lost before send")
        except AttemptFailed:
            reusable = True
            raise
        except UiError as exc:
            reusable = True
            raise AttemptFailed(str(exc)) from exc
        finally:
            if tab and not intent:
                if self.cfg.diagnostics_capture:
                    await tab.capture(self.rt.diagnostics / f"LOCAL-ONLY-{case.alias}-{n}")
                kept = False
                if reusable:        # unsent and still alive: refresh THIS tab to a new chat for the next try
                    try:
                        await tab.reset()
                        self.retry_tab[case.key] = (tab, self.browser.generation)
                        kept = True
                    except Exception:
                        pass
                if not kept:
                    await self._abandon(tab)             # unsent composer: safe to discard
            if not intent:
                clean_staging(self.rt, aid)

    async def wait_upload_ready(self, tab, case, expected):
        """Observe-only loop: never re-assigns files.  Fails on stall/error/extra; succeeds only after the
        exact set has been ready continuously for stable_seconds."""
        cfg, t0 = self.cfg, time.monotonic()
        last_sig, last_change, stable_since, clicks = None, time.monotonic(), None, 0
        while True:
            snap = await tab.snapshot()
            verdict = judge_upload(snap, expected)
            if snap["limit"]:
                raise Blocked("Copilot reported an attachment limit for this case")
            if snap["error"]:
                if clicks >= cfg.upload_retry_clicks or not await tab.click_upload_retry():
                    raise AttemptFailed("Copilot reported an upload error")
                clicks += 1
                last_change, stable_since = time.monotonic(), None
            if verdict["extra"]:
                raise AttemptFailed("unexpected attachment present")
            if verdict["signature"] != last_sig:
                last_sig, last_change = verdict["signature"], time.monotonic()
                self.log("upload-progress", case.alias, expected=len(expected), observed=verdict["observed"])
            if verdict["ready"]:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= cfg.stable_seconds:
                    return
            else:
                stable_since = None
            if time.monotonic() - last_change > cfg.upload_no_progress:
                raise AttemptFailed(f"upload stalled at {verdict['observed']}/{len(expected)} attachments")
            if time.monotonic() - t0 > cfg.upload_timeout:
                raise AttemptFailed("upload timed out")
            await self.stop.sleep(cfg.poll_ms / 1000)

    async def pre_send_checks(self, tab, case, plan):
        """Everything that must be true at the moment of sending (case, conversation, text, set, model)."""
        rec = self.journal.cases[case.key]
        if tab.target_id != rec.get("target_id"):
            raise AttemptFailed("tab identity changed")
        if rec.get("state") in ("send_intent", "submitted", "submission_uncertain"):
            raise AttemptFailed("a send was already recorded for this case")
        self.storage_alive()
        await tab.foreground()                        # the send must happen in the visible, active tab
        ev = await tab.evidence(self.needle)          # (fresh() would fail here: our own chips are attached)
        if ev["users"] or ev["replies"]:
            raise AttemptFailed("conversation already contains messages")
        if norm_ws(await tab.message_text()) != norm_ws(self.message):  # text may reset while uploading: re-fill once
            await tab.set_message(self.message)
            if norm_ws(await tab.message_text()) != norm_ws(self.message):
                raise AttemptFailed("composer text does not match the configured message")
        snap = await tab.snapshot()
        if not judge_upload(snap, [p["name"] for p in plan])["ready"]:
            raise AttemptFailed("attachment set no longer ready")
        if not files_unchanged(plan):
            raise AttemptFailed("documents changed during upload")

    async def send_and_confirm(self, tab, case, expected) -> str:
        """After send_intent we never raise Stopped and never resend.  Anything unresolved = uncertain."""
        try:  # again, right before the click (another tab may have taken the focus); never skips the send itself
            await tab.foreground()
        except Exception as exc:
            self.log("foreground", case.alias, note=safe_exc(self.redact, exc))
        try:
            await tab.click_send()
        except Exception as exc:  # a click that raised may still have gone through: reconcile, don't retry
            self.log("send-error", case.alias, note=safe_exc(self.redact, exc))
        ev = await self.reconcile(case, tab, self.cfg.send_confirm_seconds + self.cfg.reconcile_seconds)
        if isinstance(ev, str):
            return self.finish_uncertain(case, ev)
        if ev["users"] != 1:
            return self.finish_uncertain(case, "unexpected number of user messages")
        stem = lambda n: os.path.splitext(n)[0].casefold()   # the sent bubble shows names without extensions
        names = collections.Counter(stem(n) for n in ev.get("names") or [])
        if names and names != collections.Counter(stem(n) for n in expected):
            return self.finish_uncertain(case, "attachments shown in the sent message differ from the plan")
        try:
            url = await tab.url()
        except Exception:
            url = ""
        self.rec(case, "submitted", conversation_url=url,
                 evidence={"user_message": True, "attachments_visible": bool(names),
                           "generating_seen": ev["generating"], "reply_seen": bool(ev["replies"])})
        self.mark_sent(case, "submitted")
        return "submitted"

    async def reconcile(self, case, tab, window: float):
        """Look at the SAME conversation (rebinding by exact target id after a reconnect).  Returns the
        evidence dict, or a short reason string when it can't be proven."""
        deadline, target = time.monotonic() + window, tab.target_id
        while time.monotonic() < deadline:
            try:
                ev = await tab.evidence(self.needle)
                if ev["match"]:
                    return ev
            except ConnectionLost:
                try:
                    await self.browser.reconnect()
                except (RecoveryRequired, SenderError) as exc:
                    return f"connection could not be re-established ({type(exc).__name__})"
                tab = await self.browser.find_tab(target)
                if tab is None:
                    return "original tab is gone after a possible send"
            except UiError:
                pass
            await plain_sleep(self.cfg.poll_ms / 1000)
        return "no confirmation of the submission was observed"

    def finish_uncertain(self, case, reason: str) -> str:
        self.rec(case, "submission_uncertain", reason=reason)
        self.mark_sent(case, "uncertain")
        self.log("submission_uncertain", case.alias, note=reason)
        return "submission_uncertain"

    def mark_sent(self, case, state: str):
        omitted = self.journal.cases.get(case.key, {}).get("omitted")
        state = "partial" if (state == "submitted" and omitted) else state   # shown in the ledger so it is not forgotten
        if self.sent_ids.get(case.id) != state:       # a resubmission must not write a second ledger line
            self.ledger.add(case.id, state)
        self.sent_ids[case.id] = state
        self.rec(case, "submission_uncertain" if state == "uncertain" else "submitted", ledger_written=True)

    # -- answers ------------------------------------------------------------------------------
    def watch(self, case, tab):
        self.pending[case.key] = Pending(case, tab, self.browser.generation)
        self.results[case.key] = ("answer_pending", "waiting for Copilot's answer")

    async def resume(self, by_key: dict):
        """Re-run rule: a case that was submitted but never answered gets its answer collected from its saved
        conversation (same tab by exact target id, else the saved conversation link in a new tab).  Never resent."""
        for key, rec in list(self.journal.cases.items()):
            case = by_key.get(key)
            if not (case and rec.get("contract") and rec.get("state") in ("submitted", "fixup_sent")):
                continue
            tab = None
            try:
                if rec.get("target_id"):
                    tab = await self.browser.find_tab(rec["target_id"])
                if tab is None and rec.get("conversation_url", "").startswith("http"):
                    tab = await self.browser.open_url_tab(rec["conversation_url"])
            except SenderError:
                tab = None
            if tab is None:
                self.log("WARNING", case.alias, note="submitted earlier but its conversation could not be found again")
                self.results[key] = ("answer_unavailable", "conversation not found - check Copilot manually")
                continue
            self.pending[key] = Pending(case, tab, self.browser.generation, fixups=rec.get("fixups", 0))
            self.results[key] = ("answer_pending", "collecting an answer from an earlier run")
            self.log("recovered", case.alias, note="collecting the answer of an earlier submission")

    async def collect_loop(self):
        """Background collector: visits each answering tab in turn (bringing it to the front), judges finished
        answers, asks for fix-ups, and finishes overdue cases when nothing else is left to send."""
        while True:
            if not self.pending:
                if self.queue_closed:
                    return
                await asyncio.sleep(0.3)
                continue
            for key in list(self.pending):
                if key in self.pending:
                    await self.visit(self.pending[key])
            await asyncio.sleep(self.cfg.collect_poll_seconds)

    async def visit(self, p):
        case, cfg = p.case, self.cfg
        try:
            async with self.fg_lock:                  # never while the sender works in the front tab
                await p.tab.foreground()
                answer = await p.tab.read_answer()
        except ConnectionLost:
            await self.rebind(p)
            return
        except UiError:
            return                                    # try this tab again on the next round
        moment, elapsed = time.monotonic(), time.monotonic() - p.started
        if elapsed > cfg.answer_overdue_minutes * 60 and not p.overdue:
            p.overdue = True
            self.log("WARNING", case.alias, note=f"answer overdue ({cfg.answer_overdue_minutes} min): still waiting, not resending")
        finished = answer["replies"] > p.replies_before and not answer["generating"] and answer["text"].strip()
        if not finished:
            p.last_text = p.stable_since = None
            if p.overdue and not self.queue:          # nothing left to send: stop waiting, report it
                self.finish_without_answer(p, "response_timeout", "no answer within the overdue limit")
            return
        if answer["text"] != p.last_text:
            p.last_text, p.stable_since = answer["text"], moment
        if moment - p.stable_since >= cfg.answer_stable_seconds:
            await self.judge(p, answer)

    async def rebind(self, p):
        """Connection lost while collecting: reconnect, then find the tab again by its exact target id."""
        async with self.fg_lock:
            await self.browser.reconnect()
        tab = await self.browser.find_tab(p.tab.target_id)
        rec = self.journal.cases.get(p.case.key, {})
        if tab is None and rec.get("conversation_url", "").startswith("http"):
            tab = await self.browser.open_url_tab(rec["conversation_url"])
        if tab is None:
            self.finish_without_answer(p, "response_timeout", "conversation lost after a reconnect")
        else:
            p.tab, p.generation = tab, self.browser.generation

    async def judge(self, p, answer):
        obj, problems = parse_answer(answer)
        if obj is not None and not problems:
            return self.finish_valid(p, obj, answer["text"])
        self.log("fixup", p.case.alias, note="answer structure problem: " + "; ".join(problems[:3]))
        if p.fixups < self.cfg.fixup_requests:
            await self.request_fixup(p, answer, problems)
        else:
            await self.business_retry(p, problems, answer["text"])

    async def request_fixup(self, p, answer, problems):
        text = FIXUP_TEXT + " Problems found: " + "; ".join(problems[:4]) + "."
        try:
            async with self.fg_lock:
                await p.tab.send_followup(text)
        except ConnectionLost:
            return await self.rebind(p)
        except UiError as exc:
            self.log("fixup", p.case.alias, note=f"could not send the fix-up request: {safe_exc(self.redact, exc)}")
            return
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:               # confirm: one more user message in this chat
            try:
                if (await p.tab.read_answer())["users"] > answer["users"]:
                    break
            except SenderError:
                pass
            await asyncio.sleep(0.3)
        p.fixups += 1                                    # counted even if unconfirmed, so this always ends
        p.replies_before, p.last_text, p.stable_since = answer["replies"], None, None
        self.rec(p.case, "fixup_sent", fixups=p.fixups, problems="; ".join(problems[:4]))
        self.log("fixup", p.case.alias, request=f"{p.fixups}/{self.cfg.fixup_requests}", note="asked Copilot to resend in the right structure")

    async def business_retry(self, p, problems, raw: str):
        """Fix-ups did not help: resubmit the SAME instruction ID in this tab refreshed to a new chat, within the
        same total-tries budget.  (The only case where a submitted ID is sent again: no valid answer exists.)"""
        case, key = p.case, p.case.key
        if self.attempts[key] >= self.cfg.max_attempts:
            return self.finish_without_answer(p, "result_invalid", "no valid answer structure after all tries", raw=raw)
        try:
            async with self.fg_lock:
                await p.tab.reset()
            self.retry_tab[key] = (p.tab, self.browser.generation)
        except Exception:
            await self._abandon(p.tab)
        self.rec(case, "retry_pending", reason="answer structure still invalid after the fix-up requests")
        self.log("resubmit", case.alias, note=f"resubmitting in a refreshed tab (try {self.attempts[key] + 1}/{self.cfg.max_attempts})")
        del self.pending[key]
        self.queue.append(case)

    def finish_valid(self, p, obj, raw):
        case, verdict = p.case, obj["COPILOT_RESULT"]
        rows = obj["A_B_COMPARISONS"]
        diff = sum(1 for r in rows if r["MATCH"] is False)
        omitted = self.journal.cases.get(case.key, {}).get("omitted", 0)
        save_result(self.rt, case, "result_valid", obj, raw, self.attempts[case.key], p.fixups, omitted)
        self.rec(case, "result_valid", verdict=verdict, rows=len(rows), differences=diff, fixups=p.fixups)
        note = f"{len(rows)} comparison(s), {diff} difference(s)" + (f"; {omitted} file(s) were left out" if omitted else "")
        self.results[case.key] = ("result_" + verdict.lower(), note)
        self.log("result_" + verdict.lower(), case.alias, comparisons=len(rows), differences=diff, fixups=p.fixups)
        del self.pending[case.key]
        self.done_tabs.append((p.tab, p.generation))     # free to be reset and reused

    def finish_without_answer(self, p, state: str, reason: str, raw: str = ""):
        case = p.case
        save_result(self.rt, case, state, None, raw, self.attempts[case.key], p.fixups, 0)
        self.rec(case, state, reason=reason, fixups=p.fixups)
        self.results[case.key] = (state, reason)
        self.log(state, case.alias, note=reason)
        del self.pending[case.key]

    # -- queue --------------------------------------------------------------------------------
    async def run(self, queue: collections.deque) -> dict:
        self.queue, self.queue_closed, deferred, total = queue, False, set(), len(queue)
        same_reason = collections.Counter()
        collector = asyncio.ensure_future(self.collect_loop()) if self.collecting() else None
        try:
            while queue or self.pending:
                self.stop.check()
                if collector and collector.done():
                    collector.result()                    # a collector failure (e.g. reconnect budget) stops the run
                if not queue:                             # only answers are outstanding
                    await asyncio.sleep(0.3)
                    continue
                case = queue.popleft()
                self.storage_alive()
                nxt = queue[0] if queue else None
                prep = await self.take_prepared(case)
                if prep.blocked:
                    self.rec(case, "preflight_blocked", reason=prep.blocked)
                    self.log("preflight_blocked", case.alias, note=prep.blocked)
                    self.results[case.key] = ("preflight_blocked", prep.blocked)
                    self.start_prefetch(nxt)
                    continue
                self.log("start", case.alias, queue=f"{total - len(queue)}/{total}", attachments=len(prep.plan))
                if prep.left_out:
                    self.log("WARNING", case.alias, note=f"{len(prep.left_out)} file(s) will NOT be sent: the case has more files "
                                                          f"than the {self.cfg.max_attachments}-attachment limit (fixed files included)")
                elif len(prep.plan) >= int(self.cfg.max_attachments):
                    self.log("at-limit", case.alias, note=f"{len(prep.plan)}/{self.cfg.max_attachments} attachments "
                                                           "(fixed files included): this case uses the whole allowance")
                outcome, reason = "retry_pending", ""
                for _ in range(self.cfg.immediate_retries):
                    if self.attempts[case.key] >= self.cfg.max_attempts:
                        break
                    self.attempts[case.key] += 1
                    n = self.attempts[case.key]
                    route = "bulk" if n % 2 else "sequential"     # bulk first, sequential on the next fresh attempt
                    try:
                        async with self.fg_lock:                  # the sender owns the front tab for the whole attempt
                            outcome = await self.attempt(case, prep, n, route, nxt)
                        break
                    except AttemptFailed as exc:
                        reason = self.redact(str(exc))
                        self.rec(case, "retry_pending", reason=reason, attempt=n)
                        self.log("attempt-failed", case.alias, attempt=n, route=route, note=reason)
                        await self.stop.sleep(self.cfg.retry_backoff[min(n - 1, len(self.cfg.retry_backoff) - 1)])
                        prep = await self.prepare(case, want_tab=False)   # fresh manifest for the next attempt
                        if prep.blocked:
                            outcome, reason = "preflight_blocked", prep.blocked
                            break
                    except Blocked as exc:
                        outcome, reason = "preflight_blocked", str(exc)
                        break
                if outcome == "preflight_blocked":
                    self.rec(case, "preflight_blocked", reason=reason)
                elif outcome == "retry_pending":
                    if self.attempts[case.key] < self.cfg.max_attempts and case.key not in deferred:
                        deferred.add(case.key)
                        queue.append(case)                       # tail: first-attempt cases go first
                        total += 1
                        self.log("deferred", case.alias, note="moved to the end of the queue")
                        continue
                    outcome = "failed_exhausted"
                    self.rec(case, "failed_exhausted", reason=reason)
                else:
                    same_reason.clear()
                    if outcome == "submitted" and prep.left_out:
                        self.partial.add(case.key)
                        if not self.collecting():
                            outcome = "submitted_partial"
                            reason = f"{len(prep.left_out)} file(s) left out (attachment limit) - sent files only"
                if case.key in self.retry_tab and outcome != "retry_pending":   # finished: close its leftover unsent tab
                    await self._abandon(self.retry_tab.pop(case.key)[0])
                if not (outcome == "submitted" and self.collecting()):    # collector owns the result of a submitted case
                    self.results[case.key] = (outcome, reason)
                if outcome in ("failed_exhausted", "preflight_blocked") and reason:
                    same_reason[reason] += 1
                    if same_reason[reason] >= 3 and outcome == "failed_exhausted":  # no-progress guard
                        raise RecoveryRequired("three cases failed the same way - likely an environment problem; run paused")
                self.journal.snapshot()
        finally:
            self.queue_closed = True
            if collector:
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
        return self.results

    prompt_version = ""


# --------------------------------------------------------------------------------------------
# 10. COMMANDS
# --------------------------------------------------------------------------------------------

def prompt_version(cfg, message: str) -> str:
    stats = [os.stat(path) for path in (cfg.instructions_path, cfg.result_contract_path) if path]
    parts = "|".join(f"{st.st_size}|{st.st_mtime_ns}" for st in stats)
    return hashlib.sha256(f"{parts}|{message}".encode()).hexdigest()[:12]


def context(cfg, need_runtime: bool = True):
    """validate -> bootstrap storage (BEFORE Playwright) -> redactor/log.  Returns a namespace."""
    policy, problems = validate_config(cfg)
    red = Redactor()
    register_redactions(red, cfg, policy)
    if problems:
        raise SenderError("configuration problems:\n  - " + "\n  - ".join(red(p) for p in problems))
    rt = bootstrap_storage(cfg, policy)
    return types.SimpleNamespace(cfg=cfg, policy=policy, rt=rt, red=red,
                                 log=Log(rt, red, cfg.console_detail, cfg.console_show_ids))


def classify(cases, ctx, journal, listed):
    """Split the snapshot into (to_send, already_sent)."""
    todo, done = [], []
    for c in cases:
        (done if c.id in listed else todo).append(c)
    return todo, done


def check_command(args) -> int:
    cfg = load_config()
    ok = True

    def line(name, good, note=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'ok' if good else 'FAIL'}] {name}{(' - ' + note) if note else ''}")

    policy, problems = validate_config(cfg)
    red = Redactor()
    register_redactions(red, cfg, policy)
    print("CHECK (nothing is sent)")
    line("configuration and paths", not problems, red("; ".join(problems)))
    if problems:
        return 2
    print(f"  storage mapping: {policy.mapping_source} (approved drive/share treated as one storage)")
    ctx_ = context(cfg)
    rt = ctx_.rt
    line("runtime write + replace + read-back", not probe_dir(rt.state))
    line("cases root listable", bool(os.listdir(cfg.cases_root) is not None))
    line("base_message non-empty UTF-8", bool(load_message(Path(cfg.base_message_path))))
    line("temp redirected", os.path.normcase(tempfile.gettempdir()) == os.path.normcase(str(rt.temp)))
    bits = 8 * ctypes.sizeof(ctypes.c_void_p)
    print(f"  python {sys.version.split()[0]} {bits}-bit")
    pth = list(Path(sys.executable).parent.glob("python*._pth"))
    if pth:
        txt = pth[0].read_text()
        site_on = bool(re.search(r"^\s*import\s+site", txt, re.M))
        line("embedded ._pth inspected (left unchanged)", True, "site imports ENABLED - user/site packages visible" if site_on else "site disabled")
    same = os.path.normcase(os.path.realpath(sys.executable)) == os.path.normcase(os.path.realpath(cfg.python_executable))
    line("running under the configured python", same)
    try:
        ensure_dependencies(cfg, rt, ctx_.log)
        line(f"playwright {playwright_version()}", True)
        from playwright._impl._driver import compute_driver_executable
        node = str(compute_driver_executable()[0])
        line("node for Playwright driver", Path(node).is_file(), "configured" if cfg.node_executable else "bundled")

        async def driver():
            api = load_playwright()[0]
            pw = await api().start()
            await pw.stop()
        asyncio.run(driver())
        line("Playwright driver starts", True)
    except Exception as exc:
        line("dependencies / driver", False, safe_exc(ctx_.red, exc))
    line("edge executable", Path(cfg.edge_executable).is_file())
    if args.browser:
        line("browser connect + renderer", asyncio.run(browser_probe(ctx_)))
    print("\nAll checks passed." if ok else "\nSome checks failed.")
    return 0 if ok else 1


async def browser_probe(ctx) -> bool:
    stop = Stopper()
    browser = EdgeBrowser(ctx.cfg, ctx.rt, ctx.policy, ctx.log, stop)
    try:
        await browser.start(launch=True)
        tab = await browser.open_tab()
        await tab.snapshot()
        await tab.close()
        return True
    except Exception as exc:
        ctx.log("probe", note=safe_exc(ctx.red, exc))
        return False
    finally:
        await browser.close()


def plan_command(args) -> int:
    ctx = context(load_config())
    journal = Journal(ctx.rt, ctx.cfg.persist_retries)
    try:
        ledger = Ledger(ctx.rt.ledger, ctx.cfg.persist_retries)
        listed = sync_ledger(journal, ledger, ctx.log)
        cases, other, excluded = discover(ctx.cfg, ctx.policy, local_salt(ctx.rt), ctx.red)
        instr = [doc_of(ctx.policy.check(ctx.cfg.instructions_path, "review guidance"))]
        if ctx.cfg.completion_mode == "json_result":
            instr.append(doc_of(ctx.policy.check(ctx.cfg.result_contract_path, "result contract")))
        print(f"{len(cases)} case folder(s), {excluded} excluded browser-profile folder(s), "
              f"{len(other)} unrecognised folder(s): {', '.join(other) or '-'}")
        for c in cases:
            status = "already sent" if c.id in listed else "to send"
            note = c.blocked
            if not c.blocked and c.id not in listed:
                inventory(c, ctx.cfg, ctx.policy)
                note = "; ".join(c.problems)
                if not note:
                    try:
                        plan, left = build_plan(c, instr, ctx.cfg, ctx.policy)
                        note = f"{len(plan)} attachments" + (f"  WARNING: {len(left)} file(s) left out (over limit)" if left else "")
                    except Blocked as exc:
                        note = str(exc)
            print(f"  {c.alias}  {status:<13} {ctx.red(note)}")
    finally:
        journal.close()
    return 0


async def reconcile_open_intents(runner: Runner, journal: Journal, cases_by_key: dict, log) -> None:
    """Resume rule: a previous run died between send_intent and a recorded outcome.  Look at the SAME
    tab (exact target id).  Never resend.  Unprovable = submission_uncertain."""
    for key, rec in list(journal.cases.items()):
        if rec.get("state") == "send_intent":
            case = cases_by_key.get(key) or Case(rec.get("alias", key), Path("."), "", "")
            case.key, case.alias, case.id = key, rec.get("alias", ""), rec.get("instruction_id", "")
            tab = None
            if runner.browser.context is not None and rec.get("target_id"):
                tab = await runner.browser.find_tab(rec["target_id"])
            if tab is None:
                runner.finish_uncertain(case, "original tab not found after an interrupted send")
                continue
            ev = await runner.reconcile(case, tab, runner.cfg.reconcile_seconds)
            if isinstance(ev, str):
                runner.finish_uncertain(case, ev)
            else:
                runner.rec(case, "submitted", evidence={"user_message": True, "recovered": True})
                runner.mark_sent(case, "submitted")
                log("recovered", case.alias, note="earlier send confirmed on reconcile")
        elif rec.get("state") in ("preparing", "uploading", "upload_ready"):
            journal.record(key, "retry_pending", alias=rec.get("alias", ""), note="run ended before any send")


class StdinLines:
    """One background reader for the terminal, shared by every prompt (and usable as the 'press Enter' fallback)."""

    def __init__(self):
        self.loop, self.q, self.eof = asyncio.get_running_loop(), asyncio.Queue(), False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in sys.stdin:
                self.loop.call_soon_threadsafe(self.q.put_nowait, line.rstrip("\r\n"))
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self._end)

    def _end(self):
        self.eof = True
        self.q.put_nowait(None)

    def drain(self):
        while not self.q.empty() and not self.eof:
            self.q.get_nowait()

    async def line(self):
        """Next typed line; None when there is no terminal input (redirected/closed)."""
        if self.eof and self.q.empty():
            return None
        return await self.q.get()


async def wait_for_login(browser, stdin, log):
    """Poll (fast, no overall limit) until a Copilot tab shows the composer + model button = signed in.
    The moment that happens the run moves on by itself; pressing Enter is only a manual fallback."""
    enter, announced, t0 = asyncio.ensure_future(stdin.line()), False, time.monotonic()
    try:
        while True:
            browser.stop.check()
            tab = await browser.find_ready_tab()
            if tab:
                await tab.adopt()
                stdin.drain()       # an early Enter must not answer the model question
                return tab
            if enter.done() and enter.result() is not None:
                log("login", note="continuing on your confirmation")
                tab = await browser.open_tab()   # waits for the composer like any new tab
                stdin.drain()
                return tab
            if enter.done():
                enter = asyncio.ensure_future(asyncio.sleep(3600))   # no terminal input available: auto-detect only
            if not announced and time.monotonic() - t0 > 1.0:
                print("Waiting for Copilot sign-in: sign in in the Edge window - I continue by myself as soon as the chat "
                      "appears (or press Enter here to continue manually).", flush=True)
                announced = True
            await asyncio.sleep(0.25)
    finally:
        enter.cancel()


def pick_default(entries):
    """Default model: the LAST one listed under GPT (models change fast, so no version-number parsing)."""
    usable = [e for e in entries if not e.get("disabled")]          # a greyed-out model cannot be selected
    gpt = [e for e in usable if re.search(r"gpt|openai", e["group"], re.I)]
    pool = gpt or [e for e in usable if e["group"] != "Modes"] or usable
    return pool[-1] if pool else None


def model_menu(entries, default) -> str:
    lines, group = [], None
    for i, e in enumerate(entries, 1):
        if e["group"] != group:
            group = e["group"]
            lines.append(f"  {group}")
        lines.append(f"   {'*' if e is default else ' '} {i:>2}. {e['label']}{'  (unavailable now)' if e.get('disabled') else ''}")
    return "\n".join(lines)


async def choose_model(entries, default, cfg, stdin, log):
    """Ask which model to use (Enter = default).  `requested_model` in the settings skips the question."""
    if not entries:      # never reached from prepare_session (it retries until a list is read); no silent fallback
        raise RecoveryRequired("the model list could not be read - run again so you can choose a model")
    if cfg.requested_model:
        want = cfg.requested_model.casefold()
        for e in entries:
            if want in (e["label"].casefold(), e["text"].casefold()):
                if e.get("disabled"):
                    raise Blocked("requested_model is currently unavailable in Copilot")
                return e
        raise Blocked("requested_model is not offered by this Copilot experience")
    print("\nAvailable models (* = default, the last available GPT entry):\n" + model_menu(entries, default), flush=True)
    while True:
        print(f"Model number for this run (Enter = {default['label']}): ", end="", flush=True)
        try:
            line = await asyncio.wait_for(stdin.line(), cfg.model_prompt_seconds or None)
        except asyncio.TimeoutError:
            line = None
        if line is None or not line.strip():
            print(flush=True)
            return default
        if line.strip().isdigit() and 1 <= int(line) <= len(entries):
            if entries[int(line) - 1].get("disabled"):
                print("  That model is greyed out in Copilot right now - pick another one.", flush=True)
                continue
            return entries[int(line) - 1]
        print("  Please type a number from the list, or press Enter for the default.", flush=True)


async def run_async(ctx, journal, ledger, listed, cases, todo, instr, message) -> int:
    cfg, log = ctx.cfg, ctx.log
    stop = Stopper()
    browser = EdgeBrowser(cfg, ctx.rt, ctx.policy, log, stop)
    runner = Runner(cfg, ctx.rt, ctx.policy, journal, ledger, log, stop, browser, ctx.red, message, instr)
    runner.prompt_version = prompt_version(cfg, message)
    by_key = {c.key: c for c in cases}
    code, results = 0, {}
    try:
        if any(r.get("state") == "send_intent" for r in journal.cases.values()):
            try:  # look at the existing browser only; never start a new one just to reconcile
                await browser.start(launch=False)
            except ConnectionLost:
                browser.context = None
            await reconcile_open_intents(runner, journal, by_key, log)
        resumable = cfg.completion_mode == "json_result" and any(
            r.get("contract") and r.get("state") in ("submitted", "fixup_sent") and k in by_key for k, r in journal.cases.items())
        if (todo or resumable) and browser.context is None:
            await browser.start(launch=True)
        todo = [c for c in todo if runner.sent_ids.get(c.id) is None]
        if todo:
            await prepare_session(runner, browser, cfg, log, todo)
        if resumable:
            await runner.resume(by_key)
        if todo or runner.pending:
            await runner.run(collections.deque(todo))
        results = runner.results
    except Stopped:
        journal.record("_run", "cancelled", note="stopped by user")
        log("stopped", note="state preserved; Edge left open")
        code = 130
    except RecoveryRequired as exc:
        journal.record("_run", "recovery_required", note=ctx.red(str(exc)))
        log("paused", note=str(exc))
        code = 4
    finally:
        await runner.discard_prefetch()
        await browser.close()
    results = runner.results
    summarise(ctx, journal, cases, listed, results)
    ok = ("submitted", "result_correct", "result_incorrect", "result_inconclusive")   # a valid verdict is a finished case
    unresolved = [k for k, (s, _) in results.items() if s not in ok or k in runner.partial]
    return code or (1 if unresolved else 0)


async def discover_models_anywhere(browser, first, redact, log):
    """Read the model list from ANY signed-in Copilot tab, as long as it takes.  Tabs load at different speeds, so the
    tab found first may not show its picker yet: keep trying it and every other ready tab (re-scanned each round).
    There is deliberately no fallback - the user always chooses the model - so only a stop request ends this.
    Returns (tab, entries); that tab becomes the session's first tab."""
    announced, tried_note, candidates = False, None, [first]      # the tab that proved the sign-in is tried first
    while True:
        browser.stop.check()
        for tab in candidates:
            browser.stop.check()
            try:
                entries = await tab.discover_models()
                if entries:
                    if tab is not first:
                        await tab.adopt()
                        log("model", note="model list read from another Copilot tab")
                    return tab, entries
            except SenderError as exc:
                note = f"model discovery failed in a tab: {safe_exc(redact, exc)}"
                if note != tried_note:       # one log line per distinct problem, not one per retry
                    log("model", note=note)
                    tried_note = note
        if not announced:
            print("Waiting for the Copilot model list - trying every signed-in Copilot tab until one shows it "
                  "(Ctrl+C to stop).", flush=True)
            announced = True
        await browser.stop.sleep(1.0)
        candidates = [first] + [t for t in await browser.find_ready_tabs() if t.page is not first.page]   # others may be ready now


async def existing_tabs(browser, first, wanted, log) -> list:
    """Up to `wanted` more signed-in Copilot tabs that were already open in a joined Edge (no tab is opened)."""
    if wanted < 1 or not browser.reused:
        return []
    tabs = []
    for tab in await browser.find_ready_tabs():
        if len(tabs) >= wanted:
            break
        if tab.page is first.page:
            continue
        try:
            await tab.adopt()           # same tab, fresh chat if it was not already one
        except SenderError:
            continue
        tabs.append(tab)
    log("session", note=f"using {len(tabs) + 1} Copilot tab(s) that were already open")
    return tabs


async def prepare_session(runner, browser, cfg, log, todo):
    """Sign-in -> model list -> (warm-up of up to `warm_tabs` tabs runs while you choose) -> model applied."""
    stdin = StdinLines()
    first = await wait_for_login(browser, stdin, log)
    log("login", note="Copilot is signed in")
    first, entries = await discover_models_anywhere(browser, first, runner.redact, log)
    count = max(1, min(int(cfg.warm_tabs), len(todo)))
    # Joined an already-open Edge: its prepared tabs are the pool, nothing is opened.  Otherwise warm tabs while you choose.
    warm = asyncio.ensure_future(browser.open_tabs(count - 1)) if count > 1 and not browser.reused else None
    try:
        runner.model = await choose_model(entries, pick_default(entries), cfg, stdin, log)
        tabs = [first] + (await warm if warm else await existing_tabs(browser, first, count - 1, log))
    except BaseException:
        if warm:
            warm.cancel()
        raise
    runner.pool.extend((t, browser.generation) for t in tabs)
    await first.page.bring_to_front()
    for tab in tabs[1:]:      # every other tab applies the chosen model in the background, in parallel
        tab.model_task = asyncio.ensure_future(tab.ensure_model(runner.model))
    log("session", note=f"{len(tabs)} tab(s) ready" + (f", model: {runner.model['label']}" if runner.model else ""))


def summarise(ctx, journal, cases, listed, results):
    rows, counts = [], collections.Counter()
    for c in cases:
        rec = journal.cases.get(c.key, {})
        state = "skipped_already_sent" if c.id in listed and c.key not in results else rec.get("state", "queued")
        reason = rec.get("reason", c.blocked)
        if c.key in results:  # this run's outcome wins over anything older in the journal
            state, reason = results[c.key]
        counts[state] += 1
        rows.append({"case": c.alias, "state": state, "reason": ctx.red(reason)})
    summary = {"finished": now(), "counts": dict(counts), "cases": rows}
    print("\nSUMMARY  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for r in rows:
        if r["state"] not in ("submitted", "skipped_already_sent", "result_correct", "result_incorrect", "result_inconclusive"):
            print(f"  {r['case']}  {r['state']}  {r['reason']}")
    try:
        write_atomic(ctx.rt.logs / f"summary-{datetime.datetime.now():%Y%m%d-%H%M%S}.json", json.dumps(summary, indent=1))
    except PersistenceError:
        pass
    if any(s in ("submission_uncertain", "failed_exhausted") for s in counts):
        print("Unresolved cases are NOT successes: check the Copilot chats; uncertain ones are never resent.")


def run_command(args) -> int:
    cfg = load_config()
    if getattr(args, "detail", None):
        cfg.console_detail = args.detail
    ctx = context(cfg)
    ensure_dependencies(cfg, ctx.rt, ctx.log)           # first launch only; never inside the case loop
    message = load_message(Path(cfg.base_message_path))
    journal = Journal(ctx.rt, cfg.persist_retries)
    try:
        ledger = Ledger(ctx.rt.ledger, cfg.persist_retries)
        listed = sync_ledger(journal, ledger, ctx.log)
        cases, other, excluded = discover(cfg, ctx.policy, local_salt(ctx.rt), ctx.red)
        instr = [doc_of(ctx.policy.check(cfg.instructions_path, "review guidance"))]
        if cfg.completion_mode == "json_result":
            instr.append(doc_of(ctx.policy.check(cfg.result_contract_path, "result contract")))
        todo, done = classify([c for c in cases if not c.blocked], ctx, journal, listed)
        for c in cases:
            if c.blocked:
                journal.record(c.key, "preflight_blocked", alias=c.alias, instruction_id=c.id, reason=c.blocked)
        ctx.log("queue", note=f"{len(todo)} instruction(s) queued; {len(done)} already sent, {excluded} excluded, "
                              f"{len(other)} unrecognised, {sum(1 for c in cases if c.blocked)} blocked")
        return asyncio.run(run_async(ctx, journal, ledger, listed, cases, todo, instr, message))
    finally:
        journal.close()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="Send instruction-ID case folders to Microsoft 365 Copilot.")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("setup", help="paste/edit your paths and options")
    c = sub.add_parser("check", help="validate everything without sending")
    c.add_argument("--browser", action="store_true", help="also launch/connect Edge and test a page")
    sub.add_parser("plan", help="dry run: show the queue and any blocked cases")
    r = sub.add_parser("run", help="send the queue (default)")
    r.add_argument("--detail", choices=("minimal", "full"), help="console detail for this run (overrides the setting)")
    args = p.parse_args(argv)
    command = args.command or "run"
    try:
        if command == "setup":
            return setup_command()
        if command == "run" and not config_path().exists():
            print("No configuration yet - starting setup first.\n")
            if setup_command():
                return 2
        return {"check": check_command, "plan": plan_command, "run": run_command}[command](args)
    except StorageError as exc:
        print(f"STORAGE PROBLEM: {exc}. Nothing was written elsewhere; browser state was left as is.")
        return 3
    except PersistenceError as exc:
        print(f"STATE COULD NOT BE SAVED: {exc}. Stopped before any further send.")
        return 3
    except SenderError as exc:
        print(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        print("Aborted. Journal state is preserved; Edge was left open.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
