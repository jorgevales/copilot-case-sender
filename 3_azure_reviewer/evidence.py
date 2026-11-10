"""What Document Intelligence read, as numbered evidence the model can cite - and the code that decides, from those
citations alone, whether a field is handwritten and how sure the reading is.

Citation ids shown to the model:  D1.p3.l5 = line 5 of page 3 of document D1,  D1.p3.l5.w2 = its 2nd word,
D1.p3.s1 = selection mark (tick box) 1 of page 3.  The model only says WHICH text it read a value from; the code
derives HANDWRITTEN / CONFIDENCE / LOW_CONFIDENCE.  A citation that does not exist, or a value that is not in the
cited text, is low confidence (the model may have guessed).
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field


@dataclass
class Word:
    id: str
    text: str
    conf: float
    hw: bool


@dataclass
class Line:
    id: str
    text: str
    page: int
    words: list = field(default_factory=list)
    centre: tuple = (0.0, 0.0)

    @property
    def hw(self) -> bool:
        return any(w.hw for w in self.words)


@dataclass
class Mark:
    id: str
    selected: bool
    conf: float
    page: int
    near: str = ""


def _centre(polygon) -> tuple:
    pts = list(polygon or [])
    if len(pts) < 2:
        return (0.0, 0.0)
    xs, ys = pts[0::2], pts[1::2]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _overlaps(offset: int, length: int, spans: list) -> bool:
    end = offset + max(length, 1)
    return any(offset < b and a < end for a, b in spans)


class DocEvidence:
    """One document's numbered lines, words and tick boxes."""

    def __init__(self, doc_id: str, file: str):
        self.doc_id, self.file = doc_id, file
        self.lines, self.marks, self.index = [], [], {}
        self.page_numbers = []
        self.local = False                 # read on this computer (text file): confidence 1, never handwritten
        self.raw_text = ""                 # local documents that are shown whole (the snapshot JSON)

    # -- building -----------------------------------------------------------------------------
    @classmethod
    def from_di(cls, doc_id: str, file: str, results: list) -> "DocEvidence":
        ev = cls(doc_id, file)
        for ar in results:
            hw_spans = []
            for st in ar.get("styles") or []:
                if st.get("isHandwritten"):
                    hw_spans += [(s["offset"], s["offset"] + s["length"]) for s in st.get("spans") or []]
            for page in ar.get("pages") or []:
                n = int(page.get("pageNumber") or 0)
                if n in ev.page_numbers:
                    continue
                ev.page_numbers.append(n)
                words = page.get("words") or []
                used = set()
                lines = []
                for k, ln in enumerate(page.get("lines") or [], 1):
                    lid = f"{doc_id}.p{n}.l{k}"
                    line = Line(lid, ln.get("content", ""), n, centre=_centre(ln.get("polygon")))
                    spans = [(s["offset"], s["offset"] + s["length"]) for s in ln.get("spans") or []]
                    j = 0
                    for wi, w in enumerate(words):
                        if wi in used:
                            continue
                        span = w.get("span") or {}
                        off = span.get("offset", -1)
                        if any(a <= off < b for a, b in spans):
                            used.add(wi)
                            j += 1
                            word = Word(f"{lid}.w{j}", w.get("content", ""), float(w.get("confidence", 0.0)),
                                        _overlaps(off, span.get("length", 1), hw_spans))
                            line.words.append(word)
                            ev.index[word.id] = word
                    lines.append(line)
                    ev.index[lid] = line
                ev.lines += lines
                for k, sm in enumerate(page.get("selectionMarks") or [], 1):
                    c = _centre(sm.get("polygon"))
                    near = min(lines, key=lambda L: (L.centre[0] - c[0]) ** 2 + 4 * (L.centre[1] - c[1]) ** 2,
                               default=None)
                    mark = Mark(f"{doc_id}.p{n}.s{k}", sm.get("state") == "selected", float(sm.get("confidence", 0.0)),
                                n, near.text if near else "")
                    ev.marks.append(mark)
                    ev.index[mark.id] = mark
        return ev

    @classmethod
    def from_text(cls, doc_id: str, file: str, text: str) -> "DocEvidence":
        """A text document read here (.txt .md .csv .json .xml .eml): one 'page', every line citable, confidence 1."""
        ev = cls(doc_id, file)
        ev.local, ev.page_numbers = True, [1]
        for k, raw in enumerate(str(text).splitlines(), 1):
            if not raw.strip():
                continue
            lid = f"{doc_id}.p1.l{k}"
            line = Line(lid, raw.rstrip(), 1)
            for j, tok in enumerate(raw.split(), 1):
                w = Word(f"{lid}.w{j}", tok, 1.0, False)
                line.words.append(w)
                ev.index[w.id] = w
            ev.lines.append(line)
            ev.index[lid] = line
        return ev

    @classmethod
    def snapshot(cls, doc_id: str, file: str, data) -> "DocEvidence":
        """The Register snapshot: shown whole as JSON (comparisons cite its JSON location, not line ids)."""
        ev = cls(doc_id, file)
        ev.local, ev.page_numbers = True, [1]
        ev.raw_text = json.dumps(data, ensure_ascii=False, indent=1)
        return ev

    # -- showing ------------------------------------------------------------------------------
    @property
    def pages(self) -> int:
        return len(self.page_numbers)

    def handwritten_lines(self) -> int:
        return sum(1 for L in self.lines if L.hw)

    def render(self) -> str:
        """The text the model sees: one line per read line, '[id] text', '{handwritten}' where Azure saw handwriting;
        tick boxes as '[id] [X] ticked / [ ] not ticked, beside: <nearest line>'."""
        if self.raw_text:
            return self.raw_text
        out, page = [], None
        marks = {}
        for m in self.marks:
            marks.setdefault(m.page, []).append(m)
        for L in self.lines:
            if L.page != page:
                if page is not None:
                    out += _marks(marks.get(page, []))
                page = L.page
                out.append(f"--- page {page} ---")
            out.append(f"[{L.id}] {L.text}" + ("  {handwritten}" if L.hw else ""))
        if page is not None:
            out += _marks(marks.get(page, []))
        for p in sorted(set(marks) - {L.page for L in self.lines}):
            out.append(f"--- page {p} ---")
            out += _marks(marks[p])
        return "\n".join(out)


def _marks(marks: list) -> list:
    return [f"[{m.id}] {'[X] ticked' if m.selected else '[ ] not ticked'}, beside: {m.near}" for m in marks]


# ---------------------------------------------------------------------------------------------- fields

TICKED = {"ticked", "yes", "selected", "checked", "x", "true"}
UNTICKED = {"not ticked", "no", "unselected", "unchecked", "", "false", "blank"}
UNSIGNED = {"not signed", "unsigned", "no", "blank", ""}


def norm(text) -> str:
    """Comparison form of a value: lower case, letters/digits/@ only (spaces, punctuation and box gaps vanish)."""
    return re.sub(r"[^0-9a-z@]", "", html.unescape(str(text or "")).lower())


def derive_field(doc: DocEvidence, fdef: dict, value, citations, threshold: float) -> dict:
    """One EXTRACTED_FIELDS row.  CONFIDENCE = the lowest word confidence among the cited words that make up the
    value (tick box: the box's own confidence).  LOW_CONFIDENCE = below `threshold`, or a citation that does not
    exist, or a value that is not in the cited text."""
    value = "" if value is None else str(value).strip()
    citations = [str(c).strip() for c in (citations or []) if str(c).strip()]
    row = {"FIELD_ID": f"{doc.doc_id}.{fdef['id']}", "DOC_ID": doc.doc_id, "FORM": fdef.get("_form", ""),
           "FIELD": fdef["id"], "LABEL": fdef.get("label", ""), "VALUE": value, "HANDWRITTEN": False,
           "CONFIDENCE": 1.0, "LOW_CONFIDENCE": False, "CITATIONS": citations, "REASON": ""}
    kind = fdef.get("kind", "text")
    found = [doc.index.get(c) for c in citations]
    missing = [c for c, f in zip(citations, found) if f is None or not c.startswith(doc.doc_id + ".")]
    words, marks = [], []
    for f in found:
        if isinstance(f, Line):
            words += f.words
        elif isinstance(f, Word):
            words.append(f)
        elif isinstance(f, Mark):
            marks.append(f)
    reasons = []
    if missing:
        reasons.append("cited text not found: " + ", ".join(missing[:5]))
    blank = (value == "" or (kind == "checkbox" and value.lower() in UNTICKED)
             or (kind == "signature" and value.lower() in UNSIGNED))
    if kind == "checkbox":
        if marks:
            ticked = norm(value) in {norm(t) for t in TICKED}
            row["CONFIDENCE"] = min(m.conf for m in marks)
            if any(m.selected != ticked for m in marks):
                reasons.append("the cited tick box does not show that")
            row["VALUE"] = "ticked" if ticked else "not ticked"
        elif words:                                     # a hand-drawn X read as text
            row["CONFIDENCE"] = min(w.conf for w in words)
            row["HANDWRITTEN"] = any(w.hw for w in words)
        elif not blank:
            reasons.append("no tick box cited")
            row["CONFIDENCE"] = 0.0
    elif kind == "signature":
        if words:
            row["CONFIDENCE"] = min(w.conf for w in words)
            row["HANDWRITTEN"] = any(w.hw for w in words)
        elif not blank:
            reasons.append("no signature text cited")
            row["CONFIDENCE"] = 0.0
    elif not blank:
        want = norm(value)
        cited = "".join(norm(w.text) for w in words)
        if not words:
            reasons.append("no text cited for this value")
            row["CONFIDENCE"] = 0.0
        else:
            if want and want not in cited:
                reasons.append("the value is not in the cited text")
            parts = [w for w in words if norm(w.text) and norm(w.text) in want]
            pool = parts or words
            row["CONFIDENCE"] = min(w.conf for w in pool)
            row["HANDWRITTEN"] = any(w.hw for w in pool)
    row["CONFIDENCE"] = round(float(row["CONFIDENCE"]), 3)
    row["LOW_CONFIDENCE"] = bool(reasons) or row["CONFIDENCE"] < threshold
    if row["CONFIDENCE"] < threshold and not reasons:
        reasons.append(f"read with confidence {row['CONFIDENCE']:.2f} (below {threshold:.2f})")
    row["REASON"] = "; ".join(reasons)
    return row
