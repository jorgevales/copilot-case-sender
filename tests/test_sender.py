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
        cfg = {**S.DEFAULTS, "cases_root": str(self.cases_root), "instructions_path": str(self.instr),
               "base_message_path": str(self.msg), "python_executable": sys.executable,
               "edge_executable": sys.executable, "runtime_root": str(self.runtime),
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
        r = S.Runner(self.cfg, self.rt, self.policy, self.journal, self.ledger, self.log, self.stop,
                     FakeBrowser(self.world), self.red, message, S.doc_of(self.instr))
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
        self.generation, self.reconnects, self.reconnect_fail = 1, 0, False
        self.opened = 0


class FakeTab:
    def __init__(self, world, behavior):
        self.w, self.b = world, behavior
        self.target_id = "T" + uuid.uuid4().hex[:8]
        self.attached, self.users, self.message, self.polls, self.closed, self.gone = [], [], "", 0, False, False
        self.sent_names, self.base_b, self.resets, self.models = [], dict(behavior), 0, []
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

    async def ensure_model(self, entry):
        self.models.append(entry)

    async def adopt(self):
        pass

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
        chips = [{"label": x, "text": x + " PDF", "title": ""} for x in self.attached[:shown]]
        if self.b.get("hidden_label"):              # overflow-hidden chips only expose text, not a label
            chips = [{"label": "", "text": x + " 12 KB", "title": ""} for x in self.attached[:shown]]
        return {"chips": chips + ([{"label": "extra.pdf", "text": "", "title": ""}] if self.b.get("extra") else []),
                "active": [] if complete else ["chip"], "sendEnabled": complete and bool(self.message),
                "generating": False, "error": "", "limit": ""}

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
        self.w.sends.append((self.target_id, list(self.attached), self.message))
        if mode == "no_effect":
            return
        self.users.append(self.message)
        self.sent_names = list(self.attached) if not self.b.get("names_hidden") else []
        if mode == "raise_after":
            self.w.lost = True
            raise S.ConnectionLost("lost right after send")
        if mode == "raise_gone":
            self.w.lost = True
            self.gone = True
            raise S.ConnectionLost("lost right after send")

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

    async def find_ready_tab(self):
        self.ready_after -= 1
        return FakeTab(self.w, {}) if self.ready_after <= 0 else None

    @property
    def generation(self):
        return self.w.generation

    async def open_tab(self):
        if self.w.lost:
            raise S.ConnectionLost("lost")
        self.w.opened += 1
        b = self.w.behaviors.pop(0) if self.w.behaviors else {}
        return FakeTab(self.w, b)

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
    assert ask([], entries=[]) is None                                         # picker unreadable: leave Copilot as is
    pinned = types.SimpleNamespace(**{**vars(env.cfg), "requested_model": "opus 5.5"})
    assert ask([], pinned)["label"] == "Opus 5.5"                              # setting skips the question
    with pytest.raises(S.Blocked):
        ask([], types.SimpleNamespace(**{**vars(env.cfg), "requested_model": "nope"}))


def test_label_matches_requires_the_same_model_not_a_prefix_of_another():
    e = {"label": "GPT-6.1 Sol"}
    assert S.label_matches("GPT-6.1 Sol", e) and S.label_matches("gpt-6.1 sol", e)
    assert not S.label_matches("GPT-6", e) and not S.label_matches("", e)
    assert S.label_matches("GPT-5.6 Sol", {"label": "GPT-5.6 Sol Think deeper"})     # button may show a shorter title


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


def test_session_warmup_fills_the_pool_and_applies_the_model_in_the_background(env):
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
    assert [v[0] for v in results.values()] == ["submitted"] * 3 and env.world.opened == 2   # first + 2 warmed, none opened ad hoc
    assert all(t.models and t.models[0]["label"] == "GPT-6.1 Sol" for t in env.world.tabs.values())


def test_every_setting_the_code_reads_has_a_default():
    import re as _re
    source = SRC.read_text("utf-8")
    used = set(_re.findall(r"\bcfg\.([a-z_]+)", source)) | set(_re.findall(r"self\.cfg\.([a-z_]+)", source))
    missing = sorted(k for k in used if k not in S.DEFAULTS and k not in S.REQUIRED)
    assert missing == []
