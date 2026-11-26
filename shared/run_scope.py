"""Which cases of the BAM Report this run works on - the 10 run choices, in plain words (stdlib only).

Each choice keeps the report's order.  `choose` asks on the console; `from_text` reads the same choice from one
line (`--scope` on the command line, used by tests and scheduled runs):

    all                     every case                    first:5                the first 5 cases
    one:<RequestId>         one case                      ids:<id>,<id>,...       the pasted cases
    client:<C1>,<C2>        by Client Number              account:<A1>,<A2>       by Client Account Number
    type:<Sub Worktype>     by Sub Worktype (',' between) fund:<Fund>             by Fund
    source:email|post|blank by Source                     received:<dd/mm/yyyy>-<dd/mm/yyyy>  by Received Date
"""
from __future__ import annotations

import collections
import datetime
import re
from dataclasses import dataclass, field

CHOICES = [
    ("one", "Just one case"),
    ("first", "The first few cases (you say how many)"),
    ("all", "Every case in the report"),
    ("ids", "Cases I paste in"),
    ("client", "All cases for a client"),
    ("account", "All cases for an account"),
    ("type", "All cases of one type (Sub Worktype)"),
    ("fund", "All cases for a fund"),
    ("source", "Cases that came by email or post"),
    ("received", "Cases received between two dates"),
]
FIELDS = {"client": ("client_number", "Client Number"), "account": ("account", "Client Account Number"),
          "type": ("sub_worktype_csv", "Sub Worktype"), "fund": ("fund", "Fund"), "source": ("case_source", "Source")}
BLANK = "(blank)"
LIST_MAX = 60                       # longer lists are not printed: the values are typed instead
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


class ScopeError(Exception):
    """The choice cannot be used (the message is safe to show)."""


@dataclass
class Scope:
    cases: list
    label: str
    notes: list = field(default_factory=list)       # e.g. pasted IDs that are not in the report


def norm(text) -> str:
    return " ".join(str(text or "").casefold().split())


def value_of(case: dict, key: str) -> str:
    """The case's value for a by-value choice, compared loosely ('' = blank)."""
    raw = case.get(FIELDS[key][0], "")
    return re.sub(r"\s+", "", str(raw)).upper() if key == "account" else norm(raw)


def day_of(text):
    m = DATE_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def split_values(text) -> list:
    return [v.strip() for v in re.split(r"[,;\n\t]+", str(text or "")) if v.strip()]


# ---------------------------------------------------------------------------------------------- the selections

def by_ids(cases, ids, excluded=()) -> Scope:
    want = [i.strip().lower() for i in ids if i.strip()]
    if not want:
        raise ScopeError("no RequestId was given")
    have = {c["iid"]: c for c in cases}
    why = {e.get("iid"): e.get("reason", "") for e in excluded}
    picked = [c for c in cases if c["iid"] in set(want)]
    notes = []
    for i in dict.fromkeys(want):
        if i not in have:
            notes.append(f"{i}: " + (f"excluded automatically ({why[i]})" if i in why else "not in this BAM Report"))
    if not picked:
        raise ScopeError("none of the RequestIds given is a case of this report"
                         + ("" if not notes else ":\n    " + "\n    ".join(notes)))
    return Scope(picked, f"Cases I paste in ({len(picked)})", notes)


def by_values(cases, key: str, values) -> Scope:
    label = FIELDS[key][1]
    if key == "account":
        want = {re.sub(r"\s+", "", v).upper() for v in values}
    else:
        want = {"" if norm(v) in (norm(BLANK), "blank", "none") else norm(v) for v in values}
    picked = [c for c in cases if value_of(c, key) in want]
    if not picked:
        raise ScopeError(f"no case has that {label}")
    shown = ", ".join(sorted({c.get(FIELDS[key][0], "") or BLANK for c in picked}))
    return Scope(picked, f"{dict(CHOICES)[key]}: {shown[:200]}")


def by_received(cases, start, end) -> Scope:
    if start > end:
        start, end = end, start
    picked = [c for c in cases if (d := day_of(c.get("received_date"))) and start <= d <= end]
    blank = sum(1 for c in cases if not day_of(c.get("received_date")))
    if not picked:
        raise ScopeError("no case was received between those dates")
    notes = [f"{blank} case(s) have no Received Date and are not included"] if blank else []
    return Scope(picked, f"Cases received between {start:%d/%m/%Y} and {end:%d/%m/%Y}", notes)


def first(cases, n: int) -> Scope:
    if n < 1:
        raise ScopeError("the number must be 1 or more")
    return Scope(cases[:n], f"The first {min(n, len(cases))} case(s)")


def from_text(text: str, cases: list, excluded=()) -> Scope:
    """One-line choice (see the module text)."""
    kind, _, rest = str(text or "all").strip().partition(":")
    kind = kind.strip().lower()
    if kind == "all":
        return Scope(list(cases), "Every case in the report")
    if kind == "one":
        return Scope(by_ids(cases, [rest], excluded).cases, "Just one case")
    if kind == "first":
        if not rest.strip().isdigit():
            raise ScopeError("'first' needs a number, e.g. first:5")
        return first(cases, int(rest))
    if kind == "ids":
        return by_ids(cases, split_values(rest), excluded)
    if kind in FIELDS:
        return by_values(cases, kind, split_values(rest) or [BLANK])
    if kind == "received":
        days = [day_of(x) for x in re.split(r"\s*(?:-|to|\.\.)\s*(?=\d)", rest)]
        if len(days) != 2 or None in days:
            raise ScopeError("'received' needs two dates, e.g. received:01/11/2026-25/11/2026")
        return by_received(cases, *days)
    raise ScopeError(f"unknown run choice '{kind}'")


# ---------------------------------------------------------------------------------------------- on the console

def _ask(ask, prompt: str) -> str:
    try:
        return ask(prompt).strip()
    except EOFError:
        raise ScopeError("the run choice needs an answer (no console input)") from None


def pick_values(cases, key: str, ask, say) -> list:
    """A numbered list of the values in the report (with how many cases each), or typing them when there are many."""
    label = FIELDS[key][1]
    shown, counts, names = {}, collections.Counter(), {}
    for c in cases:                                  # one entry per value, spelling differences ignored
        k = value_of(c, key)
        shown.setdefault(k, c.get(FIELDS[key][0], "") or BLANK)
        counts[shown[k]] += 1
        if key == "client":                          # the client's name helps to recognise the number
            names.setdefault(shown[k], c.get("client_name", ""))
    values = sorted(counts, key=lambda v: (v == BLANK, v.casefold()))
    if len(values) <= LIST_MAX:
        say(f"\n{label}s in this report:")
        for n, v in enumerate(values, 1):
            extra = f"  {names.get(v)}" if names.get(v) else ""
            say(f"  {n:>3}  {v}{extra}  ({counts[v]} case{'s' if counts[v] != 1 else ''})")
        answer = _ask(ask, f"Type the number(s) from the list, or the {label}(s) themselves, separated by commas: ")
    else:
        answer = _ask(ask, f"There are {len(values)} different {label}s - type the one(s) you want, separated by commas: ")
    out = []
    for part in split_values(answer):
        out.append(values[int(part) - 1] if part.isdigit() and 1 <= int(part) <= len(values) and len(values) <= LIST_MAX
                   else part)
    return out


def choose(cases: list, excluded=(), ask=input, say=print) -> Scope:
    """Asks which cases to run, until a choice gives at least one case.  Enter = every case."""
    if not cases:
        raise ScopeError("the report has no case to review")
    while True:
        say(f"\nWHICH CASES? ({len(cases)} case(s) can be reviewed in this report)")
        for n, (_, name) in enumerate(CHOICES, 1):
            say(f"  {n:>2}  {name}")
        answer = _ask(ask, "Your choice (Enter = 3, every case): ") or "3"
        if not answer.isdigit() or not 1 <= int(answer) <= len(CHOICES):
            say("  Please type a number from the list.")
            continue
        kind = CHOICES[int(answer) - 1][0]
        try:
            if kind == "all":
                return Scope(list(cases), "Every case in the report")
            if kind == "one":
                iid = _ask(ask, "RequestId of the case (Enter = the first case in the report): ")
                return Scope([cases[0]] if not iid else by_ids(cases, [iid], excluded).cases, "Just one case")
            if kind == "first":
                n = _ask(ask, f"How many cases (1 to {len(cases)})? ")
                if not n.isdigit():
                    raise ScopeError("please type a whole number")
                return first(cases, int(n))
            if kind == "ids":
                say("Paste the RequestIds (one per line, or separated by commas), then press Enter on an empty line:")
                lines = []
                while True:
                    line = _ask(ask, "")
                    if not line:
                        break
                    lines.append(line)
                return by_ids(cases, split_values("\n".join(lines)), excluded)
            if kind in FIELDS:
                return by_values(cases, kind, pick_values(cases, kind, ask, say))
            start = day_of(_ask(ask, "From which Received Date (dd/mm/yyyy)? "))
            end = day_of(_ask(ask, "To which Received Date (dd/mm/yyyy)? "))
            if not start or not end:
                raise ScopeError("please type both dates as dd/mm/yyyy")
            return by_received(cases, start, end)
        except ScopeError as exc:
            say(f"  {exc} - let's try again.")
