"""Checks on the code itself (no browser, no storage, nothing written): mistakes that only show up when a line runs."""
import ast
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SOURCES = [p for d in ("1_system1_documents", "2_system2_snapshots", "3_copilot_sender", "orchestrator", "shared")
           for p in sorted((PROJECT / d).glob("*.py"))]


def keyword_clashes(sources: dict) -> list:
    """{name: code} -> calls that pass a keyword with the same name as a positional parameter of a **kwargs function of
    that name ('got multiple values for argument').  VDI 2026-11-02: event(events, 'document-result', name=...) failed
    every case and no test ran that line.  Positional-only parameters (before '/') are safe and not reported."""
    sigs, trees = {}, []
    for name, code in sources.items():
        tree = ast.parse(code)
        trees.append((name, tree))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.args.kwarg:
                sigs.setdefault(node.name, []).append([a.arg for a in node.args.args if a.arg != "self"])
    out = []
    for name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            for params in sigs.get(fn, []):
                taken = params[:len(node.args)]                   # filled by position in this call
                out += [f"{name}:{node.lineno} {fn}(... {kw.arg}=...)" for kw in node.keywords if kw.arg in taken]
    return out


def test_no_call_passes_a_keyword_that_is_already_given_by_position():
    assert keyword_clashes({f.name: f.read_text(encoding="utf-8") for f in SOURCES}) == []


def test_the_clash_check_finds_the_bug_that_failed_the_vdi_run():
    call = "\n\nevent([], 'document-result', name='a.pdf')\n"
    assert keyword_clashes({"old": "def event(items, name, **details):\n    pass" + call}) == ["old:4 event(... name=...)"]
    assert keyword_clashes({"new": "def event(items, name, /, **details):\n    pass" + call}) == []


def test_every_part_parses():
    for f in SOURCES:
        ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
