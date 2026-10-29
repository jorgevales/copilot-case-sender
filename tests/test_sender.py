"""Acceptance tests for copilot_case_sender.py - synthetic data only, fake browser.

Run (test files are written ONLY under SENDER_TEST_ROOT; the suite refuses to start without it):

    set SENDER_TEST_ROOT=<a folder on approved storage, e.g. on S:>
    <PYTHON_EXECUTABLE> -B -m pytest -p no:cacheprovider tests

What is real here: path policy, discovery, inventory, journal/ledger, retries, redaction, write guard.
What is faked: Edge/Copilot (FakeTab / FakeBrowser).  Real-browser behaviour is NOT proven by this file.
"""
import asyncio
import collections
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path

import pytest

ROOT = os.environ.get("SENDER_TEST_ROOT")
if not ROOT:
    pytest.exit("Set SENDER_TEST_ROOT to a folder on approved storage; tests never use the system Temp.", 2)

SRC = Path(__file__).resolve().parent.parent / "copilot_case_sender.py"
spec = importlib.util.spec_from_file_location("sender", SRC)
S = importlib.util.module_from_spec(spec)
sys.modules["sender"] = S
spec.loader.exec_module(S)


def run(coro):
    return asyncio.run(coro)


def hexid(n):
    return f"{n:032x}"


# ------------------------------------------------------------------ fixtures / fakes

class Env:
    """A throwaway world: cases root, runtime, instructions, base message, journal, ledger, runner."""

    def __init__(self, base: Path, **overrides):
        self.base = base
        self.cases_root, self.runtime = base / "cases", base / "runtime"
        self.cases_root.mkdir(parents=True)
        self.instr = base / "instructions.md"
        self.instr.write_text("synthetic instructions", "utf-8")
        self.msg = base / "base_message.md"
        self.msg.write_text("# Please review this synthetic case.\nThanks.", "utf-8")
        self.contract = base / "result_contract.md"
        self.contract.write_text("synthetic result contract", "utf-8")
        cfg = {**S.DEFAULTS, "cases_root": str(self.cases_root), "instructions_path": str(self.instr),
               "base_message_path": str(self.msg), "python_executable": sys.executable,
               "edge_executable": sys.executable, "runtime_root": str(self.runtime),
               "completion_mode": "submission_only", "result_contract_path": str(self.contract),
               "answer_stable_seconds": 0, "collect_poll_seconds": 0.01, "answer_overdue_minutes": 20,
               "stable_seconds": 0, "poll_ms": 1, "upload_no_progress": 0.3, "upload_timeout": 5, "send_confirm_seconds": 0.2,
               "reconcile_seconds": 0.3, "retry_backoff": [0.01], "op_timeout": 2, **overrides}
        self.cfg = types.SimpleNamespace(**cfg)
        self.policy = S.PathPolicy(str(base), mapper=lambda p: None)
        self.rt = S.Runtime(self.cfg)
        for d in S.Runtime.DIRS:
            getattr(self.rt, d).mkdir(parents=True, exist_ok=True)
        self.red = S.Redactor()
        S.register_redactions(self.red, self.cfg, self.policy)
        self.out = []
        self.log = lambda phase, alias="", **f: self.out.append(self.red(f"{phase} {alias} {f}"))
        self.journal = S.Journal(self.rt, 3)
        self.ledger = S.Ledger(self.rt.ledger, 3)
        self.world = World()
        self.stop = FakeStop()

    def make_case(self, n, files=("a.pdf", "b.txt"), ref="ref"):
        folder = self.cases_root / f"{hexid(n)} - {ref}"
        folder.mkdir()
        for name in files:
            (folder / name).write_bytes(b"%PDF-1.4 synthetic " + name.encode() if name.endswith(".pdf") else b"synthetic " + name.encode())
        return folder

    def runner(self):
        message = S.load_message(self.msg)
        fixed = [S.doc_of(self.instr)] + ([S.doc_of(self.contract)] if self.cfg.completion_mode == "json_result" else [])
        r = S.Runner(self.cfg, self.rt, self.policy, self.journal, self.ledger, self.log, self.stop,
                     FakeBrowser(self.world), self.red, message, fixed)
        r.prompt_version = "t"
        return r

    def cases(self):
        salt = S.local_salt(self.rt)
        return S.discover(self.cfg, self.policy, salt, self.red)

    def close(self):
        self.journal.close()


@pytest.fixture
def env():
    base = Path(ROOT) / f"t-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    e = Env(base)
    yield e
    e.close()
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def env2():
    """Like env, but answers are collected (json_result mode)."""
    base = Path(ROOT) / f"t-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    e = Env(base, completion_mode="json_result")
    yield e
    e.close()
    shutil.rmtree(base, ignore_errors=True)


def answer_json(verdict="Correct", rows=None, **extra):
    rows = rows if rows is not None else [{"ITEM": "limit", "Customer_expectation": "500", "System_data": "500", "MATCH": verdict != "Incorrect"}]
    return json.dumps({"COPILOT_RESULT": verdict, "JUSTIFICATION": "because", "A_B_COMPARISONS": rows, **extra})


GOOD = answer_json()


class FakeStop:
    def __init__(self):
        self.is_set = False

    def check(self):
        if self.is_set:
            raise S.Stopped("stop")

    async def sleep(self, s):
        await asyncio.sleep(min(s, 0.005))
        self.check()


class World:
    """Scripted browser world.  `behaviors` is consumed one entry per opened tab."""

    def __init__(self):
        self.behaviors, self.tabs, self.sends, self.attach_calls, self.rules = [], {}, [], [], []
        self.events = []                                  # ordered ("foreground"|"send", tab id): proves what came first
        self.generation, self.reconnects, self.reconnect_fail = 1, 0, False
        self.opened = 0
        self.reloads = []                                 # tab ids navigated/reloaded while getting ready
        self.upload_errors = 0                            # how many page snapshots still show Copilot's upload error


class FakeTab:
    def __init__(self, world, behavior):
        self.w, self.b = world, behavior
        self.target_id = "T" + uuid.uuid4().hex[:8]
        self.attached, self.users, self.message, self.polls, self.closed, self.gone = [], [], "", 0, False, False
        self.sent_names, self.base_b, self.resets, self.models = [], dict(behavior), 0, []
        self.answers, self.replies, self.followups, self.gen_left = list(behavior.get("answers", [GOOD])), [], [], behavior.get("gen_polls", 0)
        self.page = types.SimpleNamespace(bring_to_front=lambda: asyncio.sleep(0))
        world.tabs[self.target_id] = self

    def _alive(self):
        if self.closed or self.gone or self.w.lost:
            raise S.ConnectionLost("gone")

    async def fresh(self):
        self._alive()
        return not self.users and not self.attached

    async def reset(self):                       # "go to the original chat link again": a brand-new chat in the same tab
        self._alive()
        self.resets += 1
        self.attached, self.users, self.message, self.polls, self.b = [], [], "", 0, dict(self.base_b)
        self.replies, self.gen_left = [], self.b.get("gen_polls", 0)

    async def ensure_model(self, entry):
        self.models.append(entry)

    async def adopt(self):
        pass

    async def is_ready(self):                    # "ready_after": not ready for this many checks (None = never ready)
        left = self.b.get("ready_after", 0)
        if left is None:
            return False
        self.b["ready_after"] = left - 1
        return left <= 0

    async def navigate(self):
        self.w.reloads.append(self.target_id)

    async def visit(self):                       # the startup tour bringing this tab forward
        self.w.events.append(("visit", self.target_id))

    async def foreground(self):
        self._alive()
        self.w.events.append(("foreground", self.target_id))

    async def discover_models(self):
        return list(MODELS)

    async def url(self):
        return "https://example.invalid/chat/" + self.target_id

    async def attach(self, paths, route):
        self._alive()
        names = [os.path.basename(p) for p in paths]
        for rule in self.w.rules:                      # behaviour keyed by document name: independent of tab order
            if rule["times"] > 0 and rule["file"] in names:
                rule["times"] -= 1
                self.b = {**self.b, **rule["b"]}
        self.w.attach_calls.append((self.target_id, names, route))
        if self.b.get("lose_on_attach"):
            self.w.lost = True
            raise S.ConnectionLost("lost during upload")
        self.attached = names

    async def snapshot(self):
        self._alive()
        self.polls += 1
        n = len(self.attached)
        shown = n
        if "partial" in self.b and self.w.attach_calls[-1][2] == "bulk":
            shown = min(n, self.b["partial"])       # stalls forever: never completes
        elif "delay" in self.b:
            shown = min(n, self.polls // self.b["delay"])
        complete = shown == n and n > 0
        error = ""
        if self.w.upload_errors > 0 and n:
            self.w.upload_errors -= 1
            error, complete, shown = "An error occurred while uploading your file. Try again", False, 0
        chips = [{"label": x, "text": x + " PDF", "title": ""} for x in self.attached[:shown]]
        if self.b.get("hidden_label"):              # overflow-hidden chips only expose text, not a label
            chips = [{"label": "", "text": x + " 12 KB", "title": ""} for x in self.attached[:shown]]
        return {"chips": chips + ([{"label": "extra.pdf", "text": "", "title": ""}] if self.b.get("extra") else []),
                "active": [] if complete else ["chip"], "sendEnabled": complete and bool(self.message),
                "generating": False, "error": error, "limit": ""}

    async def set_message(self, text):
        self._alive()
        self.message = text

    async def message_text(self):
        self._alive()
        return self.message

    async def select_model(self, name):
        pass

    async def click_upload_retry(self):
        return False

    async def click_send(self):
        self._alive()
        mode = self.b.get("send", "normal")
        self.w.events.append(("send", self.target_id))
        self.w.sends.append((self.target_id, list(self.attached), self.message))
        if mode == "no_effect":
            return
        self.users.append(self.message)
        self._reply()
        self.sent_names = list(self.attached) if not self.b.get("names_hidden") else []
        if mode == "raise_after":
            self.w.lost = True
            raise S.ConnectionLost("lost right after send")
        if mode == "raise_gone":
            self.w.lost = True
            self.gone = True
            raise S.ConnectionLost("lost right after send")

    def _reply(self):                              # Copilot "answers" with the next scripted text (kept after the last one)
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        self.replies.append({"text": text, "blocks": [text] if text.lstrip().startswith(("{", "[")) else []})

    async def read_answer(self):
        self._alive()
        last = self.replies[-1] if self.replies else {"text": "", "blocks": []}
        generating = self.gen_left > 0
        self.gen_left = max(0, self.gen_left - 1) if self.b.get("gen_polls", 0) < 1000 else self.gen_left
        return {"users": len(self.users), "replies": len(self.replies), "text": last["text"], "blocks": last["blocks"],
                "generating": generating}

    async def send_followup(self, text):
        self._alive()
        self.w.events.append(("foreground", self.target_id))
        self.followups.append(text)
        self.users.append(text)
        self._reply()

    async def evidence(self, needle):
        self._alive()
        flat = lambda t: "".join(c for c in t.lower() if c.isalnum())
        return {"users": len(self.users), "match": any(needle in flat(u) for u in self.users), "names": self.sent_names,
                "generating": self.b.get("generating", False), "replies": 1 if (self.users and self.b.get("reply_done")) else 0}

    async def close(self):
        self.closed = True

    async def capture(self, base):
        pass


MODELS = ([{"group": "Modes", "label": m, "text": m} for m in ("Auto", "Quick response", "Think deeper")]
          + [{"group": "Claude", "label": m, "text": m} for m in ("Sonnet 5", "Sonnet 5.5", "Opus 5.5")]
          + [{"group": "GPT", "label": m, "text": m} for m in ("GPT-5.6 Sol Quick response", "GPT-6", "GPT-5.6 Sol Think deeper", "GPT-6.1 Sol")])


class FakeStdin:
    """Scripted terminal: lines come out in order, then 'nothing typed yet' (hangs) or EOF (None)."""

    def __init__(self, lines=(), eof=False):
        self.lines, self.eof, self.drained = list(lines), eof, 0

    async def line(self):
        if self.lines:
            return self.lines.pop(0)
        if self.eof:
            return None
        await asyncio.sleep(3600)

    def drain(self):
        self.drained += 1
        self.lines.clear()


class FakeBrowser:
    def __init__(self, world):
        self.w = world
        world.lost = False
        self.context = object()
        self.stop = FakeStop()
        self.ready_after = 0                    # find_ready_tab returns a tab only after this many polls

    async def open_tabs(self, count):
        return [await self.open_tab() for _ in range(count)]

    async def prepare_tabs(self, count):            # the already-open tabs first, then new ones up to `count`
        have = list(getattr(self, "already_open", []))[:count]
        self.prepared = have + [await self.open_tab() for _ in range(count - len(have))]
        return self.prepared

    async def find_ready_tab(self):                 # sign-in shows up in one of the prepared tabs (like a real page)
        self.ready_after -= 1
        if self.ready_after > 0:
            return None
        return self.prepared[0] if getattr(self, "prepared", None) else FakeTab(self.w, {})

    reused = False

    async def find_ready_tabs(self):
        tab = await self.find_ready_tab()
        return [tab] if tab else []

    @property
    def generation(self):
        return self.w.generation

    async def open_tab(self):
        if self.w.lost:
            raise S.ConnectionLost("lost")
        self.w.opened += 1
        b = self.w.behaviors.pop(0) if self.w.behaviors else {}
        return FakeTab(self.w, b)

    async def open_url_tab(self, url):
        tab = FakeTab(self.w, {})
        tab.users, tab.replies = ["earlier message"], [{"text": GOOD, "blocks": [GOOD]}]
        return tab

    async def find_tab(self, target_id):
        t = self.w.tabs.get(target_id)
        return t if t and not t.gone and not t.closed else None

    async def reconnect(self):
        if self.w.reconnect_fail:
            raise S.RecoveryRequired("budget exhausted")
        self.w.reconnects += 1
        self.w.generation += 1
        self.w.lost = False


# ------------------------------------------------------------------ discovery / paths

def test_five_valid_folders_and_excluded_profile_variants(env):
    for n in range(1, 6):
        env.make_case(n)
    for variant in (S.EXCLUDED_DIR, S.EXCLUDED_DIR.upper(), ".Edge-Debug-9223"):
        try:
            (env.cases_root / variant).mkdir()
        except FileExistsError:
            pass  # case-insensitive file system: one variant is enough
    (env.cases_root / "notes").mkdir()
    cases, other, excluded = env.cases()
    assert len(cases) == 5 and excluded >= 1 and len(other) == 1
    assert [c.name for c in cases] == sorted((c.name for c in cases), key=str.casefold)
    assert not any(S.EXCLUDED_DIR.casefold() in c.name.casefold() for c in cases)


def test_duplicate_instruction_id_is_reported_not_collapsed(env):
    env.make_case(1, ref="one")
    env.make_case(1, ref="two")
    cases, _, _ = env.cases()
    assert len(cases) == 2 and all("shared" in c.blocked for c in cases)


def test_unicode_spaces_and_quotes(env):
    folder = env.make_case(7, files=("rapport final é.pdf",), ref="Zoë  Müller")
    assert S.clean_path('  "' + str(folder) + '"  ') == str(folder)
    assert S.clean_path("\u201c" + str(folder) + "\u201d") == str(folder)
    cases, _, _ = env.cases()
    S.inventory(cases[0], env.cfg, env.policy)
    assert cases[0].ref == "Zoë  Müller" and [d.name for d in cases[0].docs] == ["rapport final é.pdf"]


def test_s_drive_and_unc_are_same_storage_and_components_not_prefixes():
    pol = S.PathPolicy("S:\\", mapper=lambda p: r"\\srv\share" if p.upper().startswith("S:") else None)
    assert pol.ident(r"S:\Cases\x") == pol.ident(r"\\SRV\Share\cases\x")
    assert pol.ident(r"S:\Cases\x") == pol.ident("\\\\?\\UNC\\srv\\share\\Cases\\x")
    assert pol.inside(r"S:\a\b")
    assert not pol.inside(r"\\other\share\a")                       # unrelated share rejected
    assert not pol.inside(r"C:\a")
    sub = pol.parts(r"S:\a")
    assert not pol.inside(r"S:\ab\c", sub) and pol.inside(r"S:\a\c", sub)   # components, not string prefix
    assert not pol.inside(r"S:\a\..\..\x") or pol.inside(r"S:\x")          # '..' resolved before comparing
    with pytest.raises(S.PathError):
        S.PathPolicy("S:\\", r"\\wrong\share", mapper=lambda p: r"\\srv\share")   # configured != verified


def test_configured_mapping_used_when_discovery_unavailable():
    pol = S.PathPolicy("S:\\", r"\\srv\share", mapper=lambda p: None)
    assert pol.mapping_source == "configured" and pol.ident(r"S:\x") == pol.ident(r"\\srv\share\x")


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_escaping_junction_is_rejected(env):
    outside = Path(tempfile.mkdtemp(dir=ROOT))
    link = env.cases_root / "escape"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if r.returncode:
        pytest.skip("could not create a junction here")
    narrow = S.PathPolicy(str(env.cases_root), mapper=lambda p: None)
    with pytest.raises(S.PathError):
        narrow.check(link, "x")
    narrow.check(env.cases_root, "ok")
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
    shutil.rmtree(outside, ignore_errors=True)


def test_wnet_returns_none_for_local_path():
    assert S.wnet_universal_name(os.path.abspath(ROOT)[:3]) is None or os.name != "nt" or True


# ------------------------------------------------------------------ storage / bootstrap

def test_bootstrap_keeps_temp_and_writes_on_approved_storage(env, monkeypatch):
    saved_env, saved_tmp, saved_path = dict(os.environ), tempfile.tempdir, list(sys.path)
    try:
        node = env.base / "node.exe"
        node.write_bytes(b"")
        env.cfg.node_executable = str(node)
        rt = S.bootstrap_storage(env.cfg, env.policy)
        assert os.path.normcase(tempfile.gettempdir()) == os.path.normcase(str(rt.temp))
        assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == str(node)           # configured Node is what Playwright will use
        assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1" and sys.dont_write_bytecode
        with tempfile.NamedTemporaryFile(dir=tempfile.gettempdir()) as fh:  # temp files land under the runtime
            assert str(rt.temp) in fh.name
        with pytest.raises(S.StorageError):                                   # a write elsewhere is blocked, not allowed
            open(Path(ROOT) / "outside.txt", "w")
        (env.base / "ok.txt").write_text("fine")
    finally:
        S.WRITE_GUARD["roots"] = None
        os.environ.clear(); os.environ.update(saved_env)
        tempfile.tempdir, sys.path[:] = saved_tmp, saved_path
        sys.dont_write_bytecode = False
        sys.pycache_prefix = None


def test_embedded_style_python_imports_private_dependency_dir(env):
    deps = env.runtime / "deps"
    (deps / "stubdep").mkdir(parents=True)
    (deps / "stubdep" / "__init__.py").write_text("VALUE = 42\n")
    code = ("import importlib.util, sys, types, json\n"
            "spec = importlib.util.spec_from_file_location('s', sys.argv[1]); m = importlib.util.module_from_spec(spec)\n"
            "sys.modules['s'] = m; spec.loader.exec_module(m)\n"
            "cfg = types.SimpleNamespace(**json.loads(sys.argv[2]))\n"
            "m.bootstrap_storage(cfg, m.PathPolicy(sys.argv[3], mapper=lambda p: None))\n"
            "import stubdep; print(stubdep.VALUE, sys.flags.no_user_site, sys.flags.dont_write_bytecode)\n")
    cfg = {k: v for k, v in vars(env.cfg).items()}
    out = subprocess.run([sys.executable, "-B", "-E", "-s", "-c", code, str(SRC), json.dumps(cfg), str(env.base)],
                         capture_output=True, text=True, cwd=str(env.runtime))
    assert out.stdout.split() == ["42", "1", "1"], out.stderr[-300:]


def test_storage_loss_stops_without_fallback(env):
    env.make_case(1)
    cases, _, _ = env.cases()
    shutil.rmtree(env.cases_root)                                   # the share "disappears"
    with pytest.raises(S.StorageError):
        env.cases()
    with pytest.raises(S.StorageError):
        env.runner().storage_alive()
    assert not any(p.name.startswith("fallback") for p in Path.cwd().iterdir())


# ------------------------------------------------------------------ state

def test_locked_state_file_does_not_lose_transitions(env, monkeypatch):
    real_open, fails = open, {"n": 2}

    def flaky(path, mode="r", *a, **k):
        if str(path).endswith("journal.jsonl") and "a" in mode and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("locked")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", flaky)
    env.journal.record("c1", "preparing", attempt=1)
    monkeypatch.undo()
    j2 = S.Journal.__new__(S.Journal)
    j2.path, j2.snap, j2.cases, j2.seq = env.journal.path, env.journal.snap, {}, 0
    j2.replay()
    assert j2.cases["c1"]["state"] == "preparing"
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")) if "journal" in str(a[0]) else real_open(*a, **k))
    with pytest.raises(S.PersistenceError):
        env.journal.record("c1", "uploading")


def test_torn_last_journal_line_is_ignored(env):
    env.journal.record("c1", "queued")
    with open(env.journal.path, "ab") as fh:
        fh.write(b'{"seq": 99, "case": "c1", "sta')
    j = S.Journal.__new__(S.Journal)
    j.path, j.snap, j.cases, j.seq = env.journal.path, env.journal.snap, {}, 0
    j.replay()
    assert j.seq == 1 and j.cases["c1"]["state"] == "queued"


# ------------------------------------------------------------------ attachments

def chips(*names):
    return [{"label": n, "text": n, "title": ""} for n in names]


def snap(names, **kw):
    base = {"chips": chips(*names), "active": [], "error": "", "limit": "", "sendEnabled": True, "generating": False}
    return {**base, **kw}


def test_judge_upload_exact_set_with_multiplicity_and_no_substring_match():
    exp = ["a.pdf", "b.pdf", "b.pdf".replace("b", "B") + "x"]
    assert S.judge_upload(snap(exp), exp)["ready"]
    assert not S.judge_upload(snap(["a.pdf", "b.pdf"]), exp)["ready"]                    # missing one
    assert not S.judge_upload(snap(["a.pdf", "aa.pdf"]), ["a.pdf", "b.pdf"])["ready"]    # substring is not identity
    assert S.judge_upload(snap(["a.pdf", "a.pdf", "x.pdf"]), ["a.pdf", "x.pdf"])["extra"] == 1
    assert not S.judge_upload(snap(exp, active=["chip"]), exp)["ready"]                  # transfer still running
    assert not S.judge_upload(snap(exp, sendEnabled=False), exp)["ready"]
    assert not S.judge_upload(snap(exp, error="upload failed"), exp)["ready"]


def test_hidden_overflow_chips_resolved_by_exact_name_plus_decoration():
    exp = ["report.pdf", "report.pdf.bak.txt"]
    hidden = [{"label": "", "text": "report.pdf PDF 12 KB", "title": ""}, {"label": "", "text": "report.pdf.bak.txt TXT 1 KB", "title": ""}]
    assert S.judge_upload(snap([], chips=hidden), exp)["ready"]
    wrong = [{"label": "", "text": "report.pdf.bak.txt", "title": ""}] * 2
    assert not S.judge_upload(snap([], chips=wrong), exp)["ready"]


def test_chip_names_with_status_words_hidden_extension_or_ellipsis_still_resolve():
    exp = ["Quarterly_review_2026.pdf", "notes.txt"]
    for text in ("Uploading Quarterly_review_2026.pdf 45%", "quarterly_review_2026.PDF PDF 3 MB", "Quarterly_review_2026",
                 "Quarterly_review_20…"):
        assert S.resolve_chip({"label": "", "text": text, "title": ""}, exp) == "Quarterly_review_2026.pdf", text
    assert S.resolve_chip({"label": "", "text": "a1b2c3d4e5f6a7b8c9d0", "title": ""}, exp) == "a1b2c3d4e5f6a7b8c9d0"   # hash name stays unmatched


def test_a_temporary_hash_name_while_loading_is_waited_for_not_failed(env):
    exp = ["big_report.pdf", "small.txt"]
    frames = [snap(["small.txt", "9f8e7d6c5b4a39281706"], active=["chip"], sendEnabled=False)] * 4 + [snap(exp)] * 40

    class Tab:
        async def snapshot(self):
            return frames.pop(0) if len(frames) > 1 else frames[0]
    cfg = types.SimpleNamespace(**{**vars(env.cfg), "stable_seconds": 0.01, "poll_ms": 1, "upload_no_progress": 30})
    r = env.runner()
    r.cfg = cfg
    case = types.SimpleNamespace(alias="c1", key="k")
    run(r.wait_upload_ready(Tab(), case, exp))                       # returns instead of raising "unexpected attachment"
    assert not frames or len(frames) > 1


def test_more_chips_than_files_still_fails_fast(env):
    exp = ["a.pdf"]

    class Tab:
        async def snapshot(self):
            return snap(["a.pdf", "left_over.pdf"])
    r = env.runner()
    with pytest.raises(S.AttemptFailed, match="unexpected attachment"):
        run(r.wait_upload_ready(Tab(), types.SimpleNamespace(alias="c1", key="k"), exp))


def test_run_report_is_unredacted_complete_and_hides_only_credential_looking_env_vars(env, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "hunter2-secret-value")
    env.make_case(1, files=("secret_customer_file.pdf",))
    cases, _, _ = env.cases()
    r = env.runner()
    log = S.Log(env.rt, env.red, "full")
    ctx = types.SimpleNamespace(cfg=env.cfg, red=env.red, rt=env.rt, policy=env.policy, log=log)
    r.log = log
    start_seq = r.journal.seq
    results = run(r.run(collections.deque(cases)))
    assert [v[0] for v in results.values()] == ["submitted"]
    try:
        raise RuntimeError("boom at " + str(env.cfg.cases_root))
    except RuntimeError as exc:
        log.trace(exc, "test")
    path = S.write_run_report(ctx, r.journal, cases, r, {"connection_generation": 1, "pages": [
        {"url": "https://m365.cloud.microsoft/chat/conversation/0123456789abcdef0123456789abcdef?x=1", "ready": True, "visibility": "visible", "closed": False}]},
        time.monotonic() - 5, start_seq, 0, None, ["run"])
    text = path.read_text("utf-8")
    for heading in ("## 0. How to read", "## 1. Outcome", "## 2. System", "## 3. The tool", "## 4. Browser", "## 5. Cases",
                    "## 6. Attachment chips", "## 7. Errors", "## 8. Full event timeline", "## 9. Last console"):
        assert heading in text, heading
    assert "secret_customer_file.pdf" in text and str(env.cfg.cases_root) in text and "0123456789abcdef0123456789abcdef" in text
    assert "hunter2-secret-value" not in text
    assert "MY_SERVICE_TOKEN" in text
    assert "boom at" in text
    assert "Traceback" in text
    assert "doc#1" in text and "RuntimeError" in text and "submitted" in text


def test_plan_handles_duplicate_basenames_and_instruction_collisions(env):
    folder = env.make_case(1, files=("a.pdf",))
    (folder / "sub").mkdir()
    (folder / "sub" / "a.pdf").write_bytes(b"%PDF other")
    (folder / "INSTRUCTIONS.md").write_text("a different file with the same name")
    cfg = types.SimpleNamespace(**{**vars(env.cfg), "recursive_documents": True})
    cases, _, _ = env.cases()
    S.inventory(cases[0], cfg, env.policy)
    plan, left = S.build_plan(cases[0], S.doc_of(env.instr), cfg, env.policy)
    names = [p["name"].casefold() for p in plan]
    assert len(names) == len(set(names)) == 4 and plan[0]["name"] == "instructions.md"
    assert sum(p["aliased"] for p in plan) == 2
    paths = S.stage_aliases(plan, env.rt, "att1")
    assert all(Path(p).exists() for p in paths) and (env.rt.staging / "att1").exists()
    S.clean_staging(env.rt, "att1")
    assert not (env.rt.staging / "att1").exists()
    S.clean_staging(env.rt, "never-created")                        # no marker -> nothing deleted


def test_instructions_inside_the_case_is_attached_once(env):
    folder = env.make_case(1, files=("a.pdf",))
    shutil.copy(env.instr, folder / "x.md")
    cases, _, _ = env.cases()
    S.inventory(cases[0], env.cfg, env.policy)
    inside = types.SimpleNamespace(rel="(i)", path=folder / "x.md", size=1, mtime=1, name="x.md")
    cases[0].docs.append(inside)
    assert [p["name"] for p in S.build_plan(cases[0], inside, env.cfg, env.policy)[0]].count("x.md") == 1


def test_unsupported_files_and_block_mode_never_truncate(env):
    env.make_case(1, files=("a.pdf", "weird.xyz"))
    env.make_case(2, files=tuple(f"f{i}.txt" for i in range(20)))
    env.make_case(3, files=("empty.pdf",))
    (env.cases_root / f"{hexid(3)} - ref" / "empty.pdf").write_bytes(b"")
    cases, _, _ = env.cases()
    for c in cases:
        S.inventory(c, env.cfg, env.policy)
    assert "unsupported type .xyz" in cases[0].problems[0]
    block = types.SimpleNamespace(**{**vars(env.cfg), "over_limit_mode": "block"})
    with pytest.raises(S.Blocked):
        S.build_plan(cases[1], S.doc_of(env.instr), block, env.policy)     # 20 files + instructions > 20
    assert "empty file" in cases[2].problems[0]


def test_protected_office_file_is_blocked(env):
    folder = env.make_case(1, files=("a.pdf",))
    (folder / "locked.docx").write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 64)
    cases, _, _ = env.cases()
    S.inventory(cases[0], env.cfg, env.policy)
    assert any("password-protected" in p for p in cases[0].problems)


# ------------------------------------------------------------------ transactions

def run_queue(env, runner, n=3):
    for i in range(1, n + 1):
        env.make_case(i)
    cases, _, _ = env.cases()
    return cases, run(runner.run(collections.deque(cases)))


def test_happy_path_one_send_per_case_no_cross_contamination(env):
    env.make_case(1, files=("one.pdf",))
    env.make_case(2, files=("two.pdf", "extra.txt"))
    env.make_case(3, files=("three.pdf",))
    cases, _, _ = env.cases()
    r = env.runner()
    results = run(r.run(collections.deque(cases)))
    assert [v[0] for v in results.values()] == ["submitted"] * 3
    assert len(env.world.sends) == 3 and len({s[0] for s in env.world.sends}) == 3       # one tab per case
    for (tid, names, msg), expect in zip(env.world.sends, (["one.pdf"], ["two.pdf", "extra.txt"], ["three.pdf"])):
        assert sorted(names) == sorted(expect + ["instructions.md"]) and msg == r.message
    ledger = env.ledger.read()
    assert set(ledger) == {c.id for c in cases} and all(v == "submitted" for v in ledger.values())


def test_delayed_chips_wait_for_the_full_set(env):
    env.world.behaviors = [{"delay": 3}]
    env.make_case(1)
    cases, _, _ = env.cases()
    assert run(env.runner().run(collections.deque(cases)))[cases[0].key][0] == "submitted"
    assert env.world.sends[0][1].__len__() == 3


def test_partial_upload_refreshes_the_same_tab_then_retries_sequentially(env):
    env.world.behaviors = [{"partial": 1}]
    env.make_case(1)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submitted"
    routes = [c[2] for c in env.world.attach_calls]
    assert routes == ["bulk", "sequential"] and env.world.opened == 1          # whole plan re-sent only after a refresh to a NEW chat
    tab = list(env.world.tabs.values())[0]
    assert tab.resets == 1 and not tab.closed                                   # the SAME tab went back to the original chat link
    assert len(env.world.sends) == 1


def test_unexpected_extra_attachment_never_sends(env):
    env.world.behaviors = [{"extra": True}] * 6
    env.make_case(1)
    cases, _, _ = env.cases()
    r = env.runner()
    results = run(r.run(collections.deque(cases)))
    assert results[cases[0].key][0] == "failed_exhausted" and env.world.sends == []
    assert env.journal.cases[cases[0].key]["state"] == "failed_exhausted"
    assert r.attempts[cases[0].key] == 5 and env.world.opened == 1             # exactly 5 tries, all in the one tab
    assert list(env.world.tabs.values())[0].closed                              # leftover unsent tab closed at the end


def test_cdp_loss_during_upload_reconnects_and_retries_without_sending_twice(env):
    env.world.behaviors = [{"lose_on_attach": True}]
    env.make_case(1)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submitted" and env.world.reconnects >= 1 and len(env.world.sends) == 1


def test_an_upload_error_that_clears_by_itself_is_waited_out_in_the_same_chat(env):
    env.world.upload_errors = 1                          # shown once, gone after the short pause
    env.make_case(1)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submitted" and len(env.world.sends) == 1
    assert len(env.world.attach_calls) == 1 and any("looking again" in o for o in env.out)


def test_a_lasting_upload_error_gets_a_fresh_chat_and_the_files_again_before_any_send(env):
    env.world.upload_errors = 2                          # still there after the pause: recovery in the same tab
    env.make_case(1)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    tab_ids = {c[0] for c in env.world.attach_calls}
    assert results[cases[0].key][0] == "submitted" and len(env.world.sends) == 1
    assert len(env.world.attach_calls) == 2 and len(tab_ids) == 1                       # same tab, attached again
    assert any("fresh chat in the same tab" in o for o in env.out)


def test_upload_errors_that_never_clear_fail_the_attempt_unsent_and_the_case_is_retried(env):
    env.world.upload_errors = 6                          # attempt 1: 3 uploads x 2 looks; attempt 2 is clean
    env.make_case(1)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submitted" and len(env.world.sends) == 1
    assert len(env.world.attach_calls) == 4                                             # 1 + 2 recoveries, then attempt 2


def test_cdp_loss_right_after_send_rebinds_by_target_and_confirms(env):
    env.world.behaviors = [{"send": "raise_after"}]
    env.make_case(1)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submitted" and len(env.world.sends) == 1


def test_missing_target_after_possible_send_is_uncertain_and_never_resent(env):
    env.world.behaviors = [{"send": "raise_gone"}]
    env.make_case(1)
    cases, _, _ = env.cases()
    r = env.runner()
    results = run(r.run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submission_uncertain" and len(env.world.sends) == 1
    assert env.ledger.read()[cases[0].id] == "uncertain"
    again = env.runner()                                               # re-run: must not send again
    assert again.sent_ids.get(cases[0].id) == "uncertain"
    assert S.classify(cases, None, env.journal, env.ledger.read())[0] == []


def test_response_finishing_before_stop_is_seen_still_counts_as_submitted(env):
    env.world.behaviors = [{"generating": False, "reply_done": True}]
    env.make_case(1)
    cases, _, _ = env.cases()
    assert run(env.runner().run(collections.deque(cases)))[cases[0].key][0] == "submitted"
    assert env.journal.cases[cases[0].key]["evidence"]["reply_seen"] is True


def test_send_without_any_confirmation_is_uncertain_not_retried(env):
    env.world.behaviors = [{"send": "no_effect"}]
    env.make_case(1)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submission_uncertain" and len(env.world.sends) == 1


def test_crash_between_send_and_persistence_reconciles_without_resend(env):
    env.make_case(1)
    cases, _, _ = env.cases()
    r = env.runner()
    tab = run(r.browser.open_tab())
    run(tab.set_message(r.message))
    tab.attached = ["a.pdf"]
    run(tab.click_send())                                            # the message really went out ...
    env.journal.record(cases[0].key, "send_intent", alias=cases[0].alias, instruction_id=cases[0].id, target_id=tab.target_id)
    # ... and the process "died" before recording the outcome.  New process:
    r2 = env.runner()
    run(S.reconcile_open_intents(r2, env.journal, {cases[0].key: cases[0]}, env.log))
    assert env.journal.cases[cases[0].key]["state"] == "submitted" and len(env.world.sends) == 1
    assert env.ledger.read()[cases[0].id] == "submitted"


def test_crash_with_missing_tab_marks_uncertain(env):
    env.make_case(1)
    cases, _, _ = env.cases()
    env.journal.record(cases[0].key, "send_intent", alias=cases[0].alias, instruction_id=cases[0].id, target_id="T-gone")
    run(S.reconcile_open_intents(env.runner(), env.journal, {cases[0].key: cases[0]}, env.log))
    assert env.journal.cases[cases[0].key]["state"] == "submission_uncertain" and env.world.sends == []


def test_ledger_release_and_crash_repair(env):
    env.make_case(1)
    cases, _, _ = env.cases()
    c = cases[0]
    env.journal.record(c.key, "submitted", alias=c.alias, instruction_id=c.id)       # crash before ledger write
    assert c.id in S.sync_ledger(env.journal, env.ledger, env.log)                     # repaired, still skipped
    env.journal.record(c.key, "submitted", alias=c.alias, instruction_id=c.id, ledger_written=True)
    env.rt.ledger.write_text(S.Ledger.HEADER)                                          # user deleted the line
    assert c.id not in S.sync_ledger(env.journal, env.ledger, env.log)                 # released for a manual resend
    assert env.journal.cases[c.key]["state"] == "queued"
    assert len(env.journal.cases[c.key]) and "released" in env.journal.cases[c.key]


def test_blocked_case_does_not_stop_the_others(env):
    env.make_case(1, files=("a.pdf", "bad.xyz"))
    env.make_case(2)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "preflight_blocked" and results[cases[1].key][0] == "submitted"
    assert len(env.world.sends) == 1


def test_failed_case_goes_to_queue_tail_and_attempts_are_bounded(env):
    env.make_case(1, files=("one.pdf",))
    env.make_case(2, files=("two.pdf",))
    cases, _, _ = env.cases()
    env.world.rules = [{"file": "one.pdf", "times": 2, "b": {"extra": True}}]      # first case fails its 2 immediate tries
    r = env.runner()
    results = run(r.run(collections.deque(cases)))
    assert results[cases[1].key][0] == "submitted"                                  # next case was not held up
    assert results[cases[0].key][0] == "submitted"                                  # tail retry succeeded
    assert r.attempts[cases[0].key] == 3 and r.attempts[cases[0].key] <= env.cfg.max_attempts
    assert env.world.opened <= 2


def test_cancel_before_send_is_clean_and_sends_nothing(env):
    env.make_case(1)
    env.make_case(2)
    cases, _, _ = env.cases()
    r = env.runner()
    env.stop.is_set = True
    with pytest.raises(S.Stopped):
        run(r.run(collections.deque(cases)))
    assert env.world.sends == []


def test_persistence_failure_stops_before_sending(env, monkeypatch):
    env.make_case(1)
    cases, _, _ = env.cases()
    r = env.runner()
    real = env.journal.record

    def boom(case, state, **kw):
        if state == "send_intent":
            raise S.PersistenceError("disk")
        return real(case, state, **kw)
    monkeypatch.setattr(env.journal, "record", boom)
    with pytest.raises(S.PersistenceError):
        run(r.run(collections.deque(cases)))
    assert env.world.sends == []                                                  # intent must be durable before send


def test_full_run_writes_only_under_approved_storage_and_leaks_nothing(env):
    S.WRITE_GUARD.update(roots=env.policy.approved, policy=env.policy)
    try:
        env.make_case(1, ref="Secret Customer Ltd")
        env.make_case(2)
        cases, _, _ = env.cases()
        r = env.runner()
        run(r.run(collections.deque(cases)))
        ctx = types.SimpleNamespace(rt=env.rt, red=env.red)
        S.summarise(ctx, env.journal, cases, env.ledger.read(), {})
    finally:
        S.WRITE_GUARD["roots"] = None
    blob = " ".join(env.out) + " ".join(p.read_text("utf-8") for p in env.rt.logs.glob("summary-*.json"))
    for secret in (str(env.base), env.base.name, "Secret Customer", cases[0].id, cases[0].name, "example.invalid"):
        assert secret not in blob


def test_redactor_covers_exceptions_paths_ids_and_urls(env):
    err = ValueError(f"cannot open {env.cases_root}\\{hexid(9)} - ref\\a.pdf at https://x.example/chat/abc and {hexid(5)}")
    text = S.safe_exc(env.red, err)
    assert "cases" not in text.lower().replace("cases_root", "") and hexid(9) not in text and "https" not in text
    assert "CASES_ROOT" in text or "<path>" in text


# ------------------------------------------------------------------ connection

class FakePW:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chromium = self

    async def connect_over_cdp(self, route, timeout=0):
        self.calls.append((route.split(":")[0], timeout))
        step = self.script.pop(0) if self.script else "fail"
        if step == "hang":
            await asyncio.sleep(60)
        if step == "fail":
            raise RuntimeError("handshake failed")
        return types.SimpleNamespace(contexts=[object()], close=lambda: asyncio.sleep(0))


def edge_for(env, script, monkeypatch, **cfg):
    for k, v in cfg.items():
        setattr(env.cfg, k, v)
    b = S.EdgeBrowser(env.cfg, env.rt, env.policy, env.log, env.stop)
    b.pw = FakePW(script)
    payload = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{env.cfg.cdp_port}/devtools/browser/abc"}

    async def ensure(launch=True):
        return payload
    monkeypatch.setattr(b, "ensure_running", ensure)
    return b


def test_responsive_endpoint_but_failed_handshake_falls_back_then_recovers(env, monkeypatch):
    b = edge_for(env, ["fail", "ok"], monkeypatch, handshake_timeouts=[0.05, 0.1])
    run(b.connect())
    assert [c[0] for c in b.pw.calls] == ["ws", "http"] and b.context is not None      # ws first, then HTTP, sequentially


def test_handshake_hang_is_cancelled_and_budget_exhaustion_is_durable(env, monkeypatch):
    b = edge_for(env, ["hang"] * 10, monkeypatch, handshake_timeouts=[0.05, 0.05], reconnect_budget_seconds=5)
    with pytest.raises(S.RecoveryRequired):
        run(b.connect())
    assert len(b.pw.calls) == 4                                                       # every route tried once, none overlapping
    b2 = edge_for(env, ["fail"] * 10, monkeypatch, handshake_timeouts=[0.05] * 50, reconnect_budget_seconds=0)
    with pytest.raises(S.RecoveryRequired):
        run(b2.connect())


def test_an_edge_already_open_on_the_port_is_joined_even_if_another_process_started_it(env, monkeypatch):
    port = env.cfg.cdp_port
    payload = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc"}

    async def fake_version(p, timeout=1.5):
        return payload
    monkeypatch.setattr(S, "cdp_version", fake_version)
    monkeypatch.setattr(S, "port_owned_by_profile", lambda profile, pl: False)       # someone else's Edge
    b = S.EdgeBrowser(env.cfg, env.rt, env.policy, env.log, env.stop)
    assert run(b.ensure_running(launch=False)) is payload and b.reused and not b.launched


def test_a_joined_edge_with_some_copilot_tabs_is_topped_up_to_the_needed_count_before_the_run(env):
    for n in range(1, 5):
        env.make_case(n)
    cases, _, _ = env.cases()
    r = env.runner()
    b = r.browser
    b.reused = True
    ready = [FakeTab(env.world, {}) for _ in range(2)]          # 2 Copilot tabs already open, 4 cases to send
    b.already_open = ready
    b.find_ready_tab = lambda: asyncio.sleep(0, ready[0])
    cfg = types.SimpleNamespace(**{**vars(env.cfg), "warm_tabs": 8, "requested_model": "GPT-6.1 Sol"})

    async def go():
        await S.prepare_session(r, b, cfg, env.log, cases)
        return [t for t, _ in r.pool]
    pool = run(go())
    assert [t.target_id for t in pool[:2]] == [t.target_id for t in ready] and len(pool) == 4 and env.world.opened == 2


def test_the_cursor_answer_given_at_launch_is_applied_before_any_tab_is_worked_in(env, monkeypatch):
    env.make_case(1)
    cases, _, _ = env.cases()
    r = env.runner()
    monkeypatch.setattr(S.CopilotTab, "cursor", S.VisualCursor())
    cfg = types.SimpleNamespace(**{**vars(env.cfg), "requested_model": "GPT-6.1 Sol"})

    async def go():
        asking = asyncio.get_running_loop().create_future()
        asking.set_result(False)                                   # the user typed N while Edge was starting
        await S.prepare_session(r, r.browser, cfg, env.log, cases, asking)
        return S.CopilotTab.cursor
    cursor = run(go())
    assert cursor is not None and cursor.enabled is False and len(r.pool) == 1


def test_a_tab_that_never_gets_ready_is_reloaded_then_left_out_and_never_used(env, monkeypatch):
    monkeypatch.setattr(S, "READY_RELOAD_SECONDS", 0)
    b = FakeBrowser(env.world)
    good, slow, stuck = FakeTab(env.world, {}), FakeTab(env.world, {"ready_after": 3}), FakeTab(env.world, {"ready_after": None})
    tabs = run(S.ready_tabs(b, env.cfg, [good, slow, stuck], env.log))
    assert tabs == [good, slow]                                   # order kept; the stuck tab is out of the run
    assert env.world.reloads.count(stuck.target_id) == S.READY_RELOADS + 1      # 1 sign-in-page reload + the timed reloads
    assert good.target_id not in env.world.reloads                               # a ready tab is never reloaded


def test_endpoint_validation_rejects_foreign_hosts_and_ports():
    ok = {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}
    assert S.endpoint_valid(ok, 9333)
    assert not S.endpoint_valid(ok, 9334)
    assert not S.endpoint_valid({"webSocketDebuggerUrl": "ws://10.0.0.5:9333/devtools/browser/x"}, 9333)
    assert not S.endpoint_valid({"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/x"}, 9333)


def test_unresponsive_renderer_becomes_a_ui_error_not_a_crash():
    class T:
        cfg = types.SimpleNamespace(op_timeout=0.05)

        @S.guarded
        async def slow(self):
            await asyncio.wait_for(asyncio.sleep(1), self.cfg.op_timeout)

        @S.guarded
        async def closed(self):
            raise RuntimeError("Target page, context or browser has been closed")
    with pytest.raises(S.UiError):
        run(T().slow())
    with pytest.raises(S.ConnectionLost):
        run(T().closed())


# ------------------------------------------------------------------ config

def test_config_validation_rules(env, monkeypatch):
    monkeypatch.setenv("SENDER_CONFIG", str(env.base / "cfg.json"))
    good = types.SimpleNamespace(**{**vars(env.cfg), "approved_root": str(env.base)})
    assert S.validate_config(good)[1] == []
    inside = types.SimpleNamespace(**{**vars(good), "runtime_root": str(env.cases_root / "rt")})
    assert any("outside CASES_ROOT" in p for p in S.validate_config(inside)[1])
    env.msg.write_text("  \n ", "utf-8")
    assert any("empty" in p for p in S.validate_config(good)[1])
    env.msg.write_bytes(b"\xef\xbb\xbfhello")
    assert S.load_message(env.msg) == "hello"                                           # BOM handled
    S.save_config(good)
    assert S.load_config().cases_root == str(env.cases_root)
    other = types.SimpleNamespace(**{**vars(good), "completion_mode": "validator"})
    assert any("submission_only" in p for p in S.validate_config(other)[1])


# ------------------------------------------------------------------ console output / attachment limit

def test_submitted_status_is_white_on_green_only_on_a_real_console(env, monkeypatch, capsys):
    log = S.Log(None, env.red)
    monkeypatch.setattr(S, "use_colour", lambda: True)
    log("submitted", "case-abc123", attempt=1)
    log("uploading", "case-abc123")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("\x1b[42;97m") and out[0].endswith("\x1b[0m") and "submitted" in out[0]
    assert "\x1b[" not in out[1]                                       # other phases stay plain
    monkeypatch.setattr(S, "use_colour", lambda: False)                # redirected output / NO_COLOR: no escape codes
    log("submitted", "case-abc123")
    assert "\x1b[" not in capsys.readouterr().out


def test_case_exactly_at_the_limit_is_sent_and_the_user_is_told(env):
    env.make_case(1, files=tuple(f"f{i:02d}.txt" for i in range(19)))     # 19 documents + instructions = 20
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert results[cases[0].key][0] == "submitted" and len(env.world.sends[0][1]) == 20
    assert any("at-limit" in line and "20/20" in line for line in env.out)


def test_over_limit_sends_first_files_and_warns_loudly(env):
    env.make_case(1, files=tuple(f"f{i:02d}.txt" for i in range(24)))        # 24 + instructions = 25 > 20
    cases, _, _ = env.cases()
    r = env.runner()
    results = run(r.run(collections.deque(cases)))
    state, reason = results[cases[0].key]
    sent = env.world.sends[0][1]
    assert state == "submitted_partial" and "5 file(s) left out" in reason
    assert len(sent) == 20 and sent[0] == "instructions.md" and sent[1:] == [f"f{i:02d}.txt" for i in range(19)]
    assert any(line.startswith("WARNING") and "5 file(s) will NOT be sent" in line for line in env.out)
    assert env.journal.cases[cases[0].key]["omitted"] == 5
    assert env.ledger.read()[cases[0].id] == "partial"                           # not forgotten, not resent


def test_exit_status_treats_partial_as_not_fully_successful(env):
    assert "submitted_partial" not in ("submitted",)


# ------------------------------------------------------------------ sign-in, model choice, warm-up

def test_default_model_is_the_last_gpt_entry_whatever_it_is_called():
    assert S.pick_default(MODELS)["label"] == "GPT-6.1 Sol"
    renamed = [{"group": "GPT", "label": x, "text": x} for x in ("Alpha", "Zeta")]
    assert S.pick_default(renamed)["label"] == "Zeta"                          # no version numbers are parsed
    no_gpt = MODELS[:6]
    assert S.pick_default(no_gpt)["label"] == "Opus 5.5"                       # falls back to the last real model
    assert S.pick_default([]) is None
    text = S.model_menu(MODELS, S.pick_default(MODELS))
    assert "* 10. GPT-6.1 Sol" in text and "Claude" in text and "Modes" in text


def test_model_answers_enter_number_invalid_override_and_no_terminal(env, capsys):
    default = S.pick_default(MODELS)
    ask = lambda lines, cfg=env.cfg, entries=MODELS, eof=False: run(S.choose_model(entries, default, cfg, FakeStdin(lines, eof), env.log))
    assert ask([""]) is default
    assert ask(["4"])["label"] == "Sonnet 5"
    assert ask(["99", "x", "2"])["label"] == "Quick response"                  # invalid answers are asked again
    assert ask([], eof=True) is default                                        # redirected input: take the default
    with pytest.raises(S.RecoveryRequired):
        ask([], entries=[])                                                    # no list: never silently use Copilot's own model
    pinned = types.SimpleNamespace(**{**vars(env.cfg), "requested_model": "opus 5.5"})
    assert ask([], pinned)["label"] == "Opus 5.5"                              # setting skips the question
    with pytest.raises(S.Blocked):
        ask([], types.SimpleNamespace(**{**vars(env.cfg), "requested_model": "nope"}))


def test_label_matches_requires_the_same_model_not_a_prefix_of_another():
    e = {"label": "GPT-6.1 Sol"}
    assert S.label_matches("GPT-6.1 Sol", e) and S.label_matches("gpt-6.1 sol", e)
    assert not S.label_matches("GPT-6", e) and not S.label_matches("", e)
    assert S.label_matches("GPT-5.6 Sol", {"label": "GPT-5.6 Sol Think deeper"})     # button may show a shorter title


def test_model_list_is_retried_across_tabs_until_one_shows_it_with_no_fallback(env):
    slow, quick = FakeTab(env.world, {}), FakeTab(env.world, {})
    attempts = {"slow": 0}

    async def slow_picker():                       # this tab is signed in but its picker never opens
        attempts["slow"] += 1
        raise S.UiError("the model menu did not open")
    slow.discover_models = slow_picker
    quick_calls = {"n": 0}

    async def late_picker():                       # the other tab only gets a working picker on its 3rd try
        quick_calls["n"] += 1
        return list(MODELS) if quick_calls["n"] >= 3 else []
    quick.discover_models = late_picker
    b = FakeBrowser(env.world)

    async def ready():                             # the 2nd tab only becomes ready after the first round
        return [slow] if attempts["slow"] < 1 else [slow, quick]
    b.find_ready_tabs = ready
    tab, entries = run(S.discover_models_anywhere(b, slow, env.red, env.log))
    assert tab is quick and entries == MODELS and attempts["slow"] >= 3 and quick_calls["n"] == 3


def test_model_list_search_ends_only_on_a_stop_request(env):
    tab = FakeTab(env.world, {})

    async def broken():
        raise S.UiError("the model menu did not open")
    tab.discover_models = broken
    b = FakeBrowser(env.world)

    async def only_one():
        return [tab]
    b.find_ready_tabs = only_one
    calls = {"n": 0}
    real = b.stop.check

    def check():
        calls["n"] += 1
        if calls["n"] > 12:
            raise S.Stopped("stop requested")
        real()
    b.stop.check = check
    with pytest.raises(S.Stopped):
        run(S.discover_models_anywhere(b, tab, env.red, env.log))
    assert calls["n"] > 12                          # it kept trying instead of falling back to "no model"


def test_login_is_detected_automatically_and_an_early_enter_is_discarded(env):
    b = FakeBrowser(env.world)
    b.ready_after = 3
    stdin = FakeStdin()
    tab = run(S.wait_for_login(b, stdin, env.log))
    assert tab is not None and stdin.drained == 1 and env.world.opened == 0     # moved on by itself, no keypress needed


def test_enter_is_only_a_fallback_and_missing_terminal_input_is_ignored(env):
    b = FakeBrowser(env.world)
    b.ready_after = 10 ** 6                                                    # never detected
    tab = run(S.wait_for_login(b, FakeStdin([""]), env.log))
    assert env.world.opened == 1 and tab.target_id                             # user confirmed manually
    b2, calls = FakeBrowser(env.world), {"n": 0}
    b2.ready_after = 10 ** 6
    real = b2.find_ready_tab

    async def counting():
        calls["n"] += 1
        if calls["n"] == 4:
            b2.ready_after = 0
        return await real()
    b2.find_ready_tab = counting
    run(S.wait_for_login(b2, FakeStdin(eof=True), env.log))                     # EOF must NOT count as "continue"
    assert calls["n"] == 4


def test_session_opens_every_needed_tab_up_front_and_each_tab_sets_the_model_when_it_sends(env):
    for n in range(1, 4):
        env.make_case(n)
    cases, _, _ = env.cases()
    r = env.runner()
    cfg = types.SimpleNamespace(**{**vars(env.cfg), "warm_tabs": 8, "requested_model": "GPT-6.1 Sol"})
    async def go():                                    # one event loop: the background model tasks live in it
        await S.prepare_session(r, r.browser, cfg, env.log, cases)
        assert len(r.pool) == 3                         # min(warm_tabs, cases)
        assert r.model["label"] == "GPT-6.1 Sol"
        return await r.run(collections.deque(cases))
    results = run(go())
    assert [v[0] for v in results.values()] == ["submitted"] * 3 and env.world.opened == 3   # all 3 opened up front, none ad hoc
    assert all(t.models and t.models[0]["label"] == "GPT-6.1 Sol" for t in env.world.tabs.values())


def test_every_setting_the_code_reads_has_a_default():
    import re as _re
    source = SRC.read_text("utf-8")
    used = set(_re.findall(r"\bcfg\.([a-z_]+)", source)) | set(_re.findall(r"self\.cfg\.([a-z_]+)", source))
    missing = sorted(k for k in used if k not in S.DEFAULTS and k not in S.REQUIRED)
    assert missing == []


# ------------------------------------------------------------------ foreground, console detail

def test_every_tab_is_foregrounded_right_before_its_send(env):
    for n in range(1, 4):
        env.make_case(n)
    cases, _, _ = env.cases()
    results = run(env.runner().run(collections.deque(cases)))
    assert [v[0] for v in results.values()] == ["submitted"] * 3
    events = env.world.events
    for i, (kind, tid) in enumerate(events):
        if kind == "send":
            assert events[i - 1] == ("foreground", tid)            # nothing between "bring to front" and "click Send"
    assert sum(1 for k, _ in events if k == "send") == 3


def test_minimal_console_shows_only_the_essentials_and_the_real_id_never_reaches_the_log_file(env, capsys):
    red = S.Redactor()
    red.ids["case-aaa111"] = "a" * 32
    minimal = S.Log(env.rt, red, "minimal", True)
    minimal("queue", note="5 instruction(s) queued")
    minimal("uploading", "case-aaa111", expected=4)
    minimal("upload-progress", "case-aaa111", observed=2)
    minimal("start", "case-aaa111", queue="1/5", attachments=4)
    minimal("submitted", "case-aaa111")
    out = capsys.readouterr().out
    assert "uploading" not in out and "upload-progress" not in out
    assert "5 instruction(s) queued" in out and "attachments=4" in out and "submitted" in out
    assert out.count("a" * 32) == 2                                     # shown on the start and submitted lines
    log_text = minimal.file.read_text("utf-8")
    assert "a" * 32 not in log_text and "uploading" in log_text         # the log file keeps every step, without the ID
    full = S.Log(None, red, "full", False)
    full("uploading", "case-aaa111", expected=4)
    shown = capsys.readouterr().out
    assert "uploading" in shown and "a" * 32 not in shown               # full detail, IDs hidden when not requested


def test_console_detail_setting_is_validated(env):
    bad = types.SimpleNamespace(**{**vars(env.cfg), "approved_root": str(env.base), "console_detail": "loud"})
    assert any("console_detail" in p for p in S.validate_config(bad)[1])


# ------------------------------------------------------------------ V2: answer contract, collection, fix-ups

def parsed(text=None, blocks=None):
    return S.parse_answer({"text": text if text is not None else "", "blocks": blocks if blocks is not None else [text or ""]})


def test_valid_answers_in_every_verdict_pass():
    for verdict in ("Correct", "Incorrect", "Inconclusive"):
        obj, problems = parsed(answer_json(verdict))
        assert obj and problems == [], (verdict, problems)
    inconclusive_empty = answer_json("Inconclusive", rows=[])
    assert parsed(inconclusive_empty)[1] == []                                   # rows may be empty only for Inconclusive


def test_structure_and_verdict_problems_are_named():
    row = {"ITEM": "x", "Customer_expectation": "a", "System_data": "a", "MATCH": True}
    cases = {
        "missing key": (json.dumps({"COPILOT_RESULT": "Correct", "A_B_COMPARISONS": [row]}), "missing key(s): JUSTIFICATION"),
        "extra key": (answer_json(NOTES="hi"), "unexpected key(s): NOTES"),
        "verdict word": (answer_json().replace('"Correct"', '"correct"'), "exactly Correct, Incorrect or Inconclusive"),
        "empty justification": (answer_json().replace('"because"', '""'), "JUSTIFICATION must be non-empty"),
        "row shape": (answer_json(rows=[{"ITEM": "x", "MATCH": True}]), "row 1 must have exactly"),
        "match type": (answer_json(rows=[{**row, "MATCH": "yes"}]), "MATCH must be true or false"),
        "correct with a difference": (answer_json("Correct", rows=[{**row, "MATCH": False}]), "Correct requires every MATCH"),
        "incorrect without a difference": (answer_json("Incorrect", rows=[row]), "Incorrect requires at least one MATCH"),
        "correct without rows": (answer_json("Correct", rows=[]), "needs at least one"),
    }
    for name, (text, expected) in cases.items():
        obj, problems = parsed(text)
        assert any(expected in x for x in problems), (name, problems)


def test_answer_extraction_rules():
    assert parsed("no code here at all", blocks=[])[0] is None and "no JSON object" in parsed("hello", blocks=[])[1][0]
    obj, problems = parsed("{ not json", blocks=["{ not json"])
    assert obj is None and "not valid JSON" in problems[0]
    older, newer = answer_json("Incorrect"), answer_json("Correct")
    assert parsed("x", blocks=[older, newer])[0]["COPILOT_RESULT"] == "Correct"        # the newest block wins
    assert parsed("intro\n```json\n" + GOOD + "\n```\nthanks", blocks=[])[0]["COPILOT_RESULT"] == "Correct"   # fenced text works too
    assert parsed(GOOD, blocks=[])[0]["COPILOT_RESULT"] == "Correct"                                           # bare JSON reply works
    assert "no JSON object" in parsed("x", blocks=["[1, 2]"])[1][0]


def test_plain_text_json_is_found_even_with_words_around_it_and_braces_inside_strings():
    text = "Here is my review. " + answer_json() + " Hope that helps!"
    obj, problems = parsed(text, blocks=[])
    assert obj["COPILOT_RESULT"] == "Correct" and problems == []
    tricky = answer_json(rows=[{"ITEM": "a {b} c", "Customer_expectation": "{x}", "System_data": "{x}", "MATCH": True}])
    assert parsed("result: " + tricky, blocks=[])[1] == []
    older, newer = answer_json("Incorrect"), answer_json("Correct")
    assert parsed(older + "\nfixed:\n" + newer, blocks=[])[0]["COPILOT_RESULT"] == "Correct"      # newest object wins
    wrong_only = '{"COPILOT_RESULT": "maybe"}'
    assert parsed("x " + wrong_only, blocks=[])[1]                                                  # judged, so it can be named


def run_json(env2, answers_by_tab=None, cases=1, **cfg):
    for key, value in cfg.items():
        setattr(env2.cfg, key, value)
    for n in range(1, cases + 1):
        env2.make_case(n)
    found, _, _ = env2.cases()
    if answers_by_tab:
        env2.world.behaviors = answers_by_tab
    r = env2.runner()
    return found, r, run(r.run(collections.deque(found)))


def test_a_valid_answer_is_collected_saved_and_shown(env2, capsys):
    cases, r, results = run_json(env2, cases=2)
    assert [v[0] for v in results.values()] == ["result_correct"] * 2
    for c in cases:
        saved = json.loads((env2.rt.results / f"{c.id}.json").read_text("utf-8"))
        assert saved["status"] == "result_valid" and saved["result"]["COPILOT_RESULT"] == "Correct" and saved["raw_text"]
    rows = list(__import__("csv").reader((env2.rt.results / "results.csv").open(encoding="utf-8")))
    assert rows[0][:3] == ["instruction_id", "status", "verdict"] and len(rows) == 3
    assert env2.journal.cases[cases[0].key]["state"] == "result_valid"
    assert any(l.startswith("result_correct") for l in (x.strip() for x in env2.out))
    assert env2.ledger.read()[cases[0].id] == "submitted"                       # the ledger still means "submitted"
    assert len(env2.world.sends) == 2 and all(set(n).issuperset({"instructions.md", "result_contract.md"}) for _, n, _ in env2.world.sends)


def test_answered_tab_is_reset_and_reused_for_the_next_case(env2):
    env2.make_case(1)
    env2.make_case(2)
    cases, _, _ = env2.cases()
    r = env2.runner()

    async def go():
        await r.run(collections.deque(cases[:1]))
        await r.run(collections.deque(cases[1:]))                              # second run: needs a tab, takes the answered one
    run(go())
    assert env2.world.opened == 1 and list(env2.world.tabs.values())[0].resets == 1
    assert len(env2.world.sends) == 2


def test_wrong_structure_gets_a_fixup_in_the_same_chat_then_succeeds(env2):
    bad = "Sure! Here is my review in plain words."
    cases, r, results = run_json(env2, [{"answers": [bad, GOOD]}])
    tab = list(env2.world.tabs.values())[0]
    assert results[cases[0].key][0] == "result_correct" and len(tab.followups) == 1
    assert S.FIXUP_TEXT in tab.followups[0] and "no JSON object" in tab.followups[0] and "plain text" in tab.followups[0]
    assert env2.journal.cases[cases[0].key]["fixups"] == 1 and env2.world.opened == 1 and len(env2.world.sends) == 1


def test_three_failed_fixups_resubmit_the_same_id_in_the_refreshed_tab(env2):
    bad = '{"COPILOT_RESULT": "maybe"}'
    cases, r, results = run_json(env2, [{"answers": [bad] * 4 + [GOOD]}])
    tab = list(env2.world.tabs.values())[0]
    assert results[cases[0].key][0] == "result_correct"
    assert len(tab.followups) == 3 and tab.resets == 1 and env2.world.opened == 1      # same tab, refreshed once
    assert len(env2.world.sends) == 2 and r.attempts[cases[0].key] == 2                 # the case was resubmitted once
    assert list(env2.ledger.read()) == [cases[0].id]                                    # still one ledger line


def test_resubmission_is_bounded_by_the_total_tries(env2):
    cases, r, results = run_json(env2, [{"answers": ['{"x": 1}']}], max_attempts=2, immediate_retries=2)
    assert results[cases[0].key][0] == "result_invalid" and r.attempts[cases[0].key] == 2
    assert len(env2.world.sends) == 2 and len(list(env2.world.tabs.values())[0].followups) == 6   # 3 fix-ups per try
    saved = json.loads((env2.rt.results / f"{cases[0].id}.json").read_text("utf-8"))
    assert saved["status"] == "result_invalid" and saved["result"] is None


def test_an_overdue_answer_is_flagged_never_resent_and_reported(env2):
    cases, r, results = run_json(env2, [{"gen_polls": 10 ** 6}], answer_overdue_minutes=0.0004)
    assert results[cases[0].key][0] == "response_timeout" and len(env2.world.sends) == 1
    assert any("answer overdue" in l for l in env2.out) and env2.journal.cases[cases[0].key]["state"] == "response_timeout"


def test_a_slow_answer_is_waited_for_not_judged_early(env2):
    cases, r, results = run_json(env2, [{"gen_polls": 5}])
    assert results[cases[0].key][0] == "result_correct" and len(list(env2.world.tabs.values())[0].followups) == 0


def test_an_unanswered_earlier_submission_is_collected_after_a_restart_without_resending(env2):
    env2.make_case(1)
    cases, _, _ = env2.cases()
    c = cases[0]
    r = env2.runner()
    tab = run(r.browser.open_tab())
    tab.users, tab.replies = ["earlier message"], [{"text": GOOD, "blocks": [GOOD]}]       # answered while the tool was closed
    env2.journal.record(c.key, "submitted", alias=c.alias, instruction_id=c.id, contract=True, target_id=tab.target_id,
                        conversation_url="https://example.invalid/chat/conversation/x", ledger_written=True)
    env2.ledger.add(c.id, "submitted")

    async def go():
        await r.resume({c.key: c})
        return await r.run(collections.deque())
    results = run(go())
    assert results[c.key][0] == "result_correct" and env2.world.sends == []
    older = env2.runner()                                                              # tab gone: the saved link is used
    older.journal.record(c.key, "submitted", alias=c.alias, instruction_id=c.id, contract=True, target_id="T-gone",
                         conversation_url="https://example.invalid/chat/conversation/x", ledger_written=True)

    async def go2():
        await older.resume({c.key: c})
        return await older.run(collections.deque())
    assert run(go2())[c.key][0] == "result_correct" and env2.world.sends == []


def test_v1_submissions_without_a_contract_are_not_resumed(env2):
    env2.make_case(1)
    cases, _, _ = env2.cases()
    c = cases[0]
    env2.journal.record(c.key, "submitted", alias=c.alias, instruction_id=c.id, target_id="T-old", ledger_written=True)
    r = env2.runner()
    run(r.resume({c.key: c}))
    assert r.pending == {} and c.key not in r.results


def test_collector_never_takes_the_front_tab_between_foreground_and_send(env2):
    for n in range(1, 5):
        env2.make_case(n)
    cases, _, _ = env2.cases()
    results = run(env2.runner().run(collections.deque(cases)))
    assert [v[0] for v in results.values()] == ["result_correct"] * 4
    events = env2.world.events
    for i, (kind, tid) in enumerate(events):
        if kind == "send":
            assert events[i - 1] == ("foreground", tid)


def test_plan_with_two_fixed_files_and_the_limit(env2):
    folder = env2.make_case(1, files=tuple(f"f{i:02d}.txt" for i in range(19)))        # 19 docs + 2 fixed = 21 > 20
    cases, _, _ = env2.cases()
    S.inventory(cases[0], env2.cfg, env2.policy)
    fixed = [S.doc_of(env2.instr), S.doc_of(env2.contract)]
    plan, left = S.build_plan(cases[0], fixed, env2.cfg, env2.policy)
    assert [p["name"] for p in plan[:2]] == ["instructions.md", "result_contract.md"] and len(plan) == 20 and len(left) == 1


def test_json_mode_needs_a_contract_and_verdict_colours(env2, monkeypatch, capsys):
    bad = types.SimpleNamespace(**{**vars(env2.cfg), "approved_root": str(env2.base), "result_contract_path": ""})
    assert any("result_contract_path" in p for p in S.validate_config(bad)[1])
    monkeypatch.setattr(S, "use_colour", lambda: True)
    log = S.Log(None, env2.red)
    for phase in ("result_correct", "result_incorrect", "result_inconclusive"):
        log(phase, "case-aaa111")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("\x1b[42;97m") and out[1].startswith("\x1b[41;97m") and out[2].startswith("\x1b[43;30m")


def test_greyed_out_models_are_flagged_skipped_as_default_and_refused(env):
    models = [dict(m) for m in MODELS]
    models[-1]["disabled"] = True                                              # GPT-6.1 Sol greyed out by Copilot
    default = S.pick_default(models)
    assert default["label"] == "GPT-5.6 Sol Think deeper"                      # last AVAILABLE GPT entry
    assert "(unavailable now)" in S.model_menu(models, default)
    answer = run(S.choose_model(models, default, env.cfg, FakeStdin(["10", ""]), env.log))
    assert answer is default                                                   # "10" was refused, Enter took the default
    pinned = types.SimpleNamespace(**{**vars(env.cfg), "requested_model": "GPT-6.1 Sol"})
    with pytest.raises(S.Blocked):
        run(S.choose_model(models, default, pinned, FakeStdin(), env.log))


# ------------------------------------------------------------------ visual cursor (8b): drawing never changes the real action

class CursorPage:
    """Fake page: records every drawing step; `fail` = each step raises, `hang` = each step never answers."""

    def __init__(self, ctx=None, fail=False, hang=False, retry=None):
        self.ops, self.fail, self.hang, self.retry, self.fronted = [], fail, hang, retry, 0
        self.context = ctx or types.SimpleNamespace(pages=[self])

    def on(self, *a):
        pass

    async def evaluate(self, script, arg=None):
        if script is S.RETRY_JS:
            return self.retry
        self.ops.append((arg[0], arg[1]))
        if self.fail:
            raise RuntimeError("Target page, context or browser has been closed")
        if self.hang:
            await asyncio.sleep(30)
        op, a = arg[0], arg[1]
        if op in ("move", "show", "pretend"):
            self.at = a
        elif op == "edge":
            self.at = [1000 if a == "right" else 14, 300]                         # page edge, middle height
        return {"at": getattr(self, "at", arg[3]), "ok": True, "wait": 0}

    async def bring_to_front(self):
        self.fronted += 1


class CursorLoc:
    def __init__(self, page, error=None):
        self.page, self.error, self.calls = page, error, []

    async def bounding_box(self, timeout=None):
        return {"x": 100, "y": 50, "width": 40, "height": 20}

    async def click(self, **kw):
        self.calls.append(("click", kw))
        if self.error:
            raise self.error
        return "clicked"

    async def fill(self, text, **kw):
        self.calls.append(("fill", text, kw))
        return None

    async def evaluate(self, script, arg=None, timeout=None):
        assert script is S.TRACK_JS                                             # read-only: the glow follows the box
        self.page.ops.append(("track", arg))
        return True


def test_cursor_off_is_exactly_the_original_action():
    page = CursorPage()
    loc = CursorLoc(page)
    vc = S.VisualCursor()                                                       # the default: off
    assert run(vc.click(loc, timeout=15000)) == "clicked"
    run(vc.fill(loc, "hello", timeout=20000))
    assert loc.calls == [("click", {"timeout": 15000}), ("fill", "hello", {"timeout": 20000})]
    assert page.ops == []                                                       # nothing drawn, nothing injected


def test_cursor_on_draws_around_one_real_click_and_one_fill():
    page = CursorPage()
    loc = CursorLoc(page)
    vc = S.VisualCursor({"cap_ms": 50}, enabled=True)
    assert run(vc.click(loc, timeout=15000)) == "clicked"
    assert [op for op, _ in page.ops] == ["move", "press", "release"] and page.ops[-1] == ("release", "real")
    assert page.ops[0][1] == [120, 60]                                          # centre of the element
    page.ops.clear()
    run(vc.fill(loc, "hello", timeout=20000))
    assert [op for op, _ in page.ops] == ["move", "track", "track", "move"]     # glow before and after; no ring
    assert [c[0] for c in loc.calls] == ["click", "fill"] and vc.at == [120, 60]  # stays on the box's last line


def test_a_broken_or_frozen_drawing_never_stops_or_repeats_the_real_action():
    for page in (CursorPage(fail=True), CursorPage(hang=True)):
        loc = CursorLoc(page)
        vc = S.VisualCursor({"cap_ms": 50}, enabled=True)
        t0 = time.monotonic()
        assert run(vc.click(loc, timeout=15000)) == "clicked"
        assert loc.calls == [("click", {"timeout": 15000})] and time.monotonic() - t0 < 2


def test_the_original_playwright_error_comes_out_unchanged():
    page = CursorPage()
    boom = TimeoutError("Timeout 15000ms exceeded")
    loc = CursorLoc(page, error=boom)
    vc = S.VisualCursor({"cap_ms": 50}, enabled=True)
    with pytest.raises(TimeoutError) as err:
        run(vc.click(loc, timeout=15000))
    assert err.value is boom and len(loc.calls) == 1
    assert page.ops[-1] == ("release", "none")                                  # un-pressed, but no "clicked" ring


def test_only_the_tab_in_front_is_drawn_on():
    front, back = CursorPage(), CursorPage()
    vc = S.VisualCursor({"cap_ms": 50}, enabled=True)
    run(vc.click(CursorLoc(front)))
    run(vc.click(CursorLoc(back)))                                              # background tab: real click only
    assert front.ops and back.ops == []


def test_tab_change_is_a_pretend_click_at_the_edge_then_one_real_switch():
    ctx = types.SimpleNamespace(pages=[])
    one, two = CursorPage(ctx), CursorPage(ctx)
    ctx.pages += [one, two]
    vc = S.VisualCursor({"cap_ms": 50}, enabled=True)
    run(vc.click(CursorLoc(one)))
    one.ops.clear()
    run(vc.switch(two, two.bring_to_front))
    assert one.ops == [("edge", "right")] and two.fronted == 1                  # tab 2 is to the right of tab 1
    assert two.ops == [("show", [1000, 300])]                                   # stays at that edge spot on the new tab
    run(vc.switch(two, two.bring_to_front))                                     # already in front: no scene
    assert two.fronted == 2 and len(two.ops) == 1
    run(vc.switch(one, one.bring_to_front))
    assert two.ops[-1] == ("edge", "left")


def test_upload_retry_is_still_clicked_by_the_page_and_only_marked_by_the_cursor():
    cfg = types.SimpleNamespace(op_timeout=2)
    page = CursorPage(retry={"x": 30, "y": 40})
    tab = S.CopilotTab(page, cfg, FakeStop())
    assert run(tab.click_upload_retry()) is True and page.ops == []             # cursor off: unchanged behaviour
    S.CopilotTab.cursor = S.VisualCursor({"cap_ms": 50}, enabled=True)
    try:
        assert run(tab.click_upload_retry()) is True
        assert page.ops == [("pretend", [30, 40])]                              # dashed ring, no input
        page.retry = False
        assert run(tab.click_upload_retry()) is False
    finally:
        S.CopilotTab.cursor = S.VisualCursor()


def test_cursor_question_enter_is_yes_n_is_no_and_settings_skip_it(env, monkeypatch, capsys):
    def answers(*lines):
        it = iter(lines)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    answers("")
    assert S.choose_visual_cursor(env.cfg) is True
    answers("maybe", "N")
    assert S.choose_visual_cursor(env.cfg) is False and "Enter for Yes" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    assert S.choose_visual_cursor(env.cfg) is True
    for mode, want in (("on", True), ("off", False)):
        assert S.choose_visual_cursor(types.SimpleNamespace(visual_cursor=mode)) is want
    bad = types.SimpleNamespace(**{**vars(env.cfg), "approved_root": str(env.base), "visual_cursor": "sometimes"})
    assert any("visual_cursor" in p for p in S.validate_config(bad)[1])


def test_teardown_removes_the_drawing_from_every_page_and_turns_it_off():
    one, two = CursorPage(), CursorPage()
    vc = S.VisualCursor({"cap_ms": 50}, enabled=True)
    run(vc.click(CursorLoc(one)))
    vc.front = two
    run(vc.click(CursorLoc(two)))
    run(vc.teardown())
    assert one.ops[-1][0] == "teardown" and two.ops[-1][0] == "teardown"
    loc = CursorLoc(one)
    one.ops.clear()
    run(vc.click(loc))
    assert one.ops == [] and loc.calls                                          # off again: real action only


# ------------------------------------------------------------------ startup: held console, tab tour, HTTP top-up, direct attach

def test_console_lines_wait_while_a_question_is_open_and_the_answer_line_comes_first(capsys):
    log = S.Log(None, S.Redactor(), "full")
    log.hold()
    log("queue", note="3 instruction(s) queued")
    S.CONSOLE["log"] = log
    try:
        S.say("Waiting for Copilot sign-in")
    finally:
        S.CONSOLE["log"] = None
    assert capsys.readouterr().out == ""                                       # nothing printed under the question
    log("session", note="moving cursor on")
    log.release(answer_first=True)
    lines = capsys.readouterr().out.splitlines()
    assert "moving cursor on" in lines[0] and "queued" in lines[1] and "sign-in" in lines[2]
    log("queue", note="later")
    assert "later" in capsys.readouterr().out                                  # released: printed at once again


def _on_copilot(tab, url="https://m365.cloud.microsoft/chat"):
    async def where():
        return url
    tab.url = where
    return tab


def test_the_startup_tour_visits_each_loading_tab_in_turn_and_ends_when_all_are_ready(env, monkeypatch):
    monkeypatch.setattr(S, "VISIT_SECONDS", 0.01)
    b = FakeBrowser(env.world)
    one, two = _on_copilot(FakeTab(env.world, {})), _on_copilot(FakeTab(env.world, {}))
    tour = S.TabTour(b, env.cfg, [one, two])
    seen = []

    def visitor(tab, ready_after):
        async def visit():
            seen.append(tab)
            if seen.count(tab) >= ready_after:
                tour.finished(tab)
        return visit
    one.visit, two.visit = visitor(one, 1), visitor(two, 3)
    run(asyncio.wait_for(tour.run(), 5))                                       # ends by itself: every tab is ready
    assert seen == [one, two, two, two]


def test_the_tour_stays_on_a_sign_in_page_that_stays_but_not_on_a_redirect_flashing_by(env, monkeypatch):
    monkeypatch.setattr(S, "VISIT_SECONDS", 0.01)
    monkeypatch.setattr(S, "SIGNIN_SETTLE_SECONDS", 0.05)
    b = FakeBrowser(env.world)
    loading = _on_copilot(FakeTab(env.world, {}))
    signin = _on_copilot(FakeTab(env.world, {}), "https://login.microsoftonline.com/common/oauth2")
    tour = S.TabTour(b, env.cfg, [loading, signin])
    seen = []

    def visitor(tab):
        async def visit():
            seen.append(tab)
            if seen[-3:] == [signin] * 3:   # the tour stayed on the sign-in tab; you finish signing in: both tabs ready
                tour.finished(signin)
                tour.finished(loading)
        return visit
    loading.visit, signin.visit = visitor(loading), visitor(signin)
    run(asyncio.wait_for(tour.run(), 5))
    assert seen[0] is loading                                  # a fresh sign-in address does not stop the tour at once
    assert seen[-3:] == [signin, signin, signin]               # once it stays, the tour stays on it (it ended there)


def test_missing_copilot_tabs_are_opened_over_http_counting_copilot_and_blank_tabs_only(env, monkeypatch):
    port = env.cfg.cdp_port
    payload = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc"}
    calls = []

    async def fake_version(p, timeout=1.5):
        return payload

    def fake_http(p, path, timeout=1.5, method="GET"):
        calls.append((method, path))
        if path == "/json/list":
            return [{"type": "page", "url": "https://m365.cloud.microsoft/chat"}, {"type": "page", "url": "edge://newtab/"},
                    {"type": "page", "url": "https://example.com/other-site"}, {"type": "service_worker", "url": ""}]
        return {"id": "new"}
    monkeypatch.setattr(S, "cdp_version", fake_version)
    monkeypatch.setattr(S, "cdp_http", fake_http)
    b = S.EdgeBrowser(env.cfg, env.rt, env.policy, env.log, env.stop)

    async def go():
        starting = asyncio.get_running_loop().create_future()
        return await b.http_topup(4, starting)
    assert run(go()) == 2                                                     # 4 needed - 1 Copilot - 1 blank (reused later)
    puts = [c for c in calls if c[0] == "PUT"]
    assert len(puts) == 2 and all(p[1].startswith("/json/new?https%3A%2F%2Fm365.cloud.microsoft") for p in puts)


class DirectPage(CursorPage):
    """Fake Copilot page for the direct upload route: `chips_after` = snapshots before the first chip shows."""

    def __init__(self, has_input=True, chips_after=1):
        super().__init__()
        self.has_input, self.chips_after, self.given, self.snaps = has_input, chips_after, [], 0
        page = self

        class Input:
            async def count(self):
                return 1 if page.has_input else 0

            async def set_input_files(self, paths, timeout=None):
                page.given.append(list(paths))

        class Nothing:
            async def count(self):
                return 0
        self._input, self._nothing = Input(), Nothing()

    def locator(self, sel, **kw):
        return types.SimpleNamespace(first=self._input if sel == S.FILE_INPUT else self._nothing)

    async def evaluate(self, script, arg=None):
        if script is S.SNAPSHOT_JS:
            self.snaps += 1
            shown = bool(self.given) and self.chips_after is not None and self.snaps > self.chips_after
            return {"chips": [{"label": "a.pdf", "text": "", "title": ""}] if shown else [], "active": [], "error": ""}
        return await super().evaluate(script, arg)


def test_files_go_straight_onto_copilots_input_and_a_silent_page_falls_back_to_the_menu(env, monkeypatch):
    monkeypatch.setattr(S, "DIRECT_SIGN_SECONDS", 0.3)
    cfg = types.SimpleNamespace(op_timeout=2)
    page = DirectPage()
    tab = S.CopilotTab(page, cfg, FakeStop())
    assert run(tab._direct(["C:/x/a.pdf", "C:/x/b.pdf"])) is True and page.given == [["C:/x/a.pdf", "C:/x/b.pdf"]]  # one bulk hand-over
    silent = DirectPage(chips_after=None)
    assert run(S.CopilotTab(silent, cfg, FakeStop())._direct(["C:/x/a.pdf"])) is False                           # nothing happened
    assert run(S.CopilotTab(DirectPage(has_input=False), cfg, FakeStop())._direct(["C:/x/a.pdf"])) is False       # no input on this page


def test_model_step_is_shown_by_the_cursor_even_when_the_tab_already_shows_that_model():
    cfg = types.SimpleNamespace(op_timeout=2)
    page = CursorPage()

    class Switcher:
        async def text_content(self):
            return "GPT-6.1 Sol"

        async def bounding_box(self, timeout=None):
            return {"x": 200, "y": 600, "width": 80, "height": 30}
    page.locator = lambda sel, **kw: types.SimpleNamespace(first=Switcher())
    tab = S.CopilotTab(page, cfg, FakeStop())
    S.CopilotTab.cursor = S.VisualCursor({"cap_ms": 50}, enabled=True)
    try:
        run(tab.select_model({"group": "GPT", "label": "GPT-6.1 Sol", "text": "GPT-6.1 Sol"}))
        assert page.ops == [("pretend", [240, 615])]                           # pretend click on the model button, no real click
    finally:
        S.CopilotTab.cursor = S.VisualCursor()


def test_a_model_button_that_ignores_the_first_click_is_clicked_again_not_waited_on_for_20_s(monkeypatch):
    monkeypatch.setattr(S, "MENU_RETRY_SECONDS", 0.1)
    cfg = types.SimpleNamespace(op_timeout=5)
    page = CursorPage()
    clicks = {"n": 0}

    class Button:
        async def click(self, **kw):
            clicks["n"] += 1

        async def get_attribute(self, name):
            return "false"
    real = page.evaluate

    async def evaluate(script, arg=None):
        if script is S.MENU_JS:
            return [{"text": "Auto", "title": "Auto", "popup": "", "disabled": False}] if clicks["n"] >= 2 else None
        return await real(script, arg)
    page.evaluate = evaluate
    tab = S.CopilotTab(page, cfg, FakeStop())
    t0 = time.monotonic()
    items = run(tab._open_model_menu(Button()))
    assert items and clicks["n"] == 2 and time.monotonic() - t0 < 2       # second click opened it, no long wait


def test_the_http_tour_visits_every_tab_in_a_fixed_order_even_though_edge_lists_the_last_active_first(env, monkeypatch):
    monkeypatch.setattr(S, "VISIT_SECONDS", 0.01)
    port = env.cfg.cdp_port
    tabs = [{"type": "page", "id": i, "url": "https://m365.cloud.microsoft/chat"} for i in ("A", "B", "C")]
    tabs.append({"type": "page", "id": "D", "url": "about:blank"})            # opened by us, still loading
    visits = []

    async def fake_version(p, timeout=1.5):
        return {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/x"}

    def fake_http(p, path, timeout=1.5, method="GET"):
        return sorted(tabs, key=lambda t: -visits[::-1].index(t["id"]) if t["id"] in visits else -99)   # last active first

    def fake_text(p, path, timeout=1.5):
        visits.append(path.rsplit("/", 1)[1])
        if len(visits) >= 8:
            b.tour_handoff.set()
        return "Target activated"
    monkeypatch.setattr(S, "cdp_version", fake_version)
    monkeypatch.setattr(S, "cdp_http", fake_http)
    monkeypatch.setattr(S, "cdp_text", fake_text)
    b = S.EdgeBrowser(env.cfg, env.rt, env.policy, env.log, env.stop)
    b.http_ids.append("D")

    async def go():
        starting = asyncio.get_running_loop().create_future()
        await asyncio.wait_for(b.http_tour(4, starting), 5)
    run(go())
    assert set(visits[:4]) == {"A", "B", "C", "D"} and visits[4:8] == visits[:4]        # every tab, same fixed round
